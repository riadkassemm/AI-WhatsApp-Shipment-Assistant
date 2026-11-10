"""A send is "uncertain" only after it reached WhatsApp's connection.

The bridge proves when a message never left (failed before the wire, or waited
past its deadline). The API turns that proof into an ordinary retry-safe
failure (recent ones are resent automatically), and marks sends with no
WhatsApp confirmation for 24 hours NOT_DELIVERED.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone

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

import httpx
import pytest

from app import support_service
from app.config import settings
from app.linked_device_client import LinkedDeviceWhatsAppClient
from app.support_delivery import deliver
from app.whatsapp_client import WhatsAppSendError

LINE = "10001"
PHONE = "96170123456"


def utc(**delta) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(**delta)).replace(tzinfo=None)


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "whatsapp_linked_device_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_linked_device_default_line_id", LINE)
    monkeypatch.setattr(settings, "whatsapp_linked_device_url", "http://127.0.0.1:3001")
    monkeypatch.setattr(settings, "whatsapp_linked_device_secret", "s" * 40)
    monkeypatch.setattr(settings, "whatsapp_linked_device_lines", {LINE: "+96170111111"})
    monkeypatch.setattr(settings, "whatsapp_linked_device_data_directory", str(tmp_path / "bridge-data"))
    real_client = httpx.AsyncClient
    seen: list[dict] = []

    def install(handler):
        def record(request):
            seen.append({"request": request, "timeout": request.extensions.get("timeout")})
            return handler(request)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(record), **kwargs))
        return seen
    return install


def test_sends_carry_a_start_deadline_and_the_api_waits_past_the_bridge(bridge) -> None:
    seen = bridge(lambda request: httpx.Response(200, json={"messages": [{"id": f"ld:{LINE}:ABC"}]}))
    client = LinkedDeviceWhatsAppClient()
    before = time.time() * 1000
    asyncio.run(client.send_text(PHONE, "Hello", phone_number_id=LINE, client_message_id="c-1"))
    asyncio.run(client.send_media(to=PHONE, media_type="audio", media_id=f"ldmedia:{LINE}:ce82b74b-1763-494a-9b79-3f99b994c354",
                                  phone_number_id=LINE, client_message_id="c-2"))
    text, audio = (json.loads(item["request"].content) for item in seen)
    assert 24_000 <= text["deadline_at_ms"] - before <= 26_000
    assert 84_000 <= audio["deadline_at_ms"] - before <= 86_000
    assert seen[0]["timeout"]["read"] == 55 and seen[1]["timeout"]["read"] == 115


def test_bridge_delivery_statuses_include_proof_of_non_sending(bridge) -> None:
    bridge(lambda request: httpx.Response(200, json={"status": "not_sent", "bridge_started_at": 1}))
    result = asyncio.run(LinkedDeviceWhatsAppClient().get_delivery_status(phone=PHONE, client_message_id="c", phone_number_id=LINE))
    assert result["status"] == "not_sent"
    bridge(lambda request: httpx.Response(200, json={"status": "in_progress"}))
    assert asyncio.run(LinkedDeviceWhatsAppClient().get_delivery_status(phone=PHONE, client_message_id="c", phone_number_id=LINE))["status"] == "in_progress"


@pytest.mark.parametrize("code,held", [("NOT_SENT", True), ("SEND_DEADLINE_EXCEEDED", True), ("CLIENT_ID_CONFLICT", False)])
def test_a_bridge_proven_non_send_is_a_retry_safe_failure_held_for_automatic_resend(code, held) -> None:
    finished = {}

    class Repository:
        async def claim_delivery(self, message_id, **_kwargs):
            return "token"

        async def begin_delivery(self, *_args):
            return True

        async def finish_delivery(self, message_id, token, **kwargs):
            finished.update(kwargs)

        async def get_message(self, message_id):
            return {"id": message_id}

    class Transport:
        async def send_text(self, **_kwargs):
            error = WhatsAppSendError("WhatsApp did not receive this message.", delivery_uncertain=False)
            error.bridge_code = code
            raise error

    message = {"id": 5, "message_type": "text", "body": "Hi", "transport_client_id": "t-5"}
    asyncio.run(deliver(Repository(), Transport(), None, {"id": 1, "whatsapp_phone": PHONE, "business_phone_number_id": LINE}, message))
    assert finished["status"] == "FAILED"
    assert finished.get("held", False) is held


@pytest.fixture
def reconcile(monkeypatch):
    state = {"evidence": None, "resolved": [], "events": [], "confirmed": []}

    async def delivery_status(**_kwargs):
        return state["evidence"]

    async def resolve(message_id, *, hold):
        state["resolved"].append((message_id, hold))
        return {"id": message_id, "ticket_id": 7, "send_status": "FAILED", "held_until": utc() if hold else None}

    async def publish(event_type, **payload):
        state["events"].append(event_type)

    async def confirm(self, event):
        state["confirmed"].append(event["message_id"])
        return True

    monkeypatch.setattr(support_service.linked_device_client, "get_delivery_status", delivery_status)
    monkeypatch.setattr(support_service.support_repository, "resolve_delivery_not_sent", resolve)
    monkeypatch.setattr(support_service, "publish_support_event", publish)
    monkeypatch.setattr(support_service, "get_transport", lambda line: "linked_device")
    monkeypatch.setattr(support_service, "_serialize_support_messages", lambda ticket_id, rows: rows)
    monkeypatch.setattr(support_service.SupportService, "confirm_linked_device_outgoing", confirm)

    def run(evidence, **row):
        state["evidence"] = evidence
        message = {"id": 9, "ticket_id": 7, "send_status": "UNCERTAIN", "transport_client_id": "t-9",
                   "created_at": utc(seconds=40), "delivery_started_at": utc(seconds=40), **row}
        ticket = {"business_phone_number_id": LINE, "whatsapp_phone": PHONE}
        asyncio.run(support_service.SupportService()._reconcile_delivery(ticket, message))
        return state
    return run


def test_reconciliation_applies_each_kind_of_bridge_evidence(reconcile) -> None:
    now_ms = int(time.time() * 1000)
    # Proven not sent, still recent: resent automatically.
    assert reconcile({"status": "not_sent", "bridge_started_at": now_ms - 3_600_000})["resolved"] == [(9, True)]
    # Proven not sent, but the conversation has moved on: staff decide.
    state = reconcile({"status": "not_sent", "bridge_started_at": now_ms}, created_at=utc(hours=2), delivery_started_at=utc(hours=2))
    assert state["resolved"][-1] == (9, False)
    # Never reserved by the running bridge, sent before it started: final.
    state = reconcile({"status": "unknown", "bridge_started_at": now_ms})
    assert state["resolved"][-1] == (9, False)
    count = len(state["resolved"])
    # Never reserved, but recent and the bridge was already running: it may still start.
    assert len(reconcile({"status": "unknown", "bridge_started_at": now_ms - 3_600_000})["resolved"]) == count
    # The same, once older than any send window: final.
    state = reconcile({"status": "unknown", "bridge_started_at": now_ms - 3_600_000}, created_at=utc(minutes=4), delivery_started_at=utc(minutes=4))
    assert state["resolved"][-1] == (9, False)
    count = len(state["resolved"])
    # On the wire without confirmation, or still running: unchanged.
    for status in ("unconfirmed", "in_progress"):
        assert len(reconcile({"status": status, "on_wire": True, "bridge_started_at": 1})["resolved"]) == count
    reconcile({"status": "confirmed", "message_id": f"ld:{LINE}:X", "timestamp": now_ms})
    assert state["confirmed"] == [f"ld:{LINE}:X"]
    assert state["events"].count("outbound_message") == 4


def test_each_reconciliation_pass_first_expires_old_unconfirmed_sends(monkeypatch) -> None:
    calls = []

    async def expire():
        calls.append("expire")
        return [{"id": 3, "ticket_id": 7, "send_status": "NOT_DELIVERED"}]

    async def candidates(after_id, limit):
        calls.append(("candidates", after_id))
        return []

    published = []

    async def publish(event_type, **payload):
        published.append((event_type, payload["message"]["send_status"]))

    monkeypatch.setattr(support_service.support_repository, "expire_unconfirmed_deliveries", expire)
    monkeypatch.setattr(support_service.support_repository, "list_delivery_reconciliation_candidates", candidates)
    monkeypatch.setattr(support_service, "publish_support_event", publish)
    monkeypatch.setattr(support_service, "_serialize_support_messages", lambda ticket_id, rows: rows)
    service = support_service.SupportService()
    asyncio.run(service.reconcile_uncertain_deliveries(0))
    asyncio.run(service.reconcile_uncertain_deliveries(40))
    assert calls == ["expire", ("candidates", 0), ("candidates", 40)], "once per full cycle"
    assert published == [("outbound_message", "NOT_DELIVERED")]


@pytest.mark.parametrize("row,expected", [
    ({"send_status": "SENT"}, "confirmed"),
    ({"send_status": "FAILED", "held_until": utc()}, "sent again automatically"),
    ({"send_status": "FAILED", "held_until": None}, "safe to send it again"),
    ({"send_status": "NOT_DELIVERED"}, "within 24 hours"),
    ({"send_status": "UNCERTAIN"}, "Nothing was resent"),
])
def test_check_status_explains_the_current_outcome(monkeypatch, row, expected) -> None:
    async def context(self, ticket_id, message_id, agent):
        return {"id": ticket_id}, {"id": message_id}

    async def nothing(self, ticket, message):
        return None

    async def current(message_id):
        return {"id": message_id, **row}

    monkeypatch.setattr(support_service.SupportService, "_delivery_action_context", context)
    monkeypatch.setattr(support_service.SupportService, "_reconcile_delivery", nothing)
    monkeypatch.setattr(support_service.support_repository, "get_message", current)
    monkeypatch.setattr(support_service, "_serialize_support_messages", lambda ticket_id, rows: rows)
    result = asyncio.run(support_service.SupportService().check_message_delivery(7, 9, agent=None))
    assert expected in result["notice"]


def test_not_delivered_messages_can_be_sent_again_but_confirmed_ones_cannot() -> None:
    assert support_service.COPYABLE_DELIVERY_STATUSES == {"UNCERTAIN", "NOT_DELIVERED"}
    from app.support_ui import _HTML
    assert "['UNCERTAIN', 'NOT_DELIVERED'].includes(m.send_status)" in _HTML
    assert "textContent = 'Send again'" in _HTML
    assert "'Not confirmed yet'" in _HTML and "Delivery uncertain" not in _HTML
