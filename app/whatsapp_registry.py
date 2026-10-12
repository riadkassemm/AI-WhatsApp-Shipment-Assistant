from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import aiomysql
import httpx

from app.config import removed_linked_device_line_ids, settings
from app.support_auth import SupportAgent
from app.whatsapp_client import configured_linked_device_lines, get_transport, linked_device_client


logger = logging.getLogger("shipment-bot")
_ID_RE = re.compile(r"^[0-9]{5,64}$")
_REQUIRED_TABLES = {
    "whatsapp_business_accounts",
    "whatsapp_phone_numbers",
    "whatsapp_integration_audit",
}


class WhatsAppRegistryError(RuntimeError):
    pass


class WhatsAppRegistryForbiddenError(WhatsAppRegistryError):
    pass


class WhatsAppRegistryValidationError(WhatsAppRegistryError):
    pass


class WhatsAppRegistryProviderError(WhatsAppRegistryError):
    pass


class WhatsAppRegistrySchemaError(WhatsAppRegistryError):
    pass


class WhatsAppRegistryConflictError(WhatsAppRegistryError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clean_id(value: str | None, *, label: str) -> str:
    cleaned = str(value or "").strip()
    if not _ID_RE.fullmatch(cleaned):
        raise WhatsAppRegistryValidationError(f"Enter a valid numeric {label}.")
    return cleaned


class WhatsAppRegistryRepository:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        create_pool = getattr(aiomysql, "create_pool", None)
        if create_pool is None:
            raise WhatsAppRegistryError("WhatsApp registry database driver is unavailable.")
        self._pool = await create_pool(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            db=settings.db_name,
            minsize=1,
            maxsize=5,
            autocommit=True,
            charset="utf8mb4",
        )

    async def fetch_one(self, query: str, params: tuple | list = ()) -> dict[str, Any] | None:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                return await cur.fetchone()

    async def fetch_all(self, query: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                return list(await cur.fetchall())

    async def execute(self, query: str, params: tuple | list = ()) -> int:
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                return int(cur.rowcount or 0)

    async def prepare_linked_device_removal(
        self, line_id: str, *, actor_id: int, close_active_tickets: bool,
    ) -> int:
        """Stop routing before native logout; retain conversations until it succeeds."""
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM whatsapp_phone_numbers WHERE phone_number_id = %s FOR UPDATE", (line_id,))
                    row = await cur.fetchone()
                    if not row or row.get("source") != "LINKED_DEVICE":
                        raise WhatsAppRegistryValidationError("Only a registered linked-device number can be removed here.")
                    await cur.execute(
                        "SELECT id FROM support_tickets WHERE business_phone_number_id = %s AND active_key IS NOT NULL FOR UPDATE",
                        (line_id,),
                    )
                    active = list(await cur.fetchall())
                    if active and not close_active_tickets:
                        raise WhatsAppRegistryConflictError(
                            f"This number has {len(active)} active conversation(s). Confirm closing them before removing and unlinking the number."
                        )
                    await cur.execute(
                        """
                        SELECT m.id FROM support_ticket_messages m
                        JOIN support_tickets t ON t.id = m.ticket_id
                        WHERE t.business_phone_number_id = %s
                          AND m.direction = 'OUTBOUND' AND m.send_status = 'PENDING'
                          AND m.created_at >= (UTC_TIMESTAMP(6) - INTERVAL 2 MINUTE)
                        LIMIT 1 FOR UPDATE
                        """, (line_id,),
                    )
                    if await cur.fetchone():
                        raise WhatsAppRegistryConflictError("A WhatsApp message is still being sent. Try removing the number after it finishes.")
                    await cur.execute(
                        "UPDATE whatsapp_phone_numbers SET is_enabled = 0, updated_by_staff_id = %s WHERE phone_number_id = %s",
                        (actor_id, line_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO whatsapp_integration_audit (action, actor_staff_id, phone_number_id, details_json)
                        VALUES ('LINKED_DEVICE_REMOVAL_STARTED', %s, %s, %s)
                        """, (actor_id, line_id, json.dumps({"close_active_tickets": close_active_tickets, "active_conversations": len(active)})),
                    )
                await conn.commit()
                return len(active)
            except Exception:
                await conn.rollback()
                raise

    async def finish_linked_device_removal(
        self, line_id: str, *, actor_id: int, actor_name: str, close_active_tickets: bool,
    ) -> list[dict[str, Any]]:
        """Archive remaining conversations only after WhatsApp confirms logout."""
        await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM whatsapp_phone_numbers WHERE phone_number_id = %s FOR UPDATE", (line_id,))
                    row = await cur.fetchone()
                    if not row or row.get("source") != "LINKED_DEVICE":
                        raise WhatsAppRegistryValidationError("Only a registered linked-device number can be removed here.")
                    if row.get("is_enabled"):
                        raise WhatsAppRegistryConflictError("This number was enabled again while unlinking. Refresh and retry removal.")
                    await cur.execute(
                        "SELECT * FROM support_tickets WHERE business_phone_number_id = %s AND active_key IS NOT NULL FOR UPDATE",
                        (line_id,),
                    )
                    tickets = list(await cur.fetchall())
                    if tickets and not close_active_tickets:
                        raise WhatsAppRegistryConflictError("A conversation became active while unlinking. Confirm closing it and retry Remove and unlink.")
                    details = json.dumps({"reason": "whatsapp_line_removed", "phone_number_id": line_id})
                    for ticket in tickets:
                        await cur.execute(
                            """
                            UPDATE support_tickets SET status = 'CLOSED', active_key = NULL,
                                closed_at = UTC_TIMESTAMP(6), resolved_by_staff_id = %s,
                                updated_at = UTC_TIMESTAMP(6), last_activity_at = UTC_TIMESTAMP(6)
                            WHERE id = %s
                            """, (actor_id, ticket["id"]),
                        )
                        event_type = "CHAT_CLOSED" if ticket.get("conversation_origin") == "STAFF" else "TICKET_CLOSED"
                        await cur.execute(
                            """
                            INSERT INTO support_ticket_events (ticket_id, event_type, actor_type, actor_id, actor_name, details_json, created_at)
                            VALUES (%s, %s, 'AGENT', %s, %s, %s, UTC_TIMESTAMP(6))
                            """, (ticket["id"], event_type, str(actor_id), actor_name, details),
                        )
                    # Keep a disabled descriptor for historic provider references and
                    # idempotent retries. The manifest permanently retires this ID.
                    await cur.execute(
                        "UPDATE whatsapp_phone_numbers SET is_enabled = 0, provider_present = 0, updated_by_staff_id = %s WHERE phone_number_id = %s",
                        (actor_id, line_id),
                    )
                    if row.get("provider_present") or tickets:
                        await cur.execute(
                            """
                            INSERT INTO whatsapp_integration_audit (action, actor_staff_id, phone_number_id, details_json)
                            VALUES ('LINKED_DEVICE_LINE_REMOVED', %s, %s, %s)
                            """, (actor_id, line_id, json.dumps({"closed_conversations": len(tickets), "unlinked": True})),
                        )
                await conn.commit()
                return tickets
            except Exception:
                await conn.rollback()
                raise

    async def persist_sync(
        self,
        *,
        waba_id: str,
        account_name: str | None,
        numbers: list[dict[str, Any]],
        actor_id: int,
    ) -> None:
        await self.connect()
        assert self._pool is not None
        now = _utcnow()
        async with self._pool.acquire() as conn:
            autocommit = getattr(conn, "autocommit", None)
            if callable(autocommit):
                await autocommit(False)
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """
                        INSERT INTO whatsapp_business_accounts (
                            waba_id, name, is_active, last_synced_at,
                            created_by_staff_id, updated_by_staff_id
                        ) VALUES (%s, %s, 1, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            name = VALUES(name), is_active = 1,
                            last_synced_at = VALUES(last_synced_at),
                            updated_by_staff_id = VALUES(updated_by_staff_id)
                        """,
                        (waba_id, account_name, now, actor_id, actor_id),
                    )
                    discovered_ids: list[str] = []
                    for item in numbers:
                        phone_id = _clean_id(str(item.get("id") or ""), label="phone-number ID")
                        discovered_ids.append(phone_id)
                        legacy_default = phone_id == str(settings.whatsapp_phone_number_id or "").strip()
                        await cur.execute(
                            """
                            INSERT INTO whatsapp_phone_numbers (
                                phone_number_id, waba_id, display_phone_number, verified_name,
                                quality_rating, is_enabled, provider_present, source,
                                last_synced_at, updated_by_staff_id
                            ) VALUES (%s, %s, %s, %s, %s, %s, 1, 'META_SYNC', %s, %s)
                            ON DUPLICATE KEY UPDATE
                                waba_id = VALUES(waba_id),
                                display_phone_number = VALUES(display_phone_number),
                                verified_name = VALUES(verified_name),
                                quality_rating = VALUES(quality_rating),
                                provider_present = 1,
                                source = IF(source = 'LEGACY_ENV', source, 'META_SYNC'),
                                last_synced_at = VALUES(last_synced_at),
                                updated_by_staff_id = VALUES(updated_by_staff_id)
                            """,
                            (
                                phone_id,
                                waba_id,
                                str(item.get("display_phone_number") or "")[:64] or None,
                                str(item.get("verified_name") or "")[:255] or None,
                                str(item.get("quality_rating") or "")[:32] or None,
                                1 if legacy_default else 0,
                                now,
                                actor_id,
                            ),
                        )
                    if discovered_ids:
                        placeholders = ",".join(["%s"] * len(discovered_ids))
                        await cur.execute(
                            f"""
                            UPDATE whatsapp_phone_numbers
                            SET provider_present = 0, is_enabled = 0,
                                updated_by_staff_id = %s
                            WHERE waba_id = %s
                              AND phone_number_id NOT IN ({placeholders})
                            """,
                            (actor_id, waba_id, *discovered_ids),
                        )
                    else:
                        await cur.execute(
                            """
                            UPDATE whatsapp_phone_numbers
                            SET provider_present = 0, is_enabled = 0,
                                updated_by_staff_id = %s
                            WHERE waba_id = %s
                            """,
                            (actor_id, waba_id),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
            finally:
                if callable(autocommit):
                    await autocommit(True)


whatsapp_registry_repository = WhatsAppRegistryRepository()


class WhatsAppRegistryService:
    def __init__(self, repository: WhatsAppRegistryRepository | None = None) -> None:
        self.repository = repository or whatsapp_registry_repository
        self._graph_base = f"https://graph.facebook.com/{settings.whatsapp_api_version}"

    @staticmethod
    def _require_manager(agent: SupportAgent) -> None:
        if not agent.is_supervisor:
            raise WhatsAppRegistryForbiddenError(
                "Only Supervisors and Admins can manage WhatsApp numbers."
            )

    async def _ensure_schema(self) -> None:
        rows = await self.repository.fetch_all(
            """
            SELECT TABLE_NAME AS table_name
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME IN (%s, %s, %s)
            """,
            (
                settings.db_name,
                "whatsapp_business_accounts",
                "whatsapp_phone_numbers",
                "whatsapp_integration_audit",
            ),
        )
        present = {str(row.get("table_name") or "") for row in rows}
        if not _REQUIRED_TABLES.issubset(present):
            raise WhatsAppRegistrySchemaError(
                "WhatsApp number registry is not installed. Apply "
                "migrations/20260921_whatsapp_number_registry.sql."
            )

    async def _seed_legacy_number(self) -> None:
        phone_id = str(settings.whatsapp_phone_number_id or "").strip()
        if phone_id and _ID_RE.fullmatch(phone_id):
            await self.repository.execute(
                """
                INSERT INTO whatsapp_phone_numbers (
                    phone_number_id, is_enabled, provider_present, source, last_synced_at
                ) VALUES (%s, 1, 1, 'LEGACY_ENV', %s)
                ON DUPLICATE KEY UPDATE
                    phone_number_id = phone_number_id
                """,
                (phone_id, _utcnow()),
            )
        for line_id, expected_number in configured_linked_device_lines().items():
            if get_transport(line_id) != "linked_device":
                continue
            await self.repository.execute(
                """
                INSERT INTO whatsapp_phone_numbers (
                    phone_number_id, display_phone_number, is_enabled,
                    provider_present, source, last_synced_at
                ) VALUES (%s, %s, 0, 1, 'LINKED_DEVICE', %s)
                ON DUPLICATE KEY UPDATE
                    source = 'LINKED_DEVICE', waba_id = NULL,
                    display_phone_number = COALESCE(VALUES(display_phone_number), display_phone_number),
                    provider_present = 1
                """,
                (line_id, expected_number or None, _utcnow()),
            )

    @property
    def _auth_headers(self) -> dict[str, str]:
        token = str(settings.whatsapp_token or "").strip()
        if not token:
            raise WhatsAppRegistryValidationError(
                "The server has no WhatsApp access token configured."
            )
        return {"Authorization": f"Bearer {token}"}

    async def _graph_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = max(10.0, float(settings.support_media_download_timeout_seconds))
        url = f"{self._graph_base}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method,
                    url,
                    headers=self._auth_headers,
                    params=params,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            logger.warning(
                "Meta WhatsApp management request rejected: status=%s path=%s",
                status or "unknown",
                path.split("?", 1)[0],
            )
            raise WhatsAppRegistryProviderError(
                f"Meta rejected the WhatsApp management request (HTTP {status or 'unknown'}). "
                "Check that the configured token can manage this WhatsApp Business Account."
            ) from exc
        except httpx.RequestError as exc:
            raise WhatsAppRegistryProviderError(
                "Could not reach Meta to manage WhatsApp numbers."
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise WhatsAppRegistryProviderError(
                "Meta returned an invalid WhatsApp management response."
            ) from exc
        if not isinstance(data, dict):
            raise WhatsAppRegistryProviderError(
                "Meta returned an unexpected WhatsApp management response."
            )
        return data

    async def _fetch_waba(self, waba_id: str) -> dict[str, Any]:
        return await self._graph_json("GET", waba_id, params={"fields": "id,name"})

    async def _subscribe_waba(self, waba_id: str) -> None:
        data = await self._graph_json("POST", f"{waba_id}/subscribed_apps")
        if data.get("success") not in {True, "true", 1, "1"}:
            raise WhatsAppRegistryProviderError(
                "Meta did not confirm the WhatsApp Business Account webhook subscription."
            )

    async def _fetch_phone_numbers(self, waba_id: str) -> list[dict[str, Any]]:
        numbers: list[dict[str, Any]] = []
        after: str | None = None
        for _page in range(20):
            params: dict[str, Any] = {
                "fields": "id,display_phone_number,verified_name,quality_rating",
                "limit": 100,
            }
            if after:
                params["after"] = after
            data = await self._graph_json("GET", f"{waba_id}/phone_numbers", params=params)
            page_items = data.get("data")
            if not isinstance(page_items, list):
                raise WhatsAppRegistryProviderError("Meta returned an invalid phone-number list.")
            for item in page_items:
                if isinstance(item, dict) and item.get("id"):
                    numbers.append(item)
            paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
            cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
            next_after = str(cursors.get("after") or "").strip()
            if not paging.get("next") or not next_after or next_after == after:
                break
            after = next_after
        return numbers

    async def _audit(
        self,
        *,
        action: str,
        actor_staff_id: int | None = None,
        waba_id: str | None = None,
        phone_number_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self.repository.execute(
            """
            INSERT INTO whatsapp_integration_audit (
                action, actor_staff_id, waba_id, phone_number_id, details_json
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                action,
                actor_staff_id,
                waba_id,
                phone_number_id,
                json.dumps(details or {}, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _normalize_account(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "waba_id": str(row.get("waba_id") or ""),
            "name": row.get("name"),
            "is_active": bool(row.get("is_active")),
            "last_synced_at": row.get("last_synced_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def _normalize_number(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "phone_number_id": str(row.get("phone_number_id") or ""),
            "waba_id": str(row.get("waba_id") or "") or None,
            "display_phone_number": row.get("display_phone_number"),
            "verified_name": row.get("verified_name"),
            "quality_rating": row.get("quality_rating"),
            "is_enabled": bool(row.get("is_enabled")),
            "provider_present": bool(row.get("provider_present")),
            "source": row.get("source"),
            "last_synced_at": row.get("last_synced_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    async def list_payload(self, agent: SupportAgent) -> dict[str, Any]:
        self._require_manager(agent)
        await self._ensure_schema()
        await self._seed_legacy_number()
        accounts = await self.repository.fetch_all(
            """
            SELECT waba_id, name, is_active, last_synced_at, created_at, updated_at
            FROM whatsapp_business_accounts
            ORDER BY COALESCE(name, waba_id), waba_id
            """
        )
        numbers = await self.repository.fetch_all(
            """
            SELECT phone_number_id, waba_id, display_phone_number, verified_name,
                   quality_rating, is_enabled, provider_present, source,
                   last_synced_at, created_at, updated_at
            FROM whatsapp_phone_numbers
            ORDER BY is_enabled DESC, COALESCE(display_phone_number, phone_number_id)
            """
        )
        removed_ids = removed_linked_device_line_ids()
        return {
            "accounts": [self._normalize_account(row) for row in accounts],
            "numbers": [self._normalize_number(row) for row in numbers
                        if not (row.get("source") == "LINKED_DEVICE" and not row.get("provider_present") and str(row.get("phone_number_id")) in removed_ids)],
        }

    async def prepare_linked_device_removal(
        self, agent: SupportAgent, line_id: str, *, close_active_tickets: bool,
    ) -> int:
        self._require_manager(agent)
        line_id = _clean_id(line_id, label="linked-device line ID")
        if line_id not in configured_linked_device_lines() and line_id not in removed_linked_device_line_ids():
            raise WhatsAppRegistryValidationError("This linked-device number is not registered.")
        await self._ensure_schema()
        await self._seed_legacy_number()
        return await self.repository.prepare_linked_device_removal(
            line_id, actor_id=int(agent.id), close_active_tickets=close_active_tickets,
        )

    async def finish_linked_device_removal(self, agent: SupportAgent, line_id: str, *, close_active_tickets: bool) -> int:
        self._require_manager(agent)
        tickets = await self.repository.finish_linked_device_removal(
            line_id, actor_id=int(agent.id), actor_name=agent.name, close_active_tickets=close_active_tickets,
        )
        # Do not use normal ticket resolution: it sends a customer notice, and
        # this account has already been unlinked from WhatsApp.
        from app.conversation_store import conversation_store
        from app.support_events import publish_support_event

        for ticket in tickets:
            try:
                phone = str(ticket["whatsapp_phone"])
                conversation = await conversation_store.get_or_create(
                    phone, conversation_key=f"{line_id}:{phone}",
                    business_phone_number_id=line_id,
                    business_display_phone_number=ticket.get("business_display_phone_number"),
                )
                if str(conversation.active_ticket_id or "") == str(ticket["id"]):
                    conversation.mode = "ai"
                    conversation.active_ticket_id = None
                    conversation.messages.append({"role": "system", "content": "SUPPORT EVENT: The business WhatsApp connection was removed. This conversation is closed; prior human ownership is historical."})
                    await conversation_store.save(conversation)
            except Exception:
                logger.exception("Removed WhatsApp line conversation cache could not be updated: ticket_id=%s", ticket["id"])
            await publish_support_event("ticket_updated", ticket_id=int(ticket["id"]), status="CLOSED")
        return len(tickets)

    async def sync_waba(self, agent: SupportAgent, waba_id: str) -> dict[str, Any]:
        self._require_manager(agent)
        waba_id = _clean_id(waba_id, label="WhatsApp Business Account ID")
        await self._ensure_schema()
        account = await self._fetch_waba(waba_id)
        if str(account.get("id") or "").strip() != waba_id:
            raise WhatsAppRegistryProviderError(
                "Meta returned a different WhatsApp Business Account than requested."
            )
        await self._subscribe_waba(waba_id)
        numbers = await self._fetch_phone_numbers(waba_id)
        await self.repository.persist_sync(
            waba_id=waba_id,
            account_name=str(account.get("name") or "")[:255] or None,
            numbers=numbers,
            actor_id=int(agent.id),
        )
        await self._audit(
            action="WABA_SYNCED",
            actor_staff_id=int(agent.id),
            waba_id=waba_id,
            details={"discovered_numbers": len(numbers), "subscribed": True},
        )
        logger.info(
            "WhatsApp WABA synced: actor_staff_id=%s waba_id=%s discovered_numbers=%s",
            agent.id,
            waba_id,
            len(numbers),
        )
        return await self.list_payload(agent)

    async def set_number_enabled(
        self,
        agent: SupportAgent,
        phone_number_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        self._require_manager(agent)
        phone_number_id = _clean_id(phone_number_id, label="phone-number ID")
        await self._ensure_schema()
        row = await self.repository.fetch_one(
            """
            SELECT phone_number_id, waba_id, provider_present, is_enabled, source
            FROM whatsapp_phone_numbers
            WHERE phone_number_id = %s
            LIMIT 1
            """,
            (phone_number_id,),
        )
        if not row:
            raise WhatsAppRegistryValidationError(
                "This phone number has not been discovered from Meta yet. Sync its WABA first."
            )
        if enabled and (get_transport(phone_number_id) == "removed" or (
            row.get("source") == "LINKED_DEVICE" and phone_number_id not in configured_linked_device_lines()
        )):
            raise WhatsAppRegistryValidationError("This WhatsApp connection was removed. Link the number again to create a new connection.")
        if enabled and not bool(row.get("provider_present")):
            raise WhatsAppRegistryValidationError(
                "This phone number is no longer present in the latest Meta sync."
            )
        linked_display_number = None
        if enabled and get_transport(phone_number_id) == "linked_device":
            try:
                linked_status = await linked_device_client.get_status()
            except Exception as exc:
                raise WhatsAppRegistryProviderError("The WhatsApp linked device service is unavailable.") from exc
            connected_line = next((
                line for line in linked_status.get("lines", [])
                if isinstance(line, dict) and str(line.get("line_id")) == phone_number_id and line.get("status") == "connected"
            ), None)
            if not connected_line:
                raise WhatsAppRegistryValidationError("Pair this WhatsApp line before adding it to the app.")
            paired_phone = str(connected_line.get("display_phone_number") or "")
            if re.fullmatch(r"[0-9]{5,15}", paired_phone):
                linked_display_number = paired_phone
        if not enabled and bool(row.get("is_enabled")):
            active = await self.repository.fetch_one(
                """
                SELECT COUNT(*) AS count
                FROM support_tickets
                WHERE business_phone_number_id = %s
                  AND active_key IS NOT NULL
                  AND status IN ('NEW','ASSIGNED','IN_PROGRESS','WAITING_CUSTOMER','WAITING_INTERNAL')
                """,
                (phone_number_id,),
            )
            active_count = int((active or {}).get("count") or 0)
            if active_count:
                raise WhatsAppRegistryConflictError(
                    f"This number still has {active_count} active support ticket(s). "
                    "Resolve them before removing the number from the app."
                )
        await self.repository.execute(
            """
            UPDATE whatsapp_phone_numbers
            SET is_enabled = %s, display_phone_number = COALESCE(%s, display_phone_number),
                updated_by_staff_id = %s
            WHERE phone_number_id = %s
            """,
            (1 if enabled else 0, linked_display_number, int(agent.id), phone_number_id),
        )
        await self._audit(
            action="WHATSAPP_NUMBER_ENABLED" if enabled else "WHATSAPP_NUMBER_DISABLED",
            actor_staff_id=int(agent.id),
            waba_id=str(row.get("waba_id") or "") or None,
            phone_number_id=phone_number_id,
        )
        logger.info(
            "WhatsApp number state changed: actor_staff_id=%s phone_number_id=%s enabled=%s",
            agent.id,
            phone_number_id,
            enabled,
        )
        return await self.list_payload(agent)

    async def observe_inbound(
        self,
        *,
        waba_id: str | None,
        phone_number_id: str | None,
        display_phone_number: str | None,
    ) -> bool:
        phone_id = str(phone_number_id or "").strip()
        if not phone_id:
            # Real Meta webhook payloads include metadata.phone_number_id. This
            # fallback preserves legacy unit/integration payloads only.
            return bool(str(settings.whatsapp_phone_number_id or "").strip())
        if get_transport(phone_id) == "removed":
            return False
        try:
            phone_id = _clean_id(phone_id, label="phone-number ID")
            await self._ensure_schema()
            await self._seed_legacy_number()
        except WhatsAppRegistrySchemaError:
            return phone_id == str(settings.whatsapp_phone_number_id or "").strip()

        clean_waba: str | None = None
        if waba_id:
            try:
                clean_waba = _clean_id(waba_id, label="WhatsApp Business Account ID")
            except WhatsAppRegistryValidationError:
                clean_waba = None
        if clean_waba:
            await self.repository.execute(
                """
                INSERT INTO whatsapp_business_accounts (waba_id, is_active)
                VALUES (%s, 1)
                ON DUPLICATE KEY UPDATE is_active = is_active
                """,
                (clean_waba,),
            )

        legacy_enabled = phone_id == str(settings.whatsapp_phone_number_id or "").strip()
        await self.repository.execute(
            """
            INSERT INTO whatsapp_phone_numbers (
                phone_number_id, waba_id, display_phone_number,
                is_enabled, provider_present, source
            ) VALUES (%s, %s, %s, %s, 1, 'WEBHOOK')
            ON DUPLICATE KEY UPDATE
                waba_id = COALESCE(VALUES(waba_id), waba_id),
                display_phone_number = COALESCE(VALUES(display_phone_number), display_phone_number),
                provider_present = 1
            """,
            (
                phone_id,
                clean_waba,
                str(display_phone_number or "")[:64] or None,
                1 if legacy_enabled else 0,
            ),
        )
        row = await self.repository.fetch_one(
            """
            SELECT is_enabled, provider_present
            FROM whatsapp_phone_numbers
            WHERE phone_number_id = %s
            LIMIT 1
            """,
            (phone_id,),
        )
        return bool(row and row.get("is_enabled") and row.get("provider_present"))

    async def list_enabled_numbers(self, agent: SupportAgent) -> list[dict[str, Any]]:
        """Return enabled WhatsApp lines safe for normal staff outbound use."""
        _ = agent
        await self._ensure_schema()
        await self._seed_legacy_number()
        rows = await self.repository.fetch_all(
            """
            SELECT phone_number_id, display_phone_number, verified_name, source
            FROM whatsapp_phone_numbers
            WHERE is_enabled = 1 AND provider_present = 1
            ORDER BY COALESCE(display_phone_number, verified_name, phone_number_id)
            """
        )
        return [
            {
                "phone_number_id": str(row.get("phone_number_id") or ""),
                "display_phone_number": row.get("display_phone_number"),
                "verified_name": row.get("verified_name"),
            }
            for row in rows
            if row.get("source") != "LINKED_DEVICE" or get_transport(str(row.get("phone_number_id"))) == "linked_device"
        ]

    async def is_number_enabled(self, phone_number_id: str | None) -> bool:
        phone_id = str(phone_number_id or settings.whatsapp_phone_number_id or "").strip()
        if not phone_id:
            return False
        try:
            await self._ensure_schema()
            await self._seed_legacy_number()
        except WhatsAppRegistrySchemaError:
            return phone_id == str(settings.whatsapp_phone_number_id or "").strip()
        row = await self.repository.fetch_one(
            """
            SELECT is_enabled, provider_present, source
            FROM whatsapp_phone_numbers
            WHERE phone_number_id = %s
            LIMIT 1
            """,
            (phone_id,),
        )
        return bool(
            row and row.get("is_enabled") and row.get("provider_present")
            and (row.get("source") != "LINKED_DEVICE" or get_transport(phone_id) == "linked_device")
        )


whatsapp_registry_service = WhatsAppRegistryService()
