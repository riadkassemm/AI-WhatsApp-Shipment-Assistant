from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

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
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass
    fake_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules["openai"] = fake_openai

from app.config import settings
settings.redis_url = ""

from app.conversation_store import Conversation
from app.language import claim_greeting, resolution_message
from app.support_auth import SupportAgent
from app.support_service import SupportService
from app.support_ui import _HTML
import app.support_service as support_service_module


def _ticket(*, status: str = "NEW") -> dict[str, Any]:
    return {
        "id": 77,
        "ticket_reference": "CS-77",
        "status": status,
        "active_key": "123456789:96170000000" if status not in {"RESOLVED", "CLOSED"} else None,
        "current_assignee_id": "7",
        "current_assignee_name": "Agent A",
        "communication_style": "en",
        "whatsapp_phone": "96170000000",
        "business_phone_number_id": "123456789",
        "business_display_phone_number": "+961 1 234 567",
    }


def test_claim_uses_staff_custom_greeting(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent A", role="agent")
        seen: dict[str, Any] = {}

        async def fake_claim(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], bool]:
            return _ticket(), True

        async def fake_get_ticket(ticket_id: int) -> dict[str, Any]:
            assert ticket_id == 77
            return _ticket()

        async def fake_preferences(staff_id: int) -> dict[str, Any]:
            assert staff_id == 7
            return {"custom_greeting_message": "Hello from my custom greeting."}

        async def fake_send(**kwargs: Any) -> tuple[dict[str, Any], bool]:
            seen.update(kwargs)
            return {"id": 1, "body": kwargs["body"]}, True

        monkeypatch.setattr(support_service_module.support_repository, "claim_ticket", fake_claim)
        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(support_service_module.staff_repository, "get_message_preferences", fake_preferences)
        monkeypatch.setattr(service, "_send_system_customer_message", fake_send)

        await service.claim(77, agent)
        assert seen["body"] == "Hello from my custom greeting."

    asyncio.run(run())


def test_blank_greeting_uses_existing_language_aware_default(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent A", role="agent")
        ticket = _ticket()
        ticket["communication_style"] = "fr"
        seen: dict[str, Any] = {}

        async def fake_claim(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], bool]:
            return ticket, True

        async def fake_get_ticket(ticket_id: int) -> dict[str, Any]:
            assert ticket_id == 77
            return ticket

        async def fake_preferences(_staff_id: int) -> dict[str, Any]:
            return {"custom_greeting_message": "   "}

        async def fake_send(**kwargs: Any) -> tuple[dict[str, Any], bool]:
            seen.update(kwargs)
            return {}, True

        monkeypatch.setattr(support_service_module.support_repository, "claim_ticket", fake_claim)
        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(support_service_module.staff_repository, "get_message_preferences", fake_preferences)
        monkeypatch.setattr(service, "_send_system_customer_message", fake_send)

        await service.claim(77, agent)
        assert seen["body"] == claim_greeting("fr", "Agent A")

    asyncio.run(run())


def test_resolve_uses_resolving_staff_custom_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent A", role="agent")
        original = _ticket(status="IN_PROGRESS")
        resolved = {**original, "status": "RESOLVED", "active_key": None}
        seen: dict[str, Any] = {}

        async def fake_get_ticket(_ticket_id: int) -> dict[str, Any]:
            return original

        async def fake_resolve(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], bool]:
            return resolved, True

        async def fake_preferences(staff_id: int) -> dict[str, Any]:
            assert staff_id == 7
            return {"custom_resolution_message": "Thanks — this is all sorted now."}

        async def fake_send(**kwargs: Any) -> tuple[dict[str, Any], bool]:
            seen.update(kwargs)
            return {"id": 2, "body": kwargs["body"]}, True

        conversation = Conversation(phone_number=resolved["whatsapp_phone"])

        async def fake_get_or_create(*_args: Any, **_kwargs: Any) -> Conversation:
            return conversation

        async def fake_save(_conversation: Conversation) -> None:
            return None

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(support_service_module.support_repository, "resolve_ticket", fake_resolve)
        monkeypatch.setattr(support_service_module.staff_repository, "get_message_preferences", fake_preferences)
        monkeypatch.setattr(service, "_send_system_customer_message", fake_send)
        monkeypatch.setattr(support_service_module.conversation_store, "get_or_create", fake_get_or_create)
        monkeypatch.setattr(support_service_module.conversation_store, "save", fake_save)

        await service.resolve_and_return_to_ai(77, agent=agent)
        assert seen["body"] == "Thanks — this is all sorted now."
        assert conversation.mode == "ai"
        assert conversation.active_ticket_id is None

    asyncio.run(run())


def test_missing_preferences_fail_safe_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()

        async def broken_preferences(_staff_id: int) -> dict[str, Any]:
            raise RuntimeError("preference store unavailable")

        monkeypatch.setattr(support_service_module.staff_repository, "get_message_preferences", broken_preferences)
        default = resolution_message("en")
        result = await service._automatic_message_for_staff(
            staff_id="7",
            preference_key="custom_resolution_message",
            default_message=default,
        )
        assert result == default

    asyncio.run(run())


def test_preference_update_is_scoped_to_authenticated_staff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="14", name="Agent B", role="agent")
        seen: dict[str, Any] = {}

        async def fake_update(staff_id: int, **kwargs: Any) -> dict[str, Any]:
            seen["staff_id"] = staff_id
            seen.update(kwargs)
            return {
                "custom_greeting_message": kwargs["greeting_message"],
                "custom_resolution_message": kwargs["resolution_message"],
                "updated_at": None,
            }

        monkeypatch.setattr(support_service_module.staff_repository, "update_message_preferences", fake_update)
        result = await service.update_message_preferences(
            agent,
            greeting_message="  My greeting  ",
            resolution_message="   ",
        )
        assert seen == {
            "staff_id": 14,
            "greeting_message": "My greeting",
            "resolution_message": None,
        }
        assert result["greeting_message"] == "My greeting"
        assert result["resolution_message"] == ""

    asyncio.run(run())


def test_support_ui_exposes_per_staff_message_preferences() -> None:
    assert 'id="messagePreferencesBtn"' in _HTML
    assert 'id="customGreetingMessage"' in _HTML
    assert 'id="customResolutionMessage"' in _HTML
    assert "/api/support/message-preferences" in _HTML
    assert "Empty = existing language-aware default" in _HTML
    assert 'id="manageAiBtn"' not in _HTML
