"""Step 2a: AI can be switched on or off for each WhatsApp number.

AI answers on a number only when the global setting allows it AND that number's
switch is on. Every number starts with AI off. Only Admins and Super Admins can
change it, and switching AI off also stops an AI reply that is still in progress.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ["REDIS_URL"] = ""

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

from pydantic import ValidationError  # noqa: E402

from app import main, support_api  # noqa: E402
from app.ai_availability import (  # noqa: E402
    AIAvailabilityService,
    AIControlConfiguration,
    evaluate_ai_availability,
)
from app.ai_rate_limit import AIQuotaDecision  # noqa: E402
from app.conversation_store import InMemoryConversationStore  # noqa: E402
from app.support_auth import SupportAgent  # noqa: E402
from app.support_models import AIReplyResult  # noqa: E402
from app.support_ui import _HTML  # noqa: E402
from app.whatsapp_client import IncomingWhatsAppMessage  # noqa: E402
from app.whatsapp_registry import (  # noqa: E402
    WhatsAppRegistryForbiddenError,
    WhatsAppRegistryService,
    WhatsAppRegistryValidationError,
)

AGENT = SupportAgent(id="7", name="Agent", role="agent")
ADMIN = SupportAgent(id="6", name="Admin", role="admin")
SUPER = SupportAgent(id="5", name="Super", role="supervisor")
LINE = "90052"
PHONE = "96170123456"


def _configuration(enabled: bool = True) -> AIControlConfiguration:
    return AIControlConfiguration(
        control_mode="MANUAL", manual_ai_enabled=enabled, schedule_start_time="08:00",
        schedule_end_time="18:00", schedule_timezone="UTC", version=3,
    )


class FakeControlRepository:
    def __init__(self, *, global_on: bool = True, lines: dict[str, bool] | None = None) -> None:
        self.global_on = global_on
        self.lines = lines or {}
        self.line_queries: list[str] = []

    async def get(self):
        return _configuration(self.global_on)

    async def get_line_ai_enabled(self, line_id: str) -> bool:
        self.line_queries.append(line_id)
        return self.lines.get(line_id, False)

    async def count_ai_enabled_lines(self) -> int:
        return sum(self.lines.values())


async def _key_ok():
    return True, None


def _service_with(repository, credential_state=_key_ok) -> AIAvailabilityService:
    # Unit tests never touch a database: the OpenAI key state is injected.
    return AIAvailabilityService(repository, credential_state=credential_state)


# ---------------------------------------------------------------------------
# Effective availability
# ---------------------------------------------------------------------------

def test_global_only_evaluation_is_unchanged_for_the_dashboard():
    repository = FakeControlRepository(lines={})
    result = asyncio.run(_service_with(repository).get_effective_ai_availability())
    assert result.effective_ai_enabled is True and result.effective_reason == "MANUAL_ON"
    assert repository.line_queries == []


def test_number_switch_gates_ai_when_global_setting_is_on():
    repository = FakeControlRepository(lines={LINE: True, "90048": False})
    service = _service_with(repository)
    on = asyncio.run(service.get_effective_ai_availability(line_id=LINE))
    off = asyncio.run(service.get_effective_ai_availability(line_id="90048"))
    unknown = asyncio.run(service.get_effective_ai_availability(line_id="99999"))
    missing = asyncio.run(service.get_effective_ai_availability(line_id=None))
    assert on.effective_ai_enabled is True and on.effective_reason == "MANUAL_ON"
    for result in (off, unknown, missing):
        assert result.effective_ai_enabled is False
        assert result.effective_reason == "LINE_AI_DISABLED"


def test_global_off_wins_without_consulting_the_number():
    repository = FakeControlRepository(global_on=False, lines={LINE: True})
    result = asyncio.run(_service_with(repository).get_effective_ai_availability(line_id=LINE))
    assert result.effective_ai_enabled is False and result.effective_reason == "MANUAL_OFF"
    assert repository.line_queries == []


def test_settings_payload_reports_how_many_numbers_have_ai_on():
    repository = FakeControlRepository(lines={LINE: True, "90048": True, "90054": False})
    payload = asyncio.run(_service_with(repository).get_settings_payload())
    assert payload["ai_enabled_line_count"] == 2
    assert payload["effective_reason"] == "MANUAL_ON"


# ---------------------------------------------------------------------------
# Routing and send-time fence
# ---------------------------------------------------------------------------

class Turn:
    """Real routing and send path with fake providers; availability is scripted."""

    def __init__(self, monkeypatch, availability_sequence: list[bool]) -> None:
        self.store = InMemoryConversationStore()
        self.availability_calls: list[object] = []
        self.handoffs: list[str] = []
        self.provider_sends = 0
        self.ai_calls = 0
        sequence = list(availability_sequence)

        async def availability(*_args, line_id=None, **_kwargs):
            self.availability_calls.append(line_id)
            enabled = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            result = evaluate_ai_availability(_configuration(True))
            if enabled:
                return result
            return type(result)(result.configuration, False, "LINE_AI_DISABLED", None)

        async def get_ai_reply(conversation, user_message, **_kwargs):
            self.ai_calls += 1
            conversation.messages.append({"role": "user", "content": user_message})
            conversation.messages.append({"role": "assistant", "content": "On its way."})
            return AIReplyResult(reply_text="On its way.")

        async def send_text(**_kwargs):
            self.provider_sends += 1
            return {"messages": [{"id": "wamid.out"}]}

        async def finalize_handoff(*, handoff, **_kwargs):
            self.handoffs.append(handoff.reason)
            return {"id": 5, "ticket_reference": "CS-T", "status": "NEW"}, True

        async def none(*_args, **_kwargs):
            return None

        async def reconcile(conversation, active_ticket):
            conversation.mode = "human" if active_ticket else "ai"

        class Quota:
            async def consume(self, _customer):
                return AIQuotaDecision(True)

            async def claim_notice(self, _customer):
                return False

        monkeypatch.setattr(main, "conversation_store", self.store)
        monkeypatch.setattr(main, "ai_rate_limiter", Quota())
        monkeypatch.setattr(main, "get_ai_reply", get_ai_reply)
        monkeypatch.setattr(main.whatsapp_client, "send_text", send_text)
        monkeypatch.setattr(main.support_service, "get_active_ticket", none)
        monkeypatch.setattr(main.support_service, "reconcile_conversation_mode", reconcile)
        monkeypatch.setattr(main.support_service, "get_message_by_whatsapp_id", none)
        monkeypatch.setattr(main.support_service, "get_ticket_by_source_message", none)
        monkeypatch.setattr(main.support_service, "finalize_handoff", finalize_handoff)
        monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", availability)
        monkeypatch.setattr(main, "maybe_handle_shipping_quote", none)

    def incoming(self, message_id: str = "wamid.in") -> IncomingWhatsAppMessage:
        return IncomingWhatsAppMessage(
            PHONE, message_id, "text", "where is my parcel", str(int(time.time())),
            business_phone_number_id=LINE,
        )


def test_routing_checks_the_receiving_number_and_hands_off_when_it_is_off(monkeypatch):
    async def run():
        turn = Turn(monkeypatch, [False])
        response = await main._process_incoming_message(turn.incoming())
        assert json.loads(response.body)["status"] == "handed_off_ai_disabled"
        assert turn.handoffs == ["AI_DISABLED_LINE"]
        assert turn.availability_calls == [LINE]
        assert (turn.ai_calls, turn.provider_sends) == (0, 0)
    asyncio.run(run())


def test_number_switched_off_mid_turn_stops_the_reply_and_hands_off(monkeypatch):
    async def run():
        # ON when the message is routed, OFF by the time the reply would be sent.
        turn = Turn(monkeypatch, [True, False])
        response = await main._process_incoming_message(turn.incoming())
        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "handed_off_after_failed_turn"
        assert turn.ai_calls == 1
        assert turn.provider_sends == 0  # the drafted AI reply was never sent
        assert turn.handoffs == ["AI_DISABLED_DURING_TURN"]
        assert turn.availability_calls[0] == LINE and LINE in turn.availability_calls[1:]
    asyncio.run(run())


def test_reply_is_sent_when_ai_stays_on(monkeypatch):
    async def run():
        turn = Turn(monkeypatch, [True])
        response = await main._process_incoming_message(turn.incoming())
        assert json.loads(response.body)["status"] == "handled"
        assert (turn.ai_calls, turn.provider_sends, turn.handoffs) == (1, 1, [])
    asyncio.run(run())


def test_unreadable_ai_state_at_send_time_fails_closed(monkeypatch):
    async def run():
        turn = Turn(monkeypatch, [True])
        calls = {"n": 0}
        real = main.ai_availability_service.get_effective_ai_availability

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise ConnectionError("database unavailable")
            return await real(*args, **kwargs)

        monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", flaky)
        await main._process_incoming_message(turn.incoming())
        assert turn.provider_sends == 0
        assert turn.handoffs == ["AI_AVAILABILITY_UNAVAILABLE"]
    asyncio.run(run())


def test_sends_outside_an_automated_turn_are_not_rechecked(monkeypatch):
    async def run():
        checked = []

        async def availability(*_args, **kwargs):
            checked.append(kwargs)
            raise AssertionError("must not be consulted outside an automated turn")

        monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", availability)
        await main._ensure_automated_reply_allowed(LINE)
        assert checked == []
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Registry service: permissions, audit, bulk
# ---------------------------------------------------------------------------

def _service(row, *, rows=None):
    executed: list[tuple[str, tuple]] = []

    async def execute(query, params=()):
        executed.append((query, params))
        return 1

    repository = SimpleNamespace(
        fetch_one=AsyncMock(return_value=row), fetch_all=AsyncMock(return_value=rows or []), execute=execute,
    )
    service = WhatsAppRegistryService(repository)
    service._ensure_schema = AsyncMock()
    service.list_payload = AsyncMock(return_value={"numbers": []})
    return service, executed


ROW = {"phone_number_id": LINE, "waba_id": None, "source": "LINKED_DEVICE", "provider_present": 1,
       "removed_at": None, "ai_enabled": 0}


def test_only_admins_and_super_admins_switch_ai_and_each_change_is_audited():
    service, executed = _service(ROW)
    with pytest.raises(WhatsAppRegistryForbiddenError):
        asyncio.run(service.set_number_ai_enabled(AGENT, LINE, True))
    with pytest.raises(WhatsAppRegistryForbiddenError):
        asyncio.run(service.set_all_numbers_ai_enabled(AGENT, False))
    assert executed == []
    for manager in (ADMIN, SUPER):
        service, executed = _service(ROW)
        asyncio.run(service.set_number_ai_enabled(manager, LINE, True))
        update, audit = executed
        assert "SET ai_enabled = %s, ai_updated_by_staff_id = %s, ai_updated_at = UTC_TIMESTAMP(6)" in update[0]
        assert update[1] == (1, int(manager.id), LINE)
        assert audit[1][0] == "AI_ENABLED"
        assert json.loads(audit[1][4]) == {"before": False, "after": True}


def test_unchanged_switch_writes_nothing_and_unavailable_numbers_are_refused():
    service, executed = _service({**ROW, "ai_enabled": 1})
    asyncio.run(service.set_number_ai_enabled(ADMIN, LINE, True))
    assert executed == []
    for row in (None, {**ROW, "removed_at": "2026-11-01"}, {**ROW, "provider_present": 0}):
        service, executed = _service(row)
        with pytest.raises(WhatsAppRegistryValidationError):
            asyncio.run(service.set_number_ai_enabled(ADMIN, LINE, True))
        assert executed == []


def test_enable_all_targets_active_numbers_and_disable_all_targets_every_saved_number():
    rows = [{"phone_number_id": "90052", "waba_id": None}, {"phone_number_id": "90048", "waba_id": "w1"}]
    service, executed = _service(None, rows=rows)
    asyncio.run(service.set_all_numbers_ai_enabled(ADMIN, True))
    select_sql = service.repository.fetch_all.await_args.args[0]
    assert "is_enabled = 1" in select_sql and "removed_at IS NULL" in select_sql
    updates = [entry for entry in executed if entry[0].lstrip().startswith("UPDATE")]
    audits = [entry for entry in executed if "INSERT INTO whatsapp_integration_audit" in entry[0]]
    assert [u[1][2] for u in updates] == ["90052", "90048"]
    assert {a[1][0] for a in audits} == {"AI_BULK_ENABLED"}

    service, executed = _service(None, rows=rows)
    asyncio.run(service.set_all_numbers_ai_enabled(SUPER, False))
    select_sql = service.repository.fetch_all.await_args.args[0]
    assert "is_enabled = 1" not in select_sql and "removed_at IS NULL" in select_sql
    assert {a[1][0] for a in executed if "INSERT INTO whatsapp_integration_audit" in a[0]} == {"AI_BULK_DISABLED"}


def test_request_models_accept_only_real_booleans_and_the_all_scope():
    assert support_api.WhatsAppNumberAIRequest(enabled=True).enabled is True
    for bad in ({"enabled": "true"}, {"enabled": 1}, {"enabled": True, "extra": 1}):
        with pytest.raises(ValidationError):
            support_api.WhatsAppNumberAIRequest(**bad)
    assert support_api.WhatsAppAllNumbersAIRequest(enabled=False, scope="all").scope == "all"
    with pytest.raises(ValidationError):
        support_api.WhatsAppAllNumbersAIRequest(enabled=False, scope="some")


def test_bulk_route_is_registered_before_the_number_id_route():
    paths = [route.path for route in support_api.router.routes
             if "PUT" in getattr(route, "methods", set()) and "whatsapp-numbers" in getattr(route, "path", "")]
    assert paths.index("/api/support/whatsapp-numbers/ai") < paths.index("/api/support/whatsapp-numbers/{phone_number_id}")


def test_migration_defaults_every_number_to_ai_off():
    from pathlib import Path
    sql = Path("migrations/20261122_whatsapp_number_ai_enabled.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS ai_enabled TINYINT(1) NOT NULL DEFAULT 0" in sql


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def test_dashboard_has_the_numbers_tab_and_manager_only_controls():
    assert 'id="aiTabNumbers"' in _HTML and 'role="tab"' in _HTML
    assert 'id="aiNumbersList"' in _HTML
    renderer = _HTML[_HTML.index("function renderAiNumbers("): _HTML.index("async function loadAiNumbers(")]
    assert "toggle.disabled = !editable" in renderer
    assert "role', 'switch'" in renderer
    assert "'/ai'" in _HTML[_HTML.index("async function setNumberAi("):]
    assert "scope: 'all'" in _HTML[_HTML.index("async function setAllNumbersAi("):]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_numbers_tab_lists_active_numbers_with_their_switch_state():
    code = _HTML[_HTML.index("function aiNumberLabel("): _HTML.index("async function loadAiNumbers(")]
    script = r"""
const assert = require('node:assert/strict');
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.className = '';
    this.textContent = ''; this.disabled = false; this.listeners = {};
    this.classList = {toggle: (name, on) => { this.hidden = on; }}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren() { this.children = []; }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
}
const nodes = {aiNumbersList: new Node('div'), aiNumbersBulkActions: new Node('div')};
const $ = id => nodes[id];
const document = {createElement: tag => new Node(tag)};
const state = {};
let manager = true;
const canManageAiSettings = () => manager;
""" + code + r"""
renderAiNumbers([
  {phone_number_id: '90052', display_name: 'Export', is_enabled: true, ai_enabled: true, source: 'LINKED_DEVICE'},
  {phone_number_id: '90048', display_phone_number: '96170000048', is_enabled: true, ai_enabled: false, source: 'META_SYNC'},
  {phone_number_id: '90099', display_name: 'Inactive', is_enabled: false, ai_enabled: true},
]);
const rows = nodes.aiNumbersList.children;
assert.equal(rows.length, 2, 'only numbers active in the app');
assert.equal(rows[0].children[0].children[0].textContent, 'Export');
assert.equal(rows[0].children[1].attrs['aria-checked'], 'true');
assert.equal(rows[1].children[0].children[0].textContent, '+96170000048');
assert.equal(rows[1].children[1].attrs['aria-checked'], 'false');
assert.equal(rows[1].children[1].disabled, false);
assert.equal(nodes.aiNumbersBulkActions.hidden, false);
manager = false;
renderAiNumbers([{phone_number_id: '90052', display_name: 'Export', is_enabled: true, ai_enabled: true}]);
assert.equal(nodes.aiNumbersList.children[0].children[1].disabled, true, 'agents see the switch read-only');
assert.equal(nodes.aiNumbersBulkActions.hidden, true);
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
