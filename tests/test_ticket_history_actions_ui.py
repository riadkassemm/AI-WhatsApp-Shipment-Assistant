"""History action routing, stale responses, and selection use original ticket IDs."""

import json
import shutil
import subprocess

import pytest

from app.support_ui import _ASSETS


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")


def run_node(script: str) -> None:
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("scenario", ["current", "history", "remove", "failure", "switch", "read_only", "deleted"])
def test_history_reactions_keep_current_ticket_and_draft(scenario: str) -> None:
    rendering = _ASSETS["rendering.js"]
    reaction = rendering[
        rendering.index("async function sendMessageReaction("):
        rendering.index("function messageReactionBar(")
    ]
    ticket_id = rendering[
        rendering.index("function messageTicketId("):
        rendering.index("function startMessageEdit(")
    ]
    script = "const scenario = " + json.dumps(scenario) + ";\n" + r"""
const assert = require('node:assert/strict');
const message = {id: 12, ticket_id: scenario === 'current' ? 42 : 9,
  whatsapp_message_id: 'old-provider-id', body: 'Historical question',
  reactions: [{reactor_type: 'CUSTOMER', emoji: '❤️'}]};
if (scenario === 'remove') message.reactions.push({reactor_type: 'AGENT', emoji: '👍'});
if (scenario === 'deleted') message.deleted_for_everyone_at = 'now';
const state = {ticketId: 42, composerEnabled: scenario !== 'read_only', busy: false,
  currentTicketData: {messages: [message]}, previousTicketHistory: [{ticket: {id: 9}, messages: [message]}],
  replyTarget: {id: 88, ticketId: 42}, pendingMedia: {file: 'draft.png'}};
const draft = {text: 'Keep this reply', start: 3, end: 8};
const calls = [], opened = [], changes = [], notices = [], busy = [];
const crypto = {randomUUID: () => 'request-id'};
const setWorkspaceBusy = value => busy.push(value);
const showError = () => {};
const notify = (...args) => notices.push(args);
const changeCachedMessage = (...args) => changes.push(args);
const renderConversation = () => {};
const focusMessageComposer = () => { throw Error('Reaction must preserve focus and caret'); };
let resolveRequest, rejectRequest;
const api = (path, options) => {
  calls.push({path, body: JSON.parse(options.body)});
  return new Promise((resolve, reject) => {resolveRequest = resolve; rejectRequest = reject});
};
const openTicket = async (id, options) => opened.push({id, options});
""" + ticket_id + reaction + r"""
(async () => {
  const menu = {open: true};
  const task = sendMessageReaction(message, '👍', menu);
  if (scenario === 'read_only' || scenario === 'deleted') {
    await task; assert.deepEqual(calls, []); assert.equal(state.busy, false); return;
  }
  assert.equal(state.busy, true); assert.equal(menu.open, false);
  if (scenario === 'switch') state.ticketId = 84;
  const savedDraft = JSON.stringify(draft), reply = state.replyTarget, media = state.pendingMedia;
  if (scenario === 'failure') rejectRequest(Error('Reaction failed'));
  else resolveRequest({reaction: {reactor_type: 'AGENT', emoji: scenario === 'remove' ? '' : '👍'}});
  await task;
  assert.deepEqual(calls, [{
    path: '/api/support/tickets/' + message.ticket_id + '/messages/12/reaction',
    body: {emoji: scenario === 'remove' ? '' : '👍', client_reaction_id: 'request-id',
      ...(scenario === 'current' ? {} : {context_ticket_id: 42})}
  }]);
  assert.equal(state.busy, false); assert.deepEqual(busy, [true, false]);
  assert.equal(state.ticketId, scenario === 'switch' ? 84 : 42);
  assert.equal(JSON.stringify(draft), savedDraft);
  assert.equal(state.replyTarget, reply); assert.equal(state.pendingMedia, media);
  if (scenario === 'failure' || scenario === 'switch') {
    assert.deepEqual(opened, []); assert.deepEqual(changes, []);
  } else {
    assert.deepEqual(opened, [{id: 42, options: {
      silent: true, refreshQueue: true, refreshHistory: scenario !== 'current'
    }}]);
    assert.equal(changes[0][0], message.ticket_id);
    assert.equal(changes[0][2].reactions.filter(item => item.reactor_type === 'AGENT').length, 1);
    assert.equal(changes[0][2].reactions[0].reactor_type, 'CUSTOMER');
  }
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    run_node(script)


def test_history_selection_forwarding_and_deleted_reply_targets() -> None:
    rendering = _ASSETS["rendering.js"]
    actions = _ASSETS["actions.js"]
    selection = actions[actions.index("function showForwardError("):actions.index("function showTransferError(")]
    cache = rendering[rendering.index("function changeCachedMessage("):rendering.index("async function refreshHistoricalMessage(")]
    reply = rendering[rendering.index("function startReplyToMessage("):rendering.index("function canChangeWhatsAppMessage(")]
    ticket_id = rendering[rendering.index("function messageTicketId("):rendering.index("function startMessageEdit(")]
    script = r"""
const assert = require('node:assert/strict');
const fields = new Map();
const $ = id => {
  if (!fields.has(id)) fields.set(id, {textContent: '', style: {}, value: '',
    classList: {toggle() {}}, querySelectorAll: () => [], replaceChildren() {}, focus() {}});
  return fields.get(id);
};
const old = {id: 4, ticket_id: 9, whatsapp_message_id: 'old-id', body: 'First question', created_at: '2026-01-01'};
const recent = {id: 8, whatsapp_message_id: 'new-id', body: 'Second question', created_at: '2026-01-02'};
const thread = {ticket: {id: 9}, messages: [old]};
const state = {ticketId: 42, composerEnabled: true, busy: false, forwardBusy: false,
  selectedMessages: new Map(), selectingMessages: false, previousTicketHistory: [thread],
  currentTicketData: {messages: [recent]}, cachedCurrentMessages: [recent]};
const calls = [], dialogs = [];
const text = (element, value) => element.textContent = value;
const showError = message => {throw Error(message)};
const messagePreview = message => message.body;
const messageActor = () => 'Customer';
const messageOccurredAt = message => message.created_at;
const renderReplyDraft = () => {};
const clearReplyTarget = () => {state.replyTarget = null};
const setAppStatus = () => {};
const openDialog = id => dialogs.push(id), closeDialog = () => {};
const emptyMarkup = () => ({outer: {}});
const loadForwardChoices = async () => {};
const crypto = {randomUUID: () => 'forward-key'};
const setButtonBusy = () => {}, notify = () => {};
const refreshAllQueueCounts = async () => {};
const api = async (path, options) => {
  calls.push({path, body: JSON.parse(options.body)});
  return {message: {id: 100 + calls.length, send_status: 'SENT'}};
};
""" + ticket_id + selection + cache + reply + r"""
(async () => {
  startReplyToMessage(old);
  assert.deepEqual(state.replyTarget, {id: 4, ticketId: 9, actor: 'Customer', preview: 'First question'});
  selectChatMessage(recent); selectChatMessage(old);
  assert.equal(state.selectedMessages.get('8').ticket_id, 42, 'selection captures the source before later UI changes');
  assert.equal(state.selectedMessages.get('4').ticket_id, 9);
  await openForwardModal([...state.selectedMessages.values()]);
  assert.deepEqual(state.forwardTarget.map(item => item.ticketId), [9, 42]);
  state.forwardSelection = {kind: 'conversation', id: 55};
  await confirmForward();
  assert.deepEqual(calls.map(call => call.path), [
    '/api/support/tickets/9/messages/4/forward', '/api/support/tickets/42/messages/8/forward'
  ]);
  assert.ok(calls.every(call => call.body.destination_ticket_id === 55));
  assert.equal(state.selectedMessages.size, 0);
  assert.equal(state.ticketId, 42);
  selectChatMessage(old); selectChatMessage(recent);
  changeCachedMessage(9, 4, {...old, body: 'Edited historical question'});
  assert.equal(state.selectedMessages.get('4').body, 'Edited historical question');
  changeCachedMessage(9, 4, {...old, deleted_for_everyone_at: 'now'});
  assert.equal(state.selectedMessages.has('4'), false);
  assert.equal(state.selectedMessages.has('8'), true, 'other selections remain');
  assert.equal(state.replyTarget, null);
  selectChatMessage({...old, deleted_for_everyone_at: 'now'});
  assert.equal(state.selectedMessages.has('4'), false);
  await openForwardModal([{...old, deleted_for_everyone_at: 'now'}]);
  assert.equal(dialogs.length, 1, 'deleted messages cannot reenter forwarding');
  startReplyToMessage(recent);
  changeCachedMessage(42, 8, null, true);
  assert.equal(state.selectedMessages.size, 0);
  assert.equal(state.replyTarget, null);
  assert.equal(state.currentTicketData.messages.length, 0);
  assert.equal(state.cachedCurrentMessages.length, 0);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    run_node(script)


@pytest.mark.parametrize("scenario", ["refresh", "switch", "reselect", "unloaded"])
def test_reaction_refresh_targets_expanded_history_without_changing_selection(scenario: str) -> None:
    rendering = _ASSETS["rendering.js"]
    refresh = rendering[rendering.index("async function refreshHistoricalMessage("):rendering.index("function upsertTimelineMessage(")]
    script = "const scenario = " + json.dumps(scenario) + ";\n" + r"""
const assert = require('node:assert/strict');
const state = {ticketId: 42, historySelectionEpoch: 1, currentTicketData: {},
  previousTicketHistory: scenario === 'unloaded' ? [] : [{ticket: {id: 9}}]};
const calls = [], changes = [], renders = [];
const historyDetailBase = () => '/api/support/tickets/' + state.ticketId;
const changeCachedMessage = (...args) => changes.push(args);
const renderConversation = data => renders.push(data);
const timelineSignature = () => 'updated';
let finish;
const api = path => {calls.push(path); return new Promise(resolve => {finish = resolve})};
""" + refresh + r"""
(async () => {
  const task = refreshHistoricalMessage(9, 4);
  if (scenario === 'unloaded') {
    await task; assert.deepEqual(calls, []); return;
  }
  assert.deepEqual(calls, ['/api/support/tickets/42/history/9/messages?before_message_id=5&limit=1']);
  if (scenario === 'switch') state.ticketId = 84;
  if (scenario === 'reselect') state.historySelectionEpoch++;
  finish({messages: [{id: 4, ticket_id: 9, reactions: [{emoji: '👍'}]}]});
  await task;
  if (scenario === 'refresh') {
    assert.equal(changes.length, 1); assert.equal(changes[0][0], 9); assert.equal(changes[0][1], 4);
    assert.equal(renders.length, 1); assert.equal(state.timelineSignature, 'updated');
    assert.equal(state.ticketId, 42);
  } else {
    assert.deepEqual(changes, []); assert.deepEqual(renders, []);
  }
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    run_node(script)


def test_live_history_events_refresh_actions_and_drop_deleted_selections() -> None:
    events = _ASSETS["events.js"]
    handler = events[
        events.index("  source.onmessage = event => {"):
        events.index("\n}\n\nfunction handleAssignedChatNotification(")
    ]
    script = r"""
const assert = require('node:assert/strict');
const source = {};
const state = {supportEventSource: source, authenticated: true, ticketId: 42, view: 'mine',
  previousTicketHistory: [{ticket: {id: 9}}], realtimeMessages: new Map(),
  selectedMessages: new Map(), mediaSource: null};
const scopes = [], refreshes = [], changes = [], searches = [], notifications = [];
let selectionUpdates = 0;
const requestDashboardRefresh = (...args) => scopes.push(...args);
const refreshHistoricalMessage = async (...args) => refreshes.push(args);
const changeCachedMessage = (...args) => changes.push(args);
const refreshMessageSearchAfterChange = id => searches.push(id);
const updateMessageSelection = () => {selectionUpdates++};
const clearReplyTarget = () => {state.replyTarget = null};
const notifyNewCustomerActivity = message => notifications.push(message);
const upsertTimelineMessage = () => {throw Error('History event must not enter the current ticket timeline')};
const dispatch = payload => source.onmessage({data: JSON.stringify(payload)});
""" + handler + r"""
dispatch({type: 'message_reaction', authorized: true, ticket_id: 9, message_id: 4});
assert.deepEqual(refreshes, [[9, 4]], 'refresh the older source ticket row');
dispatch({type: 'message_reaction', authorized: false, ticket_id: 9, message_id: 4});
dispatch({type: 'message_reaction', authorized: true, ticket_id: 99, message_id: 4});
assert.equal(refreshes.length, 1, 'unrelated and unauthorized events cannot change loaded history');
const previousNotifications = notifications.length;
dispatch({type: 'message_reaction', authorized: true, reactor_type: 'AGENT', ticket_id: 9, message_id: 4});
assert.deepEqual(refreshes, [[9, 4], [9, 4]], 'staff reactions refresh their historical source row');
dispatch({type: 'message_reaction', authorized: true, reactor_type: 'AGENT', ticket_id: 42, message_id: 8});
assert.ok(scopes.includes('conversation'), 'staff reactions refresh the current ticket too');
assert.equal(notifications.length, previousNotifications, 'staff reactions never announce customer activity');
dispatch({type: 'message_reaction', authorized: true, reactor_type: 'CUSTOMER', ticket_id: 9, message_id: 4});
assert.equal(notifications.length, previousNotifications + 1, 'customer reaction alerts remain enabled');
for (const type of ['message_deleted_for_me', 'message_deleted_for_everyone']) {
  state.selectedMessages.set('4', {id: 4, ticket_id: 9});
  state.selectedMessages.set('8', {id: 8, ticket_id: 42});
  state.replyTarget = {id: 4, ticketId: 9};
  dispatch({type, authorized: true, ticket_id: 9, message_id: 4,
    ...(type === 'message_deleted_for_everyone' ? {message: {id: 4, deleted_for_everyone_at: 'now'}} : {})});
  assert.equal(state.selectedMessages.has('4'), false);
  assert.equal(state.selectedMessages.has('8'), true);
  assert.equal(state.replyTarget, null);
}
assert.equal(selectionUpdates, 2);
assert.ok(scopes.includes('history'));
assert.equal(changes[0][3], true, 'delete for me removes the cached history row');
assert.equal(changes[1][2].deleted_for_everyone_at, 'now');
assert.deepEqual(searches, [4, 4]);
assert.equal(state.ticketId, 42);
"""
    run_node(script)
