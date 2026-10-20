from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from typing import Any

import aiomysql

from app.config import settings


_PHONE_NON_DIGIT = re.compile(r"\D+")


class ContactNotFoundError(LookupError):
    pass


class ContactConflictError(ValueError):
    pass


def normalize_phone(value: str | None) -> str:
    digits = _PHONE_NON_DIGIT.sub("", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


@dataclass(frozen=True)
class CustomerIdentity:
    source: str
    phone: str
    name: str | None = None
    user_id: str | None = None
    userid: str | None = None
    contact_id: int | None = None
    # Contacts-list key of the contact that names this number (manual:/google:/vcf:).
    contact_key: str | None = None


class CustomerDirectory:
    @staticmethod
    def _mobile_sql() -> str:
        return (
            "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(mobile), '+', ''), "
            "' ', ''), '-', ''), '(', ''), ')', ''), '.', '')"
        )

    def __init__(self) -> None:
        # Opening a connection costs several database round trips, and the
        # directory is consulted every time a conversation is opened. Reuse a
        # small pool instead of connecting per query.
        self._pool: aiomysql.Pool | None = None
        self._pool_lock: asyncio.Lock | None = None

    async def _ensure_pool(self) -> aiomysql.Pool:
        if self._pool is not None:
            return self._pool
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            if self._pool is None:
                self._pool = await aiomysql.create_pool(
                    host=settings.db_host,
                    port=settings.db_port,
                    user=settings.db_user,
                    password=settings.db_password,
                    db=settings.db_name,
                    minsize=1,
                    maxsize=6,
                    autocommit=True,
                    charset="utf8mb4",
                )
        return self._pool

    async def _fetchone(self, query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(query, params)
                return await cursor.fetchone()

    async def _fetchall(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        pool = await self._ensure_pool()
        async with pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(query, params)
                return list(await cursor.fetchall())

    async def _execute(self, query: str, params: tuple[Any, ...]) -> int:
        pool = await self._ensure_pool()
        async with pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(query, params)
                return int(cursor.rowcount or 0)

    async def _registered_user(self, phone: str) -> dict[str, Any] | None:
        normalized = normalize_phone(phone)
        if not normalized:
            return None
        expr = self._mobile_sql()
        rows = await self._fetchall(
            f"""
            SELECT id, userid, name, mobile
            FROM users
            WHERE mobile IS NOT NULL
              AND mobile <> ''
              AND ({expr} = %s OR {expr} = %s)
            LIMIT 2
            """,
            (normalized, f"00{normalized}"),
        )
        if len(rows) == 1:
            return rows[0]
        if rows or len(normalized) < 8:
            return None

        # Legacy rows sometimes store only the national/mobile part while Meta
        # supplies E.164. A suffix fallback is accepted only when it identifies
        # exactly one user, preventing an ambiguous phone match from naming the
        # wrong customer.
        suffix = normalized[-8:]
        candidates = await self._fetchall(
            f"""
            SELECT id, userid, name, mobile
            FROM users
            WHERE mobile IS NOT NULL
              AND mobile <> ''
              AND {expr} LIKE %s
            LIMIT 3
            """,
            (f"%{suffix}",),
        )
        matching = [
            row for row in candidates
            if normalize_phone(str(row.get("mobile") or "")).endswith(suffix)
        ]
        return matching[0] if len(matching) == 1 else None

    async def _saved_contact(self, phone: str) -> dict[str, Any] | None:
        normalized = normalize_phone(phone)
        if not normalized:
            return None
        return await self._fetchone(
            """
            SELECT id, normalized_phone, display_name, created_at, updated_at
            FROM support_contacts
            WHERE normalized_phone = %s
            LIMIT 1
            """,
            (normalized,),
        )

    async def _directory_contact(self, phone: str) -> dict[str, Any] | None:
        """A Google or VCF contact for this number, when no dashboard contact exists."""
        normalized = normalize_phone(phone)
        if not normalized:
            return None
        return await self._fetchone(
            """
            SELECT 'google' AS source, gc.id, gc.display_name
            FROM support_google_contacts gc
            JOIN support_google_accounts ga ON ga.id = gc.account_id
            WHERE ga.id = 1 AND gc.normalized_phone = %s
            UNION ALL
            SELECT 'vcf' AS source, vc.id, vc.display_name
            FROM support_vcf_contacts vc
            WHERE vc.normalized_phone = %s
            LIMIT 1
            """,
            (normalized, normalized),
        )

    async def resolve(self, phone: str) -> CustomerIdentity:
        normalized = normalize_phone(phone)
        # A registered user wins over a saved contact, which wins over a Google
        # or VCF contact. Look them up at once so opening a conversation waits
        # for one round trip instead of several in a row.
        user, contact, directory = await asyncio.gather(
            self._registered_user(normalized),
            self._saved_contact(normalized),
            self._directory_contact(normalized),
            return_exceptions=True,
        )
        for result in (user, contact):
            if isinstance(result, BaseException):
                raise result
        # Google/VCF naming is optional; a lookup failure must not block a conversation.
        if isinstance(directory, BaseException):
            directory = None
        if user:
            return CustomerIdentity(
                source="registered_user",
                phone=normalized,
                name=str(user.get("name") or "").strip() or None,
                user_id=str(user.get("id")) if user.get("id") is not None else None,
                userid=str(user.get("userid")) if user.get("userid") is not None else None,
            )
        if contact:
            return CustomerIdentity(
                source="saved_contact",
                phone=normalized,
                name=str(contact.get("display_name") or "").strip() or None,
                contact_id=int(contact["id"]),
                contact_key=f"manual:{int(contact['id'])}",
            )
        if directory:
            source = str(directory.get("source") or "")
            return CustomerIdentity(
                source=f"{source}_contact",
                phone=normalized,
                name=str(directory.get("display_name") or "").strip() or None,
                contact_key=f"{source}:{int(directory['id'])}",
            )
        return CustomerIdentity(source="guest", phone=normalized)

    async def save_contact(
        self,
        *,
        phone: str,
        display_name: str,
        staff_id: str,
    ) -> CustomerIdentity:
        normalized = normalize_phone(phone)
        name = str(display_name or "").strip()
        if not normalized:
            raise ValueError("A valid customer phone number is required.")
        if not name:
            raise ValueError("Contact name is required.")
        if await self._registered_user(normalized):
            raise ValueError("This phone number already belongs to a registered customer.")
        await self._execute(
            """
            INSERT INTO support_contacts (
                normalized_phone, display_name, created_by_staff_id,
                updated_by_staff_id, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
            ON DUPLICATE KEY UPDATE
                display_name = VALUES(display_name),
                updated_by_staff_id = VALUES(updated_by_staff_id),
                updated_at = UTC_TIMESTAMP(6)
            """,
            (normalized, name[:191], staff_id, staff_id),
        )
        return await self.resolve(normalized)

    async def _contact_phone(self, source: str, contact_id: int) -> str | None:
        if source == "manual":
            row = await self._fetchone("SELECT normalized_phone FROM support_contacts WHERE id = %s", (contact_id,))
        elif source == "google":
            row = await self._fetchone(
                """
                SELECT gc.normalized_phone
                FROM support_google_contacts gc
                JOIN support_google_accounts ga ON ga.id = gc.account_id
                WHERE ga.id = 1 AND gc.id = %s
                """,
                (contact_id,),
            )
        else:
            row = await self._fetchone("SELECT normalized_phone FROM support_vcf_contacts WHERE id = %s", (contact_id,))
        return str(row["normalized_phone"]) if row else None

    async def edit_contact(
        self,
        *,
        contact_key: str,
        display_name: str,
        phone: str,
        staff_id: str,
    ) -> dict[str, Any]:
        """Edit any contact in the Contacts list.

        Dashboard contacts are updated in place. Google and VCF contacts are
        never changed at their source: the edit is saved as a dashboard contact
        that takes their place in the list (``replaces_phone`` hides the
        original entry when the number was corrected).
        """
        source, _, raw_id = str(contact_key or "").partition(":")
        if source not in {"manual", "google", "vcf"} or not raw_id.isdigit():
            raise ContactNotFoundError("Contact not found.")
        contact_id = int(raw_id)
        name = " ".join(str(display_name or "").split())[:191]
        normalized = normalize_phone(phone)
        if not name:
            raise ValueError("Contact name is required.")
        if not normalized:
            raise ValueError("A valid phone number is required.")
        original_phone = await self._contact_phone(source, contact_id)
        if original_phone is None:
            raise ContactNotFoundError("Contact not found.")
        if await self._registered_user(normalized):
            raise ContactConflictError("This phone number already belongs to a registered customer.")

        if source == "manual":
            other = await self._fetchone(
                "SELECT id FROM support_contacts WHERE normalized_phone = %s AND id <> %s",
                (normalized, contact_id),
            )
            if other:
                raise ContactConflictError("Another saved contact already uses this number.")
            await self._execute(
                """
                UPDATE support_contacts
                SET display_name = %s, normalized_phone = %s,
                    updated_by_staff_id = %s, updated_at = UTC_TIMESTAMP(6)
                WHERE id = %s
                """,
                (name, normalized, staff_id, contact_id),
            )
            saved_id = contact_id
        else:
            replaces = original_phone if original_phone != normalized else None
            await self._execute(
                """
                INSERT INTO support_contacts (
                    normalized_phone, display_name, replaces_phone, created_by_staff_id,
                    updated_by_staff_id, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE
                    display_name = VALUES(display_name),
                    replaces_phone = COALESCE(VALUES(replaces_phone), replaces_phone),
                    updated_by_staff_id = VALUES(updated_by_staff_id),
                    updated_at = UTC_TIMESTAMP(6)
                """,
                (normalized, name, replaces, staff_id, staff_id),
            )
            row = await self._fetchone("SELECT id FROM support_contacts WHERE normalized_phone = %s", (normalized,))
            if not row:
                raise ContactNotFoundError("Contact could not be saved.")
            saved_id = int(row["id"])
        return {
            "contact_key": f"manual:{saved_id}",
            "source": "manual",
            "display_name": name,
            "normalized_phone": normalized,
        }


customer_directory = CustomerDirectory()
