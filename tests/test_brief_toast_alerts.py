import shutil
import subprocess

import pytest

from app.support_ui import _HTML

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")


def _run(script: str) -> None:
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_alerts_are_brief_self_hiding_toasts_without_repeat_spam() -> None:
    toast_code = _HTML[_HTML.index("const TOAST_DURATION_MS"): _HTML.index("function positionToastRegion(")]
    errors = _HTML[_HTML.index("function showError("): _HTML.index("function markPageLeaving(")]
    _run(r"""
const assert = require('node:assert/strict');
const state = {pageLeaving: false};
const timers = [];
const window = {setTimeout(fn, ms) { timers.push({fn, ms}); return timers.length; }, clearTimeout() {}};
const region = {children: [], append(node) { this.children.push(node); }};
const element = tag => { const node = {tag, children: [], attrs: {}, className: '', textContent: '', title: '',
  setAttribute(k, v) { this.attrs[k] = v; }, append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = items; }, addEventListener() {},
  remove() { region.children = region.children.filter(item => item !== node); }}; return node; };
const document = {createElement: element};
const createIcon = () => element('icon');
const positionToastRegion = () => {};
const $ = id => id === 'toastRegion' ? region : null;
""" + toast_code + errors + r"""
const long = 'The number is inactive in the app, but WhatsApp unlinking did not finish. Its conversations are retained. Retry Remove and unlink.';
assert.equal(briefMessage(long), 'The number is inactive in the app, but WhatsApp unlinking did not finish.');
assert.ok(briefMessage('x'.repeat(300)).length <= 110);
notify(long, 'error');
const toast = region.children[0];
assert.equal(toast.children[1].textContent, briefMessage(long), 'the toast shows only the brief text');
assert.equal(toast.title, long, 'the full text stays available on hover');
assert.equal(timers.at(-1).ms, 6500, 'errors hide after a few seconds');
notify('Saved.', 'success');
assert.equal(timers.at(-1).ms, 4000, 'successes hide sooner');
timers.at(-1).fn();
assert.equal(region.children.some(node => node.children[1]?.textContent === 'Saved.'), false, 'the toast removed itself');
const before = region.children.length;
showError('Could not load conversations.');
showError('Could not load conversations.');
assert.equal(region.children.length, before + 1, 'a repeated page error is shown once a minute at most');
showError('');
assert.equal(region.children.length, before + 1, 'clearing an error shows nothing');
""")


def test_number_problems_alert_once_per_page_and_again_after_a_change() -> None:
    function = _HTML[_HTML.index("function alertLinkedDeviceProblems("): _HTML.index("async function loadLinkedDeviceStatus(")]
    _run(r"""
const assert = require('node:assert/strict');
const state = {whatsappRegistry: {numbers: [{phone_number_id: '90052', display_name: 'Export'}]}};
const shown = [];
const notify = (message, type) => shown.push([message, type]);
""" + function + r"""
const failed = [{line_id: '90052', status: 'error'}, {line_id: '90047', status: 'connected'}];
alertLinkedDeviceProblems(failed);
assert.deepEqual(shown, [['Export: connection failed.', 'error']]);
alertLinkedDeviceProblems(failed);
assert.equal(shown.length, 1, 'polling does not repeat the same alert');
alertLinkedDeviceProblems([{line_id: '90052', status: 'connected'}]);
alertLinkedDeviceProblems(failed);
assert.equal(shown.length, 2, 'a problem that comes back alerts again');
alertLinkedDeviceProblems([{line_id: '90056', status: 'disconnected', recovery_category: 'storage_failure'}]);
assert.deepEqual(shown.at(-1), ['90056: needs storage recovery.', 'error']);
""")
