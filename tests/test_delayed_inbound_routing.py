"""Delayed customer messages: recorded in the ticket open at send time, never sent to AI.

Drives the real inbound routing with an in-memory conversation store and faked
support-service calls. Real MariaDB coverage is in
tests/integration/test_delayed_inbound_routing.py.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from datetime import datetime, timezone

fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {}))
sys.modules["aiomysql"] = fake_aiomysql
fake_bcrypt = types.ModuleType("bcrypt")
fake_bcrypt.checkpw = lambda *_args, **_kwargs: False
fake_bcrypt.hashpw = lambda *_args, **_kwargs: b"hash"
fake_bcrypt.gensalt = lambda *_args, **_kwargs: b"salt"
sys.modules.setdefault("bcrypt", fake_bcrypt)
fake_openai = types.ModuleType("openai")
fake_openai.AsyncOpenAI = type("AsyncOpenAI", (), {"__init__": lambda self, *a, **k: None})
sys.modules.setdefault("openai", fake_openai)

import pytest

from app import main
from app.conversation_store import InMemoryConversationStore
from app.support_media import SupportMediaError
from app.support_repository import SupportConflictError
from app.support_service import SupportService
from app.whatsapp_client import IncomingWhatsAppMessage

NOW = 1_800_000_000
PHONE = "96170123456"
LINE = "90015"
CLOSED = {"id": 7, "whatsapp_phone": PHONE, "business_phone_number_id": LINE, "status": "RESOLVED",
          "active_key": None, "current_assignee_id": "3", "customer_name": "Customer"}
ACTIVE = {**CLOSED, "id": 8, "status": "WAITING_CUSTOMER", "active_key": f"{LINE}:{PHONE}"}


def message(age_seconds: int | None, *, message_id="ld:90015:LATE", kind="text", media_id=None, line=LINE):
    timestamp = None if age_seconds is None else str(NOW - age_seconds)
    return IncomingWhatsAppMessage(
        PHONE, message_id, kind, "late text" if kind == "text" else None, timestamp,
        business_phone_number_id=line, media_id=media_id,
        media_mime_type="image/jpeg" if media_id else None,
    )


@pytest.fixture
def harness(monkeypatch):
    """Fresh store, frozen clock, recorded support calls and trapped AI/sends."""
    calls: dict[str, list] = {"lookup": [], "route": [], "media": [], "ai": [], "send": [], "active": []}
    state = {"match": (CLOSED, "matched"), "existing": None, "route_errors": [], "media_error": None, "active_ticket": None}
    monkeypatch.setattr(main.time, "time", lambda: float(NOW))
    monkeypatch.setattr(main, "conversation_store", InMemoryConversationStore())
    monkeypatch.setattr(main.settings, "whatsapp_max_inbound_age_seconds", 900)

    async def lookup(**kwargs):
        calls["lookup"].append(kwargs)
        if isinstance(state["match"], Exception):
            raise state["match"]
        return state["match"]

    async def existing(_message_id):
        return state["existing"]

    async def route(**kwargs):
        calls["route"].append(kwargs)
        if state["route_errors"]:
            raise state["route_errors"].pop(0)
        return {"id": 500 + len(calls["route"]), "ticket_id": kwargs["ticket"]["id"]}, True

    async def media(**kwargs):
        calls["media"].append(kwargs)
        if state["media_error"]:
            raise state["media_error"]

    async def active_ticket(*_args, **_kwargs):
        calls["active"].append(True)
        return state["active_ticket"]

    async def no_ai(*args, **kwargs):
        calls["ai"].append(True)
        raise AssertionError("delayed messages must never reach the AI")

    async def no_send(*args, **kwargs):
        calls["send"].append(True)
        raise AssertionError("delayed messages must never trigger an outgoing message")

    monkeypatch.setattr(main.support_service, "ticket_for_delayed_inbound", lookup)
    monkeypatch.setattr(main.support_service, "get_message_by_whatsapp_id", existing)
    monkeypatch.setattr(main.support_service, "route_human_inbound", route)
    monkeypatch.setattr(main.support_service, "capture_inbound_media", media)
    monkeypatch.setattr(main.support_service, "get_active_ticket", active_ticket)
    monkeypatch.setattr(main, "get_ai_reply", no_ai)
    monkeypatch.setattr(main.whatsapp_client, "send_text", no_send)
    return calls, state


def run(incoming):
    response = asyncio.run(main._process_incoming_message(incoming))
    return response.status_code, json.loads(response.body)


def test_freshness_threshold_boundaries(harness):
    calls, _state = harness
    assert main._inbound_expired(str(NOW - 899)) is False
    assert main._inbound_expired(str(NOW - 900)) is False  # at the cutoff: still fresh
    assert main._inbound_expired(str(NOW - 901)) is True
    assert main._inbound_expired(None) is False
    assert main._inbound_expired("not-a-time") is False
    assert calls["lookup"] == []


def test_delayed_message_is_recorded_in_closed_send_time_ticket_without_ai(harness):
    calls, _state = harness
    status, body = run(message(1800))
    assert (status, body) == (200, {"status": "delayed_message_recorded", "ticket_id": 7})
    lookup = calls["lookup"][0]
    assert lookup["phone_number"] == PHONE and lookup["business_phone_number_id"] == LINE
    assert lookup["sent_at"] == datetime.fromtimestamp(NOW - 1800, tz=timezone.utc).replace(tzinfo=None)
    route = calls["route"][0]
    assert route["ticket"] is CLOSED and route["require_active"] is False
    assert route["whatsapp_message_id"] == "ld:90015:LATE" and route["whatsapp_timestamp"] == str(NOW - 1800)
    assert route["expected_phone"] == PHONE and route["expected_line_id"] == LINE
    # Ticket metadata (language/style) is not rewritten by a late message.
    assert "current_language" not in route and "communication_style" not in route
    assert calls["ai"] == [] and calls["send"] == []


def test_delayed_message_for_still_active_ticket_uses_normal_active_recording(harness):
    calls, state = harness
    state["match"] = (ACTIVE, "matched")
    assert run(message(1800))[1]["status"] == "delayed_message_recorded"
    assert calls["route"][0]["require_active"] is True


def test_active_ticket_closing_during_recording_falls_back_to_history(harness):
    calls, state = harness
    state["match"] = (ACTIVE, "matched")
    state["route_errors"] = [SupportConflictError("Ticket is no longer active.")]
    assert run(message(1800))[1]["status"] == "delayed_message_recorded"
    assert [call["require_active"] for call in calls["route"]] == [True, False]


@pytest.mark.parametrize("reason", ["no_ticket_at_send_time", "ambiguous_ticket_at_send_time", "no_line_identity"])
def test_without_a_unique_send_time_ticket_the_message_is_not_attached(harness, reason):
    calls, state = harness
    state["match"] = (None, reason)
    status, body = run(message(1800))
    assert (status, body) == (200, {"status": "stale_message_ignored", "reason": "expired"})
    assert calls["route"] == [] and calls["ai"] == [] and calls["send"] == []


def test_already_recorded_message_is_not_inserted_again(harness):
    calls, state = harness
    state["existing"] = {"id": 99, "ticket_id": 7}
    assert run(message(1800)) == (200, {"status": "duplicate_support_message", "ticket_id": 7})
    assert calls["route"] == []


def test_temporary_failures_are_retried_instead_of_dropped(harness):
    calls, state = harness
    state["match"] = RuntimeError("database unavailable")
    assert run(message(1800)) == (503, {"status": "support_state_unavailable"})
    state["match"] = (CLOSED, "matched")
    state["route_errors"] = [RuntimeError("connection lost mid-insert")]
    assert run(message(1800, message_id="ld:90015:LATE-2")) == (503, {"status": "support_state_unavailable"})
    # The retried delivery records it.
    assert run(message(1800, message_id="ld:90015:LATE-2"))[1]["status"] == "delayed_message_recorded"
    assert calls["ai"] == []


def test_delayed_media_is_captured_for_the_matched_ticket(harness):
    calls, state = harness
    assert run(message(1800, kind="image", media_id="ldmedia:90015:abc"))[1]["status"] == "delayed_message_recorded"
    assert calls["media"][0]["ticket"] is CLOSED and calls["media"][0]["media_id"] == "ldmedia:90015:abc"
    state["media_error"] = SupportMediaError("temporarily unavailable", retryable=True)
    assert run(message(1800, message_id="ld:90015:IMG2", kind="image", media_id="ldmedia:90015:def")) == (503, {"status": "media_capture_retry"})


def test_timely_messages_keep_the_existing_routing(harness):
    calls, state = harness
    state["active_ticket"] = ACTIVE
    status, _body = run(message(60))
    assert status == 200
    assert calls["lookup"] == []
    assert calls["route"] and "require_active" not in calls["route"][0]


def test_messages_without_a_timestamp_are_not_treated_as_delayed(harness):
    calls, state = harness
    state["active_ticket"] = ACTIVE
    assert run(message(None))[0] == 200
    assert calls["lookup"] == []


def test_recent_duplicates_are_still_dropped(harness):
    calls, _state = harness
    store = main.conversation_store

    async def remember():
        conversation = await store.get_or_create(PHONE, conversation_key=f"{LINE}:{PHONE}",
                                                 business_phone_number_id=LINE)
        conversation.recent_inbound_message_ids.append("ld:90015:DUP")
        await store.save(conversation)
    asyncio.run(remember())
    assert run(message(1800, message_id="ld:90015:DUP")) == (200, {"status": "stale_message_ignored", "reason": "recent_duplicate"})
    assert calls["lookup"] == []


def _drain_burst(monkeypatch, ages):
    async def scenario():
        store = InMemoryConversationStore()
        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main.time, "time", lambda: float(NOW))
        monkeypatch.setattr(main.settings, "whatsapp_message_burst_debounce_seconds", 0)
        monkeypatch.setattr(main.settings, "whatsapp_message_burst_max_wait_seconds", 0)

        async def no_active(*_args, **_kwargs):
            return None

        async def ai_enabled():
            return types.SimpleNamespace(effective_ai_enabled=True)
        monkeypatch.setattr(main.support_service, "get_active_ticket", no_active)
        monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", ai_enabled)
        seen: list[tuple[str | None, str | None]] = []

        async def fake_process(incoming, **_kwargs):
            seen.append((incoming.message_id, incoming.text))
            return main._json_response(200, {"status": "ok"})
        monkeypatch.setattr(main, "_process_incoming_message", fake_process)
        route = f"{LINE}:{PHONE}"
        for index, age in enumerate(ages):
            item = message(age, message_id=f"ld:90015:M{index}")
            claim = await store.claim_incoming_message(item.message_id)
            await store.enqueue_incoming_work(route, main._queued_inbound_item(item, claim))
        await main._drive_inbound_queue(route)
        return seen
    return asyncio.run(scenario())


def test_delayed_items_in_a_burst_are_not_merged_into_one_ai_turn(monkeypatch):
    # Control: timely adjacent texts are still merged into one AI turn.
    assert _drain_burst(monkeypatch, [20, 10]) == [("ld:90015:M1", "late text\nlate text")]
    # Any item past the cutoff keeps every original message separate.
    assert _drain_burst(monkeypatch, [1800, 10]) == [("ld:90015:M0", "late text"), ("ld:90015:M1", "late text")]


def test_service_requires_exactly_one_ticket_on_the_line_at_send_time(monkeypatch):
    from datetime import datetime
    import app.support_service as service_module
    found: list = []

    class Repository:
        async def find_tickets_open_at_whatsapp_timestamp(self, peer, line, sent_at):
            found.append((peer, line, sent_at))
            return results

    monkeypatch.setattr(service_module, "support_repository", Repository())
    service = SupportService()
    sent = datetime(2026, 10, 29, 12, 48)
    results = []
    assert asyncio.run(service.ticket_for_delayed_inbound(phone_number="+961 70 123456", business_phone_number_id=LINE, sent_at=sent)) == (None, "no_ticket_at_send_time")
    assert found[-1] == (PHONE, LINE, sent)
    results = [CLOSED, ACTIVE]
    assert asyncio.run(service.ticket_for_delayed_inbound(phone_number=PHONE, business_phone_number_id=LINE, sent_at=sent)) == (None, "ambiguous_ticket_at_send_time")
    results = [CLOSED]
    assert asyncio.run(service.ticket_for_delayed_inbound(phone_number=PHONE, business_phone_number_id=LINE, sent_at=sent)) == (CLOSED, "matched")
    assert asyncio.run(service.ticket_for_delayed_inbound(phone_number=PHONE, business_phone_number_id=None, sent_at=sent)) == (None, "no_line_identity")


def test_recording_into_a_closed_ticket_does_not_change_its_status(monkeypatch):
    import app.support_service as service_module
    events, transitions, inserts = [], [], []

    class Repository:
        async def add_ticket_message(self, **kwargs):
            inserts.append(kwargs)
            return {"id": 1, "ticket_id": kwargs["ticket_id"], "direction": "INBOUND", "sender_type": "CUSTOMER",
                    "message_type": "text", "body": kwargs["body"]}, True

        async def mark_customer_replied(self, ticket_id):
            transitions.append(ticket_id)

    async def publish(event_type, **payload):
        events.append(event_type)

    monkeypatch.setattr(service_module, "support_repository", Repository())
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    waiting_but_closed = {**CLOSED, "status": "WAITING_CUSTOMER"}
    asyncio.run(SupportService().route_human_inbound(
        ticket=waiting_but_closed, body="late", whatsapp_message_id="ld:90015:X", whatsapp_timestamp="1",
        message_type="text", require_active=False, expected_phone=PHONE, expected_line_id=LINE))
    assert transitions == []
    assert inserts[0]["require_active"] is False and inserts[0]["expected_phone"] == PHONE
    # Staff still see it as new customer input.
    assert events == ["customer_message"]
