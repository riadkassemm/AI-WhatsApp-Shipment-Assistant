"""Deleting a message releases the workspace without resetting its composer."""

import json
import shutil
import subprocess

import pytest

from app.support_ui import _ASSETS


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize(
    "scenario",
    ["everyone", "me", "media", "failure", "read_only", "switch", "switch_read_only", "history"],
)
def test_message_delete_restores_current_workspace_controls(scenario: str) -> None:
    rendering = _ASSETS["rendering.js"]
    media = _ASSETS["media.js"]
    deletion = rendering[
        rendering.index("function closeMessageDelete("):
        rendering.index("async function sendMessageReaction(")
    ]
    workspace = rendering[
        rendering.index("function setWorkspaceBusy("):
        rendering.index("function latestInboundMessageId(")
    ]
    composer = media[
        media.index("function updateComposerControls("):
        media.index("function recordingMimeType(")
    ]
    enable_composer = media[
        media.index("function setComposerEnabled("):
        media.index("function updateComposerCount(")
    ]
    script = "const scenario = " + json.dumps(scenario) + ";\n" + r"""
const assert = require('node:assert/strict');
function element() {
  const classes = new Set();
  return {
    disabled: false, value: '', selectionStart: 0, selectionEnd: 0,
    selectionDirection: 'forward', attributes: {}, focusCalls: 0,
    classList: {
      toggle(name, force) { if (force) classes.add(name); else classes.delete(name) },
      contains(name) { return classes.has(name) }
    },
    setAttribute(name, value) { this.attributes[name] = value },
    querySelector() { return null },
    focus() { this.focusCalls++ }
  };
}
const fields = new Map();
const $ = id => {
  if (!fields.has(id)) fields.set(id, element());
  return fields.get(id);
};
let ticketActions = [element()];
const replyButton = element(), forwardButton = element();
const reactionMenu = element(), reactionSummary = element();
reactionMenu.querySelector = selector => selector === 'summary' ? reactionSummary : null;
const document = {querySelectorAll(selector) {
  if (selector === '[data-ticket-action="true"]') return ticketActions;
  if (selector === '.message-reply-btn') return [replyButton];
  if (selector === '.message-reaction-menu') return [reactionMenu];
  if (selector === '.message-forward-btn') return [forwardButton];
  throw Error('Unexpected selector: ' + selector);
}};
const setIcon = () => {};
const target = {id: 5, ticketId: scenario === 'history' ? 9 : 42, canDeleteEveryone: true};
const state = {
  ticketId: 42, busy: false, composerEnabled: true, deleteTarget: target,
  realtimeMessages: new Map([[String(target.ticketId) + ':5', {id: 5}]]),
  replyTarget: {id: 91, actor: 'Customer', preview: 'Original question'},
  pendingMedia: null, pendingMediaItems: [], editTarget: null, forwardBusy: false
};
$('message').value = 'Unsent reply for ticket 42';
$('message').selectionStart = 3;
$('message').selectionEnd = 8;
$('message').selectionDirection = 'backward';
if (scenario === 'media') {
  const draft = {id: 'draft-1', kind: 'image', file: {name: 'photo.png'}, caption: 'Keep this caption'};
  state.pendingMedia = draft;
  state.pendingMediaItems = [draft];
}
const opened = [], closed = [], notifications = [], changes = [], searchUpdates = [], calls = [];
const closeDialog = id => closed.push(id);
const notify = (...args) => notifications.push(args);
const changeCachedMessage = (...args) => changes.push(args);
const refreshMessageSearchAfterChange = id => searchUpdates.push(id);
const cancelMessageEdit = () => { throw Error('Unrelated composer edit was cancelled') };
let resolveRequest, rejectRequest;
const api = (path, options) => {
  calls.push({path, method: options.method, body: JSON.parse(options.body)});
  return new Promise((resolve, reject) => { resolveRequest = resolve; rejectRequest = reject });
};
const openTicket = async (id, options) => {
  opened.push({id, options});
  // A normal silent detail refresh redraws ticket actions and recomputes the
  // composer while the deletion request still owns the busy flag.
  assert.equal(state.busy, true);
  ticketActions = [element(), element()];
  for (const button of ticketActions) button.disabled = state.busy;
  setComposerEnabled(scenario !== 'read_only');
  assert.equal($('message').disabled, true);
};
function composerSnapshot() {
  return {
    text: $('message').value,
    start: $('message').selectionStart,
    end: $('message').selectionEnd,
    direction: $('message').selectionDirection,
    reply: state.replyTarget,
    media: state.pendingMedia,
    mediaItems: state.pendingMediaItems
  };
}
""" + deletion + workspace + composer + enable_composer + r"""
(async () => {
  updateComposerControls();
  assert.equal($('message').disabled, false);
  const scope = scenario === 'me' ? 'me' : 'everyone';
  const task = deleteMessage(scope);
  assert.equal(state.busy, true);
  assert.equal($('deleteMeBtn').disabled, true);
  assert.equal($('deleteEveryoneBtn').disabled, true);
  assert.equal($('message').disabled, true);
  assert.equal($('sendBtn').disabled, true);
  assert.equal(replyButton.disabled, true);
  assert.equal(forwardButton.disabled, true);
  assert.equal(reactionMenu.classList.contains('disabled'), true);
  assert.ok(ticketActions.every(button => button.disabled));

  const switched = scenario === 'switch' || scenario === 'switch_read_only';
  if (switched) {
    // Sidebar selection can change while the transport awaits confirmation.
    state.ticketId = 84;
    state.replyTarget = {id: 192, actor: 'Another customer', preview: 'Different question'};
    $('message').value = 'Draft belonging to ticket 84';
    $('message').selectionStart = 2;
    $('message').selectionEnd = 6;
    setComposerEnabled(scenario !== 'switch_read_only');
  }
  const expectedComposer = composerSnapshot();
  if (scenario === 'failure') rejectRequest(Error('WhatsApp did not confirm deletion'));
  else resolveRequest({message: {id: 5, deleted_for_everyone_at: scope === 'everyone' ? 'now' : null}});
  await task;

  assert.equal(state.ticketId, switched ? 84 : 42, 'the selected ticket survives completion');
  assert.equal(state.busy, false);
  assert.equal($('deleteMeBtn').disabled, false);
  assert.equal($('deleteEveryoneBtn').disabled, false);
  assert.ok(ticketActions.every(button => !button.disabled), 'refreshed ticket actions become clickable');
  const canReply = scenario !== 'read_only' && scenario !== 'switch_read_only';
  assert.equal(state.composerEnabled, canReply);
  assert.equal($('message').disabled, !canReply, 'reply permissions still govern the composer');
  assert.equal($('sendBtn').disabled, !canReply);
  assert.equal($('imageBtn').disabled, !canReply);
  assert.equal($('locationBtn').disabled, !canReply);
  assert.equal($('voiceBtn').disabled, !canReply || scenario === 'media');
  assert.equal(replyButton.disabled, !canReply);
  assert.equal(reactionMenu.classList.contains('disabled'), !canReply);
  assert.equal(reactionSummary.attributes['aria-disabled'], canReply ? 'false' : 'true');
  assert.equal(forwardButton.disabled, false, 'forwarding recovers even in a read-only ticket');
  assert.deepEqual(composerSnapshot(), expectedComposer, 'text and caret are unchanged');
  assert.equal(state.replyTarget, expectedComposer.reply, 'reply target is retained');
  assert.equal(state.pendingMedia, expectedComposer.media, 'staged media is retained');
  assert.equal(state.pendingMediaItems, expectedComposer.mediaItems, 'attachment list is retained');
  assert.equal($('message').focusCalls, 0, 'cleanup does not move the caret');
  assert.deepEqual(calls, [{
    path: '/api/support/tickets/' + target.ticketId + '/messages/5/delete',
    method: 'POST', body: {scope}
  }]);
  if (scenario === 'failure') {
    assert.equal(state.deleteTarget, target, 'failed deletion remains available to retry');
    assert.deepEqual(opened, []);
    assert.deepEqual(closed, []);
    assert.deepEqual(changes, []);
    assert.deepEqual(searchUpdates, []);
    assert.deepEqual(notifications, [['WhatsApp did not confirm deletion', 'error']]);
  } else {
    assert.equal(state.deleteTarget, null);
    assert.deepEqual(closed, ['messageDeleteModal']);
    assert.deepEqual(opened, switched ? [] : [{
      id: 42, options: {silent: true, refreshHistory: scenario === 'history'}
    }]);
    assert.equal(changes.length, 1);
    assert.equal(changes[0][0], target.ticketId);
    assert.equal(changes[0][1], target.id);
    assert.equal(changes[0][3], scope === 'me');
    assert.deepEqual(searchUpdates, [5]);
    assert.equal(state.realtimeMessages.has(String(target.ticketId) + ':5'), false);
    assert.deepEqual(notifications, [[scope === 'me'
      ? 'Message removed from your support history.'
      : 'Message deleted for everyone on WhatsApp.']]);
  }
})().catch(error => { console.error(error); process.exitCode = 1 });
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
