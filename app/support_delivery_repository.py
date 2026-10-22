"""Atomic outbound ownership. No provider I/O occurs inside a database transaction."""
from __future__ import annotations

import uuid

import aiomysql


DELIVERY_LEASE_MINUTES = 5


class DeliveryRepositoryMixin:
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
                    await cur.execute("""
                        UPDATE support_ticket_messages
                        SET delivery_token = %s, delivery_claimed_at = UTC_TIMESTAMP(6),
                            delivery_started_at = NULL, send_status = 'PENDING',
                            delivery_retry_safe = 0, error_message = NULL
                        WHERE id = %s AND direction = 'OUTBOUND'
                          AND deleted_for_everyone_at IS NULL AND whatsapp_message_id IS NULL
                          AND ((send_status = 'PENDING' AND delivery_started_at IS NULL
                                AND (delivery_token IS NULL OR delivery_claimed_at < UTC_TIMESTAMP(6) - INTERVAL %s MINUTE))
                               OR (%s AND send_status = 'FAILED' AND delivery_retry_safe = 1 AND delivery_token <=> %s))
                          AND (sender_type = 'SYSTEM' OR
                               (sender_type = 'AGENT' AND sender_id = %s AND %s))
                    """, (token, message_id, DELIVERY_LEASE_MINUTES, retry, expected_token,
                          ticket['current_assignee_id'], bool(ticket['active_key']) and ticket['status'] in
                          ('ASSIGNED', 'IN_PROGRESS', 'WAITING_CUSTOMER', 'WAITING_INTERNAL')))
                    claimed = cur.rowcount == 1
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
                    await cur.execute("""
                        UPDATE support_ticket_messages
                        SET send_status = 'DISPATCHING', delivery_started_at = UTC_TIMESTAMP(6),
                            delivery_claimed_at = UTC_TIMESTAMP(6)
                        WHERE id = %s AND delivery_token = %s AND send_status = 'PENDING'
                          AND direction = 'OUTBOUND' AND deleted_for_everyone_at IS NULL
                          AND whatsapp_message_id IS NULL AND delivery_started_at IS NULL
                          AND delivery_claimed_at >= UTC_TIMESTAMP(6) - INTERVAL %s MINUTE
                          AND (sender_type = 'SYSTEM' OR
                               (sender_type = 'AGENT' AND sender_id = %s AND %s))
                    """, (message_id, token, DELIVERY_LEASE_MINUTES, ticket['current_assignee_id'],
                          bool(ticket['active_key']) and ticket['status'] in
                          ('ASSIGNED', 'IN_PROGRESS', 'WAITING_CUSTOMER', 'WAITING_INTERNAL')))
                    changed = cur.rowcount == 1
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return changed

    async def finish_delivery(self, message_id: int, token: str, *, status: str,
                              provider_id: str | None = None, error: str | None = None) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE support_ticket_messages SET send_status = %s,
                        whatsapp_message_id = COALESCE(%s, whatsapp_message_id),
                        error_message = %s, delivery_retry_safe = %s, updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s AND delivery_token = %s
                      AND ((%s = 'SENT' AND send_status IN ('DISPATCHING', 'UNCERTAIN'))
                           OR (%s IN ('FAILED', 'UNCERTAIN') AND send_status IN ('PENDING', 'DISPATCHING')
                               AND delivery_claimed_at >= UTC_TIMESTAMP(6) - INTERVAL %s MINUTE))
                """, (status, provider_id, error, status == 'FAILED', message_id, token, status, status, DELIVERY_LEASE_MINUTES))
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
