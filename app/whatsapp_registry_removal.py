"""Physical registry deletion with durable suppression in the existing audit log."""
from contextlib import asynccontextmanager
import hashlib
from typing import Any

import aiomysql

from app.config import settings

async def removed_number(cur: Any, phone_id: str) -> bool:
    await cur.execute("""SELECT id FROM whatsapp_integration_audit WHERE phone_number_id=%s
        AND action IN ('META_NUMBER_REMOVED','LINKED_DEVICE_LINE_REMOVED') LIMIT 1""", (phone_id,))
    return bool(await cur.fetchone())


async def delete_number_profile(cur: Any, row: dict) -> None:
    # Tickets already retain the actual phone/assignee; snapshot the custom label
    # and retired state before removing the mutable directory record.
    await cur.execute("""UPDATE support_tickets SET business_display_name=%s,
        business_display_phone_number=COALESCE(%s,business_display_phone_number),
        business_number_retired_at=COALESCE(business_number_retired_at,%s,UTC_TIMESTAMP(6))
        WHERE business_phone_number_id=%s""",
        (row.get('display_name'), row.get('display_phone_number') or None, row.get('removed_at'), row['phone_number_id']))
    await cur.execute('DELETE FROM whatsapp_phone_numbers WHERE phone_number_id=%s', (row['phone_number_id'],))


class RegistryMutationMixin:
    @asynccontextmanager
    async def mutation_connection(self):
        """Serialize discovery/deletion across processes, including absent rows.

        Acquire before row locks; hold only across database work, never provider
        I/O. A removed number cannot reappear from a concurrent stale sync.
        """
        await self.connect()
        name = 'wa-registry:' + hashlib.sha256(settings.db_name.encode()).hexdigest()[:32]
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute('SELECT GET_LOCK(%s,5) AS acquired', (name,))
                result = await cur.fetchone()
                if not result or result.get('acquired') != 1:
                    from app.whatsapp_registry import WhatsAppRegistryConflictError
                    raise WhatsAppRegistryConflictError('WhatsApp numbers are being updated. Please retry.')
            try:
                yield conn
            finally:
                async with conn.cursor() as cur:
                    await cur.execute('SELECT RELEASE_LOCK(%s)', (name,))
                    await cur.fetchone()
