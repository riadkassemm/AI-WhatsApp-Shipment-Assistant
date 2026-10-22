from __future__ import annotations

import asyncio
import os
from pathlib import Path

os.environ.setdefault("REDIS_URL", "")

from app import staff_management
from app.staff_repository import StaffNotFoundError, StaffRepository
from app.support_auth import SupportAgent
from app.support_ui import _HTML


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self._result = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        self.conn.statements.append((normalized, params))
        if "GET_LOCK" in normalized:
            self._result = {"acquired": 1}
        elif normalized.startswith("SELECT id, name, email"):

            self._result = {"id": params[0]} if self.conn.row_exists else None
        else:
            self._result = None

    async def fetchall(self):
        return []

    async def fetchone(self):
        return self._result


class FakeConnection:
    def __init__(self, *, row_exists: bool = True) -> None:
        self.row_exists = row_exists
        self.statements: list[tuple[str, object]] = []
        self.committed = False
        self.rolled_back = False

    def cursor(self, *_args):
        return FakeCursor(self)

    async def begin(self) -> None:
        pass

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *_exc):
                return False

        return _Acquire()


def test_permanent_delete_removes_account_and_personal_records() -> None:
    async def run() -> None:
        conn = FakeConnection()
        repo = StaffRepository()
        repo._pool = FakePool(conn)  # type: ignore[assignment]

        async def reload(staff_id: int):
            return {"id": staff_id, "name": "Agent", "role": "agent", "is_active": 0}

        repo.get_public_staff_by_id = reload  # type: ignore[method-assign]
        deleted = await repo.delete_staff(12)

        sql = [statement for statement, _ in conn.statements]
        assert "DELETE FROM staff WHERE id = %s" in sql
        for table in ("staff_sessions", "support_quick_replies", "support_agent_heartbeats", "support_agent_activity"):
            assert f"DELETE FROM {table} WHERE staff_id = %s" in sql
        assert not any(s.startswith("UPDATE staff SET") for s in sql)
        assert any("deleted_at IS NULL FOR UPDATE" in s for s in sql)
        assert conn.committed and not conn.rolled_back
        assert deleted["id"] == 12

    asyncio.run(run())


def test_delete_of_missing_or_already_deleted_row_is_not_found() -> None:
    async def run() -> None:
        conn = FakeConnection(row_exists=False)
        repo = StaffRepository()
        repo._pool = FakePool(conn)  # type: ignore[assignment]
        try:
            await repo.delete_staff(12)
        except StaffNotFoundError:
            pass
        else:  # pragma: no cover - assertion path
            raise AssertionError("expected StaffNotFoundError")
        assert conn.rolled_back
        assert not any(s.startswith("UPDATE") for s, _ in conn.statements)

    asyncio.run(run())


def test_deleted_staff_are_excluded_from_lookup_listing_and_editing() -> None:
    import inspect

    for method in (
        StaffRepository.get_staff_by_email,
        StaffRepository.get_staff_by_id,
        StaffRepository.list_public_staff,
        StaffRepository._find_email_conflict,
        StaffRepository.update_staff,
    ):
        assert "deleted_at IS NULL" in inspect.getsource(method), method.__name__


def test_inactive_staff_account_can_be_deleted() -> None:
    async def run() -> None:
        actor = SupportAgent(id="1", name="Supervisor", role="supervisor")
        target = {
            "id": 7, "name": "Former Agent", "email": "former@example.com",
            "role": "agent", "is_active": 0,
        }
        calls: list[int] = []
        repo = staff_management.staff_repository
        original_get = repo.get_staff_by_id
        original_delete = repo.delete_staff

        async def get_target(_staff_id: int):
            return target

        async def delete_target(staff_id: int):
            calls.append(staff_id)
            return {**target}

        repo.get_staff_by_id = get_target  # type: ignore[method-assign]
        repo.delete_staff = delete_target  # type: ignore[method-assign]
        try:
            deleted = await staff_management.delete_staff_account(actor, 7)
        finally:
            repo.get_staff_by_id = original_get  # type: ignore[method-assign]
            repo.delete_staff = original_delete  # type: ignore[method-assign]

        assert calls == [7]
        assert deleted["deleted"] is True
        assert deleted["already_inactive"] is True
        assert deleted["is_active"] is False

    asyncio.run(run())


def test_staff_deletion_migration_adds_tombstone_column() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "20261017_staff_account_deletion.sql"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS deleted_at DATETIME(6) NULL" in migration


def test_staff_list_offers_delete_for_inactive_accounts_but_not_self() -> None:
    render = _HTML[_HTML.index("function renderStaffAccounts("):_HTML.index("function renderStaffLoading(")]
    assert "Edit / reactivate" not in render
    assert "if (!isSelf) {" in render
    assert "item.is_active && String(item.id) !== String(state.agent?.id)" not in render
    assert "notify('Staff account deleted.');" in _HTML
