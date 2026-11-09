"""Atomic outbound ownership. No provider I/O occurs inside a database transaction."""
from __future__ import annotations

import uuid

import aiomysql


DELIVERY_LEASE_MINUTES = 5
# A reply refused because its line was offline is resent automatically within this window.
HELD_REPLY_MINUTES = 30


class DeliveryRepositoryMixin:
    async def administrative_delivery_blocked(self, line_id: str, message_id: int) -> bool:
        """A pause, or older retained work for the SAME customer, blocks admission.

        Order is a per-conversation property: unrelated customers on the line
        never wait for each other. Uncertain work is never resent, so it cannot
        block later replies (it would otherwise block them forever)."""
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""SELECT 1 FROM whatsapp_bridge_controls WHERE line_id=%s AND paused=1
                    UNION ALL SELECT 1 FROM support_ticket_messages cur
                    JOIN support_ticket_messages m ON m.ticket_id=cur.ticket_id AND m.id<cur.id
                    JOIN support_tickets t ON t.id=m.ticket_id
                    WHERE cur.id=%s AND m.admin_hold=1 AND m.direction='OUTBOUND'
                      AND m.deleted_for_everyone_at IS NULL
                      AND (m.send_status IN ('PENDING','DISPATCHING')
                           OR (m.send_status='FAILED' AND m.delivery_retry_safe=1))
                      AND (t.active_key IS NOT NULL OR m.sender_type='SYSTEM')
                    UNION ALL SELECT 1 FROM support_ticket_messages cur
                    JOIN support_tickets t ON t.id=cur.ticket_id
                    JOIN whatsapp_paused_bot_replies b ON b.line_id=%s AND b.state IN ('queued','dispatching')
                      AND b.created_at<=cur.created_at
                      AND REGEXP_REPLACE(b.recipient,'[^0-9]','')=REGEXP_REPLACE(t.whatsapp_phone,'[^0-9]','')
                    WHERE cur.id=%s LIMIT 1""", (line_id,message_id,line_id,message_id))
                return bool(await cur.fetchone())

    async def release_unclaimable_admin_holds(self) -> list[int]:
        """Return retained staff replies the resume worker can never claim to staff.

        A conversation closed or transferred while its line was paused leaves a
        reply its original sender may no longer send. It was never handed to a
        transport (PENDING without a live claim, or a proven non-send), so it
        becomes an ordinary "Not sent" reply instead of blocking the
        conversation forever. Returns the affected ticket IDs."""
        if self._pool is None:
            await self.connect()
        unclaimable = """m.admin_hold=1 AND m.direction='OUTBOUND' AND m.sender_type='AGENT'
              AND m.deleted_for_everyone_at IS NULL
              AND ((m.send_status='FAILED' AND m.delivery_retry_safe=1)
                   OR (m.send_status='PENDING' AND m.delivery_started_at IS NULL
                       AND (m.delivery_token IS NULL OR m.delivery_claimed_at < UTC_TIMESTAMP(6) - INTERVAL %s MINUTE)))
              AND NOT ((t.active_key IS NOT NULL AND m.sender_id <=> t.current_assignee_id
                        AND t.status IN ('ASSIGNED','IN_PROGRESS','WAITING_CUSTOMER','WAITING_INTERNAL'))
                    OR (t.active_key IS NOT NULL AND t.status IN ('NEW','ASSIGNED','IN_PROGRESS','WAITING_CUSTOMER','WAITING_INTERNAL')
                        AND m.is_forwarded=1 AND m.forwarded_from_ticket_id>0 AND m.forwarded_from_message_id>0
                        AND BINARY m.client_message_id LIKE CONCAT('contact-forward:', m.forwarded_from_ticket_id, ':',
                                                                  m.forwarded_from_message_id, ':_%%')))"""
        async with self._pool.acquire() as conn:
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await conn.begin()
                    await cur.execute("SELECT m.id,m.ticket_id FROM support_ticket_messages m JOIN support_tickets t ON t.id=m.ticket_id WHERE "
                                      + unclaimable + " ORDER BY m.id LIMIT 200 FOR UPDATE", (DELIVERY_LEASE_MINUTES,))
                    rows = list(await cur.fetchall())
                    if rows:
                        ids = [int(row['id']) for row in rows]
                        await cur.execute("""UPDATE support_ticket_messages m JOIN support_tickets t ON t.id=m.ticket_id
                            SET m.admin_hold=0, m.held_until=NULL, m.send_status='FAILED', m.delivery_retry_safe=1,
                                m.error_message='Not sent; retry is available.', m.updated_at=UTC_TIMESTAMP(6)
                            WHERE m.id IN (""" + ','.join(['%s'] * len(ids)) + ") AND " + unclaimable,
                            (*ids, DELIVERY_LEASE_MINUTES))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return sorted({int(row['ticket_id']) for row in rows})

    async def hold_for_administration(self, message_id: int) -> None:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""UPDATE support_ticket_messages SET admin_hold=1,
                    error_message='Waiting for administrative resume; sends automatically.',updated_at=UTC_TIMESTAMP(6)
                    WHERE id=%s AND direction='OUTBOUND' AND deleted_for_everyone_at IS NULL
                      AND (send_status='PENDING' OR (send_status='FAILED' AND delivery_retry_safe=1))""", (message_id,))
            await conn.commit()

    async def get_delivery_replacement(self, ticket_id: int, message_id: int):
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute('SELECT * FROM support_ticket_messages WHERE ticket_id=%s AND replacement_for_message_id=%s',
                                  (ticket_id, message_id))
                return await cur.fetchone()

    async def list_delivery_reconciliation_candidates(self, after_id: int = 0, limit: int = 20):
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""SELECT m.*, t.whatsapp_phone, t.business_phone_number_id
                    FROM support_ticket_messages m JOIN support_tickets t ON t.id=m.ticket_id
                    WHERE m.id > %s AND m.direction='OUTBOUND' AND m.transport_client_id IS NOT NULL
                      AND m.deleted_for_everyone_at IS NULL
                      AND (m.send_status='UNCERTAIN' OR (m.send_status='DISPATCHING'
                           AND m.delivery_claimed_at < UTC_TIMESTAMP(6)-INTERVAL %s MINUTE))
                    ORDER BY m.id LIMIT %s""", (after_id, DELIVERY_LEASE_MINUTES, limit))
                return list(await cur.fetchall())

    async def expire_held_replies(self) -> list[int]:
        """End holds past their window; return the affected ticket IDs."""
        if self._pool is None:
            await self.connect()
        from app.config import settings
        protected = ' AND admin_hold=0' if settings.whatsapp_admin_recovery_enabled else ''
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""SELECT DISTINCT ticket_id FROM support_ticket_messages
                    WHERE send_status='FAILED' AND held_until IS NOT NULL AND held_until <= UTC_TIMESTAMP(6)""" + protected)
                tickets = [int(row['ticket_id']) for row in await cur.fetchall()]
                if tickets:
                    await cur.execute("""UPDATE support_ticket_messages SET held_until=NULL, updated_at=UTC_TIMESTAMP(6)
                        WHERE send_status='FAILED' AND held_until IS NOT NULL AND held_until <= UTC_TIMESTAMP(6)""" + protected)
            await conn.commit()
        return tickets

    async def list_held_replies(self, limit: int = 50):
        """Staff replies held for an offline line, oldest first, on live conversations."""
        if self._pool is None:
            await self.connect()
        from app.config import settings
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                if settings.whatsapp_admin_recovery_enabled:
                    await cur.execute("""SELECT m.* FROM support_ticket_messages m JOIN support_tickets t ON t.id=m.ticket_id
                        LEFT JOIN whatsapp_bridge_controls c ON c.line_id=t.business_phone_number_id
                        WHERE COALESCE(c.paused,0)=0 AND m.direction='OUTBOUND'
                          AND m.deleted_for_everyone_at IS NULL
                          AND ((m.admin_hold=1 AND (m.send_status='PENDING' OR (m.send_status='FAILED' AND m.delivery_retry_safe=1))
                                AND (m.sender_type='SYSTEM' OR (m.sender_type='AGENT' AND t.active_key IS NOT NULL)))
                            OR (m.send_status='FAILED' AND m.delivery_retry_safe=1 AND m.held_until>UTC_TIMESTAMP(6)
                                AND m.sender_type='AGENT' AND m.client_message_id IS NOT NULL AND t.active_key IS NOT NULL))
                        ORDER BY m.id LIMIT %s""", (limit,))
                    return list(await cur.fetchall())
                await cur.execute("""SELECT m.* FROM support_ticket_messages m JOIN support_tickets t ON t.id=m.ticket_id
                    WHERE m.send_status='FAILED' AND m.delivery_retry_safe=1 AND m.held_until > UTC_TIMESTAMP(6)
                      AND m.direction='OUTBOUND' AND m.sender_type='AGENT' AND m.client_message_id IS NOT NULL
                      AND m.deleted_for_everyone_at IS NULL AND t.active_key IS NOT NULL
                    ORDER BY m.id LIMIT %s""", (limit,))
                return list(await cur.fetchall())

    async def _lock_delivery_ticket(self, conn, cur, message_id: int):
        """Start a transaction locking the ticket before any message lock.

        The autocommit lookup only locates the immutable parent and does not
        establish a stale transaction snapshot before waiting for its lock.
        Never lock a message and then its ticket: inserts and lifecycle changes
        lock the ticket first. Repository pools use autocommit outside this block.
        """
        await cur.execute("SELECT ticket_id FROM support_ticket_messages WHERE id = %s", (message_id,))
        message = await cur.fetchone()
        if not message:
            return None
        await conn.begin()
        await cur.execute("SELECT * FROM support_tickets WHERE id = %s FOR UPDATE", (message["ticket_id"],))
        return await cur.fetchone()

    async def _fence_deliveries_for_lifecycle(self, cur, ticket_id: int) -> bool:
        """Under the ticket lock, refuse active work and cancel idle preparation.

        Dispatches remain blocking until completion or explicit recovery, even
        after their lease expires. UNCERTAIN is retained, never reset here.
        """
        await cur.execute("""
            SELECT id FROM support_ticket_messages
            WHERE ticket_id = %s AND direction = 'OUTBOUND'
              AND (send_status = 'DISPATCHING'
                   OR (send_status = 'PENDING' AND delivery_token IS NOT NULL
                       AND (delivery_claimed_at IS NULL OR
                            delivery_claimed_at >= UTC_TIMESTAMP(6) - INTERVAL %s MINUTE)))
            ORDER BY id LIMIT 1 FOR UPDATE
        """, (ticket_id, DELIVERY_LEASE_MINUTES))
        if await cur.fetchone():
            return False
        # Fence unclaimed/expired preparation, including SYSTEM messages, so
        # reopening or transferring back cannot revive a suspended worker.
        await cur.execute("""
            UPDATE support_ticket_messages
            SET send_status = 'FAILED', delivery_retry_safe = 1,
                delivery_token = REPLACE(UUID(), '-', ''),
                error_message = 'Not sent; conversation changed before dispatch. Explicit retry required.',
                updated_at = UTC_TIMESTAMP(6)
            WHERE ticket_id = %s AND direction = 'OUTBOUND' AND send_status = 'PENDING'
        """, (ticket_id,))
        return True

    async def claim_delivery(self, message_id: int, *, retry: bool = False, expected_token: str | None = None) -> str | None:
        if self._pool is None:
            await self.connect()
        token = uuid.uuid4().hex
        async with self._pool.acquire() as conn:
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    ticket = await self._lock_delivery_ticket(conn, cur, message_id)
                    if not ticket:
                        await conn.rollback()
                        return None
                    # Never steal a dispatch; its matching late confirmation may
                    # still resolve uncertainty, but it must not be sent again.
                    await cur.execute("""
                        UPDATE support_ticket_messages SET send_status = 'UNCERTAIN',
                            error_message = 'Delivery confirmation was lost; do not resend.'
                        WHERE id = %s AND send_status = 'DISPATCHING'
                          AND delivery_claimed_at < UTC_TIMESTAMP(6) - INTERVAL %s MINUTE
                    """, (message_id, DELIVERY_LEASE_MINUTES))
                    await cur.execute("SELECT whatsapp_message_id FROM support_ticket_messages WHERE id=%s FOR UPDATE", (message_id,))
                    prior = await cur.fetchone()
                    # Only the validated contact-forward service writes both this
                    # namespace and forwarding provenance. Ordinary client IDs
                    # cannot grant delivery rights into another owner's ticket.
                    await cur.execute("""
                        UPDATE support_ticket_messages
                        SET whatsapp_message_id = NULL, delivery_token = %s, delivery_claimed_at = UTC_TIMESTAMP(6),
                            delivery_started_at = NULL, send_status = 'PENDING',
                            delivery_retry_safe = 0, error_message = NULL
                        WHERE id = %s AND direction = 'OUTBOUND'
                          AND deleted_for_everyone_at IS NULL
                          AND (whatsapp_message_id IS NULL OR (%s AND send_status='FAILED' AND delivery_retry_safe=1))
                          AND ((send_status = 'PENDING' AND delivery_started_at IS NULL
                                AND (delivery_token IS NULL OR delivery_claimed_at < UTC_TIMESTAMP(6) - INTERVAL %s MINUTE))
                               OR (%s AND send_status = 'FAILED' AND delivery_retry_safe = 1 AND delivery_token <=> %s))
                          AND (sender_type = 'SYSTEM' OR
                               (sender_type = 'AGENT' AND (
                                   (sender_id = %s AND %s)
                                   OR (%s AND sender_id IS NOT NULL AND sender_id <> ''
                                       AND is_forwarded = 1
                                       AND forwarded_from_ticket_id > 0 AND forwarded_from_message_id > 0
                                       AND BINARY client_message_id LIKE CONCAT(
                                           'contact-forward:', forwarded_from_ticket_id, ':',
                                           forwarded_from_message_id, ':_%%')))))
                    """, (token, message_id, retry, DELIVERY_LEASE_MINUTES, retry, expected_token,
                          ticket['current_assignee_id'], bool(ticket['active_key']) and ticket['status'] in
                          ('ASSIGNED', 'IN_PROGRESS', 'WAITING_CUSTOMER', 'WAITING_INTERNAL'),
                          bool(ticket['active_key']) and ticket['status'] in
                          ('NEW', 'ASSIGNED', 'IN_PROGRESS', 'WAITING_CUSTOMER', 'WAITING_INTERNAL')))
                    claimed = cur.rowcount == 1
                    if claimed and prior and prior.get('whatsapp_message_id'):
                        await cur.execute("""INSERT INTO support_ticket_events
                            (ticket_id,event_type,actor_type,actor_id,actor_name,details_json,created_at)
                            VALUES (%s,'DELIVERY_RETRY','SYSTEM',NULL,NULL,JSON_OBJECT('message_id',%s,'failed_provider_id',%s),UTC_TIMESTAMP(6))""",
                            (ticket['id'], message_id, prior['whatsapp_message_id']))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return token if claimed else None

    async def begin_delivery(self, message_id: int, token: str) -> bool:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    ticket = await self._lock_delivery_ticket(conn, cur, message_id)
                    if not ticket:
                        await conn.rollback()
                        return False
                    # Recheck the persisted contact-forward capability at the
                    # provider boundary as well as during the delivery claim.
                    await cur.execute("""
                        UPDATE support_ticket_messages
                        SET send_status = 'DISPATCHING', delivery_started_at = UTC_TIMESTAMP(6),
                            delivery_claimed_at = UTC_TIMESTAMP(6)
                        WHERE id = %s AND delivery_token = %s AND send_status = 'PENDING'
                          AND direction = 'OUTBOUND' AND deleted_for_everyone_at IS NULL
                          AND whatsapp_message_id IS NULL AND delivery_started_at IS NULL
                          AND delivery_claimed_at >= UTC_TIMESTAMP(6) - INTERVAL %s MINUTE
                          AND (sender_type = 'SYSTEM' OR
                               (sender_type = 'AGENT' AND (
                                   (sender_id = %s AND %s)
                                   OR (%s AND sender_id IS NOT NULL AND sender_id <> ''
                                       AND is_forwarded = 1
                                       AND forwarded_from_ticket_id > 0 AND forwarded_from_message_id > 0
                                       AND BINARY client_message_id LIKE CONCAT(
                                           'contact-forward:', forwarded_from_ticket_id, ':',
                                           forwarded_from_message_id, ':_%%')))))
                    """, (message_id, token, DELIVERY_LEASE_MINUTES, ticket['current_assignee_id'],
                          bool(ticket['active_key']) and ticket['status'] in
                          ('ASSIGNED', 'IN_PROGRESS', 'WAITING_CUSTOMER', 'WAITING_INTERNAL'),
                          bool(ticket['active_key']) and ticket['status'] in
                          ('NEW', 'ASSIGNED', 'IN_PROGRESS', 'WAITING_CUSTOMER', 'WAITING_INTERNAL')))
                    changed = cur.rowcount == 1
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return changed

    async def finish_delivery(self, message_id: int, token: str, *, status: str,
                              provider_id: str | None = None, error: str | None = None, failure_code: int | None = None,
                              held: bool = False) -> None:
        from app.whatsapp_failures import failure_policy
        retry_safe = status == 'FAILED'
        if retry_safe and failure_code is not None:
            error, retry_safe = failure_policy(failure_code)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE support_ticket_messages SET send_status = %s,
                        whatsapp_message_id = COALESCE(%s, whatsapp_message_id),
                        read_sync_pending = CASE WHEN %s='SENT' AND sender_type='AGENT' THEN 1 ELSE read_sync_pending END,
                        error_message = %s, delivery_retry_safe = %s, updated_at = UTC_TIMESTAMP(6),
                        held_until = CASE WHEN %s THEN COALESCE(held_until, UTC_TIMESTAMP(6) + INTERVAL %s MINUTE) ELSE NULL END
                    WHERE id = %s AND delivery_token = %s
                      AND ((%s = 'SENT' AND send_status IN ('DISPATCHING', 'UNCERTAIN'))
                           OR (%s IN ('FAILED', 'UNCERTAIN') AND send_status IN ('PENDING', 'DISPATCHING')
                               AND delivery_claimed_at >= UTC_TIMESTAMP(6) - INTERVAL %s MINUTE))
                """, (status, provider_id, status, error, retry_safe, bool(held and retry_safe), HELD_REPLY_MINUTES,
                      message_id, token, status, status, DELIVERY_LEASE_MINUTES))
                from app.config import settings
                if settings.whatsapp_admin_recovery_enabled:
                    # A retained reply leaves the administrative queue on any
                    # outcome except a proven, transient non-send: it is sent,
                    # uncertain (never resent) or an ordinary failure that staff
                    # may resend manually. None of those may block later replies.
                    await cur.execute("""UPDATE support_ticket_messages SET admin_hold=0
                        WHERE id=%s AND admin_hold=1 AND (send_status='SENT' OR (%s AND send_status IN ('UNCERTAIN','FAILED')
                          AND NOT (send_status='FAILED' AND delivery_retry_safe=1 AND held_until IS NOT NULL)))""",
                        (message_id, cur.rowcount == 1))
            await conn.commit()

    async def interrupt_delivery(self, message_id: int, token: str) -> None:
        """Record cancellation without guessing whether dispatch reached WhatsApp.

        A matching confirmed/reclaimed attempt is never overwritten. Inspect the
        persisted boundary, not the caller's potentially stale local flag.
        """
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE support_ticket_messages
                    SET delivery_retry_safe = (send_status = 'PENDING'),
                        error_message = CASE WHEN send_status = 'PENDING'
                            THEN 'Preparation interrupted; retry is available.'
                            ELSE 'Delivery confirmation was interrupted; do not resend.' END,
                        send_status = CASE WHEN send_status = 'PENDING' THEN 'FAILED' ELSE 'UNCERTAIN' END,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s AND delivery_token = %s AND direction = 'OUTBOUND'
                      AND (send_status = 'DISPATCHING' OR
                           (send_status = 'PENDING' AND delivery_started_at IS NULL
                            AND delivery_claimed_at >= UTC_TIMESTAMP(6) - INTERVAL %s MINUTE))
                """, (message_id, token, DELIVERY_LEASE_MINUTES))
            await conn.commit()

    async def recover_abandoned_deliveries(self) -> int:
        """Quarantine lost dispatches. Never automatically resend customer messages."""
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE support_ticket_messages SET send_status='UNCERTAIN',
                        error_message='Delivery confirmation was lost; do not resend.', updated_at=UTC_TIMESTAMP(6)
                    WHERE send_status='DISPATCHING'
                      AND delivery_claimed_at < UTC_TIMESTAMP(6) - INTERVAL %s MINUTE
                """, (DELIVERY_LEASE_MINUTES,))
                changed = cur.rowcount
            await conn.commit()
        return changed
