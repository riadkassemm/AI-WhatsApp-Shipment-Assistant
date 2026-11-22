"""Automated turns on AI tickets (Step 3b): opening the ticket and fenced sends."""
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
import json

import pytest

import app.main as main
from app.client_chat_auth import extract_credentials
from app.conversation_store import Conversation
from app.support_repository import SupportConflictError
from app.whatsapp_client import IncomingWhatsAppMessage, WhatsAppSendError


def _incoming() -> IncomingWhatsAppMessage:
    return IncomingWhatsAppMessage("1555", "wamid.1", "text", "hello", "1", business_phone_number_id="90005")


def _open(monkeypatch: pytest.MonkeyPatch, *, opened=None, record=None):
    recorded: list[dict] = []

    async def open_ai_ticket(**_kwargs):
        if isinstance(opened, Exception):
            raise opened
        return opened, True

    async def route_human_inbound(**kwargs):
        if record is not None:
            raise record
        recorded.append(kwargs)
        return {"id": 1}, True

    async def reconcile(_conversation, _ticket):
        return None

    monkeypatch.setattr(main.support_service, "open_ai_ticket", open_ai_ticket)
    monkeypatch.setattr(main.support_service, "route_human_inbound", route_human_inbound)
    monkeypatch.setattr(main.support_service, "reconcile_conversation_mode", reconcile)
    conversation = Conversation(phone_number="1555", business_phone_number_id="90005")
    result = asyncio.run(main._ai_ticket_for_turn(
        conversation=conversation, incoming=_incoming(),
        parsed_auth=extract_credentials("hello", auth_state="guest", pending_username=None, has_pending_password=False),
        ai_ticket=None,
    ))
    return result, recorded


def _status(response) -> tuple[int, str]:
    return response.status_code, json.loads(response.body)["status"]


def test_ticket_is_opened_and_the_message_stored_before_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    (ticket, response), recorded = _open(monkeypatch, opened={"id": 9, "handling_mode": "AI"})
    assert response is None and ticket["id"] == 9
    assert recorded[0]["whatsapp_message_id"] == "wamid.1" and recorded[0]["body"] == "hello"


def test_a_human_ticket_that_won_the_race_keeps_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    (ticket, response), recorded = _open(monkeypatch, opened={"id": 9, "handling_mode": "HUMAN"})
    assert ticket is None and _status(response) == (503, "support_state_changed") and recorded == []


def test_a_changed_ticket_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    (ticket, response), _ = _open(monkeypatch, opened={"id": 9, "handling_mode": "AI"},
                                  record=SupportConflictError("Ticket is no longer active."))
    assert ticket is None and _status(response) == (503, "support_state_changed")


def test_an_unavailable_database_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    (ticket, response), _ = _open(monkeypatch, opened=RuntimeError("db down"))
    assert ticket is None and _status(response) == (503, "support_state_unavailable")


def _send(monkeypatch: pytest.MonkeyPatch, outcome):
    async def send_ai_reply(_ticket, **_kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def not_revoked(_message_id):
        return False

    monkeypatch.setattr(main.support_service, "send_ai_reply", send_ai_reply)
    monkeypatch.setattr(main, "_is_inbound_revoked", not_revoked)
    turn = {"line_id": "90005", "suppressed": None, "ticket": {"id": 9}}
    token = main._automated_turn.set(turn)
    try:
        return asyncio.run(main._send_ai_ticket_reply({"id": 9}, body="hi", source_message_id="wamid.1")), turn
    finally:
        main._automated_turn.reset(token)


def test_a_sent_ai_reply_reports_its_provider_id(monkeypatch: pytest.MonkeyPatch) -> None:
    response, turn = _send(monkeypatch, {"send_status": "SENT", "whatsapp_message_id": "wamid.out"})
    assert response == {"messages": [{"id": "wamid.out"}]} and turn["suppressed"] is None


def test_a_fenced_ai_reply_suppresses_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(main.AutomatedReplySuppressed):
        _send(monkeypatch, SupportConflictError("no longer AI"))


@pytest.mark.parametrize(("status", "uncertain"), [("FAILED", False), ("UNCERTAIN", True)])
def test_an_unsent_ai_reply_fails_the_turn(monkeypatch: pytest.MonkeyPatch, status: str, uncertain: bool) -> None:
    with pytest.raises(WhatsAppSendError) as caught:
        _send(monkeypatch, {"send_status": status})
    assert caught.value.delivery_uncertain is uncertain
