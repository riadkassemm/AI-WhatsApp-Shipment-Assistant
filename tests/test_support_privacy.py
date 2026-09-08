from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

# Lightweight stubs for production-only dependencies unavailable in the artifact runtime.
fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {})
)
sys.modules["aiomysql"] = fake_aiomysql

fake_bcrypt = sys.modules.get("bcrypt") or types.ModuleType("bcrypt")
fake_bcrypt.checkpw = getattr(fake_bcrypt, "checkpw", lambda *_args, **_kwargs: False)
fake_bcrypt.hashpw = getattr(fake_bcrypt, "hashpw", lambda *_args, **_kwargs: b"hash")
fake_bcrypt.gensalt = getattr(fake_bcrypt, "gensalt", lambda *_args, **_kwargs: b"salt")
sys.modules["bcrypt"] = fake_bcrypt

fake_openai = sys.modules.get("openai") or types.ModuleType("openai")
if not hasattr(fake_openai, "AsyncOpenAI"):
    class _AsyncOpenAI:
        def __init__(self, *_args, **_kwargs) -> None:
            pass
    fake_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules["openai"] = fake_openai

from app.support_auth import SupportAgent
from app.support_repository import SupportRepository
from app.support_service import SupportForbiddenError, SupportService
from app.support_ui import _HTML
from app.main import _disable_support_response_caching
import app.support_service as support_service_module


def _ticket(*, assignee: str | None = "9", status: str = "IN_PROGRESS") -> dict[str, Any]:
    return {
        "id": 42,
        "ticket_reference": "SUP-42",
        "active_key": "business-a:96170123456" if status not in {"RESOLVED", "CLOSED"} else None,
        "status": status,
        "current_assignee_id": assignee,
        "current_assignee_name": "Agent B" if assignee else None,
        "context_json": "{}",
        "customer_id": None,
        "tracking_reference": None,
    }


def test_regular_agent_cannot_open_unrelated_active_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent A", role="agent")
        calls = {"messages": 0, "events": 0}

        async def fake_get_ticket(ticket_id: int) -> dict[str, Any]:
            assert ticket_id == 42
            return _ticket(assignee="9")

        async def fake_participated(ticket_id: int, agent_id: str) -> bool:
            assert ticket_id == 42
            assert agent_id == "7"
            return False

        async def fake_messages(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            calls["messages"] += 1
            return []

        async def fake_events(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            calls["events"] += 1
            return []

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "agent_has_participated",
            fake_participated,
        )
        monkeypatch.setattr(support_service_module.support_repository, "list_messages", fake_messages)
        monkeypatch.setattr(support_service_module.support_repository, "list_events", fake_events)

        with pytest.raises(SupportForbiddenError, match="tickets you have worked on"):
            await service.ticket_detail(42, agent)
        assert calls == {"messages": 0, "events": 0}

    asyncio.run(run())


def test_regular_agent_cannot_fetch_unrelated_ticket_media(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent A", role="agent")

        async def fake_get_ticket(_ticket_id: int) -> dict[str, Any]:
            return _ticket(assignee="9")

        async def fake_participated(_ticket_id: int, _agent_id: str) -> bool:
            return False

        async def should_not_load_message(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("media metadata must not be loaded before ticket authorization")

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "agent_has_participated",
            fake_participated,
        )
        monkeypatch.setattr(
            support_service_module.support_repository,
            "get_message",
            should_not_load_message,
        )

        with pytest.raises(SupportForbiddenError, match="media from tickets you have worked on"):
            await service.get_ticket_media(42, 123, agent)

    asyncio.run(run())


def test_previous_participant_can_keep_watching_transferred_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent A", role="agent")

        async def fake_get_ticket(_ticket_id: int) -> dict[str, Any]:
            return _ticket(assignee="9")

        async def fake_participated(_ticket_id: int, agent_id: str) -> bool:
            return agent_id == "7"

        async def fake_rows(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "agent_has_participated",
            fake_participated,
        )
        monkeypatch.setattr(support_service_module.support_repository, "list_messages", fake_rows)
        monkeypatch.setattr(support_service_module.support_repository, "list_events", fake_rows)

        detail = await service.ticket_detail(42, agent)
        assert detail["ticket"]["id"] == 42
        assert detail["permissions"]["is_read_only_observer"] is True
        assert detail["permissions"]["can_reply"] is False

    asyncio.run(run())


def test_unassigned_ticket_remains_visible_for_claiming(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent A", role="agent")

        async def fake_get_ticket(_ticket_id: int) -> dict[str, Any]:
            return _ticket(assignee=None, status="NEW")

        async def should_not_check_participation(*_args: Any, **_kwargs: Any) -> bool:
            raise AssertionError("claimable unassigned tickets must not require prior participation")

        async def fake_rows(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "agent_has_participated",
            should_not_check_participation,
        )
        monkeypatch.setattr(support_service_module.support_repository, "list_messages", fake_rows)
        monkeypatch.setattr(support_service_module.support_repository, "list_events", fake_rows)

        detail = await service.ticket_detail(42, agent)
        assert detail["permissions"]["can_claim"] is True

    asyncio.run(run())


def test_supervisor_keeps_global_ticket_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        supervisor = SupportAgent(id="1", name="Supervisor", role="supervisor")

        async def fake_get_ticket(_ticket_id: int) -> dict[str, Any]:
            return _ticket(assignee="9")

        async def should_not_check_participation(*_args: Any, **_kwargs: Any) -> bool:
            raise AssertionError("supervisors must bypass agent participation checks")

        async def fake_rows(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "agent_has_participated",
            should_not_check_participation,
        )
        monkeypatch.setattr(support_service_module.support_repository, "list_messages", fake_rows)
        monkeypatch.setattr(support_service_module.support_repository, "list_events", fake_rows)

        detail = await service.ticket_detail(42, supervisor)
        assert detail["ticket"]["id"] == 42
        assert detail["permissions"]["is_supervisor"] is True

    asyncio.run(run())


def test_list_service_passes_supervisor_visibility_to_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        seen: list[dict[str, Any]] = []

        async def fake_list_tickets(**kwargs: Any) -> dict[str, Any]:
            seen.append(dict(kwargs))
            return {"items": [], "page": 1, "page_size": 30, "total": 0}

        monkeypatch.setattr(support_service_module.support_repository, "list_tickets", fake_list_tickets)
        await service.list_tickets(
            view="history",
            agent=SupportAgent(id="7", name="Agent", role="agent"),
            page=1,
            page_size=30,
        )
        await service.list_tickets(
            view="history",
            agent=SupportAgent(id="1", name="Admin", role="admin"),
            page=1,
            page_size=30,
        )
        assert seen[0]["can_view_all"] is False
        assert seen[1]["can_view_all"] is True

    asyncio.run(run())


class _FakeCursor:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.calls.append((sql, tuple(params)))

    async def fetchone(self) -> dict[str, Any]:
        return {"total": 0}

    async def fetchall(self) -> list[dict[str, Any]]:
        return []


class _FakeConnection:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls

    async def __aenter__(self) -> "_FakeConnection":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def cursor(self, *_args: Any, **_kwargs: Any) -> _FakeCursor:
        return _FakeCursor(self.calls)


class _AcquireContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self.connection

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakePool:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.connection = _FakeConnection(calls)

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self.connection)


def test_regular_agent_history_and_active_queries_are_participation_scoped() -> None:
    async def run() -> None:
        for view in ("history", "active", "waiting"):
            calls: list[tuple[str, tuple[Any, ...]]] = []
            repository = SupportRepository()
            repository._pool = _FakePool(calls)  # type: ignore[assignment]
            await repository.list_tickets(
                view=view,
                agent_id="7",
                can_view_all=False,
                page=1,
                page_size=30,
            )
            assert len(calls) == 2
            for sql, params in calls:
                assert "TICKET_CLAIMED" in sql
                assert "$.from_agent_id" in sql
                assert "$.to_agent_id" in sql
                assert params.count("7") >= 4

    asyncio.run(run())


def test_supervisor_history_query_remains_global() -> None:
    async def run() -> None:
        calls: list[tuple[str, tuple[Any, ...]]] = []
        repository = SupportRepository()
        repository._pool = _FakePool(calls)  # type: ignore[assignment]
        await repository.list_tickets(
            view="history",
            agent_id="1",
            can_view_all=True,
            page=1,
            page_size=30,
        )
        assert len(calls) == 2
        for sql, _params in calls:
            assert "TICKET_CLAIMED" not in sql
            assert "$.to_agent_id" not in sql

    asyncio.run(run())


def test_dashboard_clears_ticket_panel_when_server_denies_private_ticket() -> None:
    assert "Conversation access is restricted to participating agents." in _HTML
    assert "You can only open tickets you have claimed or worked on." in _HTML
    assert "if(e.status===403)" in _HTML


def test_support_responses_are_marked_no_store() -> None:
    async def run() -> None:
        from fastapi import Response

        request = types.SimpleNamespace(url=types.SimpleNamespace(path="/api/support/tickets/42"))

        async def call_next(_request: Any) -> Response:
            return Response(content="ok")

        response = await _disable_support_response_caching(request, call_next)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"

    asyncio.run(run())


def test_dashboard_wipes_previous_agent_workspace_and_disables_fetch_cache() -> None:
    assert "function clearTicketWorkspace()" in _HTML
    assert "Ticket conversation will appear here." in _HTML
    assert "clearTicketWorkspace();$('dashboard').classList.add('hidden')" in _HTML
    assert "cache:'no-store'" in _HTML
