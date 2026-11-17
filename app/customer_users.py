"""Read-only access to the optional live customer replica.

Replica IDs are namespaced: numeric IDs in the old support snapshot can identify
different customers. Never fall back to that snapshot after a replica failure.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiomysql

from app.config import settings


def customer_connection_options() -> dict[str, Any]:
    if settings.customer_users_replica_enabled:
        return {
            "host": settings.customer_users_db_host,
            "port": settings.customer_users_db_port,
            "db": settings.customer_users_db_name,
            "user": settings.customer_users_db_user,
            "password": settings.customer_users_db_password,
            "connect_timeout": 5,
            "autocommit": True,
            "charset": "utf8mb4",
        }
    return {
        "host": settings.db_host,
        "port": settings.db_port,
        "db": settings.db_name,
        "user": settings.db_user,
        "password": settings.db_password,
        "autocommit": True,
        "charset": "utf8mb4",
    }


def customer_identity_id(database_id: Any) -> str:
    value = str(database_id)
    return f"replica:{value}" if settings.customer_users_replica_enabled else value


def customer_identity_matches_source(customer_id: str) -> bool:
    value = str(customer_id)
    if settings.customer_users_replica_enabled:
        return value.startswith("replica:") and value[8:].isascii() and value[8:].isdigit()
    return not value.startswith("replica:")


class CustomerUsersRepository:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None
        self._pool_lock: asyncio.Lock | None = None

    async def _ensure_pool(self) -> aiomysql.Pool:
        if self._pool is None:
            if self._pool_lock is None:
                self._pool_lock = asyncio.Lock()
            async with self._pool_lock:
                if self._pool is None:
                    self._pool = await aiomysql.create_pool(
                        **customer_connection_options(), minsize=1, maxsize=6,
                    )
        return self._pool

    async def fetch_all(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        pool = await self._ensure_pool()
        async with pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(query, params)
                return list(await cursor.fetchall())

    async def get_by_identity(self, customer_id: str) -> dict[str, Any] | None:
        if not customer_identity_matches_source(customer_id):
            return None
        database_id = customer_id[8:] if settings.customer_users_replica_enabled else customer_id
        rows = await self.fetch_all(
            "SELECT id, uuid, userid, name, mobile, email FROM users WHERE id = %s LIMIT 1",
            (database_id,),
        )
        return rows[0] if rows else None

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
        self._pool_lock = None


customer_users = CustomerUsersRepository()
