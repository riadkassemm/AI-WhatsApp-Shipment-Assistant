"""AI ticket model (Step 3a): pure helpers, routes and queue wiring."""
from __future__ import annotations

import sys
import types

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

import asyncio
from pathlib import Path

import pytest

import app.support_api as support_api
import app.support_service as support_service_module
from app.config import Settings
from app.conversation_store import Conversation
from app.support_delivery_repository import _ai_may_send
from app.support_service import _ai_escalation_code, is_ai_ticket, support_service


def test_reason_codes_are_stable_identifiers() -> None:
    assert _ai_escalation_code("identity verification required") == "IDENTITY_VERIFICATION_REQUIRED"
    assert _ai_escalation_code("CHANGE_REQUIRES_HUMAN") == "CHANGE_REQUIRES_HUMAN"
    assert _ai_escalation_code("  -- ") == "AI_ESCALATED"
    assert _ai_escalation_code(None) == "AI_ESCALATED"
    assert len(_ai_escalation_code("x" * 300)) == 64


def test_is_ai_ticket_reads_the_handling_mode() -> None:
    assert is_ai_ticket({"handling_mode": "AI"})
    assert not is_ai_ticket({"handling_mode": "HUMAN"})
    assert not is_ai_ticket({})
    assert not is_ai_ticket(None)


def test_ai_send_fence_requires_an_active_ai_ticket() -> None:
    live = {"handling_mode": "AI", "active_key": "90005:1555", "status": "NEW"}
    assert _ai_may_send(live)
    assert not _ai_may_send({**live, "handling_mode": "HUMAN"})
    assert not _ai_may_send({**live, "active_key": None})
    assert not _ai_may_send({**live, "status": "RESOLVED"})


def test_take_over_route_and_ai_queue_view_are_registered() -> None:
    import app.main as main
    paths = main.app.openapi()["paths"]
    assert "post" in paths["/api/support/tickets/{ticket_id}/take-over"]
    source = Path("app/support_api.py").read_text()
    assert 'pattern="^(ai|unassigned|mine|watching|active|waiting|chats|history)$"' in source


def test_quiet_resolve_defaults_to_two_hours() -> None:
    assert Settings.model_fields["support_ai_ticket_quiet_resolve_minutes"].default == 120
    # Owner decision 2026-11-22: an unanswered AI question closes after 24 hours.
    assert Settings.model_fields["support_ai_ticket_unanswered_resolve_minutes"].default == 1440


def test_an_ai_ticket_keeps_the_conversation_in_ai_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[tuple[str, str | None]] = []

    class Cache:
        async def save(self, conversation: Conversation) -> None:
            saved.append((conversation.mode, conversation.active_ticket_id))

    monkeypatch.setattr(support_service_module, "conversation_store", Cache())
    conversation = Conversation(phone_number="1555", mode="human")
    asyncio.run(support_service.reconcile_conversation_mode(conversation, {"id": 5, "handling_mode": "AI"}))
    assert saved[-1] == ("ai", "5")
    asyncio.run(support_service.reconcile_conversation_mode(conversation, {"id": 5, "handling_mode": "HUMAN"}))
    assert saved[-1] == ("human", "5")


def test_queue_counts_include_the_ai_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.support_auth import SupportAgent
    seen: list[str] = []

    async def list_tickets(**kwargs):
        seen.append(kwargs["view"])
        return {"total": 3 if kwargs["view"] == "ai" else 0}

    async def count_contacts() -> int:
        return 0

    monkeypatch.setattr(support_service_module.support_repository, "list_tickets", list_tickets)
    monkeypatch.setattr(support_service_module.google_contacts_service, "count_contacts", count_contacts)
    counts = asyncio.run(support_service.queue_counts(SupportAgent(id="7", name="Agent", role="agent")))
    assert counts["ai"] == 3 and "ai" in seen
