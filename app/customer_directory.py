from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
from typing import Any

import aiomysql

from app.config import settings


_PHONE_NON_DIGIT = re.compile(r"\D+")


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

    async def resolve(self, phone: str) -> CustomerIdentity:
        normalized = normalize_phone(phone)
        # A registered user wins over a saved contact; look both up at once so
        # opening a conversation waits for one lookup instead of two in a row.
        user, contact = await asyncio.gather(
            self._registered_user(normalized),
            self._saved_contact(normalized),
        )
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


customer_directory = CustomerDirectory()
