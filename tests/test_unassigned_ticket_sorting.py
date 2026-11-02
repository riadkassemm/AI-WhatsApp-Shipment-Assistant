"""Creation-date sorting keeps pagination and the selected conversation intact."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from app.support_ui import _ASSETS, _TEMPLATE


def _run(script: str) -> None:
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_sort_control_is_scoped_to_unassigned_and_keeps_other_filters() -> None:
    source = _ASSETS["rendering.js"]
    helpers = source[source.index("function renderQueueFilters("):source.index("function uniqueQueueItems(")]
    actions = _ASSETS["actions.js"]
    listeners = actions[actions.index("for (const [id, key] of [['queueSupportNumber'"):actions.index("$('queueSearch').addEventListener('keydown'")]
    _run(r"""
const assert = require('node:assert/strict');
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {
    value: '', hidden: false, handlers: {},
    classList: {toggle(name, hidden) {elements.get(id).hidden = hidden}},
    addEventListener(event, handler) {this.handlers[event] = handler},
  });
  return elements.get(id);
};
const state = {view: 'unassigned', query: 'invoice', page: 4, queueRequest: 0,
  queueExhausted: true, ticketId: 42, replyTarget: {id: 9},
  queueFilters: {supportNumber: '90005', staffId: '', status: 'all', read: 'all', sort: 'newest'}};
const draft = {value: 'Unsent draft', selectionStart: 3, selectionEnd: 8};
elements.set('message', draft);
let nextRefresh;
const setTimeout = callback => {nextRefresh = callback; return 1};
const clearTimeout = () => {};
let refreshes = 0;
const loadQueue = options => {assert.deepEqual(options, {silent: true}); refreshes++};
""" + helpers + listeners + r"""
renderQueueFilters();
assert.equal($('queueSortLabel').hidden, false);
assert.equal($('queueSort').value, 'newest');
$('queueSort').handlers.change({target: {value: 'oldest'}});
assert.equal(state.queueFilters.sort, 'oldest');
assert.equal(state.page, 1);
assert.equal(state.queueRequest, 1);
assert.equal(state.queueExhausted, false);
nextRefresh();
assert.equal(refreshes, 1);
const params = new URL(queuePath(2, 30), 'http://test').searchParams;
assert.equal(params.get('sort'), 'oldest');
assert.equal(params.get('support_number'), '90005');
assert.equal(params.get('q'), 'invoice');
assert.equal(params.get('page'), '2');
assert.equal(state.ticketId, 42);
assert.deepEqual(state.replyTarget, {id: 9});
assert.deepEqual(draft, {value: 'Unsent draft', selectionStart: 3, selectionEnd: 8});
for (const view of ['mine', 'chats', 'watching', 'active', 'waiting', 'history', 'contacts']) {
  state.view = view;
  renderQueueFilters();
  assert.equal($('queueSortLabel').hidden, true, view);
  assert.equal(new URL(queuePath(1, 30), 'http://test').searchParams.has('sort'), false, view);
}
""")
    assert '<span class="sr-only">Sort by ticket creation date</span>' in _TEMPLATE
    assert '<option value="newest">Newest</option><option value="oldest">Oldest</option>' in _TEMPLATE


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_sort_survives_pagination_refresh_and_discards_stale_pages() -> None:
    source = _ASSETS["rendering.js"]
    helpers = source[source.index("function queueSupportsUnreadFilter("):source.index("function setQueueView(")]
    _run(r"""
const assert = require('node:assert/strict');
const document = {hidden: false};
const QUEUE_MAX_REQUEST_SIZE = 500;
const state = {authenticated: true, view: 'unassigned', query: '', page: 1, pageSize: 2,
  total: 0, queueItems: [], queueRequest: 0, queueLoadingMore: false, queueExhausted: false,
  queueFilters: {sort: 'newest'}, ticketId: 42, unassignedSnapshot: null};
const queue = {scrollTop: 70, attributes: {},
  setAttribute(key, value) {this.attributes[key] = value},
  getAttribute(key) {return this.attributes[key]}};
const $ = id => id === 'queue' ? queue : {};
const rows = [1, 2, 3, 4, 5, 6].map(id => ({id, last_activity_at: String(id)}));
const requests = [];
let holdNextPage = false, releasePage;
const api = async path => {
  const params = new URL(path, 'http://test').searchParams;
  requests.push(params);
  const ordered = params.get('sort') === 'oldest' ? rows : [...rows].reverse();
  const page = Number(params.get('page')), size = Number(params.get('page_size'));
  const result = {items: ordered.slice((page - 1) * size, page * size), total: rows.length};
  if (holdNextPage) {
    holdNextPage = false;
    await new Promise(resolve => {releasePage = resolve});
  }
  return result;
};
const renderQueue = () => {};
const renderQueueSkeleton = () => {};
const renderQueueError = error => {throw Error(error)};
const renderQueueFilters = () => false;
const updateQueueCount = () => {};
const refreshAllQueueCounts = async () => {};
const saveSupportSelection = () => {};
const noteBackgroundRead = () => {};
const transientReadError = () => false;
const notify = message => {throw Error(message)};
const text = () => {};
let nextRefresh, notifications = 0;
const setTimeout = callback => {nextRefresh = callback; return 1};
const clearTimeout = () => {};
const notifyNewCustomerActivity = () => {notifications++};
""" + helpers + r"""
(async () => {
  await loadQueue();
  assert.deepEqual(state.queueItems.map(row => row.id), [6, 5]);
  await loadMoreQueue();
  assert.deepEqual(state.queueItems.map(row => row.id), [6, 5, 4, 3]);
  assert.equal(requests.at(-1).get('sort'), 'newest');
  holdNextPage = true;
  const stalePage = loadMoreQueue();
  assert.equal(typeof releasePage, 'function');
  state.queueFilters.sort = 'oldest';
  queueCriteriaChanged();
  await nextRefresh();
  assert.deepEqual(state.queueItems.map(row => row.id), [1, 2]);
  releasePage();
  await stalePage;
  assert.deepEqual(state.queueItems.map(row => row.id), [1, 2], 'old pages cannot overwrite the new sort');
  await loadMoreQueue();
  assert.deepEqual(state.queueItems.map(row => row.id), [1, 2, 3, 4]);
  await loadQueue({silent: true});
  assert.deepEqual(state.queueItems.map(row => row.id), [1, 2, 3, 4]);
  assert.equal(requests.at(-1).get('sort'), 'oldest');
  assert.equal(queue.scrollTop, 70);
  assert.equal(state.ticketId, 42);
  assert.equal(state.unassignedSnapshot, null, 'sorted rows do not seed activity notifications');
  await pollUnassignedActivity();
  assert.equal(requests.at(-1).has('sort'), false, 'activity snapshots retain activity ordering');
  assert.equal(notifications, 0, 'a different sort cannot appear to create new tickets');
  rows.unshift({id: 7, last_activity_at: 'new'});
  await pollUnassignedActivity();
  assert.equal(notifications, 1, 'new ticket notifications still work');
})().catch(error => {console.error(error); process.exitCode = 1});
""")
