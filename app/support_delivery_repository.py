"""Atomic outbound ownership. No provider I/O occurs inside a database transaction."""
from __future__ import annotations

import uuid


class DeliveryRepositoryMixin:
    async def claim_delivery(self, message_id: int, *, retry: bool = False, expected_token: str | None = None) -> str | None:
        if self._pool is None:
            await self.connect()
        token = uuid.uuid4().hex
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                # A lost process may have crossed the provider boundary. Never steal
                # its dispatch; late authenticated confirmations can still mark SENT.
                await cur.execute("""
                    UPDATE support_ticket_messages SET send_status = 'UNCERTAIN',
                        error_message = 'Delivery confirmation was lost; do not resend.'
                    WHERE id = %s AND send_status = 'DISPATCHING'
                      AND delivery_claimed_at < UTC_TIMESTAMP(6) - INTERVAL 5 MINUTE
                """, (message_id,))
                await cur.execute("""
                    UPDATE support_ticket_messages
                    SET delivery_token = %s, delivery_claimed_at = UTC_TIMESTAMP(6),
                        delivery_started_at = NULL, send_status = 'PENDING',
                        delivery_retry_safe = 0, error_message = NULL
                    WHERE id = %s AND direction = 'OUTBOUND'
                      AND deleted_for_everyone_at IS NULL AND whatsapp_message_id IS NULL
                      AND ((send_status = 'PENDING' AND delivery_started_at IS NULL
                            AND (delivery_token IS NULL OR delivery_claimed_at < UTC_TIMESTAMP(6) - INTERVAL 5 MINUTE))
                           OR (%s AND send_status = 'FAILED' AND delivery_retry_safe = 1 AND delivery_token <=> %s))
                """, (token, message_id, retry, expected_token))
                claimed = cur.rowcount == 1
            await conn.commit()
        return token if claimed else None

    async def begin_delivery(self, message_id: int, token: str) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE support_ticket_messages m JOIN support_tickets t ON t.id=m.ticket_id
                    SET m.send_status = 'DISPATCHING', m.delivery_started_at = UTC_TIMESTAMP(6),
                        m.delivery_claimed_at = UTC_TIMESTAMP(6)
                    WHERE m.id = %s AND m.delivery_token = %s AND m.send_status = 'PENDING'
                      AND m.delivery_started_at IS NULL
                      AND (m.sender_type = 'SYSTEM' OR
                           (m.sender_type = 'AGENT' AND t.current_assignee_id = m.sender_id
                            AND t.active_key IS NOT NULL
                            AND t.status IN ('ASSIGNED','IN_PROGRESS','WAITING_CUSTOMER','WAITING_INTERNAL')))
                """, (message_id, token))
                changed = cur.rowcount == 1
            await conn.commit()
        return changed

    async def finish_delivery(self, message_id: int, token: str, *, status: str,
                              provider_id: str | None = None, error: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE support_ticket_messages SET send_status = %s,
                        whatsapp_message_id = COALESCE(%s, whatsapp_message_id),
                        error_message = %s, delivery_retry_safe = %s, updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s AND delivery_token = %s AND send_status <> 'SENT'
                """, (status, provider_id, error, status == 'FAILED', message_id, token))
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
                      AND delivery_claimed_at < UTC_TIMESTAMP(6) - INTERVAL 5 MINUTE
                """)
                changed = cur.rowcount
            await conn.commit()
        return changed
