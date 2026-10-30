"""Exercise the embedded viewer/editor logic without a browser test framework."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.support_ui import _HTML, support_image_editor_script
from app.config import settings

# Connection-health hooks used by queue/conversation loading: the shipped error
# classifier and no-op recorders (tests/dashboard_connection.test.cjs covers them).
CONNECTION_HOOKS = _HTML[_HTML.index("function transientReadError("):_HTML.index("function backgroundRetryDelay(")] + """
const noteBackgroundRead = () => {}, forgetBackgroundRead = () => {}, resetConnectionHealth = () => {};
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_message_copy_handles_text_and_clipboard_fallback() -> None:
    helpers = _HTML[_HTML.index("function messageTextForCopy("): _HTML.index("function renderReplyDraft(")]
    script = r"""
const assert = require('node:assert/strict');
const notifications = [];
const notify = (...args) => notifications.push(args);
let clipboardValue = '';
const navigator = {clipboard: {writeText: async value => {clipboardValue = value}}};
let fallbackValue = '';
const document = {
  body: {append(field) {this.field = field}},
  createElement() {return {value: '', style: {}, select() {fallbackValue = this.value}, remove() {}}},
  execCommand(command) {assert.equal(command, 'copy'); return true}
};
""" + helpers + r"""
(async () => {
  const text = '  Tracking 123\nhttps://example.com  ';
  assert.equal(messageTextForCopy({body: text}), text, 'copy keeps the exact message text');
  assert.equal(messageTextForCopy({body: text, deleted_for_everyone_at: 'now'}), '');
  assert.equal(messageTextForCopy({body: 'Voice note', media_kind: 'audio', media_url: '/audio'}), '');
  assert.equal(messageTextForCopy({body: 'caption', media_kind: 'image', media_url: '/image'}), 'caption');
  await copyMessageText(text);
  assert.equal(clipboardValue, text);
  navigator.clipboard.writeText = async () => {throw new Error('Permission denied')};
  await copyMessageText('Fallback text');
  assert.equal(fallbackValue, 'Fallback text');
  assert.deepEqual(notifications, [['Message copied.'], ['Message copied.']]);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "copy.onclick = () => copyMessageText(bodyText);" in _HTML
    assert ".message-body {" in _HTML and "user-select: text !important;" in _HTML
    assert "::selection { background: var(--primary); color: var(--primary-contrast); }" in _HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_selected_conversation_text_survives_refresh_and_ctrl_c() -> None:
    selection_helpers = _HTML[_HTML.index("function selectedConversationText("): _HTML.index("function renderConversation(")]
    script = r"""
const assert = require('node:assert/strict');
const inside = {id: 'message text'};
const outside = {id: 'composer'};
const conversation = {contains(node) {return node === inside}};
const $ = id => {assert.equal(id, 'conversation'); return conversation};
let selection = {isCollapsed: false, anchorNode: inside, focusNode: inside, toString() {return 'selected customer text'}};
const scheduled = [];
const window = {getSelection() {return selection}, setTimeout(callback) {scheduled.push(callback)}};
const state = {ticketId: 42, pendingConversationRender: null, conversationSelectionPointer: null,
  currentTicketData: {ticket: {id: 42}, messages: [{body: 'latest'}]}};
const renders = [];
const renderConversation = (data, options) => renders.push([data, options]);
const timelineSignature = () => 'latest-signature';
""" + selection_helpers + r"""
assert.equal(selectedConversationText(), 'selected customer text');
const clipboard = {value: '', setData(type, value) {assert.equal(type, 'text/plain'); this.value = value}};
let prevented = false;
handleConversationCopy({clipboardData: clipboard, preventDefault() {prevented = true}});
assert.equal(clipboard.value, 'selected customer text');
assert.equal(prevented, true);
state.pendingConversationRender = {data: state.currentTicketData, options: {preserveAudio: true}};
beginConversationSelection({button: 0, pointerId: 7, target: {closest: () => ({})}});
selection = {isCollapsed: true, anchorNode: inside, focusNode: inside, toString() {return ''}};
flushPendingConversationRender();
assert.equal(renders.length, 0, 'refresh waits from pointer down, before text is highlighted');
selection = {isCollapsed: false, anchorNode: inside, focusNode: inside, toString() {return 'selected customer text'}};
finishConversationSelection({pointerId: 7});
scheduled.shift()();
assert.equal(renders.length, 0, 'refresh waits after the pointer is released while text remains selected');
flushPendingConversationRender();
assert.equal(renders.length, 0, 'new messages wait while text remains selected');
selection = {isCollapsed: true, anchorNode: inside, focusNode: inside, toString() {return ''}};
flushPendingConversationRender();
assert.equal(renders.length, 1);
assert.equal(renders[0][1].preserveAudio, true);
assert.equal(state.timelineSignature, 'latest-signature');
assert.equal(state.pendingConversationRender, null);
selection = {isCollapsed: false, anchorNode: outside, focusNode: outside, toString() {return 'draft'}};
assert.equal(selectedConversationText(), '', 'the composer keeps its native copy behavior');
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "if (!forceScroll && (state.conversationSelectionPointer !== null || selectedConversationText())) {" in _HTML
    assert "$('conversation').addEventListener('pointerdown', beginConversationSelection);" in _HTML
    assert "document.addEventListener('copy', handleConversationCopy);" in _HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_outbound_whatsapp_event_refreshes_queue_without_interrupting_open_ticket() -> None:
    functions = _HTML[
        _HTML.index("function disconnectSupportEvents("):
        _HTML.index("function setButtonBusy(")
    ]
    script = r"""
const assert = require('node:assert/strict');
let source;
class EventSource {
  constructor(path, options) {
    assert.equal(path, '/api/support/events');
    assert.equal(options.withCredentials, true);
    source = this;
  }
  close() {}
}
const state = {authenticated: true, supportEventSource: null, ticketId: 42,
  view: 'mine', previousTicketHistory: []};
let queueRefreshes = 0;
let countRefreshes = 0;
let ticketReloads = 0;
let customerNotifications = 0;
const upserts = [];
const loadQueue = async options => {
  assert.deepEqual(options, {silent: true});
  queueRefreshes++;
};
const refreshAllQueueCounts = async () => {countRefreshes++};
const requestDashboardRefresh = scope => {if(scope==='counts')refreshAllQueueCounts();else if(scope==='queue')loadQueue({silent:true});else openTicket(state.ticketId,{silent:true});};
const openTicket = async () => {ticketReloads++};
const upsertTimelineMessage = (message, ticketId) => upserts.push([message.id, ticketId]);
const notifyNewCustomerActivity = () => {customerNotifications++};
""" + functions + r"""
connectSupportEvents();
const emit = payload => source.onmessage({data: JSON.stringify(payload)});
emit({type: 'outbound_message', authorized: true, ticket_id: 99,
  message: {id: 1001, direction: 'OUTBOUND'}});
assert.equal(queueRefreshes, 1, 'a message in another ticket updates its queue position');
assert.equal(countRefreshes, 1);
assert.equal(ticketReloads, 0, 'the currently open conversation stays mounted');
assert.equal(customerNotifications, 0);
assert.deepEqual(upserts, []);

emit({type: 'outbound_message', authorized: true, ticket_id: 42,
  message: {id: 1002, direction: 'OUTBOUND'}});
assert.equal(queueRefreshes, 2);
assert.deepEqual(upserts, [[1002, 42]], 'the current conversation receives the message');
assert.equal(ticketReloads, 0, 'voice-note playback is not disturbed by a full reload');
assert.equal(customerNotifications, 0, 'staff-side sends must not alert as customer activity');

emit({type: 'outbound_message', authorized: false, ticket_id: 42,
  message: {id: 1003, direction: 'OUTBOUND'}});
assert.equal(queueRefreshes, 2, 'unauthorized events do not touch the queue');
assert.deepEqual(upserts, [[1002, 42]]);
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_complete_removal_is_available_for_active_and_failed_pairings() -> None:
    function = _HTML[_HTML.index("function renderLinkedDeviceStatus("): _HTML.index("async function loadLinkedDeviceStatus(")]
    script = r"""
const assert = require('node:assert/strict');
function element() {return {children: [], classList: {toggle() {}},
  replaceChildren() {this.children = []}, append(...children) {this.children.push(...children)}}}
const document = {createElement: element};
const elements = {linkedDeviceSection: element(), linkedDeviceLines: element(),
  addLinkedDeviceBtn: element(), linkedDeviceStatus: element()};
const $ = key => elements[key];
const text = (element, value) => {element.textContent = value};
const state = {linkedDeviceBusy: false, whatsappBusy: false, whatsappRegistry: {numbers: [
  {phone_number_id: '90001', is_enabled: true}, {phone_number_id: '90002', is_enabled: false}]}};
const pendingLinkedDeviceAddition = () => null;
const createSupportNumberLabel = line => {const node = element(); node.textContent = line.display_name || line.display_phone_number || line.line_id; return node};
const changes = [];
const changeLinkedDeviceConnection = (...args) => changes.push(args);
const removeLinkedDeviceNumber = id => changes.push(['remove', id]);
""" + function + r"""
renderLinkedDeviceStatus({configured: true, lines: [
  {line_id: '90001', status: 'connected', is_paired: true, waiting_history_reactions: 2},
  {line_id: '90002', status: 'error', is_paired: true, error: 'Number already linked', pending_callbacks: 1, failed_callbacks: 1}]});
const rows = elements.linkedDeviceLines.children;
assert.equal(rows[0].children[1].textContent, 'Connected');
assert.match(rows[0].children[1].title, /2 reactions are waiting for older messages/);
assert.match(rows[1].children[1].textContent, /1 updates pending/);
assert.match(rows[1].children[1].textContent, /1 failed updates need administrator attention/);
assert.equal(rows[0].children[2].textContent, 'Remove and unlink');
assert.equal(rows[0].children[2].disabled, false);
assert.equal(rows[1].children[2].textContent, 'Pair WhatsApp');
const unlink = rows[1].children[3];
assert.equal(unlink.textContent, 'Remove and unlink');
assert.equal(unlink.disabled, false);
unlink.onclick();
assert.deepEqual(changes, [['remove', '90002']]);
"""
    subprocess.run(["node", "-e", script], check=True, timeout=10)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_remove_linked_number_requires_confirmation_and_waits_for_remote_success() -> None:
    function = _HTML[_HTML.index("async function removeLinkedDeviceNumber("): _HTML.index("async function changeLinkedDeviceConnection(")]
    script = r"""
const assert = require('node:assert/strict');
const state = {linkedDeviceBusy: false, whatsappBusy: false, linkedDeviceRequest: 0,
  linkedDeviceStatus: {configured: true, lines: [{line_id: '90001', status: 'connected'}, {line_id: '90002', status: 'connected'}]},
  whatsappRegistry: {numbers: [{phone_number_id: '90001'}, {phone_number_id: '90002'}]}};
let manager = true;
const canManageWhatsApp = () => manager;
let consent = false;
const confirmDialog = async ({message}) => {assert.match(message, /Active conversations.*closed/); assert.match(message, /history.*kept/); return consent};
const clearWhatsAppFeedback = () => {};
const renderLinkedDeviceStatus = payload => {state.linkedDeviceStatus = payload};
const renderWhatsAppRegistry = payload => {state.whatsappRegistry = payload};
let refreshes = 0;
const loadWhatsAppRegistry = async () => {refreshes++};
let cleared = false;
const saveLinkedDeviceAddition = id => {assert.equal(id, null); cleared = true};
const feedback = [];
const showWhatsAppFeedback = (kind, message) => feedback.push({kind, message});
const requests = [];
let finish;
const api = async (path, options) => {
  requests.push({path, method: options.method, body: JSON.parse(options.body)});
  return await new Promise(resolve => {finish = resolve});
};
""" + function + r"""
(async () => {
  await removeLinkedDeviceNumber('90002');
  assert.equal(requests.length, 0); // Cancel keeps both numbers.
  consent = true;
  manager = false;
  await removeLinkedDeviceNumber('90002');
  assert.equal(requests.length, 0);
  manager = true;
  const removing = removeLinkedDeviceNumber('90002');
  await new Promise(resolve => setTimeout(resolve, 0)); // let the in-app confirmation settle
  assert.equal(state.linkedDeviceBusy, true);
  assert.equal(state.linkedDeviceStatus.lines.length, 2);
  assert.equal(state.whatsappRegistry.numbers.length, 2);
  assert.equal(feedback.length, 0); // No success or optimistic disappearance.
  assert.deepEqual(requests, [{path: '/api/support/whatsapp-linked-device/lines/90002', method: 'DELETE', body: {close_active_tickets: true}}]);
  await removeLinkedDeviceNumber('90002');
  assert.equal(requests.length, 1); // No duplicate request while waiting.
  finish({configured: true, lines: [{line_id: '90001', status: 'connected'}],
    registry: {numbers: [{phone_number_id: '90001'}]}});
  await removing;
  assert.equal(state.linkedDeviceBusy, false);
  assert.deepEqual(state.linkedDeviceStatus.lines, [{line_id: '90001', status: 'connected'}]);
  assert.deepEqual(state.whatsappRegistry.numbers, [{phone_number_id: '90001'}]);
  assert.equal(cleared, true);
  assert.equal(refreshes, 1);
  assert.equal(feedback[0].kind, 'whatsappSuccess');
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    subprocess.run(["node", "-e", script], check=True, timeout=10)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_linking_number_retries_same_request_and_keeps_manager_busy_guard() -> None:
    function = _HTML[_HTML.index("function linkedDeviceAdditionStorageKey()"): _HTML.index("async function changeLinkedDeviceConnection(")]
    script = r"""
const assert = require('node:assert/strict');
const state = {agent: {id: '10', linked_device_available: true}, linkedDeviceBusy: false,
  whatsappBusy: false, linkedDeviceRequest: 0, linkedDeviceAdditionId: null,
  linkedDeviceStatus: null};
let manager = true;
const canManageWhatsApp = () => manager;
let uuidCalls = 0;
const crypto = {randomUUID() {return '00000000-0000-4000-8000-00000000000' + (++uuidCalls)}};
const storage = new Map();
const sessionStorage = {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)};
const button = {};
const $ = () => button;
const clearWhatsAppFeedback = () => {};
const setButtonBusy = (element, busy) => {element.disabled = busy};
const feedback = [];
const showWhatsAppFeedback = (kind, message) => feedback.push({kind, message});
const registries = [];
const renderWhatsAppRegistry = registry => registries.push(registry);
const renderLinkedDeviceStatus = payload => {state.linkedDeviceStatus = payload};
let polls = 0;
const loadLinkedDeviceStatus = async () => {polls++};
const loadWhatsAppRegistry = async () => {polls++};
const requests = [];
const api = async (path, options) => {
  assert.equal(path, '/api/support/whatsapp-linked-device/lines');
  assert.equal(options.method, 'POST');
  assert.equal(state.linkedDeviceBusy, true);
  requests.push(JSON.parse(options.body));
  if (requests.length === 1) throw new Error('Response lost');
  return {configured: true, lines: [{line_id: '90001', status: 'connected'},
    {line_id: requests.length === 2 ? '90002' : '90003', status: 'qr'}],
    registry: {numbers: [{phone_number_id: '90001', is_enabled: true},
      {phone_number_id: requests.length === 2 ? '90002' : '90003', is_enabled: false}]}};
};
""" + function + r"""
(async () => {
  manager = false;
  await addLinkedDeviceNumber();
  assert.equal(requests.length, 0);
  manager = true;
  state.whatsappBusy = true;
  await addLinkedDeviceNumber();
  assert.equal(requests.length, 0);
  state.whatsappBusy = false;
  await addLinkedDeviceNumber();
  const originalRequest = requests[0].request_id;
  assert.equal(state.linkedDeviceAdditionId, originalRequest);
  assert.equal(storage.get('shipment-support-linked-device-addition:10'), originalRequest);
  assert.equal(state.linkedDeviceBusy, false);
  assert.match(feedback[0].message, /Retry linking/);
  state.linkedDeviceAdditionId = null; // A refreshed page restores its pending UUID.
  await addLinkedDeviceNumber();
  assert.equal(requests[1].request_id, originalRequest);
  assert.equal(uuidCalls, 1);
  assert.equal(state.linkedDeviceAdditionId, null);
  assert.equal(storage.size, 0);
  assert.equal(registries[0].numbers[0].is_enabled, true);
  assert.equal(registries[0].numbers[1].is_enabled, false);
  assert.equal(state.linkedDeviceStatus.lines[0].status, 'connected');
  await addLinkedDeviceNumber();
  assert.notEqual(requests[2].request_id, originalRequest);
  assert.equal(uuidCalls, 2);
  assert.equal(polls, 3);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    subprocess.run(["node", "-e", script], check=True, timeout=10)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_viewer_pointer_zoom_and_editor_object_history() -> None:
    zoom = _HTML[_HTML.index("function applyMediaZoom()"): _HTML.index("function openMediaViewer(")]
    editor = Path(__file__).resolve().parents[1].joinpath("app/support_image_editor.js").read_text()
    editor = editor.replace("__IMAGE_MAX_BYTES__", "5242880")
    script = r"""
const assert = require('node:assert/strict');
const window = {};
const image = {naturalWidth: 1000, naturalHeight: 800, style: {}};
const body = {
  clientWidth: 1000, clientHeight: 800, classList: {toggle() {}},
  querySelector() {return image},
  getBoundingClientRect() {return {left: 0, top: 0, width: 1000, height: 800}}
};
const inputs = {mediaViewerBody: body, mediaViewerZoom: {}, editorColor: {value: '#ff0000'},
  editorSize: {value: '8'}, editorCaption: {value: ''}};
const $ = key => inputs[key];
const text = (element, value) => {element.textContent = value};
const state = {mediaZoom: 1, mediaTranslateX: 0, mediaTranslateY: 0};
const context = {
  clearRect() {}, drawImage() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {},
  fillText() {}, strokeRect() {}, setLineDash() {}, measureText(s) {return {width: s.length * 8}},
  translate() {}, rotate() {}, fillRect() {}
};
const document = {createElement(name) {
  assert.equal(name, 'canvas');
  return {style: {}, width: 0, height: 0, tabIndex: 0, setAttribute() {}, addEventListener() {},
    setPointerCapture() {}, getContext() {return context},
    getBoundingClientRect() {return {left: 0, top: 0, width: 100, height: 100}},
    toBlob(callback, mime) {callback(new Blob(['pixels'], {type: mime}))}};
}};
const crypto = {randomUUID() {return 'test-id'}};
let answer = 'مرحبا';
const prompt = () => answer;
""" + zoom + editor + r"""
zoomMediaAt(2, 750, 400);
assert.equal(state.mediaZoom, 2);
assert.equal(state.mediaTranslateX, -250);
assert.equal(state.mediaTranslateY, 0);
state.mediaTranslateX += 40;
assert.equal(state.mediaTranslateX, -210);
resetMediaZoom();
assert.equal(state.mediaZoom, 1);
assert.equal(state.mediaTranslateX, 0);

(async () => {
const e = new ChatImageEditor({width: 100, height: 100}, 'image/png');
const changes = [];
e.onChange = editor => changes.push(editor.mode);
const pointer = (x, y) => ({pointerId: 1, clientX: x, clientY: y, preventDefault() {}});
e.pointerDown(pointer(20, 20));
e.pointerMove(pointer(30, 30));
e.pointerUp(pointer(30, 30));
assert.equal(e.objects[0].type, 'stroke');
assert.equal(e.objects[0].points.length, 2);
assert.equal(e.historyIndex, 1);
e.undo(); assert.equal(e.objects.length, 0);
e.redo(); assert.equal(e.objects.length, 1);
e.mode = 'select';
e.pointerDown(pointer(25, 25));
assert.equal(e.selected, 0);
e.removeSelected(); assert.equal(e.objects.length, 0);
e.undo(); assert.equal(e.objects.length, 1);
e.pointerDown(pointer(90, 90)); assert.equal(e.objects.length, 1);
e.mode = 'text';
await e.pointerDown(pointer(70, 70));
assert.equal(e.objects[1].text, 'مرحبا');
assert.equal(e.historyIndex, 2);
e.pointerDown(pointer(75, 75));
e.pointerMove(pointer(80, 78));
e.pointerUp(pointer(80, 78));
assert.equal(e.objects[1].x, 75);
assert.equal(e.objects[1].y, 73);
answer = 'updated';
await e.editText(); assert.equal(e.objects[1].text, 'updated');
e.removeSelected(); assert.equal(e.objects.length, 1);
e.undo(); assert.equal(e.objects.length, 2);
assert.equal((await e.blob()).type, 'image/png');
state.imageEditor = e;
applyMediaZoom();
assert.match(e.canvas.style.transform, /scale\(1\)/);
zoomMediaAt(2, 500, 400);
assert.match(e.canvas.style.transform, /scale\(2\)/);
e.mode = 'draw';
e.pointerDown(pointer(10, 10));
e.cancelActiveStroke();
assert.equal(e.pointer, null);
assert.equal(e.objects.length, e.history[e.historyIndex].objects.length);
assert.ok(changes.length > 0, 'the toolbar is told about changes');

// Rotate bakes the overlays into the image and swaps its size; undo restores both.
const editor = new ChatImageEditor({width: 200, height: 100}, 'image/png');
editor.pointerDown(pointer(10, 10)); editor.pointerMove(pointer(20, 20)); editor.pointerUp(pointer(20, 20));
let resized = 0;
editor.onResize = () => { resized += 1; };
editor.rotate();
assert.deepEqual([editor.canvas.width, editor.canvas.height], [100, 200]);
assert.equal(editor.objects.length, 0);
assert.equal(editor.ops[0].type, 'rotate');
assert.equal(editor.ops[0].baked.length, 1, 'the drawing is kept by baking it into the rotated image');
assert.equal(resized, 1);
editor.undo();
assert.deepEqual([editor.canvas.width, editor.canvas.height], [200, 100]);
assert.equal(editor.objects.length, 1);
editor.redo();
assert.deepEqual([editor.canvas.width, editor.canvas.height], [100, 200]);

// Crop: drag a rectangle (canvas is 100x100 CSS px here), then apply.
editor.setMode('crop');
editor.pointerDown(pointer(10, 20)); editor.pointerMove(pointer(60, 70)); editor.pointerUp(pointer(60, 70));
const crop = editor.normalizedCrop();
assert.deepEqual(crop, {x: 10, y: 40, w: 50, h: 100});
assert.equal(editor.applyCrop(), true);
assert.deepEqual([editor.canvas.width, editor.canvas.height], [50, 100]);
assert.equal(editor.mode, 'draw');
assert.deepEqual(editor.ops.map(op => op.type), ['rotate', 'crop']);
editor.undo();
assert.deepEqual([editor.canvas.width, editor.canvas.height], [100, 200]);
editor.setMode('crop');
editor.pointerDown(pointer(10, 10)); editor.pointerMove(pointer(12, 12)); editor.pointerUp(pointer(12, 12));
assert.equal(editor.normalizedCrop(), null, 'a tiny drag is not a crop');
assert.equal(editor.applyCrop(), false);
editor.cancelCrop();
assert.equal(editor.mode, 'draw');
assert.equal(editor.cropRect, null);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_custom_support_number_label_reveals_raw_phone_on_hover_and_focus() -> None:
    helpers = _HTML[_HTML.index("function supportNumberAlias("): _HTML.index("function isStaffChat(")]
    script = r"""
const assert = require('node:assert/strict');
function element() {return {children: [], attributes: {}, append(...nodes) {this.children.push(...nodes)},
  setAttribute(name, value) {this.attributes[name] = value}, focus() {this.focused = true}}}
const document = {createElement: element};
const state = {whatsappRegistry: {numbers: [{phone_number_id: '90005', display_name: 'TestNumber',
  display_phone_number: '447451212752'}]}};
""" + helpers + r"""
const ticket = {business_phone_number_id: '90005', business_display_phone_number: '447451212752',
  display_name: 'TestNumber'};
const label = createSupportNumberLabel(ticket);
assert.equal(supportNumberLabel(ticket), 'TestNumber');
assert.match(label.className, /has-alias/);
assert.equal(label.children[0].textContent, 'TestNumber');
assert.equal(label.children[1].textContent, '447451212752');
assert.equal(label.title, '447451212752');
assert.equal(label.tabIndex, 0);
assert.match(label.attributes['aria-label'], /TestNumber.*447451212752/);
label.onpointerdown({pointerType: 'touch'});
assert.equal(label.focused, true, 'a tap makes the number visible using the focus style');

const queueLabel = createSupportNumberLabel(ticket, {focusable: false});
assert.equal(queueLabel.tabIndex, undefined, 'the queue does not nest a keyboard stop inside its button');
const linked = createSupportNumberLabel({line_id: '90005', display_phone_number: '447451212752'});
assert.equal(linked.children[0].textContent, 'TestNumber', 'linked status can use registry alias');
const plain = createSupportNumberLabel({phone_number_id: '90002', display_phone_number: '96176063972'});
assert.equal(plain.textContent, '96176063972');
assert.equal(plain.children.length, 0);
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert ".support-number-label.has-alias:is(:hover, :focus) .support-number-raw," in _HTML
    assert ".support-number-label.has-alias:is(:hover, :focus) .support-number-alias," in _HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_renaming_support_number_persists_then_updates_open_views() -> None:
    functions = _HTML[_HTML.index("function refreshVisibleSupportNumberName("): _HTML.index("function renderLinkedDeviceStatus(")]
    script = r"""
const assert = require('node:assert/strict');
const queue = {scrollTop: 27};
const $ = name => {assert.equal(name, 'queue'); return queue};
const ticket = {business_phone_number_id: '90005', display_name: null};
const historical = {ticket: {business_phone_number_id: '90005', display_name: null}};
const data = {ticket, context: {}, shipment: null, customer_identity: null, permissions: {},
  previous_ticket_history: [historical]};
const state = {whatsappBusy: false, linkedDeviceBusy: false, whatsappRegistry: null,
  queueItems: [{business_phone_number_id: '90005', display_name: null}], globalSearchResults: [],
  chatLines: [{phone_number_id: '90005', display_name: null}],
  currentTicketData: data, previousTicketHistory: [historical], ticketId: 5};
const canManageWhatsApp = () => true;
const clearWhatsAppFeedback = () => {};
const feedback = [];
const showWhatsAppFeedback = (kind, value) => feedback.push([kind, value]);
const setButtonBusy = (button, busy) => {button.disabled = busy};
const renderWhatsAppRegistry = payload => {state.whatsappRegistry = payload};
let queueRenders = 0, headerRenders = 0, contextRenders = 0, historyRenders = 0;
const renderQueue = () => {queueRenders++; queue.scrollTop = 0};
const renderTicketHeader = () => {headerRenders++};
const renderContext = () => {contextRenders++};
const renderConversation = (_, options) => {assert.equal(options.preserveAudio, true); historyRenders++};
const timelineSignature = () => 'updated';
let queueReloads = 0, ticketReloads = 0;
const loadQueue = async options => {assert.deepEqual(options, {silent: true}); queueReloads++};
const openTicket = async (id, options) => {
  assert.equal(id, 5); assert.deepEqual(options, {silent: true, refreshHistory: true}); ticketReloads++;
};
const requests = [];
const api = async (path, options) => {
  requests.push([path, options.method, JSON.parse(options.body)]);
  return {accounts: [], numbers: [{phone_number_id: '90005', display_name: JSON.parse(options.body).display_name}]};
};
""" + functions + r"""
(async () => {
  await renameWhatsAppNumber('90005', ' TestNumber ', {isConnected: true});
  assert.deepEqual(requests[0], ['/api/support/whatsapp-numbers/90005/name', 'PUT', {display_name: 'TestNumber'}]);
  assert.equal(state.whatsappRegistry.numbers[0].display_name, 'TestNumber');
  assert.equal(ticket.display_name, 'TestNumber');
  assert.equal(historical.ticket.display_name, 'TestNumber');
  assert.equal(state.queueItems[0].display_name, 'TestNumber');
  assert.equal(state.chatLines[0].display_name, 'TestNumber');
  assert.equal(queue.scrollTop, 27);
  assert.equal(queueRenders, 1);
  assert.equal(headerRenders, 1);
  assert.equal(contextRenders, 1);
  assert.equal(historyRenders, 1);
  assert.equal(queueReloads, 1);
  assert.equal(ticketReloads, 1);
  await renameWhatsAppNumber('90005', '   ', {isConnected: true});
  assert.deepEqual(requests[1][2], {display_name: null}, 'an empty name restores the raw phone');
  assert.equal(ticket.display_name, null);
  assert.deepEqual(feedback.map(item => item[0]), ['whatsappSuccess', 'whatsappSuccess']);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_image_editor_asset_is_served_with_upload_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "support_ui_enabled", True)
    assert '<script src="/support/image-editor.js"></script>' in _HTML
    response = asyncio.run(support_image_editor_script())
    assert response.status_code == 200
    assert b"class ChatImageEditor" in response.body
    assert b"__IMAGE_MAX_BYTES__" not in response.body


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_text_send_clears_composer_before_provider_response() -> None:
    send = _HTML[_HTML.index("async function sendMessage()"): _HTML.index("function showMessagePreferencesError(")]
    timeline = _HTML[_HTML.index("function upsertTimelineMessage("): _HTML.index("function renderConversation(")]
    script = r"""
const assert = require('node:assert/strict');
const input = {value: 'first reply'};
const state = {
  editTarget: null, pendingMedia: null, busy: false, ticketId: 42,
  composerEnabled: true, replyTarget: null, agent: {id: '7', name: 'Agent'},
  localMessages: new Map(), realtimeMessages: new Map(), lastInboundByTicket: {},
  textSendChain: Promise.resolve(), currentTicketData: {messages: []}
};
const $ = id => id === 'message' ? input : {};
let sequence = 0;
const crypto = {randomUUID() {return String(++sequence)}};
const calls = [];
let finishFirst;
const api = (path, options) => {
  calls.push({path, payload: JSON.parse(options.body)});
  if (calls.length === 1) return new Promise(resolve => {finishFirst = resolve});
  return Promise.resolve({message: {id: 102, client_message_id: '2', body: 'second reply'}});
};
const timelineSignature = () => '';
const renderConversation = () => {};
const clearReplyTarget = () => {state.replyTarget = null};
const resizeComposer = () => {};
const updateComposerCount = () => {};
const updateComposerControls = () => {};
const showError = () => {};
const focusMessageComposer = () => {};
const loadQueue = () => Promise.resolve();
const notify = () => {};
""" + timeline + send + r"""
(async () => {
  sendMessage();
  assert.equal(input.value, '');
  assert.equal(state.currentTicketData.messages[0].send_status, 'PENDING');
  input.value = 'second reply';
  sendMessage();
  assert.equal(input.value, '');
  await Promise.resolve();
  assert.equal(calls.length, 1);
  const earlyDetail = {messages: []};
  mergeLiveMessages(earlyDetail, 42);
  assert.equal(earlyDetail.messages.length, 2);
  finishFirst({message: {id: 101, client_message_id: '1', body: 'first reply'}});
  await state.textSendChain;
  assert.equal(calls.length, 2);
  assert.deepEqual(calls.map(call => call.payload.body), ['first reply', 'second reply']);
  assert.deepEqual(state.currentTicketData.messages.map(message => message.id), [101, 102]);
  const confirmedDetail = {messages: [...state.currentTicketData.messages]};
  mergeLiveMessages(confirmedDetail, 42);
  assert.equal(confirmedDetail.messages.length, 2);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('outbound', [False, True])
def test_voice_note_survives_ticket_and_message_updates(tmp_path: Path, outbound: bool) -> None:
    configured_browser = os.environ.get("SUPPORT_TEST_BROWSER")
    browser = shutil.which(configured_browser) if configured_browser else next(
        (path for name in ("google-chrome", "google-chrome-stable", "chromium")
         if (path := shutil.which(name))), None,
    )
    if configured_browser:
        assert browser, f"Configured SUPPORT_TEST_BROWSER is unavailable: {configured_browser}"
    if not browser:
        pytest.skip("Chrome/Chromium is unavailable")
    player = _HTML[_HTML.index("function createVoiceNotePlayer("): _HTML.index("function addMessage(")]
    reconciliation = _HTML[_HTML.index("function updateAudioMessageRow("): _HTML.index("function renderConversation(")]
    script = 'const outbound = ' + str(outbound).lower() + ';\n' + r"""
const conversation = document.getElementById('conversation');
function check(condition, message) { if (!condition) throw Error(message); }
function messageRow(id, source, caption, playbackStatus = 'read') {
  const row = document.createElement('article');
  row.className = 'message-row ' + (outbound ? 'outbound' : 'inbound');
  row.dataset.messageId = id;
  const avatar = document.createElement('div');
  avatar.className = 'message-avatar';
  const stack = document.createElement('div');
  stack.className = 'message-stack';
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';
  const sender = document.createElement('div');
  sender.textContent = 'Customer';
  const media = document.createElement('div');
  media.className = 'msg-media';
  const {player} = createVoiceNotePlayer(source, 'Voice note');
  const indicator = document.createElement('span');
  const mic = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  mic.setAttribute('class', 'icon voice-playback' + (playbackStatus === 'played' ? ' confirmed-played' : ''));
  mic.setAttribute('aria-label', playbackStatus === 'played' ? 'Played by recipient' : 'Playback not confirmed');
  indicator.title = mic.getAttribute('aria-label');
  indicator.append(mic);
  if (outbound) player.prepend(indicator);
  media.append(player);
  const body = document.createElement('div');
  body.textContent = caption;
  bubble.append(sender, media, body);
  const meta = document.createElement('div');
  meta.textContent = 'Received';
  stack.append(bubble, meta);
  row.append(avatar, stack);
  return row;
}
function timeline(...children) {
  const inner = document.createElement('div');
  inner.className = 'conversation-inner';
  inner.append(...children);
  return inner;
}
function textMessage(text) {
  const row = document.createElement('article');
  row.textContent = text;
  return row;
}
try {
  const original = messageRow('7', '/media/7', 'first caption');
  conversation.append(timeline(original));
  const audio = original.querySelector('audio');
  const speed = original.querySelector('.voice-speed-btn');
  if (outbound) check(!original.querySelector('.voice-playback').classList.contains('confirmed-played'), 'read implied playback');
  check(audio.playbackRate === 1 && speed.textContent === '1x', 'initial speed');
  speed.click();
  check(audio.playbackRate === 1.5 && speed.textContent === '1.5x', '1.5x speed');
  speed.click();
  check(audio.playbackRate === 2 && speed.textContent === '2x', '2x speed');

  const removed = [];
  const observer = new MutationObserver(records => {
    for (const record of records) for (const node of record.removedNodes) {
      if (node === audio || node.contains?.(audio)) removed.push(node);
    }
  });
  observer.observe(conversation, {childList: true, subtree: true});

  check(updateConversationKeepingAudio(conversation, timeline(
    textMessage('Claimed'), messageRow('7', '/media/7', 'updated caption'), textMessage('New inbound')
  )), 'claim update was reconciled');
  check(conversation.querySelector('audio') === audio, 'claim replaced the audio');
  check(conversation.querySelector('.voice-speed-btn') === speed, 'claim reset the speed control');
  check(conversation.textContent.includes('updated caption'), 'caption was not updated');
  check(conversation.textContent.includes('New inbound'), 'incoming message was not displayed');
  if (outbound) check(!original.querySelector('.voice-playback').classList.contains('confirmed-played'), 'ordinary update implied playback');

  check(updateConversationKeepingAudio(conversation, timeline(
    textMessage('Claimed'), messageRow('7', '/media/7', 'updated caption', 'played'),
    textMessage('New inbound'), textMessage('Sent reply')
  )), 'send update was reconciled');
  check(conversation.querySelector('audio') === audio, 'send replaced the audio');
  check(audio.isConnected && audio.playbackRate === 2, 'playback state was lost');
  if (outbound) {
    const played = original.querySelector('.voice-playback');
    check(played.classList.contains('confirmed-played'), 'confirmed playback did not update the microphone');
    check(played.getAttribute('aria-label') === 'Played by recipient', 'playback accessibility label was stale');
    check(played.parentElement.title === 'Played by recipient', 'playback tooltip was stale');
    check(original.querySelectorAll('.voice-playback').length === 1, 'duplicate playback indicators');
  } else check(!original.querySelector('.voice-playback'), 'incoming audio gained a recipient indicator');
  speed.click();
  check(audio.playbackRate === 1 && speed.textContent === '1x', 'speed did not cycle');
  setTimeout(() => {
    try {
      check(removed.length === 0, 'audio was removed during update');
      document.body.dataset.result = 'passed';
    } catch (error) { document.body.dataset.result = 'failed: ' + error.message; }
  }, 0);
} catch (error) { document.body.dataset.result = 'failed: ' + error.message; }
"""
    page = tmp_path / "voice-note-test.html"
    page.write_text("<body><div id='conversation'></div><script>" + player + reconciliation + script + "</script></body>")
    result = subprocess.run(
        [browser, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
         "--user-data-dir=" + str(tmp_path / "chrome-profile"),
         "--virtual-time-budget=1000", "--dump-dom", page.as_uri()],
        text=True, capture_output=True, timeout=20,
    )
    if not configured_browser and result.returncode != 0 and ("setsockopt: Operation not permitted" in result.stderr or "Failed to create headless user data directory container" in result.stderr):
        pytest.skip("Chromium cannot start in this sandbox")
    assert result.returncode == 0, result.stderr
    assert '<body data-result="passed">' in result.stdout, result.stdout[-2000:]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_remote_edit_keeps_draft_on_rejection_and_checks_message_owner() -> None:
    message_time = _HTML[_HTML.index("function messageOccurredAt("): _HTML.index("function fmt(")]
    copy_helpers = _HTML[_HTML.index("function messageTextForCopy("): _HTML.index("function renderReplyDraft(")]
    cache_change = _HTML[_HTML.index("function changeCachedMessage("): _HTML.index("function upsertTimelineMessage(")]
    controls = _HTML[_HTML.index("function canChangeWhatsAppMessage("): _HTML.index("async function sendMessageReaction(")]
    add_message = _HTML[_HTML.index("function addMessage("): _HTML.index("function addEvent(")]
    sending = _HTML[_HTML.index("async function sendMessage()"): _HTML.index("function showMessagePreferencesError(")]
    script = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tagName) {this.tagName = tagName; this.children = []; this.dataset = {}; this.disabled = false; this.textContent = '';}
  append(...children) {this.children.push(...children)}
  setAttribute() {}
}
const document = {
  createElement: tagName => new Element(tagName),
  createTextNode: value => ({textContent: value})
};
const findEdit = node => node?.tagName === 'button' && node.textContent === 'Edit'
  ? node : (node?.children || []).map(findEdit).find(Boolean);
const initials = () => 'A';
const messageActor = () => 'Agent';
const messageClass = () => 'agent';
const fmt = value => value || '';
const renderReplyQuote = () => {};
const appendMessageTextWithLinks = (node, value) => {node.textContent = value};
const messageReactionBar = () => null;
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {value: '', classList: {add(){}, remove(){}, toggle(){}}});
  return elements.get(id);
};
const text = (element, value) => {element.textContent = value};
const state = {agent: {id: '7'}, ticketId: 42, busy: false, editTarget: null,
  pendingMedia: null, replyTarget: null, realtimeMessages: new Map(),
  cachedCurrentMessages: [], previousTicketHistory: [],
  currentTicketData: {permissions: {can_forward: false}}};
const clearReplyTarget = () => {state.replyTarget = null};
const renderReplyDraft = () => {};
const updateComposerCount = () => {};
const resizeComposer = () => {};
const updateComposerControls = () => {};
const focusMessageComposer = () => {};
const openDialog = () => {};
const closeDialog = () => {};
const setButtonBusy = () => {};
const showError = () => {};
const refreshMessageSearchAfterChange = () => {};
const opened = [];
let renderedMessage = null;
let refreshedEdit = null;
const openTicket = async (id, options) => {
  opened.push({id, options, busy: state.busy});
  if (renderedMessage) {
    const conversation = document.createElement('div');
    addMessage(conversation, renderedMessage);
    refreshedEdit = findEdit(conversation);
  }
};
const notifications = [];
const notify = message => notifications.push(message);
let reject = true;
const calls = [];
const api = async (path, options) => {
  const payload = JSON.parse(options.body);
  calls.push({path, method: options.method, payload});
  if (reject) throw Error('WhatsApp did not confirm the change');
  if (options.method === 'PATCH') renderedMessage = {
    id: Number(path.match(/messages\/(\d+)/)[1]),
    ticket_id: Number(path.match(/tickets\/(\d+)/)[1]),
    sender_type: 'AGENT', sender_id: '7', direction: 'OUTBOUND',
    message_type: 'text', body: payload.content, version: payload.version + 1,
    edited_at: 'now', can_edit: true, can_delete_for_everyone: true
  };
  return {};
};
""" + message_time + copy_helpers + cache_change + controls + add_message + sending + r"""
(async () => {
  const own = {id: 5, version: 3, sender_type: 'AGENT', sender_id: '7', body: 'original', can_edit: true, can_delete_for_everyone: true};
  assert.equal(canChangeWhatsAppMessage(own, 'edit'), true);
  assert.equal(canChangeWhatsAppMessage({...own, sender_id: '8'}, 'edit'), false);
  assert.equal(canChangeWhatsAppMessage({...own, can_edit: false}, 'edit'), false);
  assert.equal(canChangeWhatsAppMessage({...own, deleted_for_everyone_at: 'now'}, 'everyone'), false);
  $('message').value = 'unsent draft';
  startMessageEdit(own);
  assert.equal($('message').value, 'original');
  $('message').value = 'corrected';
  await sendMessage();
  assert.equal($('message').value, 'corrected');
  assert.equal(state.editTarget.id, 5);
  assert.equal(calls[0].method, 'PATCH');
  assert.deepEqual(calls[0].payload, {content: 'corrected', version: 3});
  reject = false;
  await sendMessage();
  assert.equal(state.editTarget, null);
  assert.equal($('message').value, '');
  assert.equal(notifications.at(-1), 'Message updated for everyone on WhatsApp.');
  assert.equal(opened.at(-1).busy, true);
  assert.ok(refreshedEdit, 'the refreshed message still offers Edit');
  assert.equal(refreshedEdit.disabled, false, 'Edit is immediately clickable after the refresh');
  const revised = {...own, version: 4, body: 'corrected', edited_at: 'now'};
  state.currentConversationOrigin = 'STAFF';
  assert.equal(canChangeWhatsAppMessage(revised, 'edit'), true);
  refreshedEdit.onclick();
  assert.equal(state.editTarget.version, 4);
  $('message').value = 'corrected again';
  await sendMessage();
  assert.equal(calls.at(-1).path, '/api/support/tickets/42/messages/5');
  assert.deepEqual(calls.at(-1).payload, {content: 'corrected again', version: 4});
  assert.equal(state.editTarget, null);

  const historical = {...revised, id: 6, ticket_id: 9, version: 5};
  state.currentConversationOrigin = 'CUSTOMER';
  startMessageEdit(historical);
  $('message').value = 'historical correction';
  await sendMessage();
  assert.equal(calls.at(-1).path, '/api/support/tickets/9/messages/6');
  assert.deepEqual(calls.at(-1).payload, {content: 'historical correction', version: 5});
  assert.deepEqual(opened.at(-1), {id: 42, options: {silent: true, refreshHistory: true}, busy: true});
  openMessageDelete(historical);
  await deleteMessage('everyone');
  assert.equal(calls.at(-1).path, '/api/support/tickets/9/messages/6/delete');
  assert.deepEqual(calls.at(-1).payload, {scope: 'everyone'});
  assert.deepEqual(opened.at(-1), {id: 42, options: {silent: true, refreshHistory: true}, busy: true});

  openMessageDelete({...own, can_delete_for_everyone: false});
  const before = calls.length;
  await deleteMessage('everyone');
  assert.equal(calls.length, before);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_message_timeline_reacts_to_repeat_edit_and_changed_capabilities() -> None:
    signature = _HTML[_HTML.index("function timelineSignature("): _HTML.index("function upsertTimelineMessage(")]
    script = r"""
const assert = require('node:assert/strict');
""" + signature + r"""
const message = {id: 5, body: 'first', version: 1, can_edit: true, can_delete_for_everyone: true};
const data = {ticket: {id: 42}, messages: [message], previous_ticket_history: [
  {ticket: {id: 9}, messages: [{...message, id: 6, ticket_id: 9}]}
]};
const initial = timelineSignature(data);
message.version = 2;
message.edited_at = '2026-10-12T09:00:00Z';
assert.notEqual(timelineSignature(data), initial);
const afterEdit = timelineSignature(data);
message.can_edit = false;
assert.notEqual(timelineSignature(data), afterEdit);
const afterEligibility = timelineSignature(data);
data.previous_ticket_history[0].messages[0].can_delete_for_everyone = false;
assert.notEqual(timelineSignature(data), afterEligibility);
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_support_workspace_restores_only_verified_staff_selection() -> None:
    queue_config = _HTML[_HTML.index("const QUEUE_CONFIG = "): _HTML.index("const STATUS_LABELS = ")]
    storage = _HTML[_HTML.index("const SUPPORT_SELECTION_STORAGE_PREFIX = "): _HTML.index("const $ = ")]
    restore = _HTML[_HTML.index("async function restoreSupportWorkspace("): _HTML.index("async function login(")]
    set_view = _HTML[_HTML.index("function setQueueView("): _HTML.index("function clearQueueSearch(")]
    script = r"""
const assert = require('node:assert/strict');
const resetQueueFilters = () => {};
const stored = new Map([['shipment-support-theme', 'dark']]);
const sessionStorage = {
  get length() {return stored.size}, key(index) {return [...stored.keys()][index] || null},
  getItem(key) {return stored.get(key) ?? null},
  setItem(key, value) {stored.set(key, value)}, removeItem(key) {stored.delete(key)}
};
const state = {agent: {id: '7'}, authenticated: true, view: 'chats', page: 2,
  ticketId: 44, currentConversationOrigin: 'STAFF', query: 'private customer search',
  currentTicketData: {messages: [{body: 'private message'}]}, globalSearchRequest: 0};
const buttons = ['unassigned', 'chats', 'contacts', 'history'].map(view => ({
  dataset: {view}, classList: {toggle(name, active) {if (name === 'active') this.active = active}},
  setAttribute(name, value) {this[name] = value}
}));
const elements = {tabs: {querySelectorAll: () => buttons}, queueSearch: {value: ''},
  contactsToolbar: {classList: {toggle() {}}}, searchScope: {textContent: ''}};
const $ = id => elements[id];
const text = (element, value) => {element.textContent = value};
const loadGoogleContactsStatus = async () => {};
const updateQueueSummary = () => {};
const loaded = [];
const loadQueue = async () => {loaded.push(state.view)};
const opened = [];
const openTicket = async (id, options) => {opened.push({id, options})};
""" + queue_config + storage + set_view + restore + r"""
(async () => {
  saveSupportSelection();
  const key = SUPPORT_SELECTION_STORAGE_PREFIX + '7';
  assert.deepEqual(JSON.parse(sessionStorage.getItem(key)), {
    view: 'chats', page: 2, ticketId: 44, conversationOrigin: 'STAFF'
  });
  state.view = 'unassigned'; state.page = 1; state.ticketId = null;
  state.currentConversationOrigin = null;
  await restoreSupportWorkspace();
  assert.equal(state.view, 'chats');
  assert.equal(state.page, 2);
  assert.equal(buttons.find(button => button.dataset.view === 'chats').classList.active, true);
  assert.equal(buttons.find(button => button.dataset.view === 'unassigned').classList.active, false);
  assert.deepEqual(loaded, ['chats']);
  assert.deepEqual(opened, [{id: 44, options: {
    silent: true, conversationOrigin: 'STAFF', restoring: true
  }}]);

  state.agent = {id: '8'};
  assert.equal(readSupportSelection(), null);
  sessionStorage.setItem(SUPPORT_SELECTION_STORAGE_PREFIX + '8', JSON.stringify({
    view: '__proto__', ticketId: 44, conversationOrigin: 'STAFF'
  }));
  assert.equal(readSupportSelection(), null);
  assert.equal(sessionStorage.getItem(SUPPORT_SELECTION_STORAGE_PREFIX + '8'), null);
  state.authenticated = false;
  assert.equal(readSupportSelection(), null);
  clearSupportSelections();
  assert.equal(sessionStorage.getItem(key), null);
  assert.equal(sessionStorage.getItem('shipment-support-theme'), 'dark');
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_restored_chat_and_ticket_use_correct_route_and_clear_stale_selection() -> None:
    queue_config = _HTML[_HTML.index("const QUEUE_CONFIG = "): _HTML.index("const STATUS_LABELS = ")]
    storage = _HTML[_HTML.index("const SUPPORT_SELECTION_STORAGE_PREFIX = "): _HTML.index("const $ = ")]
    merge_history = _HTML[_HTML.index("function mergeTimelineMessages("): _HTML.index("function changeCachedMessage(")]
    clear_workspace = _HTML[_HTML.index("function clearTicketWorkspace("): _HTML.index("function showLogin(")]
    open_ticket = _HTML[_HTML.index("async function openTicket("): _HTML.index("async function doAction(")]
    script = CONNECTION_HOOKS + r"""
const assert = require('node:assert/strict');
const resetQueueFilters = () => {};
const stored = new Map();
const sessionStorage = {
  get length() {return stored.size}, key(index) {return [...stored.keys()][index] || null},
  getItem(key) {return stored.get(key) ?? null},
  setItem(key, value) {stored.set(key, value)}, removeItem(key) {stored.delete(key)}
};
const state = {agent: {id: '7'}, authenticated: true, view: 'chats', page: 1,
  ticketId: null, currentTicketData: null, currentConversationOrigin: null,
  previousTicketHistory: [], previousTicketHistoryHasMore: false,
  timelineSignature: '', ticketRequest: 0, lastInboundByTicket: {},
  queueItems: [], total: 0, replyTarget: null, mediaRecorder: null, pageLeaving: false};
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {value: '', classList: {add(){}, remove(){}}, replaceChildren(){}});
  return elements.get(id);
};
const text = (element, value) => {element.textContent = value};
const window = {innerWidth: 1200};
const emptyMarkup = () => ({outer: {}});
const setComposerEnabled = () => {};
const cancelMessageEdit = () => {};
const clearMediaDraft = () => {};
const clearMessageSelection = () => {};
const clearReplyTarget = () => {};
const closeConversationSearch = () => {};
const setMobileTicketOpen = () => {};
const closeContextPanel = () => {};
const updateComposerCount = () => {};
const updateQueueSummary = () => {};
const renderQueue = () => {};
const updateSharedHistoryShortcut = () => {};
const errors = [];
const showError = message => {if (message) errors.push(message)};
const mergeLiveMessages = () => {};
const latestInboundMessageId = () => null;
const renderTicketHeader = () => {};
const renderContext = () => {};
const renderReadOnlyState = () => {};
const renderActions = () => {};
const showTicketOpening = () => {};
const document = {hidden: false};
const timelineSignature = () => 'rendered';
const renderConversation = () => {};
const isStaffChat = ticket => ticket.conversation_origin === 'STAFF';
const setAppStatus = () => {};
const notify = () => {};
const notifyNewCustomerActivity = () => {};
const loads = [];
const loadQueue = async options => {loads.push(options)};
let failure = null;
const paths = [];
const api = async path => {
  paths.push(path);
  if (failure === 'network') throw new TypeError('NetworkError when attempting to fetch resource');
  if (failure) throw Object.assign(new Error('Conversation unavailable'), {status: failure});
  return {ticket: {id: Number(path.match(/\/(\d+)/)[1]),
    conversation_origin: path.includes('/chats/') ? 'STAFF' : 'CUSTOMER'},
    messages: [], messages_has_more: false,
    previous_ticket_history: path.includes('include_previous_history=true') ? [
      {ticket: {id: 20}, messages: [{id: 201}], messages_has_more: false}
    ] : [], previous_ticket_history_has_more: false,
    events: [], permissions: {can_reply: false}, context: {}};
};
""" + queue_config + storage + merge_history + clear_workspace + open_ticket + r"""
(async () => {
  await openTicket(44, {silent: true, restoring: true, conversationOrigin: 'STAFF'});
  assert.equal(paths.at(-1), '/api/support/chats/44?include_previous_history=true');
  assert.equal(state.currentConversationOrigin, 'STAFF');
  assert.equal(readSupportSelection().ticketId, 44);
  assert.equal(readSupportSelection().conversationOrigin, 'STAFF');
  assert.equal(loads.length, 0, 'the queue was already loaded during restore');

  state.view = 'history'; state.page = 2;
  await openTicket(55, {silent: true, restoring: true});
  assert.equal(paths.at(-1), '/api/support/tickets/55?include_previous_history=true');
  assert.equal(state.currentConversationOrigin, 'CUSTOMER', 'a prior chat origin must not leak into a ticket');
  assert.equal(readSupportSelection().ticketId, 55);
  assert.equal(readSupportSelection().view, 'history');

  failure = 403;
  await openTicket(66, {silent: true, restoring: true, conversationOrigin: 'CUSTOMER'});
  assert.equal(state.ticketId, null);
  assert.equal(readSupportSelection().ticketId, null);
  assert.equal(state.view, 'history');
  assert.equal(loads.length, 1);
  assert.match(errors.at(-1), /saved conversation is no longer available/i);

  failure = 404;
  await openTicket(77, {silent: true, restoring: true, conversationOrigin: 'STAFF'});
  assert.equal(paths.at(-1), '/api/support/chats/77?include_previous_history=true');
  assert.equal(state.ticketId, null, 'a missing chat must not be polled forever');
  assert.equal(readSupportSelection().ticketId, null);
  assert.equal(loads.length, 2);

  failure = null;
  await openTicket(88, {silent: true, restoring: true, conversationOrigin: 'CUSTOMER'});
  assert.equal(readSupportSelection().ticketId, 88);
  state.cachedCurrentMessages = [{id: 1}, {id: 2}];
  state.currentMessagesExpanded = true;
  state.currentMessagesHasMore = false;
  state.previousTicketHistory = [
    {ticket: {id: 9}, messages: [{id: 90}], messages_has_more: false, _messagesExpanded: true},
    ...state.previousTicketHistory
  ];
  state.previousHistoryExpanded = true;
  await openTicket(88, {silent: true});
  assert.deepEqual(state.currentTicketData.messages.map(message => message.id), [1, 2],
    'background refresh must preserve older current messages');
  assert.deepEqual(state.currentTicketData.previous_ticket_history.map(thread => thread.ticket.id), [9, 20],
    'background refresh must preserve loaded previous pages');
  await openTicket(88, {silent: true, refreshHistory: true});
  assert.deepEqual(state.currentTicketData.previous_ticket_history.map(thread => thread.ticket.id), [9, 20],
    'refreshing history after an edit must preserve expanded pages');
  const beforeNetworkError = errors.length;
  state.pageLeaving = true;
  failure = 'network';
  await openTicket(88, {silent: true, restoring: true});
  assert.equal(errors.length, beforeNetworkError, 'unload cancellation must not paint a red error');
  assert.equal(readSupportSelection().ticketId, 88, 'unload cancellation must not clear the saved selection');
  assert.equal(state.ticketId, 88);
  state.pageLeaving = false;
  await openTicket(88, {silent: true, restoring: true});
  assert.equal(errors.at(-1), 'NetworkError when attempting to fetch resource', 'active network failures remain visible');
  assert.equal(readSupportSelection().ticketId, 88);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_history_paging_loads_every_page_and_keeps_thread_cursors() -> None:
    merge_history = _HTML[_HTML.index("function mergeTimelineMessages("): _HTML.index("function changeCachedMessage(")]
    paging = _HTML[_HTML.index("function beginHistoryLoad("): _HTML.index("function updateSharedHistoryShortcut(")]
    script = r"""
const assert = require('node:assert/strict');
const firstThread = {ticket: {id: 30, conversation_origin: 'CUSTOMER'},
  messages: [{id: 300}, {id: 301}], messages_has_more: true};
const state = {authenticated: true, ticketId: 42, currentConversationOrigin: 'CUSTOMER',
  currentTicketData: {ticket: {id: 42, conversation_origin: 'CUSTOMER'},
    messages: [{id: 100}, {id: 101}], messages_has_more: true,
    previous_ticket_history: [firstThread], previous_ticket_history_has_more: true},
  cachedCurrentMessages: [{id: 100}, {id: 101}], currentMessagesHasMore: true,
  currentMessagesExpanded: false, previousTicketHistory: [firstThread],
  previousTicketHistoryHasMore: true, previousHistoryExpanded: false,
  historyLoadBusy: false, historyLoadRequest: 0, historySelectionEpoch: 1,
  pageLeaving: false};
const button = () => ({textContent: 'Load', disabled: false, isConnected: false});
const renderOptions = [];
const renderConversation = (_data, options) => {renderOptions.push(options)};
const updateSharedHistoryShortcut = () => {};
const timelineSignature = data => JSON.stringify(data.previous_ticket_history.map(thread => thread.ticket.id));
const mergeLiveMessages = () => {};
const errors = [];
const showError = message => {errors.push(message)};
const paths = [];
let finishDelayed;
const api = path => {
  paths.push(path);
  if (path.endsWith('/history?before_ticket_id=30&limit=5')) return Promise.resolve({
    previous_ticket_history: [
      {ticket: {id: 20}, messages: [{id: 200}], messages_has_more: false},
      {ticket: {id: 25}, messages: [{id: 250}], messages_has_more: false}
    ], previous_ticket_history_has_more: true
  });
  if (path.endsWith('/history?before_ticket_id=20&limit=5')) return Promise.resolve({
    previous_ticket_history: [{ticket: {id: 10}, messages: [{id: 1000}], messages_has_more: false}],
    previous_ticket_history_has_more: false
  });
  if (path.endsWith('/history/30/messages?before_message_id=300&limit=100')) return Promise.resolve({
    messages: [{id: 298}, {id: 299}, {id: 300}], messages_has_more: false
  });
  if (path.endsWith('/messages?before_message_id=100&limit=150')) return Promise.resolve({
    messages: [{id: 98}, {id: 99}, {id: 100}], messages_has_more: false
  });
  if (path.endsWith('/messages?before_message_id=50&limit=150')) return new Promise(resolve => {finishDelayed = resolve});
  if (path.endsWith('/messages?before_message_id=40&limit=150')) return new Promise(resolve => {finishDelayed = resolve});
  if (path.endsWith('/chats/50/messages?before_message_id=500&limit=150')) return Promise.resolve({
    messages: [{id: 490}], messages_has_more: false
  });
  throw new Error('Unexpected path ' + path);
};
""" + merge_history + paging + r"""
(async () => {
  await loadEarlierConversations(button());
  assert.deepEqual(state.previousTicketHistory.map(thread => thread.ticket.id), [20, 25, 30]);
  assert.equal(state.previousTicketHistoryHasMore, true);
  assert.equal(renderOptions.at(-1).preserveScrollAnchor, '[data-history-ticket-id="30"]');
  await loadEarlierConversations(button());
  assert.deepEqual(state.previousTicketHistory.map(thread => thread.ticket.id), [10, 20, 25, 30]);
  assert.equal(state.previousTicketHistoryHasMore, false);
  await loadEarlierConversations(button());
  assert.equal(paths.length, 2, 'no third request after the last page');

  await loadOlderConversationMessages(30, true, button());
  assert.deepEqual(firstThread.messages.map(message => message.id), [298, 299, 300, 301]);
  assert.equal(firstThread.messages_has_more, false);
  assert.equal(renderOptions.at(-1).preserveScrollAnchor, '[data-message-id="300"]');
  await loadOlderConversationMessages(30, true, button());
  assert.equal(paths.length, 3, 'no duplicate prior-message request');

  await loadOlderConversationMessages(42, false, button());
  assert.deepEqual(state.currentTicketData.messages.map(message => message.id), [98, 99, 100, 101]);
  assert.equal(state.currentMessagesHasMore, false);
  assert.equal(renderOptions.at(-1).preserveScrollAnchor, '[data-message-id="100"]');

  state.cachedCurrentMessages = [{id: 50}, {id: 51}];
  state.currentMessagesHasMore = true;
  const pendingDuringPoll = loadOlderConversationMessages(42, false, button());
  state.currentTicketData = {...state.currentTicketData, messages: [{id: 50}, {id: 51}]};
  finishDelayed({messages: [{id: 49}], messages_has_more: false});
  await pendingDuringPoll;
  assert.deepEqual(state.currentTicketData.messages.map(message => message.id), [49, 50, 51],
    'a page requested before a background refresh attaches to the refreshed ticket data');

  state.cachedCurrentMessages = [{id: 40}, {id: 41}];
  state.currentMessagesHasMore = true;
  const stale = loadOlderConversationMessages(42, false, button());
  state.ticketId = 50;
  state.historySelectionEpoch++;
  state.historyLoadRequest++;
  state.historyLoadBusy = false;
  finishDelayed({messages: [{id: 39}], messages_has_more: false});
  await stale;
  assert.deepEqual(state.cachedCurrentMessages.map(message => message.id), [40, 41],
    'a response for the old selection must not affect the new selection');

  state.currentConversationOrigin = 'STAFF';
  state.currentTicketData = {ticket: {id: 50, conversation_origin: 'STAFF'},
    messages: [{id: 500}], previous_ticket_history: []};
  state.cachedCurrentMessages = [{id: 500}];
  state.currentMessagesHasMore = true;
  await loadOlderConversationMessages(50, false, button());
  assert.equal(paths.at(-1), '/api/support/chats/50/messages?before_message_id=500&limit=150');
  assert.deepEqual(state.currentTicketData.messages.map(message => message.id), [490, 500]);
  assert.deepEqual(errors, []);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_initial_history_load_survives_background_poll_and_retries_after_error() -> None:
    paging = _HTML[_HTML.index("function beginHistoryLoad("): _HTML.index("function updateSharedHistoryShortcut(")]
    shortcuts = _HTML[_HTML.index("function updateSharedHistoryShortcut("): _HTML.index("function contextValue(")]
    open_ticket = _HTML[_HTML.index("async function openTicket("): _HTML.index("async function doAction(")]
    script = CONNECTION_HOOKS + r"""
const assert = require('node:assert/strict');
const state = {agent: {id: '7'}, authenticated: true, view: 'unassigned',
  ticketId: null, currentTicketData: null, currentConversationOrigin: null,
  previousTicketHistory: [], previousTicketHistoryHasMore: false,
  previousHistoryExpanded: false, previousHistoryLoaded: false,
  previousHistoryPending: false, timelineSignature: '', ticketRequest: 0,
  historySelectionEpoch: 0, historyLoadRequest: 0, historyLoadBusy: false,
  lastInboundByTicket: {}, replyTarget: null, mediaRecorder: null, pageLeaving: false};
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {value: '', classList: {
      add(name) {classes.add(name)}, remove(name) {classes.delete(name)},
      toggle(name, present) {if (present) classes.add(name); else classes.delete(name)},
      contains(name) {return classes.has(name)}
    }, setAttribute(name, value) {this[name] = value}, replaceChildren(){}});
  }
  return elements.get(id);
};
const text = (element, value) => {element.textContent = value};
const window = {innerWidth: 1200};
const setComposerEnabled = () => {};
const cancelMessageEdit = () => {};
const clearMediaDraft = () => {};
const clearMessageSelection = () => {};
const clearReplyTarget = () => {};
const closeConversationSearch = () => {};
const setMobileTicketOpen = () => {};
const closeContextPanel = () => {};
const updateComposerCount = () => {};
const renderQueue = () => {};
const showError = () => {};
const mergeLiveMessages = () => {};
const latestInboundMessageId = () => null;
const renderTicketHeader = () => {};
const renderContext = () => {};
const renderReadOnlyState = () => {};
const renderActions = () => {};
const showTicketOpening = () => {};
const document = {hidden: false};
const emptyMarkup = () => ({outer: {}});
const timelineSignature = data => JSON.stringify((data.previous_ticket_history || []).map(item => item.ticket.id));
const renderConversation = () => {};
const isStaffChat = () => false;
const setAppStatus = () => {};
const saveSupportSelection = () => {};
const notifyNewCustomerActivity = () => {};
const loadQueue = async () => {};
const mergeTimelineMessages = (loaded, fresh) => [...loaded, ...fresh];
const paths = [];
let releaseInitial;
let failNext = false;
const detail = path => ({ticket: {id: Number(path.match(/\/tickets\/(\d+)/)[1]),
  conversation_origin: 'CUSTOMER'}, context: {}, permissions: {can_reply: false},
  events: [], messages: [], messages_has_more: false,
  previous_ticket_history: path.includes('include_previous_history=true')
    ? [{ticket: {id: 7}, messages: [{id: 70}]}] : [],
  previous_ticket_history_has_more: path.includes('/tickets/42?include_previous_history=true')});
const api = path => {
  paths.push(path);
  if (path === '/api/support/tickets/42/history?before_ticket_id=7&limit=5') {
    return Promise.resolve({previous_ticket_history: [
      {ticket: {id: 5}, messages: []}, {ticket: {id: 6}, messages: []}
    ], previous_ticket_history_has_more: false});
  }
  if (paths.length === 1) return new Promise(resolve => {releaseInitial = () => resolve(detail(path))});
  if (failNext) {failNext = false; throw new Error('Temporary network error')}
  return Promise.resolve(detail(path));
};
""" + paging + shortcuts + open_ticket + r"""
(async () => {
  const initial = openTicket(42, {silent: true});
  assert.equal(state.previousHistoryPending, true);
  await openTicket(42, {silent: true});
  assert.equal(paths.length, 1, 'background polling must not supersede the initial history request');
  releaseInitial();
  await initial;
  assert.equal(state.previousHistoryLoaded, true);
  assert.equal(state.previousHistoryPending, false);
  assert.deepEqual(state.previousTicketHistory.map(item => item.ticket.id), [7]);
  assert.equal($('viewSharedHistoryBtn').classList.contains('hidden'), false,
    'successful detail response reveals the history shortcut');
  assert.equal($('sharedHistoryCount').textContent, '(1+)');

  await loadEarlierConversations();
  assert.deepEqual(state.previousTicketHistory.map(item => item.ticket.id), [5, 6, 7]);
  assert.equal($('sharedHistoryCount').textContent, '(3)',
    'successful pagination updates the shortcut count');

  await openTicket(42, {silent: true});
  assert.equal(paths.at(-1), '/api/support/tickets/42', 'ordinary polling reuses loaded history');
  assert.deepEqual(state.currentTicketData.previous_ticket_history.map(item => item.ticket.id), [5, 6, 7]);

  failNext = true;
  await openTicket(43, {silent: true});
  assert.equal(state.previousHistoryLoaded, false);
  assert.equal(state.previousHistoryPending, false);
  await openTicket(43, {silent: true});
  assert.equal(paths.at(-1), '/api/support/tickets/43?include_previous_history=true',
    'the next background refresh retries history after an error');
  assert.deepEqual(state.previousTicketHistory.map(item => item.ticket.id), [7]);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_shared_history_shortcut_jumps_without_rerender_and_tracks_pagination() -> None:
    assert 'id="viewSharedHistoryBtn"' in _HTML
    assert '.ticket-history-shortcut-compact { display: inline; }' in _HTML
    shortcuts = _HTML[
        _HTML.index("function updateSharedHistoryShortcut("):
        _HTML.index("function contextValue(")
    ]
    script = r"""
const assert = require('node:assert/strict');
function element() {
  const classes = new Set();
  return {classList: {
    toggle(name, present) {if (present) classes.add(name); else classes.delete(name)},
    contains(name) {return classes.has(name)}
  }, setAttribute(name, value) {this[name] = value}, textContent: ''};
}
const historyButton = element();
const historyCount = element();
const latestButton = element();
const latestLabel = element();
const conversation = {...element(), scrollHeight: 1000, scrollTop: 900, clientHeight: 100,
  scrollTo(options) {this.scrollTop = options.top; this.lastScroll = options}};
const elements = {viewSharedHistoryBtn: historyButton, sharedHistoryCount: historyCount,
  scrollToLatestBtn: latestButton, scrollToLatestLabel: latestLabel, conversation};
const $ = id => elements[id];
const text = (node, value) => {node.textContent = value};
const state = {currentTicketData: {ticket: {id: 42}}, previousTicketHistory: [],
  previousTicketHistoryHasMore: false, historyJumpActive: false};
const playingAudio = {paused: false};
let rerenders = 0;
const renderConversation = () => {rerenders++};
""" + shortcuts + r"""
updateSharedHistoryShortcut();
assert.equal(historyButton.classList.contains('hidden'), true);
state.previousTicketHistory = Array.from({length: 5}, (_, id) => ({ticket: {id}}));
state.previousTicketHistoryHasMore = true;
updateSharedHistoryShortcut();
assert.equal(historyButton.classList.contains('hidden'), false);
assert.equal(historyCount.textContent, '(5+)');
assert.match(historyButton['aria-label'], /5 or more conversations/);

jumpToSharedHistory();
assert.deepEqual(conversation.lastScroll, {top: 0, behavior: 'smooth'});
assert.equal(state.historyJumpActive, true);
assert.equal(latestButton.classList.contains('hidden'), false);
assert.equal(latestLabel.textContent, 'Back to current');
assert.equal(rerenders, 0);
assert.equal(playingAudio.paused, false);

state.previousTicketHistory.push(...Array.from({length: 5}, (_, id) => ({ticket: {id: id + 5}})));
state.previousTicketHistoryHasMore = false;
updateSharedHistoryShortcut();
assert.equal(historyCount.textContent, '(10)', 'pagination updates the visible count');
conversation.scrollTop = 850;
updateScrollLatestButton();
assert.equal(state.historyJumpActive, false, 'manual scrolling near the latest messages clears history navigation');
assert.equal(latestLabel.textContent, 'Latest');
jumpToSharedHistory();
jumpToCurrentConversation();
assert.deepEqual(conversation.lastScroll, {top: 1000, behavior: 'smooth'});
assert.equal(latestLabel.textContent, 'Latest');
assert.equal(latestButton.classList.contains('hidden'), true);
assert.equal(rerenders, 0);
assert.equal(playingAudio.paused, false);

state.previousTicketHistory = [];
updateSharedHistoryShortcut();
assert.equal(historyButton.classList.contains('hidden'), true);
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_loading_earlier_timeline_preserves_scroll_anchor_and_chat_labels() -> None:
    message_time = _HTML[_HTML.index("function messageOccurredAt("): _HTML.index("function fmt(")]
    rendering = _HTML[_HTML.index("function historyLoadButton("): _HTML.index("function beginHistoryLoad(")]
    script = r"""
const assert = require('node:assert/strict');
let conversation;
class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.textContent = '';
    this.classList = {add() {}, contains() {return false}};
    this.height = tag === 'article' ? 30 : 20;
  }
  append(...children) {this.children.push(...children)}
  replaceChildren(...children) {this.children = children}
  setAttribute() {}
  getBoundingClientRect() {
    const rows = conversation.children[0]?.children || [];
    const index = rows.indexOf(this);
    return {top: rows.slice(0, index).reduce((sum, row) => sum + row.height, 0) - conversation.scrollTop};
  }
  querySelector(selector) {
    const history = selector.match(/data-history-ticket-id="(\d+)"/);
    const message = selector.match(/data-message-id="(\d+)"/);
    const match = node => history ? String(node.dataset?.historyTicketId) === history[1]
      : message ? String(node.dataset?.messageId) === message[1] : false;
    const visit = node => {
      if (match(node)) return node;
      for (const child of node.children) {const found = visit(child); if (found) return found}
      return null;
    };
    return visit(this);
  }
  get scrollHeight() {return (this.children[0]?.children || []).reduce((sum, row) => sum + row.height, 0)}
}
conversation = new Node('conversation');
conversation.clientHeight = 80;
conversation.scrollTop = 0;
const document = {createElement: tag => new Node(tag), createTextNode: value => ({textContent: value, children: []})};
const $ = id => id === 'conversation' ? conversation : new Node(id);
const state = {conversationSearchOpen: false, historyLoadBusy: false, conversationSelectionPointer: null, agent: {role: 'AGENT'}};
const isStaffChat = ticket => ticket.conversation_origin === 'STAFF';
const supportNumberLabel = () => 'Business line';
const createSupportNumberLabel = () => {const node = new Node('span'); node.textContent = 'Business line'; return node};
const formatStatus = value => value;
const fmt = value => value;
const createIcon = () => new Node('icon');
const addMessage = (target, message) => {
  const row = new Node('article'); row.dataset.messageId = String(message.id); target.append(row);
};
const addEvent = () => {};
const addDateDivider = () => {};
const dayKey = () => '';
const emptyMarkup = () => ({outer: new Node('empty')});
const loadEarlierConversations = () => {};
const loadOlderConversationMessages = () => {};
let audioReconciliations = 0;
const updateConversationKeepingAudio = () => {audioReconciliations++; return false};
const updateScrollLatestButton = () => {};
""" + message_time + rendering + r"""
assert.equal(messageOccurredAt({created_at: '2020-01-01T12:00:00Z', whatsapp_timestamp: String(Date.parse('2020-01-01T10:00:00Z') / 1000)}),
  '2020-01-01T10:00:00.000Z', 'delayed phone messages use their WhatsApp send time');
const newest = {ticket: {id: 30, conversation_origin: 'STAFF', ticket_reference: 'CHAT-30'},
  context: {}, messages: [{id: 300}], messages_has_more: true};
const data = {ticket: {id: 42}, context: {}, messages: [{id: 400}], messages_has_more: true,
  events: [], previous_ticket_history: [newest], previous_ticket_history_has_more: true};
renderConversation(data, {preserveAudio: false});
assert.equal(conversation.children[0].children[0].children[1].textContent, 'Shared customer history');
assert.equal(conversation.children[0].children[1].children[0].textContent, 'Load earlier conversations');
const chatDivider = conversation.querySelector('[data-history-ticket-id="30"]');
assert.match(chatDivider.children[0].textContent, /^Chat · CHAT-30/);
conversation.scrollTop = 35;
const before = chatDivider.getBoundingClientRect().top;
data.previous_ticket_history.unshift({ticket: {id: 20, conversation_origin: 'CUSTOMER'},
  context: {}, messages: [{id: 200}], messages_has_more: false});
renderConversation(data, {preserveScrollAnchor: '[data-history-ticket-id="30"]'});
assert.equal(conversation.querySelector('[data-history-ticket-id="30"]').getBoundingClientRect().top, before,
  'loading an earlier conversation must keep the visible chat divider stationary');
assert.ok(conversation.scrollTop > 35);
assert.equal(audioReconciliations, 1, 'pagination keeps the audio-preserving reconciliation path');

const originalMessage = conversation.querySelector('[data-message-id="400"]');
const messageTop = originalMessage.getBoundingClientRect().top;
data.messages.unshift({id: 390});
renderConversation(data, {preserveScrollAnchor: '[data-message-id="400"]'});
assert.equal(conversation.querySelector('[data-message-id="400"]').getBoundingClientRect().top, messageTop,
  'loading older current messages must keep the first visible message stationary');
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_navigation_cancellation_does_not_show_network_error_or_clear_session() -> None:
    errors = _HTML[_HTML.index("function showError("): _HTML.index("function notify(")]
    api_function = _HTML[_HTML.index("async function api("): _HTML.index("function setAgentIdentity(")]
    bootstrap = _HTML[_HTML.index("async function bootstrap("): _HTML.index("async function logout(")]
    script = r"""
const assert = require('node:assert/strict');
const state = {pageLeaving: false, queueRequest: 0, ticketRequest: 0};
const banner = {visible: false, classList: {toggle(name, visible) {banner.visible = visible}}};
const errorText = {textContent: ''};
const retryButton = {visible: false, classList: {
  add(name) {if (name === 'hidden') retryButton.visible = false},
  remove(name) {if (name === 'hidden') retryButton.visible = true}
}};
const sessionStatus = {textContent: ''};
const $ = name => name === 'error' ? banner : name === 'sessionRetryBtn' ? retryButton :
  name === 'sessionLoadingStatus' ? sessionStatus : errorText;
const text = (element, value) => {element.textContent = value};
let loginCalls = 0;
const showLogin = () => {loginCalls++};
const syncThemeControls = () => {};
let responseMode = 'network';
const fetch = async () => {
  if (responseMode === 'network') throw new TypeError('NetworkError when attempting to fetch resource');
  return {status: 401, ok: false, json: async () => ({})};
};
""" + errors + api_function + bootstrap + r"""
(async () => {
  markPageLeaving();
  assert.equal(state.pageLeaving, true);
  assert.equal(state.ticketRequest, 1);
  assert.equal(state.queueRequest, 1);
  showError('NetworkError when attempting to fetch resource');
  assert.equal(banner.visible, false);
  assert.equal(errorText.textContent, '');
  await bootstrap();
  assert.equal(loginCalls, 0, 'a cancelled /me fetch must preserve the saved workspace');
  responseMode = 'unauthorized';
  await assert.rejects(api('/api/support/me'), /session expired/);
  assert.equal(loginCalls, 0, 'a late 401 during unload must not clear the workspace');

  state.pageLeaving = false;
  showError('NetworkError when attempting to fetch resource');
  assert.equal(banner.visible, true, 'a real network error remains visible while the page is active');
  responseMode = 'network';
  await bootstrap();
  assert.equal(loginCalls, 0, 'a transient /me failure must not sign out or clear the saved selection');
  assert.equal(retryButton.visible, true);
  assert.match(sessionStatus.textContent, /Unable to check your session/);
  responseMode = 'unauthorized';
  await assert.rejects(api('/api/support/me'), /session expired/);
  assert.equal(loginCalls, 1, 'an active 401 still signs the user out');
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_session_check_prevents_login_flash_and_has_retry_or_login_fallback() -> None:
    assert '<main class="login-page" id="sessionLoading">' in _HTML
    assert '<main class="login-page hidden" id="loginPage">' in _HTML
    assert '<div class="app-shell hidden" id="dashboard">' in _HTML
    assert "$('sessionRetryBtn').addEventListener('click', bootstrap);" in _HTML
    views = _HTML[_HTML.index("function showLogin()"): _HTML.index("async function restoreSupportWorkspace()")]
    bootstrap = _HTML[_HTML.index("async function bootstrap()"): _HTML.index("async function logout()")]
    script = CONNECTION_HOOKS + r"""
const assert = require('node:assert/strict');
function element(hidden = false) {
  const classes = new Set(hidden ? ['hidden'] : []);
  return {classList: {
    add(name) {classes.add(name)}, remove(name) {classes.delete(name)},
    contains(name) {return classes.has(name)}
  }, style: {}, value: '', replaceChildren() {}, setAttribute() {}, focus() {}};
}
const elements = new Map([
  ['sessionLoading', element()], ['sessionLoadingStatus', element()],
  ['sessionRetryBtn', element(true)], ['loginPage', element(true)],
  ['dashboard', element(true)]
]);
const $ = id => {
  if (!elements.has(id)) elements.set(id, element());
  return elements.get(id);
};
const text = (node, value) => {node.textContent = value};
const window = {setTimeout(callback) {callback()}};
const state = {pageLeaving: false, authenticated: false, imageEditor: null,
  mediaRecorder: null, queueRequest: 0, ticketRequest: 0,
  globalSearchRequest: 0, linkedDeviceRequest: 0, dashboardReadBoundaries:new Map(),chatNotificationIds:new Set()};
const resetQueueFilters = () => {};
let clearedSelections = 0;
const clearSupportSelections = () => {clearedSelections++};
const disconnectSupportEvents = () => {};
const stopDashboardRefresh = () => {};
const saveLinkedDeviceAddition = () => {};
const cancelVoiceRecording = () => {};
const clearMediaDraft = () => {};
const clearMessageSelection = () => {};
const clearTicketWorkspace = () => {};
const setQueueView = () => {};
const closeDialog = () => {};
const showLoginError = () => {};
const configureStaffAccess = () => {};
const startSupportActivity = () => {};
const stopSupportActivity = () => {};
const clearAgentStatistics = () => {};
const connectSupportEvents = () => {};
const startDashboardRefresh = () => {};
const syncThemeControls = () => {};
const setAgentIdentity = () => {};
const loadAiSettings = async () => {};
const renderAiSettingsUnavailable = () => {};
const restoreSupportWorkspace = async () => {};
const refreshAllQueueCounts = async () => {};
const showError = () => {};
let mode = 'pending';
let finishMe;
const api = () => {
  if (mode === 'pending') return new Promise(resolve => {finishMe = resolve});
  if (mode === 'network') return Promise.reject(new TypeError('NetworkError'));
  if (mode === 'unauthorized') {
    showLogin();
    return Promise.reject(Object.assign(new Error('Session expired'), {status: 401}));
  }
  throw new Error('Unexpected test mode');
};
""" + views + bootstrap + r"""
(async () => {
  const pending = bootstrap();
  assert.equal($('sessionLoading').classList.contains('hidden'), false);
  assert.equal($('loginPage').classList.contains('hidden'), true);
  assert.equal($('dashboard').classList.contains('hidden'), true);
  finishMe({id: '7'});
  await pending;
  assert.equal($('sessionLoading').classList.contains('hidden'), true);
  assert.equal($('loginPage').classList.contains('hidden'), true);
  assert.equal($('dashboard').classList.contains('hidden'), false);
  assert.equal(clearedSelections, 0);

  $('sessionLoading').classList.remove('hidden');
  $('dashboard').classList.add('hidden');
  state.authenticated = false;
  mode = 'network';
  await bootstrap();
  assert.equal($('sessionLoading').classList.contains('hidden'), false);
  assert.equal($('sessionRetryBtn').classList.contains('hidden'), false);
  assert.equal($('loginPage').classList.contains('hidden'), true);
  assert.equal(clearedSelections, 0, 'a transient failure preserves saved ticket selection');

  mode = 'pending';
  const retry = bootstrap();
  assert.equal($('sessionRetryBtn').classList.contains('hidden'), true);
  finishMe({id: '7'});
  await retry;
  assert.equal($('dashboard').classList.contains('hidden'), false);

  $('sessionLoading').classList.remove('hidden');
  $('dashboard').classList.add('hidden');
  state.authenticated = false;
  mode = 'unauthorized';
  await bootstrap();
  assert.equal($('sessionLoading').classList.contains('hidden'), true);
  assert.equal($('loginPage').classList.contains('hidden'), false);
  assert.equal($('dashboard').classList.contains('hidden'), true);
  assert.equal(clearedSelections, 1, 'an expired session clears saved ticket selection');
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_claiming_a_ticket_moves_the_agent_to_my_tickets() -> None:
    do_action = _HTML[_HTML.index("async function doAction("): _HTML.index("async function setStatus(")]
    script = r"""
const assert = require('assert');
const state = {busy: false, ticketId: '41', view: 'unassigned'};
const calls = [];
let fail = false;
const setWorkspaceBusy = () => {};
const showError = () => {};
const notify = () => {};
const api = async path => {
  calls.push(['api', path]);
  if (fail) throw new Error('Ticket is already assigned');
};
const setQueueView = (view, options) => {
  calls.push(['view', view, options]);
  state.view = view;
};
const openTicket = async (id, options) => {
  calls.push(['open', id, options.refreshQueue, state.view]);
};
""" + do_action + r"""
(async () => {
  await doAction('/claim', 'Ticket claimed.');
  assert.deepStrictEqual(calls, [
    ['api', '/api/support/tickets/41/claim'],
    ['view', 'mine', {load: false}],
    ['open', '41', true, 'mine'],
  ]);

  calls.length = 0;
  state.view = 'unassigned';
  fail = true;
  await doAction('/claim', 'Ticket claimed.');
  assert.deepStrictEqual(calls, [['api', '/api/support/tickets/41/claim']]);
  assert.equal(state.view, 'unassigned', 'a failed claim stays in the current queue');

  calls.length = 0;
  fail = false;
  await doAction('/resolve', 'Ticket resolved.');
  assert.equal(calls.some(call => call[0] === 'view'), false, 'only claiming changes the queue');
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_status_menu_stays_open_across_background_refreshes() -> None:
    render = _HTML[_HTML.index("function actionButton("): _HTML.index("function setWorkspaceBusy(")]
    script = r"""
const assert = require('assert');
const makeElement = tag => ({
  tagName: tag.toUpperCase(), children: [], dataset: {}, attributes: {}, open: false, className: '',
  append(...nodes) { this.children.push(...nodes) },
  replaceChildren() { this.children = [] },
  setAttribute(name, value) { this.attributes[name] = value },
  contains(node) { return node === this || this.children.some(child => child.contains && child.contains(node)) },
  focus() { document.activeElement = this },
  querySelector(selector) {
    for (const child of this.children) {
      if (selector === '.action-menu' && child.className === 'action-menu') return child;
    }
    return null;
  },
});
const document = {activeElement: null, createElement: makeElement};
const container = makeElement('div');
const $ = () => container;
const createIcon = () => makeElement('svg');
const state = {busy: false, ticketId: '41'};
const isStaffChat = () => false;
const formatStatus = status => status;
const noop = () => {};
const openTransferModal = noop, resolveTicket = noop, deleteHistoryTicket = noop, setStatus = noop;
const closeStaffChat = noop, doAction = noop;
const data = {
  ticket: {status: 'IN_PROGRESS', active_key: 'k'},
  permissions: {can_claim: false, can_transfer: true, can_operate: true, can_delete_history: false},
};
""" + render + r"""
const menu = () => container.querySelector('.action-menu');
renderActions(data);
assert.equal(menu().open, false);

menu().open = true;
const items = menu().children[1].children;
items[1].focus();
renderActions(data);
assert.equal(menu().open, true, 'a refresh of the same ticket keeps the menu open');
assert.equal(document.activeElement, menu().children[1].children[1], 'focus stays on the same status option');
assert.equal(document.activeElement.dataset.status, 'WAITING_INTERNAL');

menu().open = false;
renderActions(data);
assert.equal(menu().open, false, 'a closed menu stays closed');

menu().open = true;
state.ticketId = '42';
renderActions(data);
assert.equal(menu().open, false, 'switching tickets closes the menu');
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_ticket_details_load_only_after_selection_not_pointer_hover() -> None:
    assert "prefetchTicketDetail" not in _HTML
    assert "takeTicketPrefetch" not in _HTML
    assert "const data = await api(detailPath);" in _HTML
    assert "requestId !== state.ticketRequest || String(state.ticketId) !== String(id)" in _HTML
