from __future__ import annotations

import asyncmy
from asyncmy.cursors import DictCursor
from asyncmy.pool import Pool

from app.config import settings


class Database:
    def __init__(self) -> None:
        self.pool: Pool | None = None

    async def connect(self) -> None:
        if self.pool is not None:
            return

        self.pool = await asyncmy.create_pool(
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

    async def close(self) -> None:
        if self.pool is not None:
            self.pool.close()
            await self.pool.wait_closed()
            self.pool = None

    async def fetch_one(
        self,
        query: str,
        params: tuple | list = (),
    ) -> dict | None:
        if self.pool is None:
            await self.connect()

        assert self.pool is not None

        async with self.pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cursor:
                await cursor.execute(query, params)
                return await cursor.fetchone()

    async def fetch_all(
        self,
        query: str,
        params: tuple | list = (),
    ) -> list[dict]:
        if self.pool is None:
            await self.connect()

        assert self.pool is not None

        async with self.pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cursor:
                await cursor.execute(query, params)
                return await cursor.fetchall()

    async def execute(
        self,
        query: str,
        params: tuple | list = (),
    ) -> int:
        if self.pool is None:
            await self.connect()

        assert self.pool is not None

        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, params)
                return cursor.rowcount


db = Database()