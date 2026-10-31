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

from app.config import settings

settings.redis_url = ""

from app.support_auth import SupportAgent
from app.support_repository import SupportConflictError
from app.support_service import SupportForbiddenError, SupportService
from app.support_ui import _HTML
import app.support_service as support_service_module


def _history_ticket(status: str = "RESOLVED") -> dict[str, Any]:
    return {
        "id": 52,
        "ticket_reference": "CS-52",
        "status": status,
        "active_key": None,
        "current_assignee_id": "7",
        "current_assignee_name": "Agent A",
        "context_json": "{}",
        "customer_id": None,
        "tracking_reference": None,
    }


def test_agent_cannot_delete_history_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent A", role="agent")
        called = {"delete": 0}

        async def should_not_get(_ticket_id: int) -> dict[str, Any]:
            raise AssertionError("agent authorization should fail before loading the ticket")

        async def should_not_delete(_ticket_id: int) -> dict[str, Any]:
            called["delete"] += 1
            return _history_ticket()

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", should_not_get)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "delete_history_ticket",
            should_not_delete,
        )

        with pytest.raises(SupportForbiddenError, match="Super Admins and Admins"):
            await service.delete_history_ticket(52, agent=agent)
        assert called["delete"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("role", ["supervisor", "admin"])
def test_manager_can_delete_resolved_history_ticket(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="2", name="Manager", role=role)
        seen: dict[str, Any] = {}

        async def fake_get_ticket(ticket_id: int) -> dict[str, Any]:
            assert ticket_id == 52
            return _history_ticket()

        async def fake_delete(ticket_id: int, **_actor) -> dict[str, Any]:
            seen["ticket_id"] = ticket_id
            return _history_ticket()

        def fake_delete_media(ticket_id: int) -> None:
            seen["media_ticket_id"] = ticket_id

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "delete_history_ticket",
            fake_delete,
        )
        monkeypatch.setattr(
            support_service_module.support_media_store,
            "delete_ticket_media",
            fake_delete_media,
        )

        result = await service.delete_history_ticket(52, agent=agent)
        assert result == {
            "deleted": True,
            "ticket_id": 52,
            "ticket_reference": "CS-52",
        }
        assert seen == {"ticket_id": 52, "media_ticket_id": 52}

    asyncio.run(run())


def test_manager_cannot_delete_active_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        supervisor = SupportAgent(id="2", name="Supervisor", role="supervisor")
        active = _history_ticket(status="IN_PROGRESS")
        active["active_key"] = "business-a:96170123456"

        async def fake_get_ticket(_ticket_id: int) -> dict[str, Any]:
            return active

        async def should_not_delete(_ticket_id: int) -> dict[str, Any]:
            raise AssertionError("active tickets must never be deleted")

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "delete_history_ticket",
            should_not_delete,
        )

        with pytest.raises(SupportConflictError, match="resolved or closed history"):
            await service.delete_history_ticket(52, agent=supervisor)

    asyncio.run(run())


def test_history_delete_permission_is_exposed_only_to_managers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        service = SupportService()

        async def fake_get_ticket(_ticket_id: int) -> dict[str, Any]:
            return _history_ticket()

        async def fake_rows(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

        async def participated(_ticket_id: int, _agent_id: str) -> bool:
            return True

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(support_service_module.support_repository, "list_messages", fake_rows)
        monkeypatch.setattr(support_service_module.support_repository, "list_events", fake_rows)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "agent_has_participated",
            participated,
        )

        agent_detail = await service.ticket_detail(
            52, SupportAgent(id="7", name="Agent", role="agent")
        )
        supervisor_detail = await service.ticket_detail(
            52, SupportAgent(id="2", name="Supervisor", role="supervisor")
        )
        admin_detail = await service.ticket_detail(
            52, SupportAgent(id="1", name="Admin", role="admin")
        )

        assert agent_detail["permissions"]["can_delete_history"] is False
        assert supervisor_detail["permissions"]["can_delete_history"] is True
        assert admin_detail["permissions"]["can_delete_history"] is True

    asyncio.run(run())


def test_support_ui_delete_action_is_permission_driven() -> None:
    assert "permissions.can_delete_history" in _HTML
    assert "Delete ticket" in _HTML
    assert "method:'DELETE'" in _HTML
    assert "This action cannot be undone" in _HTML
