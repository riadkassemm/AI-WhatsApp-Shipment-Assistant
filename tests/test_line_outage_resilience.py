from __future__ import annotations

import asyncio
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


def test_only_an_offline_line_holds_a_reply():
    offline = WhatsAppSendError("rejected", delivery_uncertain=False)
    offline.bridge_code = "NOT_CONNECTED"
    held = _deliver_with(offline)
    assert held["status"] == "FAILED" and held["held"] is True
    assert "held" not in _deliver_with(WhatsAppSendError("rejected", delivery_uncertain=False))
    uncertain = WhatsAppSendError("lost", delivery_uncertain=True)
    uncertain.bridge_code = "NOT_CONNECTED"
    result = _deliver_with(uncertain)
    assert result["status"] == "UNCERTAIN" and "held" not in result  # never resend what may have been sent


def test_banner_and_composer_explain_an_outage():
    source = Path("app/static/support/actions.js").read_text()
    helpers = source[source.index("const LINE_HEALTH_INTERVAL_MS"):source.index("function linkedDeviceAdditionStorageKey(")]
    script = r"""
const assert = require('node:assert/strict');
function element() {return {textContent: '', classList: {hidden: true, toggle(_, value) {this.hidden = value}}}}
const elements = {lineAlertBanner: element(), lineAlertText: element(), lineAlertActionBtn: element(), composerHelp: element()};
const $ = id => elements[id];
const text = (node, value) => {node.textContent = value};
let staff = false;
const canLinkWhatsApp = () => staff;
const sounds = [];
const notifyNewCustomerActivity = message => sounds.push(message);
const state = {authenticated: true, lineHealthAt: 0, lineAlertsSeen: new Set(),
  currentTicketData: {ticket: {business_phone_number_id: '90020'}, permissions: {can_reply: true}}};
""" + helpers + r"""
const down = {lines: [{line_id: '90020', name: 'Saida Office', status: 'logged_out', since: '2026-10-31T08:10:27Z', alert: true}]};
renderLineHealth(down);
assert.equal(elements.lineAlertBanner.classList.hidden, false);
assert.match(elements.lineAlertText.textContent, /^Saida Office was logged out by WhatsApp since .+\. It keeps trying to reconnect\. Replies wait and are sent automatically/);
assert.equal(elements.lineAlertActionBtn.classList.hidden, true, 'no re-pair button without link rights');
assert.deepEqual(sounds, [], 'nobody who cannot fix it is paged');
assert.match(elements.composerHelp.textContent, /^Saida Office is offline since .+ sent automatically when it reconnects/);
staff = true; state.lineAlertsSeen.clear();  // every staff role can re-pair
renderLineHealth(down); renderLineHealth(down);
assert.match(elements.lineAlertText.textContent, /Re-pair it with the same phone; every conversation and its history stays\. Replies wait/);
assert.equal(elements.lineAlertActionBtn.classList.hidden, false);
assert.equal(sounds.length, 1, 'one sound per outage');
renderLineHealth({lines: [{line_id: '90020', name: 'Saida Office', status: 'connected', since: null, alert: false}]});
assert.equal(elements.lineAlertBanner.classList.hidden, true);
assert.equal(elements.composerHelp.textContent, 'Messages are sent from the WhatsApp number for this conversation.');
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
