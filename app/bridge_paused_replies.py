"""Preserve exact bot replies refused by an administrative pause, without replaying AI.

These are customer delivery records, deliberately absent from management APIs.
Unknown dispatches never become eligible merely because a worker/lease vanished.
"""
import hashlib
import re

import aiomysql

from app.bridge_recovery_repository import bridge_recovery_repository
from app.whatsapp_registry import whatsapp_registry_repository
from app.whatsapp_client import linked_device_client, WhatsAppSendError, is_linked_device_message

# Proven non-sends that clear once the line can send again. Any other definitive
# rejection is permanent for that reply and must not be retried in a loop.
TRANSIENT_CODES = frozenset({'NOT_CONNECTED', 'BRIDGE_UNAVAILABLE', 'BRIDGE_INITIALIZING', 'STORAGE_PRESSURE',
                             'STOPPING', 'MEDIA_UPLOAD_BUSY', 'ADMIN_PAUSED', 'SEND_QUEUE_FULL'})
UNCERTAIN_RECHECK_SECONDS = 60
# Order is per customer: older retained staff work for the same customer on the
# same line goes first. Uncertain work is never resent, so it never blocks.
_STAFF_AHEAD = """SELECT m.id FROM support_ticket_messages m JOIN support_tickets t ON t.id=m.ticket_id
    WHERE t.business_phone_number_id=%s AND REGEXP_REPLACE(t.whatsapp_phone,'[^0-9]','')=%s
      AND m.admin_hold=1 AND m.direction='OUTBOUND' AND m.deleted_for_everyone_at IS NULL
      AND (t.active_key IS NOT NULL OR m.sender_type='SYSTEM')
      AND (m.send_status IN ('PENDING','DISPATCHING') OR (m.send_status='FAILED' AND m.delivery_retry_safe=1))"""


def digits(value):
    return re.sub(r'\D', '', str(value or ''))


class PausedReplies:
    async def waiting(self, line_id, recipient):
        if await bridge_recovery_repository.paused(line_id): return True
        peer = digits(recipient)
        return bool(await whatsapp_registry_repository.fetch_one("""SELECT id FROM whatsapp_paused_bot_replies
            WHERE line_id=%s AND REGEXP_REPLACE(recipient,'[^0-9]','')=%s AND state IN ('queued','dispatching')
            UNION ALL """ + _STAFF_AHEAD + " LIMIT 1", (line_id, peer, line_id, peer)))

    async def enqueue(self, line_id, source_id, recipient, body):
        transport_id = 'paused-bot:' + hashlib.sha256((line_id + ':' + source_id).encode()).hexdigest()
        pool = await bridge_recovery_repository.pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""INSERT INTO whatsapp_paused_bot_replies
                    (line_id,source_message_id,transport_id,recipient,body,state,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,'queued',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE id=id""",
                    (line_id,source_id,transport_id,recipient,body))
                await cur.execute('SELECT recipient,body FROM whatsapp_paused_bot_replies WHERE transport_id=%s', (transport_id,))
                row = await cur.fetchone()
                if row['recipient'] != recipient or row['body'] != body:
                    raise RuntimeError('DEFERRED_REPLY_CONFLICT')
        return {'queued':True, 'reason':'ADMIN_PAUSED'}

    async def _waiting_or_refuse(self, line_id, recipient):
        try:
            return await self.waiting(line_id, recipient)
        except Exception as exc:
            # Fail closed BEFORE any provider call: a proven non-send, so the
            # reply guard is released rather than recorded as possibly sent.
            error = WhatsAppSendError('Administrative state is unavailable.', delivery_uncertain=False)
            error.bridge_code = 'RECOVERY_STATE_UNAVAILABLE'
            raise error from exc

    async def send_or_queue(self, provider_send, *, line_id, source_id, recipient, body):
        if not source_id:
            # Do not invent a retry identity for an uncorrelated customer send.
            if await self._waiting_or_refuse(line_id, recipient):
                error = WhatsAppSendError('This number is paused.', delivery_uncertain=False)
                error.bridge_code = 'ADMIN_PAUSED'; raise error
            return await provider_send()
        if await self._waiting_or_refuse(line_id, recipient):
            return await self.enqueue(line_id, source_id, recipient, body)
        try: return await provider_send()
        except WhatsAppSendError as exc:
            if exc.delivery_uncertain or getattr(exc, 'bridge_code', None) != 'ADMIN_PAUSED': raise
            return await self.enqueue(line_id, source_id, recipient, body)

    async def confirm(self, event):
        """Record positive bridge evidence that a retained bot reply was accepted.

        An ordinary bot reply is never mirrored into a support conversation (and
        never opens an unassigned ticket); a retained one must not be either.
        The exact reply text already lives in the AI conversation history."""
        pool = await bridge_recovery_repository.pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""SELECT id,line_id,recipient,state,provider_message_id,dispatch_at
                    FROM whatsapp_paused_bot_replies WHERE transport_id=%s""", (event['client_message_id'],))
                row = await cur.fetchone()
                if not row or row['line_id'] != event['line_id'] or digits(row['recipient']) != digits(event['sender_phone']):
                    return False
                if row['provider_message_id'] and row['provider_message_id'] != event['message_id']:
                    return False
                if row['state'] not in {'dispatching','uncertain','sent'} or not row['dispatch_at']: return False
                await cur.execute("""UPDATE whatsapp_paused_bot_replies SET state='sent',provider_message_id=%s,
                    updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND state IN ('dispatching','uncertain','sent')""",
                    (event['message_id'],row['id']))
        return True

    async def process(self):
        lines = await whatsapp_registry_repository.fetch_all("""SELECT DISTINCT line_id FROM whatsapp_paused_bot_replies
            WHERE state IN ('queued','dispatching')
               OR (state='uncertain' AND updated_at<UTC_TIMESTAMP(6)-INTERVAL %s SECOND) LIMIT 32""",
            (UNCERTAIN_RECHECK_SECONDS,))
        for item in lines:
            line = item['line_id']
            async with bridge_recovery_repository.coordinated('bot:' + line) as conn:
                if conn is None: continue
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await self._resolve_unknown(cur, line)
                    await self._dispatch_queued(cur, line)

    async def _resolve_unknown(self, cur, line):
        # This worker holds the line's dispatch mutex, so a 'dispatching' row
        # belongs to an ended holder. That ending cannot prove the provider did
        # not send: only positive evidence resolves it, and it is never resent.
        await cur.execute("""UPDATE whatsapp_paused_bot_replies SET state='uncertain',updated_at=UTC_TIMESTAMP(6)-INTERVAL %s SECOND
            WHERE line_id=%s AND state='dispatching'""", (UNCERTAIN_RECHECK_SECONDS, line))
        await cur.execute("""SELECT id,recipient,transport_id FROM whatsapp_paused_bot_replies WHERE line_id=%s
            AND state='uncertain' AND updated_at<UTC_TIMESTAMP(6)-INTERVAL %s SECOND ORDER BY id LIMIT 8""",
            (line, UNCERTAIN_RECHECK_SECONDS - 1))
        for row in list(await cur.fetchall()):
            await cur.execute("UPDATE whatsapp_paused_bot_replies SET updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND state='uncertain'", (row['id'],))
            try:
                evidence = await linked_device_client.get_delivery_status(
                    phone=row['recipient'], phone_number_id=line, client_message_id=row['transport_id'])
            except WhatsAppSendError:
                return
            if evidence.get('status') == 'confirmed' and is_linked_device_message(evidence.get('message_id'), line):
                await self.confirm({'client_message_id':row['transport_id'],'line_id':line,
                                    'sender_phone':row['recipient'],'message_id':evidence['message_id']})

    async def _dispatch_queued(self, cur, line):
        from app.conversation_store import conversation_store
        await cur.execute("""SELECT *, (dispatch_at IS NOT NULL AND updated_at>UTC_TIMESTAMP(6)-INTERVAL 15 SECOND) AS cooling
            FROM whatsapp_paused_bot_replies WHERE line_id=%s AND state='queued' ORDER BY id LIMIT 20""", (line,))
        blocked = set()
        for row in list(await cur.fetchall()):
            # A recent proven non-send (offline/paused/storage) applies to the
            # whole line: wait before contacting the bridge again.
            if row['cooling']: return
            peer = digits(row['recipient'])
            if peer in blocked: continue
            if await conversation_store.is_inbound_revoked(row['source_message_id']):
                await cur.execute("UPDATE whatsapp_paused_bot_replies SET state='revoked',updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND state='queued'", (row['id'],))
                continue
            await cur.execute(_STAFF_AHEAD + " AND m.created_at<%s LIMIT 1", (line, peer, row['created_at']))
            if await cur.fetchone():
                blocked.add(peer); continue
            # Re-read immediately before each dispatch: a pause saved meanwhile wins.
            if await bridge_recovery_repository.paused(line): return
            await cur.execute("UPDATE whatsapp_paused_bot_replies SET state='dispatching',dispatch_at=UTC_TIMESTAMP(6),updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND state='queued'", (row['id'],))
            if cur.rowcount != 1:
                blocked.add(peer); continue
            try:
                result = await linked_device_client.send_text(row['recipient'],row['body'],phone_number_id=line,client_message_id=row['transport_id'])
                provider_id = result.get('messages', [{}])[0].get('id')
                if not is_linked_device_message(provider_id, line):
                    raise WhatsAppSendError('Confirmation unavailable.', delivery_uncertain=True)
            except BaseException as exc:
                safe = isinstance(exc, WhatsAppSendError) and not exc.delivery_uncertain
                transient = safe and getattr(exc, 'bridge_code', None) in TRANSIENT_CODES
                await cur.execute("UPDATE whatsapp_paused_bot_replies SET state=%s,updated_at=UTC_TIMESTAMP(6) WHERE id=%s AND state='dispatching'",
                                  ('queued' if transient else 'failed' if safe else 'uncertain', row['id']))
                if not isinstance(exc, Exception): raise
                # A line-wide condition (offline, paused, storage) ends this pass;
                # a reply-specific rejection or uncertainty does not hold others.
                if transient: return
                continue
            await self.confirm({'client_message_id':row['transport_id'],'line_id':line,
                                'sender_phone':row['recipient'],'message_id':provider_id})


paused_replies = PausedReplies()
