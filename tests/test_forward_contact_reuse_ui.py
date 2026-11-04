"""Contact-forward batches retain contact routing and safe retry state."""

import json
import shutil
import subprocess

import pytest

from app.support_ui import _ASSETS


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")


@pytest.mark.parametrize("scenario", ["success", "failure", "unconfirmed", "uncertain", "conversation"])
def test_contact_forward_batch_reuses_contact_route_and_preserves_draft(scenario: str) -> None:
    actions = _ASSETS["actions.js"]
    forwarding = actions[
        actions.index("async function confirmForward("):
        actions.index("function showTransferError(")
    ]
    script = "const scenario = " + json.dumps(scenario) + ";\n" + r"""
const assert = require('node:assert/strict');
const selectedContact = {kind: 'contact', key: 'contact:alice', id: 'alice',
  phone: '155500099', name: 'Alice'};
const destination = scenario === 'conversation'
  ? {kind: 'conversation', id: 55} : selectedContact;
const originals = [
  {ticketId: 9, messageId: 4, clientId: 'first-attempt'},
  {ticketId: 42, messageId: 8, clientId: 'second-attempt'},
  {ticketId: 10, messageId: 12, clientId: 'third-attempt'}
];
const draft = {value: 'Keep this draft', selectionStart: 3, selectionEnd: 8};
const pendingMedia = {name: 'draft.png'}, replyTarget = {id: 77, ticketId: 42};
const ticket = {ticket: {id: 42}, permissions: {can_send: true}};
const state = {ticketId: 42, currentTicketData: ticket, pendingMedia, replyTarget,
  forwardBusy: false, forwardTarget: originals.map(item => ({...item})),
  forwardSelection: {...destination}, selectedMessages: new Map(originals.map(item =>
    [String(item.messageId), {...item}])), selectingMessages: true};
const fields = new Map([['message', draft]]);
const $ = id => {
  if (!fields.has(id)) fields.set(id, {disabled: false});
  return fields.get(id);
};
const calls = [], errors = [], closed = [], notices = [], updates = [];
const showForwardError = message => {if (message) errors.push(message)};
const setButtonBusy = () => {};
const updateMessageSelection = () => updates.push([...state.selectedMessages.keys()]);
const clearMessageSelection = () => {
  state.selectedMessages.clear(); state.selectingMessages = false;
};
const closeDialog = id => closed.push(id);
const notify = message => notices.push(message);
let refreshed = 0, retrying = false;
const refreshAllQueueCounts = async () => {refreshed++};
const openTicket = () => {throw Error('Forwarding must not switch the selected chat')};
const focusMessageComposer = () => {throw Error('Forwarding must not reset the caret')};
const api = async (path, options) => {
  const body = JSON.parse(options.body);
  calls.push({path, body});
  if (!retrying && body.client_message_id === 'second-attempt') {
    if (scenario === 'failure') throw Error('Connection interrupted');
    if (scenario === 'unconfirmed') return {destination: {id: 55}, message: {}};
    if (scenario === 'uncertain') return {
      destination: {id: 55}, message: {id: 102, send_status: 'UNCERTAIN'}
    };
  }
  return {destination: {id: 55}, message: {
    id: 100 + originals.findIndex(item => item.clientId === body.client_message_id),
    send_status: 'SENT'
  }};
};
""" + forwarding + r"""
const assertPreserved = () => {
  assert.equal(state.ticketId, 42);
  assert.equal(state.currentTicketData, ticket);
  assert.equal(state.pendingMedia, pendingMedia);
  assert.equal(state.replyTarget, replyTarget);
  assert.deepEqual(draft, {value: 'Keep this draft', selectionStart: 3, selectionEnd: 8});
  assert.equal(state.forwardBusy, false);
};
const assertRequests = expected => {
  assert.deepEqual(calls, expected.map(index => ({
    path: '/api/support/tickets/' + originals[index].ticketId + '/messages/' +
      originals[index].messageId + (scenario === 'conversation' ? '/forward' : '/forward-contact'),
    body: scenario === 'conversation'
      ? {destination_ticket_id: 55, client_message_id: originals[index].clientId}
      : {phone_number: selectedContact.phone, customer_name: selectedContact.name,
          client_message_id: originals[index].clientId}
  })));
};
(async () => {
  await confirmForward();
  assertPreserved();
  if (['failure', 'unconfirmed', 'uncertain'].includes(scenario)) {
    assertRequests([0, 1]);
    assert.deepEqual(closed, []); assert.deepEqual(notices, []); assert.equal(refreshed, 0);
    assert.deepEqual(state.forwardSelection, selectedContact,
      'an existing destination must not convert contact forwarding into an owned-chat route');
    assert.equal($('confirmForwardBtn').disabled, false);
    assert.equal(errors.length, 1);
    if (scenario === 'uncertain') {
      assert.match(errors[0], /Forwarding paused/);
      assert.deepEqual(state.forwardTarget, [originals[2]],
        'a persisted uncertain attempt must not be blindly resent');
      assert.deepEqual([...state.selectedMessages.keys()], ['12']);
    } else {
      assert.deepEqual(state.forwardTarget, originals.slice(1),
        'an unconfirmed attempt retains its original idempotency key');
      assert.deepEqual([...state.selectedMessages.keys()], ['8', '12']);
    }
    retrying = true;
    await confirmForward();
    assertPreserved();
    assertRequests(scenario === 'uncertain' ? [0, 1, 2] : [0, 1, 1, 2]);
  } else {
    assertRequests([0, 1, 2]);
    assert.deepEqual(errors, []);
  }
  assert.deepEqual(closed, ['forwardModal']);
  assert.deepEqual(notices, ['Selected messages forwarded.']);
  assert.equal(refreshed, 1);
  assert.equal(state.forwardTarget, null); assert.equal(state.forwardSelection, null);
  assert.equal(state.selectedMessages.size, 0); assert.equal(state.selectingMessages, false);
  assert.equal($('confirmForwardBtn').disabled, true);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
