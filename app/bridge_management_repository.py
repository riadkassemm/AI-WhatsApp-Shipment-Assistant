"""Read-only operational metadata. Never seed the registry or fetch message bodies."""
from __future__ import annotations

from app.whatsapp_registry import whatsapp_registry_repository


class BridgeManagementRepository:
    async def numbers(self) -> list[dict]:
        return await whatsapp_registry_repository.fetch_all(
            """SELECT phone_number_id, display_name, display_phone_number, is_enabled
               FROM whatsapp_phone_numbers WHERE source = 'LINKED_DEVICE' AND removed_at IS NULL
               ORDER BY phone_number_id"""
        )

    async def retired_label(self, line_id: str) -> dict | None:
        return await whatsapp_registry_repository.fetch_one(
            """SELECT business_display_name AS display_name,
                      business_display_phone_number AS display_phone_number
               FROM support_tickets WHERE business_phone_number_id = %s
               ORDER BY id DESC LIMIT 1""", (line_id,)
        )

    async def history(self, line_id: str) -> list[dict]:
        # Deliberately omit details_json: historical/free-text audit payloads
        # are not an operational disclosure contract.
        return await whatsapp_registry_repository.fetch_all(
            """SELECT id, action, actor_staff_id, created_at FROM whatsapp_integration_audit
               WHERE phone_number_id = %s ORDER BY id DESC LIMIT 30""", (line_id,)
        )


bridge_management_repository = BridgeManagementRepository()
