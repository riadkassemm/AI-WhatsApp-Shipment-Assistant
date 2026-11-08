from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

# Keep imports lightweight in artifact validation environments.
fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {})
)
sys.modules["aiomysql"] = fake_aiomysql

from app import whatsapp_line_health as health
from app.support_delivery import deliver
from app.whatsapp_client import WhatsAppSendError

REGISTRY = [{"phone_number_id": "90020", "name": "Saida Office"}, {"phone_number_id": "90021", "name": "Tyre Office"}]


def test_health_alerts_at_once_for_logout_and_after_two_minutes_otherwise(monkeypatch):
    monkeypatch.setattr(health, "_first_seen_down", {})
    now = 1_800_000_000.0
    lines = health.summarize(REGISTRY, [
        {"line_id": "90020", "status": "logged_out", "logged_out_at": int((now - 5) * 1000),
         "disconnected_since": int((now - 900) * 1000), "logout_reason": "conflict:device_removed"},
        {"line_id": "90021", "status": "disconnected", "disconnected_since": int((now - 30) * 1000)},
    ], now)
    assert [(line["status"], line["alert"]) for line in lines] == [("logged_out", True), ("reconnecting", False)]
    assert lines[0]["since"] == health._iso(now - 5)  # the logout time, not the earlier drop
    assert lines[1]["since"] == health._iso(now - 30)
    assert lines[0]["logout_reason"] == "conflict:device_removed" and lines[1]["logout_reason"] is None
    later = health.summarize(REGISTRY, [{"line_id": "90020", "status": "connected"},
                                        {"line_id": "90021", "status": "connecting", "disconnected_since": int((now - 30) * 1000)}],
                             now + 100)
    assert [(line["status"], line["alert"], line["since"]) for line in later][0] == ("connected", False, None)
    assert later[1]["alert"] is True  # 130 s without reconnecting


def test_health_treats_an_unreachable_bridge_as_offline_from_first_sight(monkeypatch):
    monkeypatch.setattr(health, "_first_seen_down", {})
    now = 1_800_000_000.0
    first = health.summarize(REGISTRY[:1], None, now)
    assert first[0]["status"] == "offline" and first[0]["alert"] is False
    assert health.summarize(REGISTRY[:1], None, now + 120)[0]["alert"] is True
    assert health.summarize(REGISTRY[:1], [{"line_id": "90020", "status": "connected"}], now + 130)[0]["alert"] is False
    assert health._first_seen_down == {}


def test_storage_health_exposes_only_known_operational_issues_for_enabled_lines():
    summary = health.summarize_storage({
        "level": "critical", "disk": {"private": "path"}, "secret": "credentials",
        "issues": [
            {"level": "warning", "code": "COMPACTION_FAILED", "line_id": "90020", "error": "private auth bytes"},
            {"level": "critical", "code": "STORE_UNAVAILABLE", "line_id": "removed-line"},
            {"level": "critical", "code": "DISK_SPACE_LOW"},
            {"level": "critical", "code": "untrusted text"},
            None,
        ],
    }, REGISTRY)
    assert summary == {"level": "critical", "issues": [
        {"level": "warning", "code": "COMPACTION_FAILED", "line_id": "90020", "name": "Saida Office"},
        {"level": "critical", "code": "DISK_SPACE_LOW", "line_id": None, "name": None},
    ]}
    assert health.summarize_storage(None, REGISTRY) is None
    assert health.summarize_storage({"level": "warning", "issues": "private payload"}, REGISTRY) == {"level": "ok", "issues": []}


def test_storage_health_retains_only_allowlisted_store_kinds():
    summary = health.summarize_storage({"level": "critical", "issues": [
        {"code": "STORE_UNAVAILABLE", "level": "critical", "line_id": "90020", "kind": kind,
         "creds": "private session", "error": "private path"}
        for kind in ["auth", "state", "outbox", "private value", {"secret": "value"}]
    ]}, REGISTRY)
    assert [issue.get("kind") for issue in summary["issues"]] == ["auth", "state", "outbox", None, None]
    assert "private" not in json.dumps(summary)
    assert "secret" not in json.dumps(summary)


def test_unreachable_bridge_uses_fresh_independent_storage_report_and_redacts_details(tmp_path, monkeypatch):
    filename = tmp_path / "status.json"
    filename.write_text(json.dumps({"version": 1, "checked_at": 1000, "level": "critical", "secret": "private",
                                   "issues": [{"code": "DISK_SPACE_LOW", "level": "critical", "path": "/private"},
                                              {"code": "COMPACTION_FAILED", "level": "warning", "line_id": "90020"},
                                              {"code": "STORE_UNAVAILABLE", "level": "critical", "line_id": "90099"}]}))
    filename.chmod(0o600)
    monkeypatch.setenv("WHATSAPP_LINKED_DEVICE_STORAGE_WATCHDOG_REPORT", str(filename))
    result = health.watchdog_storage(REGISTRY, 1010)
    assert result["level"] == "critical"
    assert [issue["code"] for issue in result["issues"]] == ["BRIDGE_UNAVAILABLE", "DISK_SPACE_LOW", "COMPACTION_FAILED"]
    assert result["issues"][2]["name"] == "Saida Office"
    assert "private" not in json.dumps(result) and "90099" not in json.dumps(result)


def test_missing_stale_untrusted_and_future_watchdog_reports_expose_monitoring_gap(tmp_path, monkeypatch):
    filename = tmp_path / "status.json"
    monkeypatch.setenv("WHATSAPP_LINKED_DEVICE_STORAGE_WATCHDOG_REPORT", str(filename))

    def gap():
        result = health.watchdog_storage(REGISTRY, 1000)
        assert [issue["code"] for issue in result["issues"]] == ["BRIDGE_UNAVAILABLE", "MONITOR_UNAVAILABLE"]

    gap()
    for checked in [100, 1001, float("nan"), True]:
        filename.write_text(json.dumps({"version": 1, "checked_at": checked, "level": "ok", "issues": []}))
        filename.chmod(0o600)
        gap()
    filename.write_text(json.dumps({"version": 1, "checked_at": 1000, "level": "ok", "issues": []}))
    filename.chmod(0o644)
    gap()
    filename.chmod(0o600)
    target = tmp_path / "private.json"
    filename.rename(target)
    filename.symlink_to(target)
    gap()
    filename.unlink()
    filename.write_bytes(b"x" * 65537)
    filename.chmod(0o600)
    gap()


def test_api_health_reports_independent_storage_failure_when_bridge_status_fails(tmp_path, monkeypatch):
    from app.whatsapp_registry import whatsapp_registry_repository

    filename = tmp_path / "status.json"
    filename.write_text(json.dumps({"version": 1, "checked_at": 1000, "level": "critical",
                                   "issues": [{"code": "INODES_LOW", "level": "critical"}]}))
    filename.chmod(0o600)
    monkeypatch.setenv("WHATSAPP_LINKED_DEVICE_STORAGE_WATCHDOG_REPORT", str(filename))
    monkeypatch.setattr(health, "_cache", {"at": 0.0, "payload": None})
    monkeypatch.setattr(health, "_lock", asyncio.Lock())
    monkeypatch.setattr(health, "configured_linked_device_lines", lambda: {"90020": "", "90021": ""})
    monkeypatch.setattr(health.settings, "whatsapp_linked_device_secret", "s" * 32)
    monkeypatch.setattr(health.time, "time", lambda: 1001)

    async def registry(*_args):
        return REGISTRY

    async def unavailable():
        raise OSError("private provider failure")

    monkeypatch.setattr(whatsapp_registry_repository, "fetch_all", registry)
    monkeypatch.setattr(health.linked_device_client, "get_status", unavailable)
    result = asyncio.run(health.line_health())
    assert result["available"] is False
    assert result["storage"]["level"] == "critical"
    assert [issue["code"] for issue in result["storage"]["issues"]] == ["BRIDGE_UNAVAILABLE", "INODES_LOW"]
    assert all(line["status"] == "offline" for line in result["lines"])
    assert "private" not in json.dumps(result)


def test_slow_health_probe_caches_from_completion_for_waiting_staff(monkeypatch):
    from app.whatsapp_registry import whatsapp_registry_repository

    clock = {'wall': 1000.0, 'monotonic': 10.0}
    probes = []
    monkeypatch.setattr(health, '_cache', {'at': 0.0, 'payload': None})
    monkeypatch.setattr(health, '_lock', asyncio.Lock())
    monkeypatch.setattr(health, 'configured_linked_device_lines', lambda: {'90020': '', '90021': ''})
    monkeypatch.setattr(health.settings, 'whatsapp_linked_device_secret', 's' * 32)
    monkeypatch.setattr(health.time, 'time', lambda: clock['wall'])
    monkeypatch.setattr(health.time, 'monotonic', lambda: clock['monotonic'])

    async def registry(*_args):
        return REGISTRY

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed():
            probes.append(True)
            entered.set()
            await release.wait()
            return {'lines': []}

        monkeypatch.setattr(whatsapp_registry_repository, 'fetch_all', registry)
        monkeypatch.setattr(health.linked_device_client, 'get_status', delayed)
        first = asyncio.create_task(health.line_health())
        await entered.wait()
        waiting = [asyncio.create_task(health.line_health()) for _ in range(8)]
        clock.update(wall=1030.0, monotonic=40.0)
        release.set()
        result = await first
        assert all(item is result for item in await asyncio.gather(*waiting))
        assert len(probes) == 1
        assert result['checked_at'] == health._iso(1030)
        clock.update(wall=900.0, monotonic=41.0)
        assert await health.line_health() is result, 'wall-clock rollback cannot expire a fresh monotonic cache'

    asyncio.run(run())


def test_storage_banner_warns_before_outage_without_suggesting_pairing():
    source = (Path(__file__).resolve().parents[1] / "app/static/support/actions.js").read_text()
    helpers = source[source.index("const LINE_HEALTH_INTERVAL_MS"):source.index("function linkedDeviceAdditionStorageKey(")]
    script = r"""
const assert = require('node:assert/strict');
function element() {return {dataset: {}, textContent: '', classList: {hidden: true, toggle(_, value) {this.hidden = value}}}}
const elements = {lineAlertBanner: element(), lineAlertText: element(), lineAlertActionBtn: element(), composerHelp: element()};
const $ = id => elements[id], text = (node, value) => {node.textContent = value};
const canViewBridgeManagement = () => true, sounds = [], notifyNewCustomerActivity = value => sounds.push(value);
const state = {lineAlertsSeen: new Set(), currentTicketData: {ticket: {business_phone_number_id: '90020'}, permissions: {can_reply: true}}};
""" + helpers + r"""
const payload = {lines: [{line_id:'90020', name:'Saida Office', status:'connected', alert:false}],
  storage:{level:'warning', issues:[{line_id:'90020', name:'Saida Office', level:'warning', code:'COMPACTION_FAILED'}]}};
renderLineHealth(payload); renderLineHealth(payload);
assert.equal(elements.lineAlertBanner.classList.hidden, false);
assert.match(elements.lineAlertText.textContent, /Saida Office.*Contact your server administrator/);
assert.ok(!elements.lineAlertText.textContent.includes('Re-pair'));
assert.equal(elements.lineAlertActionBtn.classList.hidden, true);
assert.equal(sounds.length, 1, 'one notification for the same storage issue');
payload.storage = {level:'critical', issues:[{line_id:null, level:'critical', code:'DISK_SPACE_LOW'}]};
renderLineHealth(payload);
assert.equal(sounds.length, 2, 'escalation alerts immediately');
assert.match(elements.composerHelp.textContent, /New sends are paused.*Saved sessions and delivery records are retained/);
payload.storage = {level:'ok', issues:[]};
renderLineHealth(payload);
assert.equal(elements.lineAlertBanner.classList.hidden, true);
assert.equal(elements.composerHelp.textContent, 'Connected. Messages are sent from the WhatsApp number for this conversation.');
payload.storage = {level:'critical', issues:[{line_id:'90021', name:'Tyre Office', level:'critical', code:'JOURNAL_LIMIT'}]};
renderLineHealth(payload);
assert.equal(sounds.length, 3, 'a recurrence notifies again');
assert.equal(elements.composerHelp.textContent, 'Connected. Messages are sent from the WhatsApp number for this conversation.');
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


class Repository:
    def __init__(self):
        self.finished = []

    async def claim_delivery(self, *_args, **_kwargs):
        return "token"

    async def begin_delivery(self, *_args):
        return True

    async def finish_delivery(self, message_id, token, **kwargs):
        self.finished.append(kwargs)

    async def get_message(self, message_id):
        return {"id": message_id}


def _deliver_with(error):
    async def send_text(**_kwargs):
        raise error
    repository = Repository()
    message = {"id": 5, "message_type": "text", "body": "Hello", "transport_client_id": "client-5"}
    ticket = {"id": 42, "whatsapp_phone": "96170123456", "business_phone_number_id": "90020"}
    asyncio.run(deliver(repository, types.SimpleNamespace(send_text=send_text), None, ticket, message))
    return repository.finished[0]


def test_only_known_safe_bridge_outages_or_pressure_hold_a_reply():
    offline = WhatsAppSendError("rejected", delivery_uncertain=False)
    offline.bridge_code = "NOT_CONNECTED"
    held = _deliver_with(offline)
    assert held["status"] == "FAILED" and held["held"] is True
    assert "held" not in _deliver_with(WhatsAppSendError("rejected", delivery_uncertain=False))
    uncertain = WhatsAppSendError("lost", delivery_uncertain=True)
    uncertain.bridge_code = "NOT_CONNECTED"
    result = _deliver_with(uncertain)
    assert result["status"] == "UNCERTAIN" and "held" not in result  # never resend what may have been sent
    for code in ['BRIDGE_UNAVAILABLE', 'BRIDGE_INITIALIZING', 'STORAGE_PRESSURE', 'STOPPING', 'MEDIA_UPLOAD_BUSY']:
        safe = WhatsAppSendError('synthetic pre-submission failure', delivery_uncertain=False)
        safe.bridge_code = code
        assert _deliver_with(safe)['held'] is True
        ambiguous = WhatsAppSendError('synthetic interrupted submission', delivery_uncertain=True)
        ambiguous.bridge_code = code
        assert 'held' not in _deliver_with(ambiguous)


def test_banner_and_composer_explain_an_outage():
    source = (Path(__file__).resolve().parents[1] / "app/static/support/actions.js").read_text()
    helpers = source[source.index("const LINE_HEALTH_INTERVAL_MS"):source.index("function linkedDeviceAdditionStorageKey(")]
    script = r"""
const assert = require('node:assert/strict');
function element() {return {dataset: {}, textContent: '', classList: {hidden: true, toggle(_, value) {this.hidden = value}}}}
const elements = {lineAlertBanner: element(), lineAlertText: element(), lineAlertActionBtn: element(), composerHelp: element()};
const $ = id => elements[id];
const text = (node, value) => {node.textContent = value};
let staff = false;
const canViewBridgeManagement = () => staff;
const sounds = [];
const notifyNewCustomerActivity = message => sounds.push(message);
const state = {authenticated: true, lineHealthAt: 0, lineAlertsSeen: new Set(),
  currentTicketData: {ticket: {business_phone_number_id: '90020'}, permissions: {can_reply: true}}};
""" + helpers + r"""
const down = {lines: [{line_id: '90020', name: 'Saida Office', status: 'logged_out', since: '2026-10-31T08:10:27Z', alert: true}]};
renderLineHealth(down);
assert.equal(elements.lineAlertBanner.classList.hidden, false);
assert.match(elements.lineAlertText.textContent, /^Saida Office needs relinking since .+\. Ask an authorized staff member to relink the same phone/);
assert.equal(elements.lineAlertActionBtn.classList.hidden, true, 'no re-pair button without link rights');
assert.deepEqual(sounds, [], 'nobody who cannot fix it is paged');
assert.match(elements.composerHelp.textContent, /^Saida Office: Relink required\. Ask an authorized staff member/);
staff = true; state.lineAlertsSeen.clear();  // every staff role can re-pair
renderLineHealth(down); renderLineHealth(down);
assert.match(elements.lineAlertText.textContent, /relink the same phone; conversation history stays/);
assert.equal(elements.lineAlertActionBtn.classList.hidden, false);
assert.equal(sounds.length, 1, 'one sound per outage');
renderLineHealth({lines: [{line_id: '90020', name: 'Saida Office', status: 'connected', since: null, alert: false}]});
assert.equal(elements.lineAlertBanner.classList.hidden, true);
assert.equal(elements.composerHelp.textContent, 'Connected. Messages are sent from the WhatsApp number for this conversation.');
renderLineHealth({available: false,
  lines: [{line_id: '90020', name: 'Saida Office', status: 'offline', connection_state: 'temporarily_unavailable', alert: false}],
  storage: {level: 'critical', issues: [{code: 'BRIDGE_UNAVAILABLE', level: 'critical'}, {code: 'MONITOR_UNAVAILABLE', level: 'critical'}]}});
assert.match(elements.composerHelp.textContent, /Temporarily unavailable/);
assert.doesNotMatch(elements.composerHelp.textContent, /Storage recovery required/);
assert.match(elements.lineAlertText.textContent, /connection status is unavailable/);
assert.equal(elements.lineAlertActionBtn.classList.hidden, true, 'missing telemetry is not a reason to relink');
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_held_reply_says_it_will_send_itself_in_browser(tmp_path):
    from app.support_ui import _HTML
    browser = shutil.which(os.environ.get("SUPPORT_TEST_BROWSER", "chromium"))
    assert browser, "Install the configured test browser"
    rendering = _HTML[_HTML.index("function addMessage("):_HTML.index("// Compact message toolbar:")]
    links = _HTML[_HTML.index("function appendMessageTextWithLinks("):_HTML.index("function createVoiceNotePlayer(")]
    copy = _HTML[_HTML.index("function messageTextForCopy("):_HTML.index("async function copyMessageText(")]
    script = r"""
const state = {ticketId: 42, agent: {id: '7'}, currentTicketData: {permissions: {can_reply: true}}};
const messageClass = () => 'agent', messageActor = () => 'Staff', initials = () => 'EX', renderReplyQuote = () => {};
const canChangeWhatsAppMessage = () => false, fmt = () => '12:00', messageOccurredAt = () => '2026-10-31T08:15:00Z';
const messageArrivedLate = () => false, labelMessageAction = () => {}, messageReactionBar = () => null;
const copyMessageText = () => {}, createIcon = () => document.createElement('span'), canResendMessage = () => true;
const canCopyUncertainMessage = () => false, parseServerDate = value => new Date(value);
""" + rendering + links + copy + r"""
try {
  const c = document.getElementById('messages');
  const base = {ticket_id: 42, sender_type: 'AGENT', sender_id: '7', direction: 'OUTBOUND', message_type: 'text',
    body: 'Example reply', send_status: 'FAILED', delivery_retry_safe: 1, error_message: 'Not sent; retry is available.'};
  addMessage(c, {...base, id: 1, held_until: '2026-10-31T08:45:00Z'});
  addMessage(c, {...base, id: 2, held_until: null});
  const [held, failed] = c.querySelectorAll('.send-state');
  if (!held.classList.contains('held') || !held.textContent.includes('Waiting for the line to reconnect')) throw Error('Held label missing');
  if (!held.title.includes('sent automatically')) throw Error('Held explanation missing');
  if (failed.textContent !== 'Not sent') throw Error('Ordinary failure changed');
  if ([...c.querySelectorAll('button')].filter(b => b.textContent === 'Resend').length !== 2) throw Error('Manual resend must stay available');
  document.body.dataset.result = 'passed';
} catch (error) { document.body.dataset.result = 'FAILED: ' + error.message; }
"""
    page = tmp_path / "held.html"
    page.write_text('<!doctype html><body><div id="messages"></div><script>' + script + "</script></body>")
    command = [browser, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
               "--user-data-dir=" + str(tmp_path / "profile"), "--virtual-time-budget=500", "--dump-dom", page.as_uri()]
    result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    assert 'data-result="passed"' in result.stdout, result.stdout[-700:]


def test_staff_connection_states_distinguish_recovery_from_relinking(monkeypatch):
    monkeypatch.setattr(health, "_first_seen_down", {})
    now = 1_800_000_000.0
    cases = [
        ({"status": "connected"}, "connected", "none"),
        ({"status": "disconnected", "recovery_category": "temporary_transport"}, "reconnecting", "automatic"),
        ({"status": "error", "recovery_category": "session_conflict"}, "temporarily_unavailable", "administrator"),
        ({"status": "error", "recovery_category": "storage_failure"}, "storage_recovery_required", "administrator"),
        ({"status": "logged_out"}, "relink_required", "relink"),
    ]
    for bridge, expected, action in cases:
        [line] = health.summarize(REGISTRY[:1], [{"line_id": "90020", **bridge}], now)
        assert line["connection_state"] == expected and line["recovery_action"] == action
        if expected == "reconnecting":
            assert not line["alert"], "brief recoverable gaps should not sound an alarm"
