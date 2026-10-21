import shutil
import subprocess

import pytest

from app.support_ui import _HTML


def run_node(script):
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is unavailable')
def test_reopen_button_uses_backend_permission_and_resolved_customer_state():
    functions = _HTML[_HTML.index('function actionButton('):_HTML.index('function setWorkspaceBusy(')]
    run_node(r"""
const assert = require('node:assert/strict');
const element = () => ({children: [], dataset: {}, append(...items) {this.children.push(...items)},
  replaceChildren() {this.children = []}, querySelector() {return null}, setAttribute() {}});
const document = {createElement: element, activeElement: null};
const container = element(), state = {ticketId: 42, busy: false};
const $ = () => container, createIcon = element;
const isStaffChat = ticket => ticket.conversation_origin === 'STAFF';
const reopenTicket = () => {}, openTransferModal = () => {}, closeStaffChat = () => {};
""" + functions + r"""
for (const [allowed, status, origin, active, expected] of [
  [true, 'RESOLVED', 'CUSTOMER', null, 1],
  [false, 'RESOLVED', 'CUSTOMER', null, 0],
  [true, 'CLOSED', 'CUSTOMER', null, 0],
  [true, 'RESOLVED', 'STAFF', null, 0],
  [true, 'RESOLVED', 'CUSTOMER', 'key', 0],
]) {
  renderActions({ticket: {status, conversation_origin: origin, active_key: active}, permissions: {can_reopen: allowed}});
  assert.equal(container.children.length, expected);
  if (expected) {
    assert.equal(container.children[0].title, 'Reopen ticket');
    assert.equal(container.children[0].onclick, reopenTicket);
  }
}
""")


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is unavailable')
def test_reopen_waits_for_backend_success_before_refreshing_composer_permissions():
    function = _HTML[_HTML.index('async function reopenTicket('):_HTML.index('async function closeStaffChat(')]
    run_node(r"""
const assert = require('node:assert/strict');
const state = {busy: false, ticketId: 42, agent: {id: '7'}, currentTicketData: {
  ticket: {resolved_at: '2026-10-21T10:00:00.123456Z'}, permissions: {can_reopen: true}
}};
const calls = [], notifications = [];
const setWorkspaceBusy = busy => calls.push(['busy', busy]);
const showError = () => {}, notify = (...args) => notifications.push(args);
const setQueueView = (...args) => calls.push(['view', ...args]);
const openTicket = async (...args) => calls.push(['open', ...args]);
let resolve;
let api = async (path, options) => {
  assert.equal(path, '/api/support/tickets/42/reopen');
  assert.deepEqual(JSON.parse(options.body), {resolved_at: '2026-10-21T10:00:00.123456Z'});
  return new Promise(done => {resolve = done});
};
""" + function + r"""
(async () => {
  const pending = reopenTicket();
  assert.equal(state.busy, true);
  assert.equal(calls.some(c => c[0] === 'open'), false);
  resolve({ticket: {current_assignee_id: '7'}});
  await pending;
  assert.equal(state.busy, false);
  assert.equal(calls.find(c => c[0] === 'view')[1], 'mine');
  assert.deepEqual(calls.find(c => c[0] === 'open'), ['open', 42, {silent: true, refreshQueue: true}]);
  calls.length = 0;
  api = async () => {throw Error('Active conversation conflict')};
  await reopenTicket();
  assert.equal(calls.some(c => ['open', 'view'].includes(c[0])), false);
  assert.equal(notifications.at(-1)[1], 'error');
  calls.length = 0;
  state.agent.id = '8';
  state.agent.role = 'supervisor';
  api = async () => ({ticket: {current_assignee_id: '8'}});
  await reopenTicket();
  assert.equal(calls.find(c => c[0] === 'view')[1], 'mine');
  calls.length = 0;
  // A duplicate request after another manager won must follow the stored owner.
  api = async () => ({ticket: {current_assignee_id: '7'}});
  await reopenTicket();
  assert.equal(calls.find(c => c[0] === 'view')[1], 'active');
})().catch(error => {console.error(error); process.exitCode = 1});
""")


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is unavailable')
def test_reopen_realtime_invalidation_reloads_permissions_and_discards_stale_history():
    # events.js is an independent asset; execute the real handler with a small DOM-free harness.
    from pathlib import Path
    functions = (Path(__file__).resolve().parents[1] / 'app/static/support/events.js').read_text()
    run_node(r"""
const assert = require('node:assert/strict');
let source;
class EventSource { constructor() {source = this} close() {} }
const state = {authenticated: true, query: '', ticketId: 42, ticketPrefetch: new Map([['old', {}]]),
  previousTicketHistory: [{ticket: {id: 1}}], previousHistoryExpanded: true, previousHistoryLoaded: true};
const calls = [];
const refreshAllQueueCounts = async () => calls.push('counts');
const loadQueue = async () => calls.push('queue');
const openTicket = async (...args) => calls.push(args);
""" + functions + r"""
connectSupportEvents();
source.onmessage({data: JSON.stringify({type: 'ticket_queues_changed', authorized: false})});
assert.equal(calls.length, 0);
source.onmessage({data: JSON.stringify({type: 'ticket_queues_changed', authorized: true})});
assert.equal(state.ticketPrefetch.size, 0);
assert.deepEqual(state.previousTicketHistory, []);
assert.equal(state.previousHistoryExpanded, false);
assert.deepEqual(calls, ['counts', 'queue', [42, {silent: true, refreshHistory: true}]]);
""")
