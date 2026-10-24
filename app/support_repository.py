from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiomysql

from app.support_staff_names import attach_current_staff_names
from app.config import settings
from app.support_read_sync import ReadSyncRepositoryMixin, attach_conversation_read_state
from app.support_receipts import ReceiptRepositoryMixin
from app.whatsapp_failures import failure_policy
from app.support_delivery_repository import DeliveryRepositoryMixin
from app.support_models import ACTIVE_TICKET_STATUSES, ALLOWED_TRANSITIONS, may_reopen_customer_ticket
from app.whatsapp_client import get_transport


# Messages mirrored from the linked WhatsApp app are the only outbound staff
# messages without a dashboard sender (see sync_linked_device_outbound).
_PHONE_APP_REPLY_EXISTS = """EXISTS (
    SELECT 1
    FROM support_ticket_messages phone_reply
    WHERE phone_reply.ticket_id = support_tickets.id
      AND phone_reply.direction = 'OUTBOUND'
      AND phone_reply.sender_type = 'AGENT'
      AND phone_reply.sender_id IS NULL
)"""

# An unclaimed ticket someone answered in the WhatsApp app, with no message in
# either direction for the idle window. Takes the idle minutes twice.
_IDLE_PHONE_APP_TICKET = f"""
    COALESCE(support_tickets.conversation_origin, 'CUSTOMER') <> 'STAFF'
    AND support_tickets.status = 'NEW'
    AND support_tickets.current_assignee_id IS NULL
    AND support_tickets.active_key IS NOT NULL
    AND {_PHONE_APP_REPLY_EXISTS}
    AND support_tickets.created_at < UTC_TIMESTAMP(6) - INTERVAL %s MINUTE
    AND NOT EXISTS (
        SELECT 1
        FROM support_ticket_messages recent_message
        WHERE recent_message.ticket_id = support_tickets.id
          AND recent_message.created_at >= UTC_TIMESTAMP(6) - INTERVAL %s MINUTE
    )
"""


def auto_resolve_actor_name(idle_minutes: int) -> str:
    hours, minutes = divmod(idle_minutes, 60)
    if minutes:
        window = f"{idle_minutes} minutes"
    else:
        window = "1 hour" if hours == 1 else f"{hours} hours"
    return f"Automatic (no messages for {window})"


class SupportRepositoryError(Exception):
    pass


class SupportSchemaError(SupportRepositoryError):
    """Raised when application code is newer than the support DB schema."""

    def __init__(self, missing_migrations: list[str]) -> None:
        self.missing_migrations = tuple(missing_migrations)
        migration_list = ", ".join(self.missing_migrations)
        super().__init__(
            "Support database schema is out of date. Apply the required migration(s) "
            f"before using the support console: {migration_list}"
        )


class SupportConflictError(Exception):
    pass


class SupportNotFoundError(Exception):
    pass


def _ticket_active_key(phone_number: str, business_phone_number_id: str | None) -> str:
    business_id = str(business_phone_number_id or "").strip()
    return f"{business_id}:{phone_number}" if business_id else phone_number


_SUPPORT_SCHEMA_MIGRATIONS: tuple[tuple[str, dict[str, set[str]]], ...] = (
    (
        "migrations/20260825_support_base_schema.sql",
        {
            "staff": {
                "id", "name", "email", "email_normalized", "password_hash",
                "role", "is_active", "last_login_at", "failed_login_count",
                "locked_until", "created_at", "updated_at",
            },
            "staff_sessions": {
                "id", "staff_id", "token_hash", "csrf_hash", "expires_at",
                "revoked_at", "created_at", "last_seen_at",
            },
            "support_tickets": {
                "id", "ticket_reference", "active_key", "whatsapp_phone",
                "customer_id", "customer_userid", "customer_name", "reason",
                "ai_summary", "tracking_reference", "requested_action",
                "current_language", "communication_style", "context_json",
                "source_whatsapp_message_id", "status", "current_assignee_id",
                "current_assignee_name", "created_at", "updated_at",
                "last_activity_at", "assigned_at", "resolved_at",
                "resolved_by_staff_id", "closed_at", "deleted_at",
                "deleted_by_staff_id", "deleted_by_staff_name",
            },
            "support_ticket_messages": {
                "id", "ticket_id", "sender_type", "sender_id", "sender_name",
                "direction", "message_type", "body", "whatsapp_message_id",
                "client_message_id", "whatsapp_timestamp", "send_status",
                "error_message", "created_at", "updated_at",
            },
            "support_ticket_events": {
                "id", "ticket_id", "event_type", "actor_type", "actor_id",
                "actor_name", "details_json", "created_at",
            },
        },
    ),
    (
        "migrations/20260829_support_ticket_channels_and_transfers.sql",
        {
            "support_tickets": {
                "business_phone_number_id",
                "business_display_phone_number",
            }
        },
    ),
    (
        "migrations/20260912_support_media_recovery.sql",
        {
            "support_ticket_messages": {
                "provider_media_id",
                "provider_media_mime_type",
            }
        },
    ),
    (
        "migrations/20260917_whatsapp_message_features.sql",
        {
            "support_ticket_messages": {
                "reply_to_whatsapp_message_id",
                "location_latitude",
                "location_longitude",
                "location_name",
                "location_address",
                "provider_media_filename",
            }
        },
    ),
    (
        "migrations/20260922_staff_initiated_chats.sql",
        {
            "support_tickets": {
                "conversation_origin",
                "initiated_by_staff_id",
                "initiated_by_staff_name",
            }
        },
    ),
    (
        "migrations/20260922_forwarded_message_marker.sql",
        {
            "support_ticket_messages": {
                "is_forwarded",
                "forwarded_from_ticket_id",
                "forwarded_from_message_id",
            }
        },
    ),
    (
        "migrations/20260926_whatsapp_stickers_and_reactions.sql",
        {
            "support_message_reactions": {
                "ticket_id",
                "message_id",
                "target_whatsapp_message_id",
                "reactor_key",
                "reactor_type",
                "emoji",
                "provider_reaction_id",
                "client_reaction_id",
                "provider_timestamp",
            }
        },
    ),
    (
        "migrations/20260928_support_message_changes.sql",
        {
            "support_ticket_messages": {"edited_at", "deleted_for_everyone_at", "deleted_by", "version"},
            "support_message_user_deletions": {"message_id", "user_id", "deleted_at"},
        },
    ),
    (
        "migrations/20261012_linked_device_message_changes.sql",
        {
            "support_ticket_messages": {"whatsapp_change_timestamp", "pending_change_operation_id"},
            "support_message_change_operations": {"operation_id", "message_id", "base_version", "user_id", "action", "content", "status", "lease_until", "provider_timestamp"},
            "support_message_change_events": {"event_id", "message_id", "provider_timestamp"},
        },
    ),
    (
        "migrations/20261014_whatsapp_number_display_names.sql",
        {"whatsapp_phone_numbers": {"display_name"}},
    ),
    (
        "migrations/20261017_staff_account_deletion.sql",
        {"staff": {"deleted_at"}},
    ),
    (
        "migrations/20261019_staff_quick_replies.sql",
        {"support_quick_replies": {"id", "staff_id", "title", "body", "created_at", "updated_at"}},
    ),
    (
        "migrations/20261020_editable_contacts.sql",
        {"support_contacts": {"replaces_phone"}},
    ),
    (
        "migrations/20261020_message_delivery_status.sql",
        {"support_ticket_messages": {"delivery_status", "delivered_at", "read_at"}},
    ),
    ("migrations/20261023_reply_read_sync.sql", {"support_ticket_messages": {"read_sync_pending", "read_sync_attempted_at"}, "support_delivery_receipts": {"provider_message_id", "line_id", "status", "checked_at"}}),
    ("migrations/20261023_whatsapp_failure_codes.sql", {"support_delivery_receipts": {"failure_code"}}),
    ("migrations/20261023_removed_meta_numbers.sql", {"whatsapp_phone_numbers": {"removed_at"}}),
    ("migrations/20261021_outbound_delivery_ownership.sql", {"support_ticket_messages": {
        "transport_client_id", "sender_scope", "delivery_token", "delivery_claimed_at",
        "delivery_started_at", "delivery_retry_safe",
    }}),
    ("migrations/20261022_uncertain_delivery_copies.sql", {"support_ticket_messages": {
        "replacement_for_message_id",
    }}),
    ("migrations/20261023_quick_reply_pdfs.sql", {
        "support_quick_replies": {"attachment_key", "attachment_filename", "attachment_size"},
    }),
    ("migrations/20261024_quick_reply_locations.sql", {
        "support_quick_replies": {"location_latitude", "location_longitude", "location_name", "location_address"},
    }),
    ("migrations/20261023_agent_statistics.sql", {
        "support_statistics_metadata": {"activity_tracking_started_at"},
        "support_agent_heartbeats": {"staff_id", "session_id", "tab_id", "sequence_number", "last_seen_at", "active_until"},
        "support_agent_activity": {"staff_id", "activity_day", "started_at", "ended_at"},
        "support_ticket_messages": {"statistics_sent_at"},
    }),
)

MAX_QUICK_REPLIES_PER_STAFF = 200

# Confirmed receipts advance monotonically; playback is distinct from reading.
DELIVERY_STATUS_RANK = {"delivered": 1, "read": 2, "played": 3}


def _missing_support_schema_migrations(
    schema_columns: dict[str, set[str]],
) -> list[str]:
    missing: list[str] = []
    for migration, requirements in _SUPPORT_SCHEMA_MIGRATIONS:
        if any(
            not required_columns.issubset(schema_columns.get(table_name, set()))
            for table_name, required_columns in requirements.items()
        ):
            missing.append(migration)
    return missing



class SupportRepository(DeliveryRepositoryMixin, ReadSyncRepositoryMixin, ReceiptRepositoryMixin):
    @staticmethod
    async def _lock_available_whatsapp_line(conn: Any, line_id: str | None) -> None:
        """Serialize new/reopened conversations with number removal."""
        if not line_id:
            return
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT source, is_enabled, provider_present, removed_at FROM whatsapp_phone_numbers WHERE phone_number_id = %s FOR UPDATE",
                (str(line_id),),
            )
            row = await cur.fetchone()
            if row and row.get('removed_at'):
                raise SupportConflictError("This WhatsApp number was permanently removed. Its chat history is read-only.")
            if get_transport(str(line_id)) not in {"linked_device", "removed"}:
                return
            if get_transport(str(line_id)) == "removed" or not row or row.get("source") != "LINKED_DEVICE" or not row.get("is_enabled") or not row.get("provider_present"):
                raise SupportConflictError("This WhatsApp linked-device number is inactive or removed. Link and add it to the app before opening a conversation.")

    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def _validate_schema(self, pool: aiomysql.Pool) -> None:
        table_names = sorted(
            {
                table_name
                for _migration, requirements in _SUPPORT_SCHEMA_MIGRATIONS
                for table_name in requirements
            }
        )
        placeholders = ", ".join(["%s"] * len(table_names))
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"""
                    SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME IN ({placeholders})
                    """,
                    (settings.db_name, *table_names),
                )
                rows = await cur.fetchall()

        schema_columns: dict[str, set[str]] = {}
        for row in rows:
            table_name = str(row.get("table_name") or "")
            column_name = str(row.get("column_name") or "")
            if table_name and column_name:
                schema_columns.setdefault(table_name, set()).add(column_name)

        missing = _missing_support_schema_migrations(schema_columns)
        if missing:
            raise SupportSchemaError(missing)
        # Columns alone are insufficient after a partially applied migration.
        # Refuse startup unless both idempotency boundaries are actually unique.
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""SELECT INDEX_NAME AS index_name, COLUMN_NAME AS column_name,
                           NON_UNIQUE AS non_unique
                    FROM INFORMATION_SCHEMA.STATISTICS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='support_ticket_messages'
                    ORDER BY INDEX_NAME, SEQ_IN_INDEX""", (settings.db_name,))
                indexes = {}
                for row in await cur.fetchall():
                    if not row["non_unique"]:
                        indexes.setdefault(row["index_name"], []).append(row["column_name"])
        required = {
            "uq_support_transport_client": ["transport_client_id"],
            "uq_support_scoped_client": ["ticket_id", "sender_type", "sender_scope", "client_message_id"],
        }
        if any(indexes.get(name) != columns for name, columns in required.items()):
            raise SupportSchemaError(["migrations/20261021_outbound_delivery_ownership.sql"])
        if indexes.get('uq_support_delivery_replacement') != ['replacement_for_message_id']:
            raise SupportSchemaError(['migrations/20261022_uncertain_delivery_copies.sql'])


    async def connect(self) -> None:
        if self._pool is not None:
            return
        pool = await aiomysql.create_pool(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            db=settings.db_name,
            minsize=1,
            maxsize=10,
            autocommit=True,
            charset="utf8mb4",
        )
        try:
            await self._validate_schema(pool)
        except Exception:
            pool.close()
            await pool.wait_closed()
            raise
        self._pool = pool

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def _acquire(self):
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        return self._pool.acquire()

    async def get_ticket(self, ticket_id: int) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT t.*, n.display_name, n.removed_at AS business_number_removed_at
                    FROM support_tickets t
                    LEFT JOIN whatsapp_phone_numbers n
                      ON n.phone_number_id = t.business_phone_number_id
                    WHERE t.id = %s
                    LIMIT 1
                    """,
                    (ticket_id,),
                )
                row = await cur.fetchone()
                if row:
                    await attach_current_staff_names(cur, [row])
                    await attach_conversation_read_state(cur, [row])
                return row

    async def delete_history_ticket(self, ticket_id: int) -> dict[str, Any]:
        """Hard-delete one resolved/closed ticket and its support timeline.

        The row is locked and revalidated inside the same transaction so a
        concurrent lifecycle change cannot turn an active ticket into a delete.
        Child rows are removed explicitly rather than depending on undocumented
        foreign-key cascade behavior in existing deployments.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT id, ticket_reference, status, active_key
                        FROM support_tickets
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (ticket_id,),
                    )
                    ticket = await cur.fetchone()
                    if not ticket:
                        raise SupportNotFoundError("Ticket not found.")
                    if (
                        str(ticket.get("status") or "") not in {"RESOLVED", "CLOSED"}
                        or ticket.get("active_key") is not None
                    ):
                        raise SupportConflictError(
                            "Only resolved or closed history tickets can be deleted."
                        )

                    if await self._delete_ticket_rows(cur, ticket_id) != 1:
                        raise SupportRepositoryError("History ticket delete did not remove one ticket.")
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return ticket

    @staticmethod
    async def _delete_ticket_rows(cur: Any, ticket_id: int) -> int:
        """Delete one ticket/chat and its timeline rows; return deleted ticket rows.

        Child rows are removed explicitly rather than depending on undocumented
        foreign-key cascade behavior in existing deployments.
        """
        for statement in (
            "DELETE FROM support_ticket_events WHERE ticket_id = %s",
            "DELETE FROM support_message_reactions WHERE ticket_id = %s",
            "DELETE FROM support_message_user_deletions WHERE message_id IN (SELECT id FROM support_ticket_messages WHERE ticket_id = %s)",
            "DELETE FROM support_message_change_events WHERE message_id IN (SELECT id FROM support_ticket_messages WHERE ticket_id = %s)",
            "DELETE FROM support_message_change_operations WHERE message_id IN (SELECT id FROM support_ticket_messages WHERE ticket_id = %s)",
            "DELETE FROM support_ticket_messages WHERE ticket_id = %s",
        ):
            await cur.execute(statement, (ticket_id,))
        await cur.execute("DELETE FROM support_tickets WHERE id = %s", (ticket_id,))
        return int(cur.rowcount or 0)

    async def delete_staff_chat(self, chat_id: int) -> dict[str, Any]:
        """Hard-delete one closed staff-initiated chat and its timeline.

        Like history tickets, the row is locked and revalidated in the same
        transaction so a chat reopened concurrently (by a new staff message) is
        never deleted while live.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT id, ticket_reference, status, active_key, conversation_origin
                        FROM support_tickets
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (chat_id,),
                    )
                    chat = await cur.fetchone()
                    if not chat or str(chat.get("conversation_origin") or "") != "STAFF":
                        raise SupportNotFoundError("Chat not found.")
                    if (
                        str(chat.get("status") or "") not in {"RESOLVED", "CLOSED"}
                        or chat.get("active_key") is not None
                    ):
                        raise SupportConflictError("Close this chat before deleting it.")
                    if await self._delete_ticket_rows(cur, chat_id) != 1:
                        raise SupportRepositoryError("Chat delete did not remove one chat.")
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return chat

    # Quick replies are private to one staff account. Every query is scoped by
    # staff_id, so another account's reply behaves exactly like a missing one.
    _QUICK_REPLY_COLUMNS = "id, title, body, created_at, updated_at, attachment_key, attachment_filename, attachment_size, location_latitude, location_longitude, location_name, location_address"

    async def list_quick_replies(self, staff_id: int) -> list[dict[str, Any]]:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"""
                    SELECT {self._QUICK_REPLY_COLUMNS}
                    FROM support_quick_replies
                    WHERE staff_id = %s
                    ORDER BY COALESCE(NULLIF(title, ''), body), id
                    """,
                    (int(staff_id),),
                )
                return list(await cur.fetchall())

    async def create_quick_reply(self, staff_id: int, *, title: str | None, body: str, attachment: dict | None = None, location: dict | None = None) -> dict[str, Any]:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    # Lock the staff row so concurrent saves cannot pass the limit together.
                    await cur.execute(
                        "SELECT id FROM staff WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                        (int(staff_id),),
                    )
                    if await cur.fetchone() is None:
                        raise SupportNotFoundError("Staff account not found.")
                    await cur.execute(
                        "SELECT COUNT(*) AS total FROM support_quick_replies WHERE staff_id = %s",
                        (int(staff_id),),
                    )
                    total = int(((await cur.fetchone()) or {}).get("total") or 0)
                    if total >= MAX_QUICK_REPLIES_PER_STAFF:
                        raise SupportConflictError(
                            f"You can save up to {MAX_QUICK_REPLIES_PER_STAFF} quick replies. Delete one to add another."
                        )
                    await cur.execute(
                        """
                        INSERT INTO support_quick_replies (staff_id, title, body, created_at, updated_at)
                        VALUES (%s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                        """,
                        (int(staff_id), title, body),
                    )
                    reply_id = int(cur.lastrowid)
                    if location:
                        await cur.execute("""UPDATE support_quick_replies
                            SET location_latitude=%s,location_longitude=%s,location_name=%s,location_address=%s
                            WHERE id=%s AND staff_id=%s""",
                            (location['latitude'], location['longitude'], location['name'], location['address'], reply_id, int(staff_id)))
                    if attachment:
                        await cur.execute("""UPDATE support_quick_replies
                            SET attachment_key=%s,attachment_filename=%s,attachment_size=%s
                            WHERE id=%s AND staff_id=%s""",
                            (attachment['attachment_key'],attachment['attachment_filename'],attachment['attachment_size'],reply_id,int(staff_id)))
                    await cur.execute(
                        f"SELECT {self._QUICK_REPLY_COLUMNS} FROM support_quick_replies WHERE id = %s AND staff_id = %s",
                        (reply_id, int(staff_id)),
                    )
                    row = await cur.fetchone()
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        if row is None:
            raise SupportRepositoryError("Quick reply was not saved.")
        return row

    async def update_quick_reply(
        self, staff_id: int, reply_id: int, *, title: str | None, body: str, location: dict | None = None
    ) -> dict[str, Any]:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT attachment_key, location_latitude FROM support_quick_replies WHERE id=%s AND staff_id=%s FOR UPDATE", (reply_id,staff_id))
                    current = await cur.fetchone()
                    if current is None:
                        raise SupportNotFoundError("Quick reply not found.")
                    is_location = current.get('location_latitude') is not None
                    if is_location != (location is not None):
                        raise SupportConflictError("The quick reply type changed. Close the editor and open it again.")
                    if location:
                        await cur.execute("""UPDATE support_quick_replies
                            SET location_latitude=%s,location_longitude=%s,location_name=%s,location_address=%s
                            WHERE id=%s AND staff_id=%s""",
                            (location['latitude'], location['longitude'], location['name'], location['address'], reply_id, staff_id))
                    if current.get('attachment_key') and len(body) > 1024:
                        raise SupportConflictError("This reply now contains a PDF. Limit its caption to 1024 characters.")
                    if not current.get('attachment_key') and not is_location and not body.strip():
                        raise SupportConflictError("A text quick reply needs a message.")
                    await cur.execute("""UPDATE support_quick_replies
                        SET title = %s, body = %s, updated_at = UTC_TIMESTAMP(6)
                        WHERE id = %s AND staff_id = %s""", (title,body,reply_id,staff_id))
                    await cur.execute(f"SELECT {self._QUICK_REPLY_COLUMNS} FROM support_quick_replies WHERE id=%s AND staff_id=%s", (reply_id,staff_id))
                    row = await cur.fetchone()
                await conn.commit()
                return row
            except BaseException:
                await conn.rollback()
                raise

    async def get_quick_reply(self, staff_id: int, reply_id: int) -> dict[str, Any]:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(f"SELECT {self._QUICK_REPLY_COLUMNS} FROM support_quick_replies WHERE id=%s AND staff_id=%s",
                                  (reply_id, staff_id))
                row = await cur.fetchone()
        if row is None:
            raise SupportNotFoundError("Quick reply not found.")
        return row

    async def replace_quick_reply_pdf(
        self, staff_id: int, reply_id: int, *, title: str | None, body: str, attachment: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT attachment_key, location_latitude FROM support_quick_replies WHERE id=%s AND staff_id=%s FOR UPDATE", (reply_id,staff_id))
                    old = await cur.fetchone()
                    if old is None:
                        raise SupportNotFoundError("Quick reply not found.")
                    if old.get("location_latitude") is not None:
                        raise SupportConflictError("A location quick reply cannot be replaced with a PDF.")
                    await cur.execute("""UPDATE support_quick_replies SET title=%s,body=%s,
                        attachment_key=%s,attachment_filename=%s,attachment_size=%s,updated_at=UTC_TIMESTAMP(6)
                        WHERE id=%s AND staff_id=%s""",
                        (title,body,attachment['attachment_key'],attachment['attachment_filename'],attachment['attachment_size'],reply_id,staff_id))
                    await cur.execute(f"SELECT {self._QUICK_REPLY_COLUMNS} FROM support_quick_replies WHERE id=%s AND staff_id=%s", (reply_id,staff_id))
                    row = await cur.fetchone()
                await conn.commit()
                return row, old.get('attachment_key')
            except BaseException:
                await conn.rollback()
                raise

    async def delete_quick_reply(self, staff_id: int, reply_id: int) -> str | None:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT attachment_key FROM support_quick_replies WHERE id=%s AND staff_id=%s FOR UPDATE", (reply_id,staff_id))
                    row = await cur.fetchone()
                    if row is None:
                        raise SupportNotFoundError("Quick reply not found.")
                    await cur.execute("DELETE FROM support_quick_replies WHERE id = %s AND staff_id = %s", (reply_id,staff_id))
                    if cur.rowcount != 1:
                        raise SupportNotFoundError("Quick reply not found.")
                await conn.commit()
                return row.get('attachment_key')
            except BaseException:
                await conn.rollback()
                raise

    async def quick_reply_recipients(self) -> list[dict[str, Any]]:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("""SELECT id, name, role FROM staff
                    WHERE is_active=1 AND deleted_at IS NULL ORDER BY id""")
                return list(await cur.fetchall())

    async def create_quick_reply_copies(
        self, actor_id: int, recipients: list[int], *, title: str | None, body: str,
        location: dict | None = None, attachments: dict[int, dict] | None = None,
    ) -> list[dict[str, Any]]:
        """All recipients succeed together; use the same staff lock as saves/deletion."""
        if self._pool is None:
            await self.connect()
        recipients = sorted(set(recipients))
        if not recipients:
            raise SupportConflictError("Select at least one active staff member.")
        lock_ids = sorted(set(recipients + [actor_id]))
        rows = []
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    placeholders = ','.join(['%s'] * len(lock_ids))
                    await cur.execute(f"""SELECT id, role, is_active, deleted_at FROM staff
                        WHERE id IN ({placeholders}) ORDER BY id FOR UPDATE""", lock_ids)
                    staff = {int(row['id']): row for row in await cur.fetchall()}
                    actor = staff.get(actor_id) or {}
                    if actor.get('role') not in {'admin', 'supervisor'} or not actor.get('is_active') or actor.get('deleted_at'):
                        raise SupportConflictError("Your account permissions changed. Sign in again before assigning quick replies.")
                    for staff_id in recipients:
                        target = staff.get(staff_id) or {}
                        if not target.get('is_active') or target.get('deleted_at'):
                            raise SupportConflictError("A selected staff account is no longer active. Refresh the staff selection.")
                        await cur.execute("SELECT COUNT(*) AS total FROM support_quick_replies WHERE staff_id=%s", (staff_id,))
                        if int((await cur.fetchone())['total']) >= MAX_QUICK_REPLIES_PER_STAFF:
                            raise SupportConflictError(f"Staff account {staff_id} has reached the {MAX_QUICK_REPLIES_PER_STAFF}-reply limit. No copies were added.")
                    for staff_id in recipients:
                        pdf = (attachments or {}).get(staff_id, {})
                        pin = location or {}
                        await cur.execute("""INSERT INTO support_quick_replies
                            (staff_id,title,body,attachment_key,attachment_filename,attachment_size,
                             location_latitude,location_longitude,location_name,location_address,created_at,updated_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
                            (staff_id,title,body,pdf.get('attachment_key'),pdf.get('attachment_filename'),pdf.get('attachment_size'),
                             pin.get('latitude'),pin.get('longitude'),pin.get('name'),pin.get('address')))
                        await cur.execute(f"SELECT staff_id,{self._QUICK_REPLY_COLUMNS} FROM support_quick_replies WHERE id=%s AND staff_id=%s",
                                          (cur.lastrowid,staff_id))
                        rows.append(await cur.fetchone())
                await conn.commit()
                return rows
            except BaseException:
                await conn.rollback()
                raise

    async def agent_has_participated(
        self,
        ticket_id: int,
        agent_id: str,
    ) -> bool:
        """Return whether an agent has ever been an owner/participant on a ticket.

        Participation is intentionally derived from durable ownership audit events so
        privacy checks keep working after a ticket is transferred or resolved. The
        current assignee check also covers legacy tickets whose original claim event
        may predate the audit trail.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT 1 AS participated
                    FROM support_tickets ticket
                    WHERE ticket.id = %s
                      AND (
                            ticket.current_assignee_id = %s
                            OR (
                                ticket.conversation_origin = 'STAFF'
                                AND ticket.initiated_by_staff_id = %s
                            )
                            OR EXISTS (
                                SELECT 1
                                FROM support_ticket_events participation_event
                                WHERE participation_event.ticket_id = ticket.id
                                  AND (
                                        (
                                            participation_event.event_type = 'TICKET_CLAIMED'
                                            AND participation_event.actor_id = %s
                                        )
                                        OR (
                                            participation_event.event_type = 'TICKET_TRANSFERRED'
                                            AND (
                                                JSON_UNQUOTE(JSON_EXTRACT(
                                                    participation_event.details_json,
                                                    '$.from_agent_id'
                                                )) = %s
                                                OR JSON_UNQUOTE(JSON_EXTRACT(
                                                    participation_event.details_json,
                                                    '$.to_agent_id'
                                                )) = %s
                                            )
                                        )
                                      )
                            )
                          )
                    LIMIT 1
                    """,
                    (ticket_id, agent_id, agent_id, agent_id, agent_id, agent_id),
                )
                return await cur.fetchone() is not None

    async def get_ticket_by_source_message(
        self,
        whatsapp_message_id: str,
    ) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT t.*, n.display_name
                    FROM support_tickets t
                    LEFT JOIN whatsapp_phone_numbers n
                      ON n.phone_number_id = t.business_phone_number_id
                    WHERE t.source_whatsapp_message_id = %s
                    LIMIT 1
                    """,
                    (whatsapp_message_id,),
                )
                row = await cur.fetchone()
                if row:
                    await attach_current_staff_names(cur, [row])
                return row

    async def find_active_ticket_by_phone(
        self,
        phone_number: str,
        business_phone_number_id: str | None = None,
    ) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                if business_phone_number_id:
                    await cur.execute(
                        """
                        SELECT t.*, n.display_name
                        FROM support_tickets t
                        LEFT JOIN whatsapp_phone_numbers n
                          ON n.phone_number_id = t.business_phone_number_id
                        WHERE t.whatsapp_phone = %s
                          AND t.active_key IS NOT NULL
                          AND t.status IN (%s, %s, %s, %s, %s)
                          AND (
                                t.business_phone_number_id = %s
                                OR (
                                    t.business_phone_number_id IS NULL
                                    AND t.active_key = %s
                                )
                              )
                        ORDER BY t.id DESC
                        LIMIT 1
                        """,
                        (
                            phone_number,
                            *ACTIVE_TICKET_STATUSES,
                            business_phone_number_id,
                            phone_number,
                        ),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT t.*, n.display_name
                        FROM support_tickets t
                        LEFT JOIN whatsapp_phone_numbers n
                          ON n.phone_number_id = t.business_phone_number_id
                        WHERE t.whatsapp_phone = %s
                          AND t.active_key IS NOT NULL
                          AND t.status IN (%s, %s, %s, %s, %s)
                        ORDER BY t.id DESC
                        LIMIT 1
                        """,
                        (phone_number, *ACTIVE_TICKET_STATUSES),
                    )
                row = await cur.fetchone()
                if row:
                    await attach_current_staff_names(cur, [row])
                return row

    async def find_ticket_at_whatsapp_timestamp(
        self, phone_number: str, business_phone_number_id: str,
        sent_at: datetime,
    ) -> dict[str, Any] | None:
        """Find the conversation open when a delayed provider message was sent.

        Reopened chats and customer tickets can have multiple active intervals;
        lifecycle events identify which conversation owned a delayed message.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        # WhatsApp normal messages carry whole seconds; include a ticket that
        # opened later within that same provider second.
        start_cutoff = sent_at + timedelta(seconds=1)
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM support_tickets
                    WHERE whatsapp_phone = %s AND business_phone_number_id = %s
                      AND created_at < %s
                    ORDER BY id DESC
                    """,
                    (phone_number, business_phone_number_id, start_cutoff),
                )
                for ticket in await cur.fetchall():
                    if str(ticket.get("conversation_origin") or "CUSTOMER") == "STAFF":
                        await cur.execute(
                            """
                            SELECT event_type FROM support_ticket_events
                            WHERE ticket_id = %s
                              AND event_type IN ('CHAT_STARTED', 'CHAT_REOPENED', 'CHAT_CLOSED')
                              AND created_at < %s
                            ORDER BY created_at DESC, id DESC LIMIT 1
                            """,
                            (ticket["id"], start_cutoff),
                        )
                        lifecycle = await cur.fetchone()
                        if lifecycle and lifecycle.get("event_type") in {"CHAT_STARTED", "CHAT_REOPENED"}:
                            return ticket
                    else:
                        await cur.execute(
                            """SELECT event_type FROM support_ticket_events
                               WHERE ticket_id=%s AND (event_type IN ('TICKET_REOPENED', 'TICKET_RESOLVED') OR
                                   (event_type='STATUS_CHANGED' AND JSON_VALID(details_json)
                                    AND JSON_UNQUOTE(JSON_EXTRACT(details_json, '$.to'))='CLOSED'))
                                 AND created_at < %s
                               ORDER BY created_at DESC, id DESC LIMIT 1""",
                            (ticket['id'], start_cutoff),
                        )
                        lifecycle = await cur.fetchone()
                        if lifecycle and lifecycle['event_type'] == 'TICKET_REOPENED':
                            return ticket
                        if lifecycle and lifecycle['event_type'] in {'TICKET_RESOLVED', 'STATUS_CHANGED'}:
                            continue
                        resolved_at = ticket.get("resolved_at")
                        closed_at = ticket.get("closed_at")
                        if (resolved_at is None or resolved_at >= sent_at) and (closed_at is None or closed_at >= sent_at):
                            return ticket
        return None

    async def list_previous_customer_tickets(
        self,
        *,
        current_ticket_id: int,
        phone_number: str,
        before_ticket_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return closed customer conversations in stable descending ID order.

        Business-line checks are applied by the service before any row is
        exposed. Include rows newer than the current ID: an older staff chat
        may have been reopened after an intervening ticket was resolved.
        """
        limit = max(1, min(int(limit), 100))
        phone_digits = re.sub(r"\D+", "", str(phone_number or ""))
        if phone_digits.startswith("00"):
            phone_digits = phone_digits[2:]
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT t.*, n.display_name
                    FROM support_tickets t
                    LEFT JOIN whatsapp_phone_numbers n
                      ON n.phone_number_id = t.business_phone_number_id
                    WHERE t.id <> %s
                      AND (%s IS NULL OR t.id < %s)
                      AND (
                            t.whatsapp_phone = %s
                            OR REGEXP_REPLACE(t.whatsapp_phone, '[^0-9]', '') IN (%s, %s)
                          )
                      AND t.active_key IS NULL
                      AND t.status IN ('RESOLVED', 'CLOSED')
                    ORDER BY t.id DESC
                    LIMIT %s
                    """,
                    (current_ticket_id, before_ticket_id, before_ticket_id,
                     phone_number, phone_digits, f"00{phone_digits}", limit),
                )
                rows = list(await cur.fetchall())
                return await attach_current_staff_names(cur, rows)

    async def bind_ticket_channel(
        self,
        ticket_id: int,
        *,
        phone_number: str,
        business_phone_number_id: str,
        business_display_phone_number: str | None,
    ) -> dict[str, Any]:
        """Attach Meta receiving-number metadata to a legacy active ticket once."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET business_phone_number_id = %s,
                            business_display_phone_number = COALESCE(%s, business_display_phone_number),
                            active_key = CASE
                                WHEN active_key = %s THEN %s
                                ELSE active_key
                            END,
                            updated_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                          AND active_key IS NOT NULL
                          AND business_phone_number_id IS NULL
                        """,
                        (
                            business_phone_number_id,
                            business_display_phone_number,
                            phone_number,
                            _ticket_active_key(phone_number, business_phone_number_id),
                            ticket_id,
                        ),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found after channel binding.")
        return ticket

    async def create_ticket(
        self,
        *,
        ticket_reference: str,
        phone_number: str,
        business_phone_number_id: str | None,
        business_display_phone_number: str | None,
        customer_id: str | None,
        customer_userid: str | None,
        customer_name: str | None,
        reason: str,
        ai_summary: str,
        tracking_reference: str | None,
        requested_action: str | None,
        current_language: str | None,
        communication_style: str | None,
        context_json: str,
        source_whatsapp_message_id: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """Create once, or return an existing equivalent ticket.

        active_key has a UNIQUE index, so MariaDB is the final concurrency
        guard against two active tickets for the same WhatsApp conversation.
        source_whatsapp_message_id is also unique when present.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None

        if source_whatsapp_message_id:
            existing = await self.get_ticket_by_source_message(
                source_whatsapp_message_id
            )
            if existing:
                return existing, False

        existing = await self.find_active_ticket_by_phone(
            phone_number,
            business_phone_number_id,
        )
        if existing:
            return existing, False

        active_key = _ticket_active_key(phone_number, business_phone_number_id)

        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                await self._lock_available_whatsapp_line(conn, business_phone_number_id)
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO support_tickets (
                            ticket_reference,
                            active_key,
                            whatsapp_phone,
                            business_phone_number_id,
                            business_display_phone_number,
                            customer_id,
                            customer_userid,
                            customer_name,
                            reason,
                            ai_summary,
                            tracking_reference,
                            requested_action,
                            current_language,
                            communication_style,
                            context_json,
                            source_whatsapp_message_id,
                            status,
                            created_at,
                            updated_at,
                            last_activity_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'NEW', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                        )
                        """,
                        (
                            ticket_reference,
                            active_key,
                            phone_number,
                            business_phone_number_id,
                            business_display_phone_number,
                            customer_id,
                            customer_userid,
                            customer_name,
                            reason,
                            ai_summary,
                            tracking_reference,
                            requested_action,
                            current_language,
                            communication_style,
                            context_json,
                            source_whatsapp_message_id,
                        ),
                    )
                    ticket_id = cur.lastrowid
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, details_json, created_at
                        ) VALUES (%s, 'TICKET_CREATED', 'SYSTEM', %s, UTC_TIMESTAMP(6))
                        """,
                        (
                            ticket_id,
                            json.dumps(
                                {
                                    "reason": reason,
                                    "source_whatsapp_message_id": source_whatsapp_message_id,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                await conn.commit()
            except aiomysql.IntegrityError:
                await conn.rollback()
                if source_whatsapp_message_id:
                    existing = await self.get_ticket_by_source_message(
                        source_whatsapp_message_id
                    )
                    if existing:
                        return existing, False
                existing = await self.find_active_ticket_by_phone(
                    phone_number,
                    business_phone_number_id,
                )
                if existing:
                    return existing, False
                raise
            except Exception:
                await conn.rollback()
                raise

        created = await self.get_ticket(int(ticket_id))
        if not created:
            raise SupportRepositoryError("Created support ticket could not be reloaded.")
        return created, True

    async def find_latest_staff_chat_by_phone(
        self,
        phone_number: str,
        business_phone_number_id: str,
    ) -> dict[str, Any] | None:
        """Return the latest staff-origin chat thread for one customer/line.

        Closed staff chats remain durable so a later staff-initiated contact can
        reopen the same thread instead of creating a second conversation.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT t.*, n.display_name
                    FROM support_tickets t
                    LEFT JOIN whatsapp_phone_numbers n
                      ON n.phone_number_id = t.business_phone_number_id
                    WHERE t.conversation_origin = 'STAFF'
                      AND t.whatsapp_phone = %s
                      AND t.business_phone_number_id = %s
                    ORDER BY t.id DESC
                    LIMIT 1
                    """,
                    (phone_number, business_phone_number_id),
                )
                return await cur.fetchone()

    async def reopen_staff_chat(
        self,
        chat_id: int,
        *,
        agent_id: str,
        agent_name: str,
    ) -> tuple[dict[str, Any], bool]:
        """Reopen a closed staff chat in-place, preserving its full timeline."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            try:
                # Determine the immutable channel without taking the ticket
                # lock or establishing a transaction snapshot. Removal/create
                # paths then lock number before ticket and see current state.
                async with conn.cursor(aiomysql.DictCursor) as channel_cur:
                    await channel_cur.execute("SELECT business_phone_number_id FROM support_tickets WHERE id = %s", (chat_id,))
                    channel = await channel_cur.fetchone()
                line_id = str((channel or {}).get("business_phone_number_id") or "")
                await conn.begin()
                await self._lock_available_whatsapp_line(conn, line_id)
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT * FROM support_tickets WHERE id = %s FOR UPDATE",
                        (chat_id,),
                    )
                    row = await cur.fetchone()
                    if not row or str(row.get("conversation_origin") or "") != "STAFF":
                        raise SupportNotFoundError("Chat not found.")
                    if str(row.get("business_phone_number_id") or "") != line_id:
                        raise SupportConflictError("This chat's WhatsApp number changed. Refresh before reopening it.")
                    if row.get("active_key") is not None and row.get("status") in ACTIVE_TICKET_STATUSES:
                        await conn.rollback()
                        chat = await self.get_ticket(chat_id)
                        assert chat is not None
                        return chat, False

                    active_key = _ticket_active_key(
                        str(row.get("whatsapp_phone") or ""),
                        str(row.get("business_phone_number_id") or ""),
                    )
                    await cur.execute(
                        "SELECT id, conversation_origin FROM support_tickets WHERE active_key = %s AND id <> %s LIMIT 1 FOR UPDATE",
                        (active_key, chat_id),
                    )
                    conflict = await cur.fetchone()
                    if conflict:
                        raise SupportConflictError(
                            "This customer already has an active conversation on that WhatsApp line."
                        )

                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET active_key = %s,
                            status = 'ASSIGNED',
                            current_assignee_id = %s,
                            current_assignee_name = %s,
                            assigned_at = UTC_TIMESTAMP(6),
                            resolved_at = NULL,
                            resolved_by_staff_id = NULL,
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (active_key, agent_id, agent_name, chat_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, actor_id, actor_name, details_json, created_at
                        ) VALUES (%s, 'CHAT_REOPENED', 'AGENT', %s, %s, '{}', UTC_TIMESTAMP(6))
                        """,
                        (chat_id, agent_id, agent_name),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        chat = await self.get_ticket(chat_id)
        if not chat:
            raise SupportRepositoryError("Reopened chat could not be reloaded.")
        return chat, True

    async def close_staff_chat(
        self,
        chat_id: int,
        *,
        agent_id: str,
        agent_name: str,
        expected_assignee_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Close a staff-origin chat without applying ticket-resolution semantics."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT * FROM support_tickets WHERE id = %s FOR UPDATE",
                        (chat_id,),
                    )
                    row = await cur.fetchone()
                    if not row or str(row.get("conversation_origin") or "") != "STAFF":
                        raise SupportNotFoundError("Chat not found.")
                    if expected_assignee_id is not None and str(row.get("current_assignee_id") or "") != str(expected_assignee_id):
                        raise SupportConflictError("Chat ownership changed. Refresh before closing it.")
                    if row.get("active_key") is None:
                        await conn.rollback()
                        chat = await self.get_ticket(chat_id)
                        assert chat is not None
                        return chat, False

                    if not await self._fence_deliveries_for_lifecycle(cur, chat_id):
                        raise SupportConflictError(
                            "An outbound WhatsApp message is still being sent. Refresh and close the chat after it finishes."
                        )

                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET status = 'RESOLVED',
                            active_key = NULL,
                            resolved_at = UTC_TIMESTAMP(6),
                            resolved_by_staff_id = %s,
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (int(agent_id), chat_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, actor_id, actor_name, details_json, created_at
                        ) VALUES (%s, 'CHAT_CLOSED', 'AGENT', %s, %s, '{}', UTC_TIMESTAMP(6))
                        """,
                        (chat_id, agent_id, agent_name),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        chat = await self.get_ticket(chat_id)
        if not chat:
            raise SupportNotFoundError("Chat not found after close.")
        return chat, True

    async def create_staff_chat(
        self,
        *,
        ticket_reference: str,
        phone_number: str,
        business_phone_number_id: str,
        business_display_phone_number: str | None,
        customer_id: str | None,
        customer_userid: str | None,
        customer_name: str | None,
        agent_id: str,
        agent_name: str,
    ) -> dict[str, Any]:
        """Create one active staff-origin conversation already assigned to its starter."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        existing = await self.find_active_ticket_by_phone(phone_number, business_phone_number_id)
        if existing:
            raise SupportConflictError("This customer already has an active conversation on that WhatsApp line.")
        active_key = _ticket_active_key(phone_number, business_phone_number_id)
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                await self._lock_available_whatsapp_line(conn, business_phone_number_id)
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO support_tickets (
                            ticket_reference, active_key, whatsapp_phone,
                            business_phone_number_id, business_display_phone_number,
                            conversation_origin, initiated_by_staff_id, initiated_by_staff_name,
                            customer_id, customer_userid, customer_name,
                            reason, ai_summary, context_json, status,
                            current_assignee_id, current_assignee_name, assigned_at,
                            created_at, updated_at, last_activity_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, 'STAFF', %s, %s, %s, %s, %s,
                            'STAFF_INITIATED_CHAT', '', '{}', 'ASSIGNED', %s, %s, UTC_TIMESTAMP(6),
                            UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                        )
                        """,
                        (ticket_reference, active_key, phone_number, business_phone_number_id,
                         business_display_phone_number, agent_id, agent_name, customer_id,
                         customer_userid, customer_name, agent_id, agent_name),
                    )
                    ticket_id = int(cur.lastrowid)
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, actor_id, actor_name, details_json, created_at
                        ) VALUES (%s, 'CHAT_STARTED', 'AGENT', %s, %s, '{}', UTC_TIMESTAMP(6))
                        """,
                        (ticket_id, agent_id, agent_name),
                    )
                await conn.commit()
            except aiomysql.IntegrityError as exc:
                await conn.rollback()
                existing = await self.find_active_ticket_by_phone(phone_number, business_phone_number_id)
                if existing:
                    raise SupportConflictError("This customer already has an active conversation on that WhatsApp line.") from exc
                raise
            except Exception:
                await conn.rollback()
                raise
        created = await self.get_ticket(ticket_id)
        if not created:
            raise SupportRepositoryError("Created staff chat could not be reloaded.")
        return created

    async def update_customer_identity(
        self,
        ticket_id: int,
        *,
        customer_name: str | None,
        customer_id: str | None,
        customer_userid: str | None,
    ) -> dict[str, Any]:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE support_tickets
                    SET customer_name = COALESCE(%s, customer_name),
                        customer_id = COALESCE(%s, customer_id),
                        customer_userid = COALESCE(%s, customer_userid),
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s
                    """,
                    (customer_name, customer_id, customer_userid, ticket_id),
                )
        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found after customer identity update.")
        return ticket

    async def update_customer_name_for_phone(
        self, phone_number: str, customer_name: str
    ) -> None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE support_tickets
                    SET customer_name = %s, updated_at = UTC_TIMESTAMP(6)
                    WHERE whatsapp_phone = %s
                    """,
                    (customer_name, phone_number),
                )

    async def upgrade_shared_contact(self, message_id: int, *, ticket_id: int, body: str) -> tuple[dict[str, Any], bool]:
        """Replace an old unsupported marker without changing identity or reviving a revoke."""
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE support_ticket_messages SET body=%s, message_type='contacts',
                        version=version+1, updated_at=UTC_TIMESTAMP(6)
                    WHERE id=%s AND ticket_id=%s AND message_type='unsupported'
                      AND deleted_for_everyone_at IS NULL
                """, (body, message_id, ticket_id))
                changed = cur.rowcount == 1
        message = await self.get_message(message_id)
        if not message or int(message['ticket_id']) != ticket_id:
            raise SupportNotFoundError("Message not found.")
        return message, changed

    async def add_ticket_message(
        self,
        *,
        ticket_id: int,
        sender_type: str,
        direction: str,
        body: str,
        sender_id: str | None = None,
        sender_name: str | None = None,
        whatsapp_message_id: str | None = None,
        client_message_id: str | None = None,
        whatsapp_timestamp: str | None = None,
        message_type: str = "text",
        reply_to_whatsapp_message_id: str | None = None,
        location_latitude: float | None = None,
        location_longitude: float | None = None,
        location_name: str | None = None,
        location_address: str | None = None,
        send_status: str | None = None,
        error_message: str | None = None,
        is_forwarded: bool = False,
        forwarded_from_ticket_id: int | None = None,
        forwarded_from_message_id: int | None = None,
        require_active: bool = False,
        require_assignee_id: str | None = None,
        expected_phone: str | None = None,
        expected_line_id: str | None = None,
        current_language: str | None = None,
        communication_style: str | None = None,
        replacement_for_message_id: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None

        if whatsapp_message_id:
            existing = await self.get_message_by_whatsapp_id(whatsapp_message_id)
            if existing:
                return existing, False
        if client_message_id:
            existing = await self.get_message_by_client_id(client_message_id, ticket_id=ticket_id, sender_type=sender_type, sender_id=sender_id)
            if existing:
                return existing, False

        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    # Lock the parent before insert/FK checks to avoid upgrade deadlocks.
                    await cur.execute(
                        """
                        SELECT id, status, active_key, current_assignee_id,
                               whatsapp_phone, business_phone_number_id
                        FROM support_tickets
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (ticket_id,),
                    )
                    ticket_state = await cur.fetchone()
                    if not ticket_state:
                        raise SupportNotFoundError("Ticket not found.")
                    if require_active and (
                        ticket_state.get("active_key") is None
                        or ticket_state.get("status") not in ACTIVE_TICKET_STATUSES
                    ):
                        raise SupportConflictError("Ticket is no longer active.")
                    if require_assignee_id is not None and str(
                        ticket_state.get("current_assignee_id") or ""
                    ) != str(require_assignee_id):
                        raise SupportConflictError(
                            "Ticket ownership changed. Refresh before sending a message."
                        )
                    if expected_phone is not None and str(ticket_state.get("whatsapp_phone") or "") != expected_phone:
                        raise SupportConflictError("The WhatsApp counterpart changed before this message was stored.")
                    if expected_line_id is not None and str(ticket_state.get("business_phone_number_id") or "") != expected_line_id:
                        raise SupportConflictError("The WhatsApp number changed before this message was stored.")
                    if replacement_for_message_id is not None:
                        # Ticket -> original message -> replacement. Serialize
                        # against lifecycle changes and late confirmations.
                        await cur.execute('SELECT * FROM support_ticket_messages WHERE id=%s AND ticket_id=%s FOR UPDATE',
                                          (replacement_for_message_id, ticket_id))
                        original = await cur.fetchone()
                        if not original or original['direction'] != 'OUTBOUND' or original.get('deleted_for_everyone_at'):
                            raise SupportNotFoundError('Original message not found.')
                        await cur.execute('SELECT * FROM support_ticket_messages WHERE replacement_for_message_id=%s',
                                          (replacement_for_message_id,))
                        existing_copy = await cur.fetchone()
                        if existing_copy:
                            await conn.commit()
                            return existing_copy, False
                        if original['send_status'] != 'UNCERTAIN':
                            raise SupportConflictError('The original delivery is no longer uncertain. Refresh before sending.')
                        if not require_active or require_assignee_id != sender_id or sender_type != 'AGENT':
                            raise SupportConflictError('A new copy requires the assigned agent and an active conversation.')
                    if client_message_id:
                        await cur.execute("""SELECT * FROM support_ticket_messages
                            WHERE ticket_id=%s AND sender_type=%s AND sender_scope=%s AND client_message_id=%s""",
                            (ticket_id, sender_type, str(sender_id or ""), client_message_id))
                        existing = await cur.fetchone()
                        if existing:
                            await conn.commit()
                            return existing, False

                    await cur.execute(
                        """
                        INSERT INTO support_ticket_messages (
                            ticket_id,
                            sender_type,
                            sender_id,
                            sender_name,
                            direction,
                            message_type,
                            body,
                            reply_to_whatsapp_message_id,
                            location_latitude,
                            location_longitude,
                            location_name,
                            location_address,
                            whatsapp_message_id,
                            client_message_id,
                            transport_client_id,
                            whatsapp_timestamp,
                            send_status,
                            error_message,
                            is_forwarded,
                            forwarded_from_ticket_id,
                            forwarded_from_message_id,
                            created_at,
                            updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                        )
                        """,
                        (
                            ticket_id,
                            sender_type,
                            sender_id,
                            sender_name,
                            direction,
                            message_type,
                            body,
                            reply_to_whatsapp_message_id,
                            location_latitude,
                            location_longitude,
                            location_name,
                            location_address,
                            whatsapp_message_id,
                            client_message_id,
                            uuid.uuid4().hex if client_message_id else None,
                            whatsapp_timestamp,
                            send_status,
                            error_message,
                            1 if is_forwarded else 0,
                            forwarded_from_ticket_id,
                            forwarded_from_message_id,
                        ),
                    )
                    message_id = cur.lastrowid
                    if replacement_for_message_id is not None:
                        await cur.execute('UPDATE support_ticket_messages SET replacement_for_message_id=%s WHERE id=%s',
                                          (replacement_for_message_id, message_id))
                        await cur.execute("""INSERT INTO support_ticket_events
                            (ticket_id,event_type,actor_type,actor_id,actor_name,details_json,created_at)
                            VALUES (%s,'UNCERTAIN_MESSAGE_COPY','AGENT',%s,%s,%s,UTC_TIMESTAMP(6))""",
                            (ticket_id, sender_id, sender_name, json.dumps({'original_message_id': replacement_for_message_id,
                                                                         'message_id': message_id, 'duplicate_risk_accepted': True})))
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET last_activity_at = UTC_TIMESTAMP(6),
                            updated_at = UTC_TIMESTAMP(6),
                            current_language = COALESCE(%s, current_language),
                            communication_style = COALESCE(%s, communication_style)
                        WHERE id = %s
                        """,
                        (current_language, communication_style, ticket_id),
                    )
                await conn.commit()
            except aiomysql.IntegrityError:
                await conn.rollback()
                # Reuse this connection; acquiring a second one while all racing
                # inserts hold pool slots can deadlock a bounded pool.
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("""SELECT * FROM support_ticket_messages
                        WHERE (%s IS NOT NULL AND whatsapp_message_id=%s)
                           OR (ticket_id=%s AND sender_type=%s AND sender_scope=%s AND client_message_id=%s)
                        LIMIT 1""", (whatsapp_message_id, whatsapp_message_id, ticket_id, sender_type,
                                    str(sender_id or ""), client_message_id))
                    existing = await cur.fetchone()
                    if existing:
                        return existing, False
                raise
            except Exception:
                await conn.rollback()
                raise

        message = await self.get_message(int(message_id))
        if not message:
            raise SupportRepositoryError("Created ticket message could not be reloaded.")
        return message, True

    async def get_message(self, message_id: int) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM support_ticket_messages WHERE id = %s LIMIT 1",
                    (message_id,),
                )
                row = await cur.fetchone()
                if row:
                    await attach_current_staff_names(cur, [row])
                return row

    async def change_message(
        self, message_id: int, *, ticket_id: int, user_id: str,
        action: str, content: str | None = None, version: int | None = None,
    ) -> dict[str, Any]:
        """Hide for one staff user; global changes require remote confirmation."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT * FROM support_ticket_messages WHERE id = %s AND ticket_id = %s FOR UPDATE",
                        (message_id, ticket_id),
                    )
                    row = await cur.fetchone()
                    if not row:
                        raise SupportNotFoundError("Message not found.")
                    if action == "me":
                        await cur.execute(
                            "INSERT IGNORE INTO support_message_user_deletions (message_id, user_id) VALUES (%s, %s)",
                            (message_id, user_id),
                        )
                    else:
                        # A bare repository write must never simulate a change in
                        # the customer's chat. Global mutations use the durable
                        # claim/confirmation flow below.
                        raise SupportConflictError("Remote WhatsApp confirmation is required before changing a message.")
                    await cur.execute("SELECT * FROM support_ticket_messages WHERE id = %s", (message_id,))
                    result = await cur.fetchone()
                await conn.commit()
                return result
            except Exception:
                await conn.rollback()
                raise

    async def claim_message_change(
        self, message_id: int, *, ticket_id: int, user_id: str,
        action: str, content: str | None, version: int, operation_id: str,
        window_open: bool = True,
    ) -> dict[str, Any]:
        """Reserve one durable remote request without keeping a DB lock over HTTP."""
        if action not in {"edit", "everyone"}:
            raise ValueError("Invalid message action")
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM support_ticket_messages WHERE id = %s AND ticket_id = %s FOR UPDATE", (message_id, ticket_id))
                    row = await cur.fetchone()
                    if not row:
                        raise SupportNotFoundError("Message not found.")
                    if str(row.get("sender_type") or "").upper() != "AGENT" or str(row.get("sender_id") or "") != str(user_id) or row.get("direction") != "OUTBOUND":
                        raise SupportConflictError("Only the original sender can change this message.")
                    await cur.execute("SELECT * FROM support_message_change_operations WHERE operation_id = %s FOR UPDATE", (operation_id,))
                    operation = await cur.fetchone()
                    if operation and (int(operation.get("message_id") or 0) != message_id or str(operation.get("user_id") or "") != str(user_id) or operation.get("action") != action or (action == "edit" and int(operation.get("base_version") or 0) != version) or operation.get("content") != content):
                        raise SupportConflictError("That message change request ID was already used for another change.")
                    if operation and operation.get("status") == "CONFIRMED":
                        result = {"operation_id": operation_id, "needs_remote": False, "message": row}
                    else:
                        if operation and operation.get("status") == "FAILED":
                            raise SupportConflictError("WhatsApp rejected this message change. Refresh before trying another change.")
                        if row.get("deleted_for_everyone_at"):
                            raise SupportConflictError("This message was already deleted for everyone.")
                        if row.get("send_status") != "SENT" or not row.get("whatsapp_message_id"):
                            raise SupportConflictError("Only messages confirmed as sent by WhatsApp can be changed.")
                        if action == "edit" and str(row.get("message_type") or "").lower() != "text":
                            raise SupportConflictError("Only sent text messages can be edited.")
                        pending = str(row.get("pending_change_operation_id") or "")
                        if pending and pending != operation_id:
                            raise SupportConflictError("Another message change is awaiting WhatsApp confirmation. Retry that change first.")
                        if not operation and version != int(row.get("version") or 1):
                            raise SupportConflictError("This message changed in another session. Refresh before saving.")
                        if not operation and not window_open:
                            if action == "edit":
                                raise SupportConflictError("WhatsApp allows editing messages only within 15 minutes of sending.")
                            raise SupportConflictError("WhatsApp allows deleting messages for everyone only within two days of sending.")
                        lease = operation.get("lease_until") if operation else None
                        now = datetime.now(timezone.utc).replace(tzinfo=None)
                        if isinstance(lease, datetime) and lease.replace(tzinfo=None) > now:
                            raise SupportConflictError("This message change is already awaiting WhatsApp confirmation.")
                        if not operation:
                            await cur.execute(
                                """INSERT INTO support_message_change_operations
                                   (operation_id, message_id, base_version, user_id, action, content, status, lease_until)
                                   VALUES (%s, %s, %s, %s, %s, %s, 'PENDING', DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 120 SECOND))""",
                                (operation_id, message_id, version, user_id, action, content),
                            )
                        else:
                            await cur.execute("UPDATE support_message_change_operations SET status = 'PENDING', lease_until = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 120 SECOND), updated_at = UTC_TIMESTAMP(6) WHERE operation_id = %s", (operation_id,))
                        await cur.execute("UPDATE support_ticket_messages SET pending_change_operation_id = %s WHERE id = %s", (operation_id, message_id))
                        result = {"operation_id": operation_id, "needs_remote": True, "message": row}
                await conn.commit()
                return result
            except Exception:
                await conn.rollback()
                raise

    async def record_message_change_failure(
        self, message_id: int, *, operation_id: str, definitive: bool,
    ) -> dict[str, Any] | None:
        """Keep uncertain requests retryable; a concurrently confirmed echo wins."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM support_ticket_messages WHERE id = %s FOR UPDATE", (message_id,))
                    row = await cur.fetchone()
                    await cur.execute("SELECT * FROM support_message_change_operations WHERE operation_id = %s FOR UPDATE", (operation_id,))
                    operation = await cur.fetchone()
                    if operation and operation.get("status") == "CONFIRMED":
                        result = row
                    else:
                        await cur.execute("UPDATE support_message_change_operations SET status = %s, lease_until = NULL, updated_at = UTC_TIMESTAMP(6) WHERE operation_id = %s AND status <> 'CONFIRMED'", ("FAILED" if definitive else "UNCERTAIN", operation_id))
                        if definitive:
                            await cur.execute("UPDATE support_ticket_messages SET pending_change_operation_id = NULL WHERE id = %s AND pending_change_operation_id = %s", (message_id, operation_id))
                        result = None
                await conn.commit()
                return result
            except Exception:
                await conn.rollback()
                raise

    async def _write_confirmed_message_change(
        self, cur: Any, row: dict[str, Any], *, action: str,
        content: str | None, timestamp: int, deleted_by: str | None,
    ) -> bool:
        """Apply provider order; a deleted message can never be resurrected."""
        if row.get("deleted_for_everyone_at"):
            return False
        if action == "edit" and (str(row.get("message_type") or "").lower() != "text" or timestamp <= int(row.get("whatsapp_change_timestamp") or 0)):
            return False
        changed_at = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).replace(tzinfo=None)
        if action == "everyone":
            await cur.execute(
                """UPDATE support_ticket_messages SET body = '', deleted_for_everyone_at = %s,
                   deleted_by = %s, provider_media_id = NULL, provider_media_mime_type = NULL,
                   provider_media_filename = NULL, location_latitude = NULL, location_longitude = NULL,
                   location_name = NULL, location_address = NULL, error_message = NULL,
                   whatsapp_change_timestamp = GREATEST(COALESCE(whatsapp_change_timestamp, 0), %s),
                   version = version + 1, updated_at = UTC_TIMESTAMP(6) WHERE id = %s""",
                (changed_at, deleted_by, timestamp, row["id"]),
            )
            await cur.execute("DELETE FROM support_message_reactions WHERE message_id = %s", (row["id"],))
            await cur.execute("UPDATE support_message_change_operations SET content = NULL WHERE message_id = %s", (row["id"],))
            return True
        changed = row.get("body") != content or not row.get("edited_at")
        await cur.execute(
            """UPDATE support_ticket_messages SET body = %s, edited_at = %s,
               whatsapp_change_timestamp = %s, version = version + %s,
               updated_at = UTC_TIMESTAMP(6) WHERE id = %s""",
            (content, changed_at, timestamp, 1 if changed else 0, row["id"]),
        )
        return changed

    async def confirm_message_change(
        self, message_id: int, *, operation_id: str, timestamp: int,
    ) -> dict[str, Any]:
        """Finalize an acknowledged request, preserving a newer callback's body."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM support_ticket_messages WHERE id = %s FOR UPDATE", (message_id,))
                    row = await cur.fetchone()
                    await cur.execute("SELECT * FROM support_message_change_operations WHERE operation_id = %s FOR UPDATE", (operation_id,))
                    operation = await cur.fetchone()
                    if not row or not operation or int(operation.get("message_id") or 0) != message_id:
                        raise SupportNotFoundError("The confirmed message change could not be found.")
                    if operation.get("status") != "CONFIRMED":
                        await self._write_confirmed_message_change(cur, row, action=operation["action"], content=operation.get("content"), timestamp=timestamp, deleted_by=operation.get("user_id"))
                        await cur.execute("UPDATE support_message_change_operations SET status = 'CONFIRMED', provider_timestamp = %s, lease_until = NULL, updated_at = UTC_TIMESTAMP(6) WHERE operation_id = %s", (timestamp, operation_id))
                    await cur.execute("UPDATE support_ticket_messages SET pending_change_operation_id = NULL WHERE id = %s AND pending_change_operation_id = %s", (message_id, operation_id))
                    await cur.execute("SELECT * FROM support_ticket_messages WHERE id = %s", (message_id,))
                    result = await cur.fetchone()
                await conn.commit()
                return result
            except Exception:
                await conn.rollback()
                raise

    async def apply_message_change_event(
        self, message_id: int, *, event_id: str, action: str,
        content: str | None, timestamp: int, operation_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Deduplicate trusted provider callbacks and reconcile their pending request."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM support_ticket_messages WHERE id = %s FOR UPDATE", (message_id,))
                    row = await cur.fetchone()
                    if not row:
                        raise SupportNotFoundError("Message not found.")
                    await cur.execute("INSERT IGNORE INTO support_message_change_events (event_id, message_id, provider_timestamp) VALUES (%s, %s, %s)", (event_id, message_id, timestamp))
                    changed = False
                    if cur.rowcount:
                        changed = await self._write_confirmed_message_change(cur, row, action=action, content=content, timestamp=timestamp, deleted_by=None)
                        pending = str(row.get("pending_change_operation_id") or "")
                        if pending and (not operation_id or operation_id == pending):
                            await cur.execute("SELECT * FROM support_message_change_operations WHERE operation_id = %s FOR UPDATE", (pending,))
                            operation = await cur.fetchone()
                            matches = operation and operation.get("action") == action and (action == "everyone" or operation.get("content") == content)
                            if matches and timestamp > int(row.get("whatsapp_change_timestamp") or 0):
                                await cur.execute("UPDATE support_message_change_operations SET status = 'CONFIRMED', provider_timestamp = %s, lease_until = NULL, updated_at = UTC_TIMESTAMP(6) WHERE operation_id = %s", (timestamp, pending))
                                await cur.execute("UPDATE support_ticket_messages SET pending_change_operation_id = NULL WHERE id = %s AND pending_change_operation_id = %s", (message_id, pending))
                    await cur.execute("SELECT * FROM support_ticket_messages WHERE id = %s", (message_id,))
                    result = await cur.fetchone()
                await conn.commit()
                return result, changed
            except Exception:
                await conn.rollback()
                raise

    async def message_hidden_for_user(self, message_id: int, user_id: str) -> bool:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM support_message_user_deletions WHERE message_id = %s AND user_id = %s LIMIT 1",
                    (message_id, user_id),
                )
                return bool(await cur.fetchone())

    async def set_message_media_reference(
        self,
        message_id: int,
        *,
        provider_media_id: str,
        provider_media_mime_type: str | None,
        provider_media_filename: str | None = None,
    ) -> None:
        """Persist the provider media locator for authenticated lazy recovery.

        This metadata is not exposed to the staff browser. Local protected media
        remains the fast path, while the provider ID lets another backend instance
        recover the attachment if the local cache is absent.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE support_ticket_messages
                    SET provider_media_id = %s,
                        provider_media_mime_type = %s,
                        provider_media_filename = %s,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s
                    """,
                    (provider_media_id, provider_media_mime_type, provider_media_filename, message_id),
                )

    async def get_message_by_whatsapp_id(self, message_id: str) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM support_ticket_messages
                    WHERE whatsapp_message_id = %s LIMIT 1
                    """,
                    (message_id,),
                )
                row = await cur.fetchone()
                if row:
                    await attach_current_staff_names(cur, [row])
                return row

    async def apply_delivery_status(
        self,
        whatsapp_message_id: str,
        *,
        status: str,
        at: datetime,
        business_phone_number_id: str | None,
        phone_number: str | None,
        failure_code: int | None = None,
    ) -> tuple[dict[str, Any], bool] | None:
        """Move an outgoing message forward to delivered or read; never backward."""
        if status not in {*DELIVERY_STATUS_RANK, "failed"}:
            raise ValueError("Unknown delivery status.")
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT m.*, t.business_phone_number_id AS ticket_line_id, t.whatsapp_phone AS ticket_phone
                        FROM support_ticket_messages m
                        JOIN support_tickets t ON t.id = m.ticket_id
                        WHERE m.whatsapp_message_id = %s AND m.direction = 'OUTBOUND'
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (whatsapp_message_id,),
                    )
                    row = await cur.fetchone()
                    if not row or (
                        business_phone_number_id
                        and str(row.get("ticket_line_id") or "") not in {"", business_phone_number_id}
                    ) or (phone_number and str(row.get("ticket_phone") or "") != phone_number):
                        await conn.rollback()
                        return None
                    current = DELIVERY_STATUS_RANK.get(str(row.get("delivery_status") or ""), 0)
                    changed = DELIVERY_STATUS_RANK.get(status, 0) > current
                    if status == 'failed' and current == 0:
                        error, retry_safe = failure_policy(failure_code)
                        # An old generic callback must not erase a known blocking error.
                        if failure_code is not None or not str(row.get('error_message') or '').startswith('WhatsApp 131047:'):
                            changed = row.get('send_status') != 'FAILED' or row.get('error_message') != error or bool(row.get('delivery_retry_safe')) != retry_safe
                            if changed:
                                await cur.execute("""UPDATE support_ticket_messages SET send_status='FAILED',
                                    delivery_retry_safe=%s, read_sync_pending=0,
                                    error_message=%s, updated_at=UTC_TIMESTAMP(6) WHERE id=%s""", (retry_safe, error, row['id']))
                    elif changed:
                        await cur.execute(
                            """
                            UPDATE support_ticket_messages
                            SET delivery_status = %s,
                                delivered_at = COALESCE(delivered_at, %s),
                                read_at = CASE WHEN %s IN ('read', 'played') THEN COALESCE(read_at, %s) ELSE read_at END,
                                -- MariaDB applies assignments left to right: clear the
                                -- error before send_status changes.
                                error_message = CASE WHEN send_status = 'SENT' THEN error_message ELSE NULL END,
                                read_sync_pending = IF(send_status <> 'SENT' AND sender_type='AGENT',1,read_sync_pending),
                                send_status = 'SENT'
                            WHERE id = %s
                            """,
                            (status, at, status, at, row["id"]),
                        )
                    await cur.execute("SELECT * FROM support_ticket_messages WHERE id = %s", (row["id"],))
                    updated = await cur.fetchone()
                await conn.commit()
                return updated, changed
            except Exception:
                await conn.rollback()
                raise

    async def get_message_by_client_id(
        self, client_message_id: str, *, ticket_id: int, sender_type: str, sender_id: str | None = None,
    ) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """SELECT * FROM support_ticket_messages
                    WHERE client_message_id = %s AND ticket_id = %s
                      AND sender_type = %s AND sender_scope = %s LIMIT 1""",
                    (client_message_id, ticket_id, sender_type, str(sender_id or "")),
                )
                row = await cur.fetchone()
                if row:
                    await attach_current_staff_names(cur, [row])
                return row

    async def get_reaction_by_client_id(
        self, client_reaction_id: str
    ) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM support_message_reactions
                    WHERE client_reaction_id = %s LIMIT 1
                    """,
                    (client_reaction_id,),
                )
                return await cur.fetchone()

    async def upsert_message_reaction(
        self,
        *,
        ticket_id: int,
        message_id: int,
        target_whatsapp_message_id: str,
        reactor_key: str,
        reactor_type: str,
        reactor_id: str | None,
        reactor_name: str | None,
        emoji: str,
        provider_reaction_id: str | None = None,
        client_reaction_id: str | None = None,
        provider_timestamp: int | None = None,
    ) -> dict[str, Any]:
        """Upsert one actor's current reaction, retaining removals as tombstones."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO support_message_reactions (
                            ticket_id, message_id, target_whatsapp_message_id,
                            reactor_key, reactor_type, reactor_id, reactor_name,
                            emoji, provider_reaction_id, client_reaction_id,
                            provider_timestamp, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                        )
                        ON DUPLICATE KEY UPDATE
                            reactor_type = IF(
                                VALUES(provider_timestamp) IS NULL
                                OR provider_timestamp IS NULL
                                OR VALUES(provider_timestamp) >= provider_timestamp,
                                VALUES(reactor_type), reactor_type
                            ),
                            reactor_id = IF(
                                VALUES(provider_timestamp) IS NULL
                                OR provider_timestamp IS NULL
                                OR VALUES(provider_timestamp) >= provider_timestamp,
                                VALUES(reactor_id), reactor_id
                            ),
                            reactor_name = IF(
                                VALUES(provider_timestamp) IS NULL
                                OR provider_timestamp IS NULL
                                OR VALUES(provider_timestamp) >= provider_timestamp,
                                VALUES(reactor_name), reactor_name
                            ),
                            emoji = IF(
                                VALUES(provider_timestamp) IS NULL
                                OR provider_timestamp IS NULL
                                OR VALUES(provider_timestamp) >= provider_timestamp,
                                VALUES(emoji), emoji
                            ),
                            provider_reaction_id = IF(
                                VALUES(provider_timestamp) IS NULL
                                OR provider_timestamp IS NULL
                                OR VALUES(provider_timestamp) >= provider_timestamp,
                                COALESCE(VALUES(provider_reaction_id), provider_reaction_id),
                                provider_reaction_id
                            ),
                            client_reaction_id = IF(
                                VALUES(provider_timestamp) IS NULL
                                OR provider_timestamp IS NULL
                                OR VALUES(provider_timestamp) >= provider_timestamp,
                                COALESCE(VALUES(client_reaction_id), client_reaction_id),
                                client_reaction_id
                            ),
                            provider_timestamp = IF(
                                VALUES(provider_timestamp) IS NULL
                                OR provider_timestamp IS NULL
                                OR VALUES(provider_timestamp) >= provider_timestamp,
                                COALESCE(VALUES(provider_timestamp), provider_timestamp),
                                provider_timestamp
                            ),
                            updated_at = IF(
                                VALUES(provider_timestamp) IS NULL
                                OR provider_timestamp IS NULL
                                OR VALUES(provider_timestamp) >= provider_timestamp,
                                UTC_TIMESTAMP(6), updated_at
                            )
                        """,
                        (
                            ticket_id,
                            message_id,
                            target_whatsapp_message_id,
                            reactor_key,
                            reactor_type,
                            reactor_id,
                            reactor_name,
                            emoji,
                            provider_reaction_id,
                            client_reaction_id,
                            provider_timestamp,
                        ),
                    )
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET last_activity_at = UTC_TIMESTAMP(6),
                            updated_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (ticket_id,),
                    )
            except aiomysql.IntegrityError:
                # A concurrent replay may hit the provider/client event unique key.
                # The natural (message, reactor) row is already authoritative.
                pass

        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT * FROM support_message_reactions
                    WHERE message_id = %s AND reactor_key = %s
                    LIMIT 1
                    """,
                    (message_id, reactor_key),
                )
                row = await cur.fetchone()
        if not row:
            raise SupportRepositoryError("Message reaction could not be reloaded.")
        return row

    async def apply_linked_device_reaction(
        self, *, ticket_id: int, message_id: int,
        target_whatsapp_message_id: str, line_id: str,
        emoji: str, provider_reaction_id: str,
        provider_timestamp: int | None,
    ) -> tuple[dict[str, Any], bool]:
        """Apply one phone-origin reaction once, including concurrent replays."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        reactor_key = f"business:{line_id}"
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT * FROM support_message_reactions
                        WHERE message_id = %s AND reactor_key = %s FOR UPDATE
                        """,
                        (message_id, reactor_key),
                    )
                    previous = await cur.fetchone()
                    if previous and (
                        previous.get("provider_reaction_id") == provider_reaction_id
                        or (provider_timestamp is not None and previous.get("provider_timestamp") is not None
                            and int(previous["provider_timestamp"]) > provider_timestamp)
                    ):
                        await conn.commit()
                        return previous, False
                    if previous:
                        await cur.execute(
                            """
                            UPDATE support_message_reactions
                            SET reactor_type = 'AGENT', reactor_id = NULL,
                                reactor_name = 'WhatsApp app', emoji = %s,
                                provider_reaction_id = %s, provider_timestamp = %s,
                                updated_at = UTC_TIMESTAMP(6)
                            WHERE id = %s
                            """,
                            (emoji, provider_reaction_id, provider_timestamp, previous["id"]),
                        )
                    else:
                        await cur.execute(
                            """
                            INSERT INTO support_message_reactions (
                                ticket_id, message_id, target_whatsapp_message_id,
                                reactor_key, reactor_type, reactor_id, reactor_name,
                                emoji, provider_reaction_id, provider_timestamp,
                                created_at, updated_at
                            ) VALUES (
                                %s, %s, %s, %s, 'AGENT', NULL, 'WhatsApp app',
                                %s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                            )
                            """,
                            (ticket_id, message_id, target_whatsapp_message_id,
                             reactor_key, emoji, provider_reaction_id, provider_timestamp),
                        )
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET last_activity_at = UTC_TIMESTAMP(6), updated_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (ticket_id,),
                    )
                    await cur.execute(
                        """
                        SELECT * FROM support_message_reactions
                        WHERE message_id = %s AND reactor_key = %s LIMIT 1
                        """,
                        (message_id, reactor_key),
                    )
                    applied = await cur.fetchone()
                await conn.commit()
                if not applied:
                    raise SupportRepositoryError("Linked-device reaction could not be reloaded.")
                return applied, True
            except Exception:
                await conn.rollback()
                raise

    async def _attach_message_reactions(
        self, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        message_ids = [int(row["id"]) for row in rows if row.get("id") is not None]
        if not message_ids:
            return rows
        placeholders = ", ".join(["%s"] * len(message_ids))
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"""
                    SELECT id, ticket_id, message_id, reactor_type, reactor_id,
                           reactor_name, emoji, created_at, updated_at
                    FROM support_message_reactions
                    WHERE message_id IN ({placeholders}) AND emoji <> ''
                    ORDER BY id ASC
                    """,
                    tuple(message_ids),
                )
                reactions = list(await cur.fetchall())
                await attach_current_staff_names(cur, rows + reactions)
        by_message: dict[int, list[dict[str, Any]]] = {}
        for reaction in reactions:
            by_message.setdefault(int(reaction["message_id"]), []).append(reaction)
        for row in rows:
            row["reactions"] = by_message.get(int(row.get("id") or 0), [])
        return rows

    async def update_message_send_state(
        self,
        message_id: int,
        *,
        send_status: str,
        whatsapp_message_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE support_ticket_messages
                    SET send_status = %s,
                        whatsapp_message_id = COALESCE(%s, whatsapp_message_id),
                        error_message = %s,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s
                      AND (%s <> 'FAILED' OR COALESCE(send_status, '') <> 'SENT')
                      AND (%s IS NULL OR whatsapp_message_id IS NULL OR whatsapp_message_id = %s)
                    """,
                    (send_status, whatsapp_message_id, error_message, message_id, send_status,
                     whatsapp_message_id, whatsapp_message_id),
                )
                if not cur.rowcount and send_status == "SENT" and whatsapp_message_id:
                    await cur.execute(
                        "SELECT whatsapp_message_id FROM support_ticket_messages WHERE id = %s",
                        (message_id,),
                    )
                    current = await cur.fetchone()
                    if current and current[0] not in {None, whatsapp_message_id}:
                        raise SupportConflictError("This dashboard message was confirmed with a different WhatsApp ID.")

    async def confirm_linked_device_outgoing(
        self, *, client_message_id: str, whatsapp_message_id: str,
        phone_number: str, business_phone_number_id: str,
        whatsapp_timestamp: str,
    ) -> tuple[dict[str, Any], bool] | None:
        """Reconcile a bridge-confirmed send with its original dashboard row."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT * FROM support_ticket_messages WHERE transport_client_id = %s FOR UPDATE",
                        (client_message_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        await conn.rollback()
                        return None
                    await cur.execute(
                        "SELECT whatsapp_phone, business_phone_number_id FROM support_tickets WHERE id = %s",
                        (row["ticket_id"],),
                    )
                    ticket = await cur.fetchone()
                    if (
                        not ticket or str(ticket.get("whatsapp_phone") or "") != phone_number
                        or str(ticket.get("business_phone_number_id") or "") != business_phone_number_id
                        or str(row.get("sender_type") or "").upper() not in {"AGENT", "SYSTEM"}
                        or (str(row.get("sender_type") or "").upper() == "AGENT" and not row.get("sender_id"))
                        or (str(row.get("sender_type") or "").upper() == "SYSTEM" and row.get("client_message_id") not in {
                            f"system:claim:{row['ticket_id']}", f"system:resolved:{row['ticket_id']}"
                        })
                        or row.get("direction") != "OUTBOUND"
                    ):
                        raise SupportConflictError("The confirmed WhatsApp send does not match its dashboard message.")
                    if row.get("whatsapp_message_id") and row["whatsapp_message_id"] != whatsapp_message_id:
                        # A resent message is already confirmed by its newer
                        # attempt. A late confirmation of the earlier attempt
                        # changes nothing.
                        await conn.rollback()
                        return row, False
                    changed = (
                        row.get("send_status") != "SENT"
                        or row.get("whatsapp_message_id") != whatsapp_message_id
                        or row.get("error_message") is not None
                    )
                    if changed:
                        await cur.execute(
                            """
                            UPDATE support_ticket_messages
                            SET send_status = 'SENT', whatsapp_message_id = %s,
                                read_sync_pending = IF(sender_type='AGENT',1,read_sync_pending),
                                whatsapp_timestamp = COALESCE(whatsapp_timestamp, %s),
                                error_message = NULL, updated_at = UTC_TIMESTAMP(6)
                            WHERE id = %s
                            """,
                            (whatsapp_message_id, whatsapp_timestamp, row["id"]),
                        )
                    await cur.execute("SELECT * FROM support_ticket_messages WHERE id = %s", (row["id"],))
                    confirmed = await cur.fetchone()
                await conn.commit()
                return confirmed, changed
            except aiomysql.IntegrityError as exc:
                await conn.rollback()
                raise SupportConflictError("The confirmed WhatsApp message ID is already attached to another message.") from exc
            except Exception:
                await conn.rollback()
                raise

    async def list_messages(
        self,
        ticket_id: int,
        *,
        limit: int = 100,
        before_id: int | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        params: list[Any] = [ticket_id]
        extra = ""
        if before_id is not None:
            extra = " AND id < %s"
            params.append(before_id)
        if user_id is not None:
            extra += " AND NOT EXISTS (SELECT 1 FROM support_message_user_deletions d WHERE d.message_id = support_ticket_messages.id AND d.user_id = %s)"
            params.append(user_id)
        params.append(limit)

        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"""
                    SELECT *
                    FROM support_ticket_messages
                    WHERE ticket_id = %s {extra}
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = list(await cur.fetchall())
        rows.reverse()
        return await self._attach_message_reactions(rows)

    async def list_messages_around(
        self,
        ticket_id: int,
        message_id: int,
        *,
        before: int = 70,
        after: int = 70,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        before = max(0, min(before, 100))
        after = max(0, min(after, 100))
        visible = " AND NOT EXISTS (SELECT 1 FROM support_message_user_deletions d WHERE d.message_id = support_ticket_messages.id AND d.user_id = %s)" if user_id is not None else ""
        visible_param: tuple[Any, ...] = (user_id,) if user_id is not None else ()
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"SELECT id FROM support_ticket_messages WHERE id = %s AND ticket_id = %s {visible} LIMIT 1",
                    (message_id, ticket_id, *visible_param),
                )
                if not await cur.fetchone():
                    return []
                await cur.execute(
                    f"""
                    SELECT * FROM support_ticket_messages
                    WHERE ticket_id = %s AND id <= %s {visible}
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (ticket_id, message_id, *visible_param, before + 1),
                )
                older = list(await cur.fetchall())
                older.reverse()
                newer: list[dict[str, Any]] = []
                if after:
                    await cur.execute(
                        f"""
                        SELECT * FROM support_ticket_messages
                        WHERE ticket_id = %s AND id > %s {visible}
                        ORDER BY id ASC
                        LIMIT %s
                        """,
                        (ticket_id, message_id, *visible_param, after),
                    )
                    newer = list(await cur.fetchall())
        return await self._attach_message_reactions(older + newer)

    async def search_messages(
        self,
        *,
        query: str,
        agent_id: str,
        can_view_all: bool = False,
        ticket_id: int | None = None,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        # Escape LIKE wildcards so searches behave like literal WhatsApp text searches.
        # Use an explicit non-backslash escape character. Backslash is also the
        # SQL string escape in MariaDB/MySQL, so an ESCAPE clause using it is
        # easy to render invalid across SQL modes. ``!`` is unambiguous.
        escaped = query.replace('!', '!!').replace('%', '!%').replace('_', '!_')
        like = f"%{escaped}%"
        params: list[Any] = []

        participation_where = """
            (
                t.current_assignee_id = %s
                OR (t.conversation_origin = 'STAFF' AND t.initiated_by_staff_id = %s)
                OR EXISTS (
                    SELECT 1
                    FROM support_ticket_events participation_event
                    WHERE participation_event.ticket_id = t.id
                      AND (
                            (participation_event.event_type = 'TICKET_CLAIMED' AND participation_event.actor_id = %s)
                            OR (
                                participation_event.event_type = 'TICKET_TRANSFERRED'
                                AND (
                                    CASE
                                        WHEN JSON_VALID(participation_event.details_json)
                                        THEN JSON_UNQUOTE(JSON_EXTRACT(participation_event.details_json, '$.from_agent_id'))
                                        ELSE NULL
                                    END = %s
                                    OR CASE
                                        WHEN JSON_VALID(participation_event.details_json)
                                        THEN JSON_UNQUOTE(JSON_EXTRACT(participation_event.details_json, '$.to_agent_id'))
                                        ELSE NULL
                                    END = %s
                                )
                            )
                          )
                )
            )
        """
        access_where = "1=1"
        if not can_view_all:
            access_where = f"((t.status IN ('RESOLVED', 'CLOSED') AND t.active_key IS NULL) OR (COALESCE(t.conversation_origin, 'CUSTOMER') <> 'STAFF' AND t.status = 'NEW' AND t.current_assignee_id IS NULL AND t.active_key IS NOT NULL) OR {participation_where})"
            params.extend([agent_id, agent_id, agent_id, agent_id, agent_id])

        if ticket_id is not None:
            access_where = f"({access_where}) AND t.id = %s"
            params.append(ticket_id)

        search_where = """
            (
                COALESCE(m.body, '') LIKE %s ESCAPE '!'
                OR COALESCE(m.location_name, '') LIKE %s ESCAPE '!'
                OR COALESCE(m.location_address, '') LIKE %s ESCAPE '!'
                OR COALESCE(m.provider_media_filename, '') LIKE %s ESCAPE '!'
            )
            AND m.deleted_for_everyone_at IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM support_message_user_deletions d
                WHERE d.message_id = m.id AND d.user_id = %s
            )
        """
        access_params = tuple(params)

        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                conversations: list[dict[str, Any]] = []
                # Global search also finds conversations independently of their
                # messages, including empty conversations. Keep the same access
                # predicate for both result types; never preview hidden messages.
                if ticket_id is None:
                    customer_where = """(
                        COALESCE(t.customer_name, '') LIKE %s ESCAPE '!'
                        OR COALESCE(t.customer_userid, '') LIKE %s ESCAPE '!'
                        OR t.whatsapp_phone LIKE %s ESCAPE '!'
                        OR t.ticket_reference LIKE %s ESCAPE '!'
                    )"""
                    customer_params: list[Any] = [like] * 4
                    phone_digits = re.sub(r"[^0-9]", "", query)
                    if len(phone_digits) >= 2 and re.fullmatch(r"[+0-9\s().-]+", query):
                        customer_where = f"""({customer_where} OR
                            REGEXP_REPLACE(t.whatsapp_phone, '[^0-9]', '') LIKE %s)"""
                        customer_params.append(f"%{phone_digits}%")
                    await cur.execute(
                        f"""
                        SELECT 'conversation' AS result_type, NULL AS message_id,
                            t.id AS ticket_id, t.ticket_reference, t.customer_name,
                            t.customer_userid, t.whatsapp_phone, t.status,
                            t.conversation_origin, t.business_display_phone_number,
                            t.business_phone_number_id, n.display_name,
                            t.last_activity_at AS created_at
                        FROM support_tickets t
                        LEFT JOIN whatsapp_phone_numbers n
                          ON n.phone_number_id = t.business_phone_number_id
                        WHERE {access_where} AND {customer_where}
                        ORDER BY t.last_activity_at DESC, t.id DESC
                        LIMIT %s
                        """,
                        (*access_params, *customer_params, limit),
                    )
                    conversations = list(await cur.fetchall())
                    if len(conversations) >= limit:
                        return conversations
                await cur.execute(
                    f"""
                    SELECT
                        'message' AS result_type,
                        m.id AS message_id, m.ticket_id, m.sender_type, m.sender_id, m.sender_name,
                        m.direction, m.message_type, m.body, m.location_name,
                        m.location_address, m.provider_media_filename, m.created_at,
                        t.ticket_reference, t.customer_name, t.customer_userid,
                        t.whatsapp_phone, t.status, t.conversation_origin,
                        t.business_display_phone_number, t.business_phone_number_id,
                        n.display_name
                    FROM support_ticket_messages m
                    INNER JOIN support_tickets t ON t.id = m.ticket_id
                    LEFT JOIN whatsapp_phone_numbers n
                      ON n.phone_number_id = t.business_phone_number_id
                    WHERE {access_where} AND {search_where}
                    ORDER BY m.created_at DESC, m.id DESC
                    LIMIT %s
                    """,
                    (*access_params, like, like, like, like, agent_id, limit - len(conversations)),
                )
                rows = conversations + list(await cur.fetchall())
                return await attach_current_staff_names(cur, rows)

    async def list_events(self, ticket_id: int, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT *
                    FROM support_ticket_events
                    WHERE ticket_id = %s
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (ticket_id, limit),
                )
                rows = list(await cur.fetchall())
                return await attach_current_staff_names(cur, rows)

    async def add_event(
        self,
        ticket_id: int,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str | None = None,
        actor_name: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO support_ticket_events (
                        ticket_id, event_type, actor_type, actor_id, actor_name,
                        details_json, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
                    """,
                    (
                        ticket_id,
                        event_type,
                        actor_type,
                        actor_id,
                        actor_name,
                        json.dumps(details or {}, ensure_ascii=False),
                    ),
                )


    async def mark_customer_replied(self, ticket_id: int) -> None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET status = 'IN_PROGRESS',
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                          AND status = 'WAITING_CUSTOMER'
                          AND active_key IS NOT NULL
                        """,
                        (ticket_id,),
                    )
                    changed = cur.rowcount == 1
                    if changed:
                        await cur.execute(
                            """
                            INSERT INTO support_ticket_events (
                                ticket_id, event_type, actor_type, details_json, created_at
                            ) VALUES (%s, 'CUSTOMER_REPLIED', 'CUSTOMER', '{}', UTC_TIMESTAMP(6))
                            """,
                            (ticket_id,),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise


    async def mark_agent_reply_started(
        self,
        ticket_id: int,
        *,
        agent_id: str,
        agent_name: str,
    ) -> None:
        """Move ASSIGNED -> IN_PROGRESS only if the ticket is still active."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET status = 'IN_PROGRESS',
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                          AND status = 'ASSIGNED'
                          AND active_key IS NOT NULL
                        """,
                        (ticket_id,),
                    )
                    if cur.rowcount == 1:
                        await cur.execute(
                            """
                            INSERT INTO support_ticket_events (
                                ticket_id, event_type, actor_type, actor_id, actor_name,
                                details_json, created_at
                            ) VALUES (%s, 'STATUS_CHANGED', 'AGENT', %s, %s, %s, UTC_TIMESTAMP(6))
                            """,
                            (
                                ticket_id,
                                agent_id,
                                agent_name,
                                json.dumps({"from": "ASSIGNED", "to": "IN_PROGRESS"}),
                            ),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def claim_ticket(
        self,
        ticket_id: int,
        *,
        agent_id: str,
        agent_name: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically claim a ticket. Repeating the same agent claim is idempotent."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        changed = False
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT id, status, current_assignee_id, active_key
                        FROM support_tickets
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (ticket_id,),
                    )
                    state = await cur.fetchone()
                    if not state:
                        raise SupportNotFoundError("Ticket not found.")

                    if (
                        state.get("status") == "NEW"
                        and state.get("current_assignee_id") is None
                        and state.get("active_key") is not None
                    ):
                        await cur.execute(
                            """
                            UPDATE support_tickets
                            SET current_assignee_id = %s,
                                current_assignee_name = %s,
                                status = 'ASSIGNED',
                                assigned_at = UTC_TIMESTAMP(6),
                                updated_at = UTC_TIMESTAMP(6),
                                last_activity_at = UTC_TIMESTAMP(6)
                            WHERE id = %s
                            """,
                            (agent_id, agent_name, ticket_id),
                        )
                        await cur.execute(
                            """
                            INSERT INTO support_ticket_events (
                                ticket_id, event_type, actor_type, actor_id, actor_name,
                                details_json, created_at
                            ) VALUES (%s, 'TICKET_CLAIMED', 'AGENT', %s, %s, '{}', UTC_TIMESTAMP(6))
                            """,
                            (ticket_id, agent_id, agent_name),
                        )
                        changed = True
                    elif (
                        state.get("active_key") is not None
                        and str(state.get("current_assignee_id") or "") == str(agent_id)
                        and state.get("status") in ACTIVE_TICKET_STATUSES
                    ):
                        # Same-agent retry after a lost HTTP response. Do not create a
                        # second claim event or change lifecycle state.
                        changed = False
                    else:
                        raise SupportConflictError(
                            "Ticket has already been claimed or is not claimable."
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found after claim.")
        return ticket, changed

    async def transfer_ticket(
        self,
        ticket_id: int,
        *,
        expected_assignee_id: str,
        target_staff_id: int,
        actor_id: str,
        actor_name: str,
    ) -> dict[str, Any]:
        """Atomically transfer one active assigned ticket to another active staff member."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None

        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT id, status, active_key, current_assignee_id,
                               current_assignee_name
                        FROM support_tickets
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (ticket_id,),
                    )
                    state = await cur.fetchone()
                    if not state:
                        raise SupportNotFoundError("Ticket not found.")
                    if (
                        state.get("active_key") is None
                        or state.get("status") not in ACTIVE_TICKET_STATUSES
                    ):
                        raise SupportConflictError("Ticket is no longer active.")

                    current_id = str(state.get("current_assignee_id") or "")
                    if not current_id:
                        raise SupportConflictError("Claim the ticket before transferring it.")
                    if current_id != str(expected_assignee_id):
                        raise SupportConflictError(
                            "Ticket ownership changed. Refresh before transferring it."
                        )
                    if current_id == str(target_staff_id):
                        raise SupportConflictError("Ticket is already assigned to that staff member.")

                    await cur.execute(
                        """
                        SELECT id, name, role, is_active
                        FROM staff
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (int(target_staff_id),),
                    )
                    target = await cur.fetchone()
                    if not target or not bool(target.get("is_active")):
                        raise SupportConflictError(
                            "The selected staff member is no longer active. Refresh the staff list."
                        )

                    if not await self._fence_deliveries_for_lifecycle(cur, ticket_id):
                        raise SupportConflictError(
                            "An outbound WhatsApp message is still being sent. Refresh and transfer after it finishes."
                        )

                    previous_status = str(state.get("status") or "")
                    previous_name = str(state.get("current_assignee_name") or "Support Agent")
                    target_name = str(target.get("name") or "Support Agent")
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET current_assignee_id = %s,
                            current_assignee_name = %s,
                            status = 'ASSIGNED',
                            assigned_at = UTC_TIMESTAMP(6),
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (int(target_staff_id), target_name, ticket_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, actor_id, actor_name,
                            details_json, created_at
                        ) VALUES (%s, 'TICKET_TRANSFERRED', 'AGENT', %s, %s, %s, UTC_TIMESTAMP(6))
                        """,
                        (
                            ticket_id,
                            actor_id,
                            actor_name,
                            json.dumps(
                                {
                                    "from_agent_id": current_id,
                                    "from_agent_name": previous_name,
                                    "to_agent_id": str(target_staff_id),
                                    "to_agent_name": target_name,
                                    "previous_status": previous_status,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found after transfer.")
        return ticket

    async def update_status(
        self,
        ticket_id: int,
        *,
        status: str,
        agent_id: str,
        agent_name: str,
        expected_assignee_id: str | None = None,
    ) -> dict[str, Any]:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT status, current_assignee_id FROM support_tickets WHERE id = %s FOR UPDATE",
                        (ticket_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        raise SupportNotFoundError("Ticket not found.")
                    if expected_assignee_id is not None and str(
                        row.get("current_assignee_id") or ""
                    ) != str(expected_assignee_id):
                        raise SupportConflictError(
                            "Ticket ownership changed. Refresh before updating its status."
                        )
                    previous = row["status"]
                    if status not in ALLOWED_TRANSITIONS.get(str(previous), set()):
                        raise SupportConflictError(
                            f"Ticket is now {previous}; refresh before changing its status."
                        )
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET status = %s,
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6),
                            closed_at = CASE WHEN %s = 'CLOSED' THEN UTC_TIMESTAMP(6) ELSE closed_at END
                        WHERE id = %s
                        """,
                        (status, status, ticket_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, actor_id, actor_name,
                            details_json, created_at
                        ) VALUES (%s, 'STATUS_CHANGED', 'AGENT', %s, %s, %s, UTC_TIMESTAMP(6))
                        """,
                        (
                            ticket_id,
                            agent_id,
                            agent_name,
                            json.dumps({"from": previous, "to": status}),
                        ),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        return ticket

    async def resolve_ticket(
        self,
        ticket_id: int,
        *,
        agent_id: str,
        agent_name: str,
        expected_assignee_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Resolve ticket atomically and release its active_key.

        Returns (ticket, changed). Repeated resolve calls are idempotent.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT status, current_assignee_id FROM support_tickets WHERE id = %s FOR UPDATE",
                        (ticket_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        raise SupportNotFoundError("Ticket not found.")
                    if expected_assignee_id is not None and str(
                        row.get("current_assignee_id") or ""
                    ) != str(expected_assignee_id):
                        raise SupportConflictError(
                            "Ticket ownership changed. Refresh before resolving it."
                        )
                    if row["status"] in {"RESOLVED", "CLOSED"}:
                        await conn.rollback()
                        ticket = await self.get_ticket(ticket_id)
                        assert ticket is not None
                        return ticket, False

                    if not await self._fence_deliveries_for_lifecycle(cur, ticket_id):
                        raise SupportConflictError(
                            "An outbound WhatsApp message is still being sent. Refresh and resolve after it finishes."
                        )

                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET status = 'RESOLVED',
                            active_key = NULL,
                            resolved_at = UTC_TIMESTAMP(6),
                            resolved_by_staff_id = %s,
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (int(agent_id), ticket_id),
                    )
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, actor_id, actor_name,
                            details_json, created_at
                        ) VALUES (%s, 'TICKET_RESOLVED', 'AGENT', %s, %s, %s, UTC_TIMESTAMP(6))
                        """,
                        (ticket_id, agent_id, agent_name, json.dumps({
                            'previous_status': row['status'],
                            'last_assignee_id': row.get('current_assignee_id'),
                        })),
                    )
                    await cur.execute(
                        """
                        INSERT INTO support_ticket_events (
                            ticket_id, event_type, actor_type, actor_id, actor_name,
                            details_json, created_at
                        ) VALUES (%s, 'RETURNED_TO_AI', 'AGENT', %s, %s, '{}', UTC_TIMESTAMP(6))
                        """,
                        (ticket_id, agent_id, agent_name),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        ticket = await self.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found after resolve.")
        return ticket, True

    async def reopen_customer_ticket(
        self, ticket_id: int, *, agent_id: str, agent_name: str,
        supervisor: bool, expected_resolved_at: datetime | None,
    ) -> tuple[dict[str, Any], bool]:
        """Reopen atomically; managers take ownership, agents keep their assignment."""
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    # Locate immutable channel before the transaction's read view.
                    # Match removal/create lock order: line, ticket, messages.
                    await cur.execute("SELECT business_phone_number_id FROM support_tickets WHERE id=%s", (ticket_id,))
                    channel = await cur.fetchone()
                    if not channel:
                        raise SupportNotFoundError("Ticket not found.")
                    line_id = str(channel.get("business_phone_number_id") or "")
                    await conn.begin()
                    await self._lock_available_whatsapp_line(conn, line_id)
                    await cur.execute("SELECT * FROM support_tickets WHERE id=%s FOR UPDATE", (ticket_id,))
                    row = await cur.fetchone()
                    if not row:
                        raise SupportNotFoundError("Ticket not found.")
                    if str(row.get("business_phone_number_id") or "") != line_id:
                        raise SupportConflictError("The ticket's WhatsApp line changed. Refresh before reopening.")
                    if not may_reopen_customer_ticket(row, agent_id, supervisor=supervisor):
                        raise SupportConflictError("Reopening permission changed. Refresh the ticket.")
                    if row.get("resolved_at") != expected_resolved_at:
                        raise SupportConflictError("The ticket's resolution changed. Refresh before reopening.")
                    if row.get("active_key") is not None and row.get("status") in ACTIVE_TICKET_STATUSES:
                        # Only a completed reopen of this resolution is idempotent.
                        await cur.execute("SELECT id FROM support_ticket_events WHERE ticket_id=%s AND event_type='TICKET_REOPENED' LIMIT 1", (ticket_id,))
                        if not await cur.fetchone():
                            raise SupportConflictError("This ticket is already active and was not reopened.")
                        await conn.commit()
                        return row, False
                    if row.get("status") != 'RESOLVED' or row.get("active_key") is not None:
                        raise SupportConflictError("Only resolved customer tickets can be reopened.")
                    previous_assignee = row.get("current_assignee_id")
                    assignee = str(agent_id if supervisor else previous_assignee or "")
                    if not assignee:
                        raise SupportConflictError("The last assignee is unknown. Ownership cannot be inferred for this ticket.")
                    await cur.execute("SELECT id,name FROM staff WHERE id=%s AND is_active=1 FOR UPDATE", (assignee,))
                    target = await cur.fetchone()
                    if not target:
                        raise SupportConflictError("The assigned staff member is unavailable. Reactivate that account before reopening.")
                    if not await self._fence_deliveries_for_lifecycle(cur, ticket_id):
                        raise SupportConflictError("An outbound WhatsApp message is still being sent. Reopen after it finishes.")
                    active_key = _ticket_active_key(str(row['whatsapp_phone']), line_id)
                    # The unique active_key is the final arbiter against a new
                    # inbound ticket or another historical ticket reopening.
                    await cur.execute("SELECT id FROM support_tickets WHERE active_key=%s AND id<>%s LIMIT 1", (active_key, ticket_id))
                    if await cur.fetchone():
                        raise SupportConflictError("This customer already has an active conversation on that WhatsApp line.")
                    await cur.execute("SELECT details_json FROM support_ticket_events WHERE ticket_id=%s AND event_type='TICKET_RESOLVED' ORDER BY id DESC LIMIT 1", (ticket_id,))
                    resolution = await cur.fetchone()
                    try:
                        details = json.loads((resolution or {}).get('details_json') or '{}')
                    except (TypeError, ValueError):
                        details = {}
                    previous = details.get('previous_status') if isinstance(details, dict) else None
                    restored_status = previous if previous in ACTIVE_TICKET_STATUSES and previous != 'NEW' else 'ASSIGNED'
                    await cur.execute("""
                        UPDATE support_tickets SET status=%s, active_key=%s,
                            current_assignee_id=%s, current_assignee_name=%s,
                            assigned_at=IF(%s,UTC_TIMESTAMP(6),assigned_at),
                            updated_at=UTC_TIMESTAMP(6), last_activity_at=UTC_TIMESTAMP(6)
                        WHERE id=%s
                    """, (restored_status, active_key, assignee,
                          target['name'] if supervisor else row.get('current_assignee_name'),
                          supervisor, ticket_id))
                    await cur.execute("""
                        INSERT INTO support_ticket_events
                            (ticket_id,event_type,actor_type,actor_id,actor_name,details_json,created_at)
                        VALUES (%s,'TICKET_REOPENED','AGENT',%s,%s,%s,UTC_TIMESTAMP(6))
                    """, (ticket_id, agent_id, agent_name, json.dumps({
                        'restored_status': restored_status, 'last_assignee_id': previous_assignee,
                        'current_assignee_id': assignee,
                        'resolved_by_staff_id': row.get('resolved_by_staff_id'),
                        'resolved_at': row['resolved_at'].isoformat() if row.get('resolved_at') else None,
                    })))
                await conn.commit()
            except aiomysql.IntegrityError as exc:
                await conn.rollback()
                if exc.args and exc.args[0] == 1062:
                    raise SupportConflictError("This customer already has an active conversation on that WhatsApp line.") from exc
                raise
            except aiomysql.OperationalError as exc:
                await conn.rollback()
                if exc.args and exc.args[0] in {1020, 1205, 1213}:
                    raise SupportConflictError("Another conversation change is in progress. Refresh and retry reopening.") from exc
                raise
            except BaseException:
                await conn.rollback()
                raise
        reopened = await self.get_ticket(ticket_id)
        if not reopened:
            raise SupportNotFoundError("Ticket not found after reopening.")
        return reopened, True

    async def list_idle_phone_app_tickets(self, *, idle_minutes: int, limit: int = 50) -> list[int]:
        """IDs of unclaimed tickets answered in the WhatsApp app with no recent messages."""
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"SELECT id FROM support_tickets WHERE {_IDLE_PHONE_APP_TICKET} ORDER BY id LIMIT %s",
                    (idle_minutes, idle_minutes, limit),
                )
                return [int(row["id"]) for row in await cur.fetchall()]

    async def auto_resolve_idle_phone_app_ticket(
        self, ticket_id: int, *, idle_minutes: int,
    ) -> dict[str, Any] | None:
        """Resolve one idle phone-app ticket without a customer notice.

        The idle conditions are checked again under the row lock, so a ticket
        that was claimed or got a message after it was listed is left alone.
        Returns the resolved ticket, or None when nothing changed.
        """
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        details = json.dumps({"reason": "phone_app_idle", "idle_minutes": idle_minutes})
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        f"SELECT id FROM support_tickets WHERE id = %s AND {_IDLE_PHONE_APP_TICKET} FOR UPDATE",
                        (ticket_id, idle_minutes, idle_minutes),
                    )
                    if not await cur.fetchone():
                        await conn.rollback()
                        return None
                    await cur.execute(
                        """
                        UPDATE support_tickets
                        SET status = 'RESOLVED',
                            active_key = NULL,
                            resolved_at = UTC_TIMESTAMP(6),
                            resolved_by_staff_id = NULL,
                            updated_at = UTC_TIMESTAMP(6),
                            last_activity_at = UTC_TIMESTAMP(6)
                        WHERE id = %s
                        """,
                        (ticket_id,),
                    )
                    for event_type in ("TICKET_RESOLVED", "RETURNED_TO_AI"):
                        await cur.execute(
                            """
                            INSERT INTO support_ticket_events (
                                ticket_id, event_type, actor_type, actor_name,
                                details_json, created_at
                            ) VALUES (%s, %s, 'SYSTEM', %s, %s, UTC_TIMESTAMP(6))
                            """,
                            (ticket_id, event_type, auto_resolve_actor_name(idle_minutes), details),
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return await self.get_ticket(ticket_id)

    async def list_tickets(
        self,
        *,
        view: str,
        agent_id: str,
        can_view_all: bool = False,
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = max(1, min(page_size, 500))
        offset = (page - 1) * page_size

        where = "1=1"
        params: list[Any] = []

        # Regular agents may browse shared closed history, plus owned or
        # previously-owned live conversations. Unassigned NEW tickets remain
        # visible so someone can inspect and claim them.
        participation_where = """
            (
                support_tickets.current_assignee_id = %s
                OR (
                    support_tickets.conversation_origin = 'STAFF'
                    AND support_tickets.initiated_by_staff_id = %s
                )
                OR EXISTS (
                    SELECT 1
                    FROM support_ticket_events participation_event
                    WHERE participation_event.ticket_id = support_tickets.id
                      AND (
                            (
                                participation_event.event_type = 'TICKET_CLAIMED'
                                AND participation_event.actor_id = %s
                            )
                            OR (
                                participation_event.event_type = 'TICKET_TRANSFERRED'
                                AND (
                                    JSON_UNQUOTE(JSON_EXTRACT(
                                        CASE WHEN JSON_VALID(participation_event.details_json) THEN participation_event.details_json ELSE '{}' END,
                                        '$.from_agent_id'
                                    )) = %s
                                    OR JSON_UNQUOTE(JSON_EXTRACT(
                                        CASE WHEN JSON_VALID(participation_event.details_json) THEN participation_event.details_json ELSE '{}' END,
                                        '$.to_agent_id'
                                    )) = %s
                                )
                            )
                          )
                )
            )
        """

        regular_origin = "COALESCE(conversation_origin, 'CUSTOMER') <> 'STAFF'"
        staff_origin = "conversation_origin = 'STAFF'"

        if view == "unassigned":
            # A reply sent from the linked WhatsApp app means someone is already
            # talking to the customer, so the ticket leaves Unassigned for Active.
            where = (
                f"{regular_origin} AND status = 'NEW' AND current_assignee_id IS NULL AND active_key IS NOT NULL"
                f" AND NOT {_PHONE_APP_REPLY_EXISTS}"
            )
        elif view == "mine":
            where = f"{regular_origin} AND current_assignee_id = %s AND active_key IS NOT NULL"
            params.append(agent_id)
        elif view == "watching":
            where = f"""
                {regular_origin}
                AND active_key IS NOT NULL
                AND current_assignee_id <> %s
                AND EXISTS (
                    SELECT 1
                    FROM support_ticket_events transfer_event
                    WHERE transfer_event.ticket_id = support_tickets.id
                      AND transfer_event.event_type = 'TICKET_TRANSFERRED'
                      AND (
                            transfer_event.actor_id = %s
                            OR JSON_UNQUOTE(JSON_EXTRACT(
                                CASE WHEN JSON_VALID(transfer_event.details_json) THEN transfer_event.details_json ELSE '{{}}' END,
                                '$.from_agent_id'
                            )) = %s
                          )
                )
            """
            params.extend([agent_id, agent_id, agent_id])
        elif view == "active":
            placeholders = ",".join(["%s"] * len(ACTIVE_TICKET_STATUSES))
            # Unclaimed tickets belong to Unassigned, so Active lists assigned work
            # plus unclaimed tickets someone already answered in the WhatsApp app.
            where = (
                f"{regular_origin} AND status IN ({placeholders}) AND active_key IS NOT NULL"
                f" AND (current_assignee_id IS NOT NULL OR (status = 'NEW' AND {_PHONE_APP_REPLY_EXISTS}))"
            )
            params.extend(ACTIVE_TICKET_STATUSES)
            if not can_view_all:
                # Unclaimed NEW tickets stay visible to every agent so they can be claimed.
                where = f"({where}) AND ((status = 'NEW' AND current_assignee_id IS NULL) OR {participation_where})"
                params.extend([agent_id, agent_id, agent_id, agent_id, agent_id])
        elif view == "waiting":
            where = f"{regular_origin} AND status IN ('WAITING_CUSTOMER', 'WAITING_INTERNAL') AND active_key IS NOT NULL"
            if not can_view_all:
                where = f"({where}) AND {participation_where}"
                params.extend([agent_id, agent_id, agent_id, agent_id, agent_id])
        elif view == "chats":
            where = f"{staff_origin}"
            if not can_view_all:
                where = f"({where}) AND ((status IN ('RESOLVED', 'CLOSED') AND active_key IS NULL) OR {participation_where})"
                params.extend([agent_id, agent_id, agent_id, agent_id, agent_id])
        elif view == "history":
            where = f"{regular_origin} AND status IN ('RESOLVED', 'CLOSED')"
        else:
            raise ValueError("Unknown ticket view.")

        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"SELECT COUNT(*) AS total FROM support_tickets WHERE {where}",
                    tuple(params),
                )
                count_row = await cur.fetchone()
                total = int((count_row or {}).get("total", 0))

                list_params = [*params, page_size, offset]
                await cur.execute(
                    f"""
                    SELECT
                        support_tickets.id, ticket_reference, whatsapp_phone, customer_id,
                        business_phone_number_id, business_display_phone_number,
                        n.display_name,
                        conversation_origin, initiated_by_staff_id, initiated_by_staff_name,
                        customer_userid, customer_name,
                        LEFT(reason, 300) AS reason,
                        LEFT(ai_summary, 500) AS ai_summary,
                        tracking_reference, requested_action, current_language,
                        communication_style, status,
                        current_assignee_id, current_assignee_name,
                        support_tickets.created_at, support_tickets.updated_at,
                        last_activity_at, assigned_at, resolved_at, closed_at,
                        {_PHONE_APP_REPLY_EXISTS} AS phone_app_replied
                    FROM support_tickets
                    LEFT JOIN whatsapp_phone_numbers n
                      ON n.phone_number_id = support_tickets.business_phone_number_id
                    WHERE {where}
                    ORDER BY last_activity_at DESC, support_tickets.id DESC
                    LIMIT %s OFFSET %s
                    """,
                    tuple(list_params),
                )
                rows = list(await cur.fetchall())
                await attach_current_staff_names(cur, rows)
                await attach_conversation_read_state(cur, rows)
        return {
            "items": rows,
            "page": page,
            "page_size": page_size,
            "total": total,
        }


support_repository = SupportRepository()
