"""Reply-bounded read receipts. Provider I/O never holds a database lock."""
import logging

import aiomysql

logger = logging.getLogger('shipment-bot')


class ReadSyncRepositoryMixin:
    async def pending_read_sync(self, message_id=None):
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""SELECT m.id,m.ticket_id,t.business_phone_number_id,t.whatsapp_phone
                    FROM support_ticket_messages m JOIN support_tickets t ON t.id=m.ticket_id
                    WHERE m.read_sync_pending=1 AND m.send_status='SENT' AND m.sender_type='AGENT'
                      AND (%s IS NULL OR m.id=%s) AND (m.read_sync_attempted_at IS NULL
                        OR m.read_sync_attempted_at < UTC_TIMESTAMP(6) - INTERVAL 30 SECOND)
                    ORDER BY m.read_sync_attempted_at,m.id LIMIT 20""", (message_id, message_id))
                return list(await cur.fetchall())

    async def read_sync_targets(self, job):
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute('UPDATE support_ticket_messages SET read_sync_attempted_at=UTC_TIMESTAMP(6) WHERE id=%s', (job['id'],))
                # add_ticket_message serializes inserts on the ticket. The reply's
                # immutable ID is its boundary, including on late ACKs/retries.
                await cur.execute("""SELECT id,whatsapp_message_id FROM support_ticket_messages
                    WHERE ticket_id=%s AND id<%s AND direction='INBOUND' AND read_at IS NULL
                    AND whatsapp_message_id IS NOT NULL AND deleted_for_everyone_at IS NULL ORDER BY id LIMIT 50""", (job['ticket_id'], job['id']))
                rows = list(await cur.fetchall())
                if not rows:
                    # A previous receipt may have informed the customer without
                    # clearing our account's linked-phone unread counter. A new
                    # reply retries that bounded app-state patch as well.
                    await cur.execute("""SELECT id,whatsapp_message_id FROM support_ticket_messages
                        WHERE ticket_id=%s AND id<%s AND direction='INBOUND'
                        AND whatsapp_message_id IS NOT NULL AND deleted_for_everyone_at IS NULL
                        ORDER BY id DESC LIMIT 1""", (job['ticket_id'], job['id']))
                    rows = list(await cur.fetchall())
            await conn.commit()
            return rows

    async def confirm_incoming_read(self, job, target):
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute("""UPDATE support_ticket_messages SET read_at=UTC_TIMESTAMP(6)
                        WHERE id=%s AND ticket_id=%s AND id<%s AND direction='INBOUND' AND read_at IS NULL
                        AND whatsapp_message_id=%s""", (target['id'], job['ticket_id'], job['id'], target['whatsapp_message_id']))
                    if cur.rowcount:
                        # Attribute confirmed read-sync to the reply's original actor,
                        # never the current assignee. Retries cannot duplicate this audit.
                        await cur.execute("""INSERT INTO support_ticket_events
                            (ticket_id,event_type,actor_type,actor_id,actor_name,details_json,created_at,audit_key)
                            SELECT ticket_id,'INCOMING_MARKED_READ','AGENT',sender_id,sender_name,
                                JSON_OBJECT('mode','reply_sync','reply_id',id,'message_id',%s),UTC_TIMESTAMP(6),%s
                            FROM support_ticket_messages WHERE id=%s AND ticket_id=%s AND sender_type='AGENT'
                                AND sender_id IS NOT NULL AND send_status='SENT'
                            ON DUPLICATE KEY UPDATE audit_key=VALUES(audit_key)""",
                            (target['id'], f"read:{target['id']}", job['id'], job['ticket_id']))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def complete_read_sync(self, job):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute('UPDATE support_ticket_messages SET read_sync_pending=0 WHERE id=%s', (job['id'],))
            await conn.commit()


async def sync_reply_reads(repository, transport, publish, message_id=None):
    for job in await repository.pending_read_sync(message_id):
        try:
            targets = await repository.read_sync_targets(job)
            failed = False
            for target in targets:
                try:
                    await transport.mark_read(message_id=target['whatsapp_message_id'],
                                              phone_number_id=job['business_phone_number_id'],
                                              phone=job['whatsapp_phone'])
                    await repository.confirm_incoming_read(job, target)
                except Exception:
                    failed = True
                    logger.warning('WhatsApp incoming read sync pending: message_id=%s', target['id'])
            if not failed and len(targets) < 50:
                await repository.complete_read_sync(job)
            await publish('incoming_read', ticket_id=job['ticket_id'])
        except Exception:
            # Receipt submission is idempotent. Retry only these same IDs after
            # failures/restarts, never a fresh conversation-wide read boundary.
            logger.warning('WhatsApp incoming read sync pending: reply_id=%s', job['id'])


async def attach_conversation_read_state(cur, rows):
    if not rows:
        return
    ids = [row['id'] for row in rows]
    placeholders = ','.join(['%s'] * len(ids))
    await cur.execute(f"""SELECT ticket_id,
        SUM(direction='INBOUND' AND read_at IS NULL) AS unread_count,
        MAX(IF(direction='INBOUND',id,0)) > MAX(IF(direction='OUTBOUND'
            AND sender_type='AGENT' AND send_status='SENT',id,0)) AS awaiting_staff_reply
        FROM support_ticket_messages WHERE ticket_id IN ({placeholders}) AND deleted_for_everyone_at IS NULL
        GROUP BY ticket_id""", ids)
    states = {str(row['ticket_id']): row for row in await cur.fetchall()}
    for row in rows:
        state = states.get(str(row['id']), {})
        row['unread_count'] = int(state.get('unread_count') or 0)
        row['awaiting_staff_reply'] = bool(state.get('awaiting_staff_reply'))
