from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import aiomysql

from app.config import settings


class StaffAlreadyExistsError(Exception):
    """Raised when a case-insensitive staff email is already assigned."""

    def __init__(self, message: str, *, existing: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.existing = existing


class StaffNotFoundError(Exception):
    pass


class StaffRepository:
    _PUBLIC_COLUMNS = (
        "id, name, email, email_normalized, role, is_active, "
        "last_login_at, created_at, updated_at"
    )

    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await aiomysql.create_pool(
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
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def _ensure_pool(self) -> aiomysql.Pool:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        return self._pool

    @staticmethod
    def _email_lock_key(email_normalized: str) -> str:
        digest = hashlib.sha256(email_normalized.encode("utf-8")).hexdigest()
        return f"support-staff-email:{digest[:40]}"

    @staticmethod
    def _account_lock_key(staff_id: int) -> str:
        return f"support-staff-account:{int(staff_id)}"

    @staticmethod
    async def _acquire_named_locks(cur: Any, keys: list[str]) -> list[str]:
        acquired: list[str] = []
        for key in sorted(set(keys)):
            await cur.execute("SELECT GET_LOCK(%s, 5) AS acquired", (key,))
            row = await cur.fetchone()
            result = (row or {}).get("acquired") if isinstance(row, dict) else None
            if result != 1:
                for held in reversed(acquired):
                    try:
                        await cur.execute("SELECT RELEASE_LOCK(%s)", (held,))
                        await cur.fetchone()
                    except Exception:
                        pass
                raise RuntimeError("Could not acquire the staff-account update lock.")
            acquired.append(key)
        return acquired

    @staticmethod
    async def _release_named_locks(cur: Any, keys: list[str]) -> None:
        for key in reversed(keys):
            try:
                await cur.execute("SELECT RELEASE_LOCK(%s)", (key,))
                await cur.fetchone()
            except Exception:
                # MariaDB also releases named locks when the connection closes.
                pass

    async def get_staff_by_email(self, email_normalized: str) -> dict[str, Any] | None:
        """Find canonical email, including legacy rows with stale/null normalization."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT id, name, email, email_normalized, password_hash, role,
                           is_active, last_login_at, failed_login_count, locked_until,
                           created_at, updated_at
                    FROM staff
                    WHERE email_normalized = %s
                       OR LOWER(TRIM(email)) = %s
                    ORDER BY id
                    LIMIT 1
                    """,
                    (email_normalized, email_normalized),
                )
                return await cur.fetchone()

    async def get_staff_by_id(self, staff_id: int) -> dict[str, Any] | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT id, name, email, email_normalized, password_hash, role,
                           is_active, last_login_at, failed_login_count, locked_until,
                           created_at, updated_at
                    FROM staff
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (int(staff_id),),
                )
                return await cur.fetchone()

    async def get_public_staff_by_id(self, staff_id: int) -> dict[str, Any] | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"SELECT {self._PUBLIC_COLUMNS} FROM staff WHERE id = %s LIMIT 1",
                    (int(staff_id),),
                )
                return await cur.fetchone()

    async def list_public_staff(self, *, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"""
                    SELECT {self._PUBLIC_COLUMNS}
                    FROM staff
                    ORDER BY is_active DESC, created_at DESC, id DESC
                    LIMIT {limit}
                    """
                )
                return list(await cur.fetchall())

    async def record_failed_login(self, staff_id: int) -> None:
        pool = await self._ensure_pool()
        threshold = max(1, int(settings.support_login_max_failures))
        lock_seconds = max(1, int(settings.support_login_lock_seconds))
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE staff
                    SET failed_login_count = failed_login_count + 1,
                        locked_until = CASE
                            WHEN failed_login_count + 1 >= %s
                            THEN DATE_ADD(UTC_TIMESTAMP(6), INTERVAL %s SECOND)
                            ELSE locked_until
                        END,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s
                    """,
                    (threshold, lock_seconds, staff_id),
                )

    async def create_session_and_mark_login(
        self,
        *,
        staff_id: int,
        token_hash: str,
        csrf_hash: str,
        expires_at: datetime,
    ) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE staff
                        SET failed_login_count = 0,
                            locked_until = NULL,
                            last_login_at = UTC_TIMESTAMP(6),
                            updated_at = UTC_TIMESTAMP(6)
                        WHERE id = %s AND is_active = 1
                        """,
                        (staff_id,),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError("Staff account became unavailable during login.")
                    await cur.execute(
                        """
                        INSERT INTO staff_sessions (
                            staff_id, token_hash, csrf_hash, expires_at,
                            created_at, last_seen_at
                        ) VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                        """,
                        (
                            staff_id,
                            token_hash,
                            csrf_hash,
                            expires_at.astimezone(timezone.utc).replace(tzinfo=None),
                        ),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def get_active_session(self, token_hash: str) -> dict[str, Any] | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT
                        s.id AS session_id,
                        s.staff_id,
                        s.csrf_hash,
                        s.expires_at,
                        st.name,
                        st.email,
                        st.role,
                        st.is_active
                    FROM staff_sessions s
                    INNER JOIN staff st ON st.id = s.staff_id
                    WHERE s.token_hash = %s
                      AND s.revoked_at IS NULL
                      AND s.expires_at > UTC_TIMESTAMP(6)
                      AND st.is_active = 1
                    LIMIT 1
                    """,
                    (token_hash,),
                )
                return await cur.fetchone()

    async def touch_session(self, session_id: int) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE staff_sessions
                    SET last_seen_at = UTC_TIMESTAMP(6)
                    WHERE id = %s AND revoked_at IS NULL
                    """,
                    (session_id,),
                )

    async def revoke_session(self, token_hash: str) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE staff_sessions
                    SET revoked_at = COALESCE(revoked_at, UTC_TIMESTAMP(6))
                    WHERE token_hash = %s
                    """,
                    (token_hash,),
                )

    async def revoke_all_staff_sessions(self, staff_id: int) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE staff_sessions
                    SET revoked_at = COALESCE(revoked_at, UTC_TIMESTAMP(6))
                    WHERE staff_id = %s AND revoked_at IS NULL
                    """,
                    (int(staff_id),),
                )

    async def _find_email_conflict(
        self,
        cur: Any,
        email_normalized: str,
        *,
        excluding_staff_id: int | None = None,
    ) -> dict[str, Any] | None:
        exclusion = "" if excluding_staff_id is None else "AND id <> %s"
        params: list[Any] = [email_normalized, email_normalized]
        if excluding_staff_id is not None:
            params.append(int(excluding_staff_id))
        await cur.execute(
            f"""
            SELECT {self._PUBLIC_COLUMNS}
            FROM staff
            WHERE (email_normalized = %s OR LOWER(TRIM(email)) = %s)
              {exclusion}
            ORDER BY id
            LIMIT 1
            """,
            tuple(params),
        )
        return await cur.fetchone()

    async def create_staff(
        self,
        *,
        name: str,
        email: str,
        email_normalized: str,
        password_hash: str,
        role: str,
    ) -> int:
        """Create once under a canonical-email lock and database unique key."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                locks = await self._acquire_named_locks(
                    cur, [self._email_lock_key(email_normalized)]
                )
                try:
                    existing = await self._find_email_conflict(cur, email_normalized)
                    if existing:
                        raise StaffAlreadyExistsError(
                            "A staff account with that email already exists.",
                            existing=existing,
                        )
                    try:
                        await cur.execute(
                            """
                            INSERT INTO staff (
                                name, email, email_normalized, password_hash, role,
                                is_active, created_at, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                            """,
                            (name, email, email_normalized, password_hash, role),
                        )
                    except aiomysql.IntegrityError as exc:
                        existing = await self._find_email_conflict(cur, email_normalized)
                        raise StaffAlreadyExistsError(
                            "A staff account with that email already exists.",
                            existing=existing,
                        ) from exc
                    return int(cur.lastrowid)
                finally:
                    await self._release_named_locks(cur, locks)

    async def update_staff(
        self,
        staff_id: int,
        *,
        name: str | None = None,
        email: str | None = None,
        email_normalized: str | None = None,
        password_hash: str | None = None,
        role: str | None = None,
        is_active: bool | None = None,
        revoke_sessions: bool = False,
    ) -> dict[str, Any]:
        pool = await self._ensure_pool()
        lock_keys = [self._account_lock_key(int(staff_id))]
        if email_normalized is not None:
            lock_keys.append(self._email_lock_key(email_normalized))

        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                locks = await self._acquire_named_locks(cur, lock_keys)
                try:
                    await conn.begin()
                    await cur.execute(
                        """
                        SELECT id, name, email, email_normalized, password_hash, role,
                               is_active, last_login_at, failed_login_count, locked_until,
                               created_at, updated_at
                        FROM staff
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (int(staff_id),),
                    )
                    current = await cur.fetchone()
                    if not current:
                        raise StaffNotFoundError("Staff account not found.")

                    if email_normalized is not None:
                        conflict = await self._find_email_conflict(
                            cur,
                            email_normalized,
                            excluding_staff_id=int(staff_id),
                        )
                        if conflict:
                            raise StaffAlreadyExistsError(
                                "A staff account with that email already exists.",
                                existing=conflict,
                            )

                    assignments: list[str] = []
                    params: list[Any] = []
                    if name is not None:
                        assignments.append("name = %s")
                        params.append(name)
                    if email is not None and email_normalized is not None:
                        assignments.extend(["email = %s", "email_normalized = %s"])
                        params.extend([email, email_normalized])
                    if password_hash is not None:
                        assignments.extend(
                            [
                                "password_hash = %s",
                                "failed_login_count = 0",
                                "locked_until = NULL",
                            ]
                        )
                        params.append(password_hash)
                    if role is not None:
                        assignments.append("role = %s")
                        params.append(role)
                    if is_active is not None:
                        assignments.append("is_active = %s")
                        params.append(1 if is_active else 0)
                        if is_active:
                            assignments.extend(
                                ["failed_login_count = 0", "locked_until = NULL"]
                            )

                    if assignments:
                        assignments.append("updated_at = UTC_TIMESTAMP(6)")
                        params.append(int(staff_id))
                        try:
                            await cur.execute(
                                f"UPDATE staff SET {', '.join(assignments)} WHERE id = %s",
                                tuple(params),
                            )
                        except aiomysql.IntegrityError as exc:
                            conflict = None
                            if email_normalized is not None:
                                conflict = await self._find_email_conflict(
                                    cur,
                                    email_normalized,
                                    excluding_staff_id=int(staff_id),
                                )
                            raise StaffAlreadyExistsError(
                                "A staff account with that email already exists.",
                                existing=conflict,
                            ) from exc

                    if revoke_sessions:
                        await cur.execute(
                            """
                            UPDATE staff_sessions
                            SET revoked_at = COALESCE(revoked_at, UTC_TIMESTAMP(6))
                            WHERE staff_id = %s AND revoked_at IS NULL
                            """,
                            (int(staff_id),),
                        )
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
                finally:
                    await self._release_named_locks(cur, locks)

        updated = await self.get_public_staff_by_id(int(staff_id))
        if not updated:
            raise StaffNotFoundError("Staff account not found after update.")
        return updated

    async def soft_delete_staff(self, staff_id: int) -> dict[str, Any]:
        """Deactivate and revoke sessions while preserving ticket/audit references."""
        return await self.update_staff(
            int(staff_id),
            is_active=False,
            revoke_sessions=True,
        )


staff_repository = StaffRepository()
