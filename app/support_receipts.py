"""Durable inbox for Cloud callbacks that race the send response."""
import aiomysql


class ReceiptRepositoryMixin:
    async def save_delivery_receipt(self, provider_id, status, at, line, phone, failure_code=None):
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""INSERT INTO support_delivery_receipts
                    (provider_message_id,line_id,phone,status,occurred_at,failure_code) VALUES (%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE failure_code=COALESCE(VALUES(failure_code),failure_code)""",
                    (provider_id, line, phone, status, at, failure_code))
            await conn.commit()

    async def pending_delivery_receipts(self):
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute('DELETE FROM support_delivery_receipts WHERE created_at < UTC_TIMESTAMP(6) - INTERVAL 7 DAY LIMIT 100')
                await cur.execute("""SELECT * FROM support_delivery_receipts
                    WHERE checked_at IS NULL OR checked_at < UTC_TIMESTAMP(6) - INTERVAL 30 SECOND
                    ORDER BY checked_at,id LIMIT 100""")
                rows = list(await cur.fetchall())
            await conn.commit()
            return rows

    async def finish_receipt_check(self, receipt_id, applied):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute('DELETE FROM support_delivery_receipts WHERE id=%s' if applied else
                    'UPDATE support_delivery_receipts SET checked_at=UTC_TIMESTAMP(6) WHERE id=%s', (receipt_id,))
            await conn.commit()
