"""WhatsApp delivery ticks and resending failed support messages."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {}))
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

import app.support_service as service_module
from app.support_auth import SupportAgent
from app.support_repository import SupportConflictError, SupportNotFoundError, SupportRepository
from app.support_service import SupportSendError, SupportValidationError, support_service
from app.support_ui import _HTML

AGENT = SupportAgent(id="7", name="Rana", role="agent")
TICKET = {"id": 42, "whatsapp_phone": "96171111111", "business_phone_number_id": "90001", "status": "IN_PROGRESS"}


class FakeRepository:
    def __init__(self, message: dict[str, Any]) -> None:
        self.message = message
        self.claims = 0
        self.states: list[dict[str, Any]] = []
        self.claimable = True

    async def get_message(self, message_id):
        return dict(self.message) if int(message_id) == self.message["id"] else None

    async def restart_failed_send(self, message_id):
        self.claims += 1
        if not self.claimable or self.message["send_status"] != "FAILED":
            return False
        self.message.update(send_status="PENDING", error_message=None)
        return True

    async def update_message_send_state(self, message_id, *, send_status, whatsapp_message_id=None, error_message=None):
        self.states.append({"send_status": send_status, "whatsapp_message_id": whatsapp_message_id, "error_message": error_message})
        self.message.update(send_status=send_status, error_message=error_message)
        if whatsapp_message_id:
            self.message["whatsapp_message_id"] = whatsapp_message_id

    async def mark_agent_reply_started(self, *_args, **_kwargs):
        return None

    async def apply_delivery_status(self, whatsapp_message_id, **kwargs):
        if whatsapp_message_id != self.message.get("whatsapp_message_id"):
            return None
        self.message["delivery_status"] = kwargs["status"]
        return dict(self.message), True


class FakeWhatsApp:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def send_text(self, **kwargs):
        self.calls.append(("text", kwargs))
        if self.fail:
            raise RuntimeError("The linked-device bridge rejected the request with HTTP 500.")
        return {"messages": [{"id": "ld:90001:RESENT"}]}

    async def upload_media(self, **kwargs):
        self.calls.append(("upload", kwargs))
        return "ldmedia:90001:00000000-0000-0000-0000-000000000000"

    async def send_media(self, **kwargs):
        self.calls.append(("media", kwargs))
        return {"messages": [{"id": "ld:90001:MEDIA"}]}


def failed_message(**overrides):
    message = {
        "id": 900, "ticket_id": 42, "direction": "OUTBOUND", "sender_type": "AGENT", "sender_id": "7",
        "message_type": "text", "body": "Your parcel left Beirut", "client_message_id": "client-900",
        "send_status": "FAILED", "error_message": "The linked-device bridge rejected the request with HTTP 504.",
        "reply_to_whatsapp_message_id": "ld:90001:QUOTED", "deleted_for_everyone_at": None, "whatsapp_message_id": None,
    }
    message.update(overrides)
    return message


@pytest.fixture
def patched(monkeypatch):
    def install(message, *, whatsapp=None):
        repository = FakeRepository(message)
        events: list[tuple[str, dict[str, Any]]] = []
        whatsapp = whatsapp or FakeWhatsApp()

        async def prepare(ticket_id, agent):
            assert agent.id == "7"
            return dict(TICKET)

        async def publish(event_type, **payload):
            events.append((event_type, payload))

        monkeypatch.setattr(service_module, "support_repository", repository)
        monkeypatch.setattr(service_module, "whatsapp_client", whatsapp)
        monkeypatch.setattr(service_module, "publish_support_event", publish)
        monkeypatch.setattr(service_module, "_serialize_support_messages", lambda _ticket_id, rows, **_kw: [dict(row) for row in rows])
        monkeypatch.setattr(support_service, "_prepare_agent_outbound_ticket", prepare)
        return repository, whatsapp, events
    return install


def test_resend_sends_the_failed_text_again_on_the_same_row(patched):
    repository, whatsapp, events = patched(failed_message())
    result = asyncio.run(support_service.resend_failed_message(42, 900, agent=AGENT))
    assert whatsapp.calls == [("text", {
        "to": "96171111111", "body": "Your parcel left Beirut", "client_message_id": "client-900",
        "phone_number_id": "90001", "context_message_id": "ld:90001:QUOTED",
    })]
    assert result["send_status"] == "SENT" and result["whatsapp_message_id"] == "ld:90001:RESENT"
    assert result["id"] == 900, "no second timeline row is created"
    assert [event for event, _payload in events] == ["outbound_message", "outbound_message"]
    assert events[0][1]["message"]["send_status"] == "PENDING"


def test_resend_failure_keeps_the_row_failed(patched):
    repository, whatsapp, events = patched(failed_message(), whatsapp=FakeWhatsApp(fail=True))
    with pytest.raises(SupportSendError):
        asyncio.run(support_service.resend_failed_message(42, 900, agent=AGENT))
    assert repository.message["send_status"] == "FAILED"
    assert events[-1][1]["message"]["send_status"] == "FAILED"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"sender_id": "8"}, SupportNotFoundError),
        ({"ticket_id": 43}, SupportNotFoundError),
        ({"direction": "INBOUND"}, SupportNotFoundError),
        ({"deleted_for_everyone_at": "2026-10-20"}, SupportNotFoundError),
        ({"send_status": "SENT"}, SupportConflictError),
        ({"message_type": "reaction"}, SupportValidationError),
    ],
)
def test_resend_refuses_messages_that_are_not_your_failed_sends(patched, overrides, error):
    repository, whatsapp, _events = patched(failed_message(**overrides))
    with pytest.raises(error):
        asyncio.run(support_service.resend_failed_message(42, 900, agent=AGENT))
    assert whatsapp.calls == [] and repository.claims == 0


def test_only_one_resend_wins(patched):
    repository, whatsapp, _events = patched(failed_message())
    repository.claimable = False
    with pytest.raises(SupportConflictError):
        asyncio.run(support_service.resend_failed_message(42, 900, agent=AGENT))
    assert whatsapp.calls == []


def test_resend_media_uses_the_stored_file(patched, monkeypatch, tmp_path: Path):
    stored = tmp_path / "voice.ogg"
    stored.write_bytes(b"OggS-voice")
    asset = SimpleNamespace(path=stored, mime_type="audio/ogg", filename="voice.ogg")
    monkeypatch.setattr(service_module.support_media_store, "get", lambda ticket_id, message_id: asset)
    _repository, whatsapp, _events = patched(failed_message(message_type="audio", body="Voice note"))
    asyncio.run(support_service.resend_failed_message(42, 900, agent=AGENT))
    assert whatsapp.calls[0] == ("upload", {"content": b"OggS-voice", "mime_type": "audio/ogg", "filename": "voice.ogg", "phone_number_id": "90001"})
    kind, media = whatsapp.calls[1]
    assert kind == "media" and media["media_type"] == "audio" and media["voice"] is True and media["caption"] is None
    assert media["client_message_id"] == "client-900"

    monkeypatch.setattr(service_module.support_media_store, "get", lambda ticket_id, message_id: None)
    patched(failed_message(message_type="image", body="Image"))
    with pytest.raises(SupportConflictError):
        asyncio.run(support_service.resend_failed_message(42, 900, agent=AGENT))


def test_delivery_receipts_publish_tick_updates(patched):
    repository, _whatsapp, events = patched(failed_message(send_status="SENT", whatsapp_message_id="ld:90001:SENT1"))
    applied = asyncio.run(support_service.apply_delivery_status(
        whatsapp_message_id="ld:90001:SENT1", status="delivered", timestamp_ms=1_760_000_000_000,
        business_phone_number_id="90001", phone_number="96171111111",
    ))
    assert applied is True
    assert events == [("message_status", {"ticket_id": 42, "message_id": 900, "message": dict(repository.message)})]
    missing = asyncio.run(support_service.apply_delivery_status(
        whatsapp_message_id="ld:90001:OTHER", status="read", timestamp_ms=None,
        business_phone_number_id="90001", phone_number="96171111111",
    ))
    assert missing is False
    with pytest.raises(SupportValidationError):
        asyncio.run(support_service.apply_delivery_status(
            whatsapp_message_id="ld:90001:SENT1", status="played", timestamp_ms=None,
            business_phone_number_id=None, phone_number=None,
        ))


class _ScriptedCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sql: list[str] = []
        self.rowcount = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, sql, params=()):
        self.sql.append(" ".join(sql.split()))

    async def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _ScriptedConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.committed = self.rolled_back = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def begin(self):
        return None

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1

    def cursor(self, *_args):
        return self.cursor_obj


def _repository_with(rows):
    cursor = _ScriptedCursor(rows)
    connection = _ScriptedConnection(cursor)
    repository = SupportRepository()
    repository._pool = SimpleNamespace(acquire=lambda: connection)
    return repository, cursor, connection


def test_late_confirmation_of_an_earlier_attempt_changes_nothing():
    row = {"id": 900, "ticket_id": 42, "sender_type": "AGENT", "sender_id": "7", "direction": "OUTBOUND",
           "send_status": "SENT", "whatsapp_message_id": "ld:90001:RESENT", "error_message": None}
    repository, cursor, connection = _repository_with([row, {"whatsapp_phone": "96171111111", "business_phone_number_id": "90001"}])
    result = asyncio.run(repository.confirm_linked_device_outgoing(
        client_message_id="client-900", whatsapp_message_id="ld:90001:FIRST",
        phone_number="96171111111", business_phone_number_id="90001", whatsapp_timestamp="1760000000",
    ))
    assert result == (row, False)
    assert not any(sql.startswith("UPDATE") for sql in cursor.sql)


@pytest.mark.parametrize(("current", "incoming", "changed"), [(None, "delivered", True), ("delivered", "read", True), ("read", "delivered", False), ("read", "read", False)])
def test_delivery_status_only_moves_forward(current, incoming, changed):
    from datetime import datetime

    row = {"id": 900, "ticket_line_id": "90001", "ticket_phone": "96171111111", "delivery_status": current}
    repository, cursor, _connection = _repository_with([row, {"id": 900}])
    result = asyncio.run(repository.apply_delivery_status(
        "ld:90001:SENT1", status=incoming, at=datetime(2026, 10, 20, 12, 0),
        business_phone_number_id="90001", phone_number="96171111111",
    ))
    assert result == ({"id": 900}, changed)
    assert any(sql.startswith("UPDATE support_ticket_messages SET delivery_status") for sql in cursor.sql) is changed


def test_delivery_status_ignores_another_line_or_customer():
    from datetime import datetime

    row = {"id": 900, "ticket_line_id": "90002", "ticket_phone": "96171111111", "delivery_status": None}
    repository, _cursor, _connection = _repository_with([row])
    assert asyncio.run(repository.apply_delivery_status(
        "ld:90001:SENT1", status="read", at=datetime(2026, 10, 20), business_phone_number_id="90001", phone_number="96171111111",
    )) is None


def test_cloud_status_webhooks_record_delivery_and_read(monkeypatch):
    import app.main as main

    seen = []

    async def apply(**kwargs):
        seen.append(kwargs)
        return True

    monkeypatch.setattr(main.support_service, "apply_delivery_status", apply)
    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "1234567890"},
        "statuses": [
            {"id": "wamid.A", "status": "sent", "timestamp": "1760000000", "recipient_id": "96171111111"},
            {"id": "wamid.A", "status": "delivered", "timestamp": "1760000001", "recipient_id": "96171111111"},
            {"id": "wamid.A", "status": "read", "timestamp": "1760000002", "recipient_id": "96171111111"},
        ],
    }}]}]}
    response = asyncio.run(main.process_whatsapp_payload(payload, source="cloud"))
    assert response.status_code == 200
    assert [(item["status"], item["timestamp_ms"]) for item in seen] == [("delivered", 1_760_000_001_000), ("read", 1_760_000_002_000)]
    assert seen[0]["business_phone_number_id"] == "1234567890"


def _render_status(message: dict[str, Any], agent_id: str = "7", can_reply: bool = True) -> dict[str, Any]:
    start = _HTML.index("function addMessage(")
    end = _HTML.index("function addEvent(")
    message_time = _HTML[_HTML.index("function messageOccurredAt("): _HTML.index("function fmt(")]
    script = r"""
class Element {
  constructor(tagName) {this.tagName = tagName; this.children = []; this.dataset = {}; this.attributes = {}; this.textContent = ''; this.className = '';}
  append(...children) {this.children.push(...children)}
  setAttribute(name, value) {this.attributes[name] = value}
}
const document = {createElement: tagName => new Element(tagName), createTextNode: value => ({textContent: value})};
const createIcon = name => ({icon: name});
const initials = () => 'A';
const messageActor = () => 'Agent';
const messageClass = () => 'agent';
const fmt = value => value || '';
const formatStatus = value => value;
const renderReplyQuote = () => {};
const appendMessageTextWithLinks = (node, value) => {node.textContent = value};
const messageReactionBar = () => null;
const messageTextForCopy = () => '';
const copyMessageText = () => {};
const canChangeWhatsAppMessage = () => false;
const state = JSON.parse(process.argv[2]);
""" + message_time + _HTML[start:end] + r"""
const conversation = new Element('div');
addMessage(conversation, JSON.parse(process.argv[1]));
const found = {states: [], icons: [], buttons: []};
(function walk(node) {
  if (!node || typeof node !== 'object') return;
  if (String(node.className || '').startsWith('send-state')) found.states.push({className: node.className, label: node.attributes?.['aria-label'] || node.title, text: (node.children || []).map(child => child.textContent || '').join('')});
  if (node.icon) found.icons.push(node.icon);
  if (node.tagName === 'button') found.buttons.push(node.textContent);
  for (const child of node.children || []) walk(child);
})(conversation);
console.log(JSON.stringify(found));
"""
    state = {"agent": {"id": agent_id}, "ticketId": 42, "currentTicketData": {"permissions": {"can_reply": can_reply, "can_forward": False}}}
    result = subprocess.run(["node", "-e", script, json.dumps(message), json.dumps(state)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize(
    ("delivery", "class_name", "label", "icon"),
    [(None, "send-state ticks sent", "Sent", "check"),
     ("delivered", "send-state ticks delivered", "Delivered", "check-double"),
     ("read", "send-state ticks read", "Read", "check-double")],
)
def test_outgoing_messages_show_whatsapp_ticks(delivery, class_name, label, icon):
    message = {"id": 5, "ticket_id": 42, "direction": "OUTBOUND", "sender_type": "AGENT", "sender_id": "7",
               "message_type": "text", "body": "Hello", "send_status": "SENT", "delivery_status": delivery}
    found = _render_status(message)
    assert found["states"] == [{"className": class_name, "label": label, "text": ""}]
    assert icon in found["icons"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_failed_messages_offer_resend_only_to_their_sender():
    message = {"id": 5, "ticket_id": 42, "direction": "OUTBOUND", "sender_type": "AGENT", "sender_id": "7",
               "message_type": "text", "body": "Hello", "send_status": "FAILED", "error_message": "HTTP 500"}
    found = _render_status(message)
    assert found["states"][0]["className"] == "send-state failed" and found["states"][0]["text"] == "Not sent"
    assert "Resend" in found["buttons"]
    assert "Resend" not in _render_status(message, agent_id="8")["buttons"]
    assert "Resend" not in _render_status(message, can_reply=False)["buttons"]
    pending = _render_status({**message, "send_status": "PENDING"})
    assert pending["states"] == [{"className": "send-state pending", "label": "Sending", "text": ""}]
    assert "Send status unknown" not in _HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_text_sends_retry_network_failures_with_the_same_client_id():
    start = _HTML.index("const TEXT_SEND_RETRY_DELAYS_MS")
    end = _HTML.index("function showMessagePreferencesError(")
    script = r"""
const assert = require('node:assert/strict');
const state = {ticketId: 42, localMessages: new Map(), pageLeaving: false};
const upserts = [];
const upsertTimelineMessage = message => upserts.push({...message});
const loadQueue = () => Promise.resolve();
const showError = () => {};
const notices = [];
const notify = (message, tone) => notices.push([message, tone]);
const opened = [];
const openTicket = (id, options) => { opened.push(id); return Promise.resolve(); };
global.setTimeout = fn => fn();
let script = [];
const calls = [];
const api = async (path, options) => {
  calls.push(JSON.parse(options.body).client_message_id);
  const next = script.shift();
  if (next instanceof Error) throw next;
  return next;
};
const httpError = (status, detail) => Object.assign(new Error(detail || 'Request failed (' + status + ')'), {status, detail});
""" + _HTML[start:end] + r"""
(async () => {
  // Two network drops, then the server answers: one message, same client ID throughout.
  script = [new TypeError('Failed to fetch'), httpError(502), {message: {id: 7, client_message_id: 'c1', send_status: 'SENT'}}];
  await deliverTextMessage({client_message_id: 'c1', body: 'hi'}, 42);
  assert.deepEqual(calls, ['c1', 'c1', 'c1']);
  assert.equal(state.localMessages.has('c1'), false);
  assert.equal(upserts.at(-1).id, 7);

  // The server said WhatsApp failed: no automatic retry; the stored row is reloaded.
  calls.length = 0; upserts.length = 0;
  script = [httpError(502, 'WhatsApp could not send the message.')];
  await deliverTextMessage({client_message_id: 'c2', body: 'hi'}, 42);
  assert.deepEqual(calls, ['c2']);
  assert.equal(upserts.at(-1).send_status, 'FAILED');
  assert.deepEqual(opened, [42]);

  // Still unreachable after the retries: marked Not sent with a clear reason.
  calls.length = 0;
  script = [new TypeError('x'), new TypeError('x'), new TypeError('x')];
  await deliverTextMessage({client_message_id: 'c3', body: 'hi'}, 42);
  assert.equal(calls.length, 3);
  assert.match(upserts.at(-1).error_message, /could not reach the server/);
  assert.equal(notices.at(-1)[1], 'error');
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
