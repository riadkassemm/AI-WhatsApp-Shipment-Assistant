"""The queue loads more rows as staff scroll instead of paging."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from app.support_ui import _HTML


def test_queue_footer_has_no_page_buttons() -> None:
    assert 'id="previousPageBtn"' not in _HTML
    assert 'id="nextPageBtn"' not in _HTML
    assert "function changePage(" not in _HTML
    assert "pageSize: 50," in _HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_queue_appends_pages_on_scroll_and_refresh_keeps_loaded_rows() -> None:
    start = _HTML.index("function queuePath(")
    end = _HTML.index("async function pollUnassignedActivity(")
    transient = _HTML[_HTML.index("function transientReadError("):_HTML.index("function backgroundRetryDelay(")]
    unread = _HTML[_HTML.index("function queueSupportsUnreadFilter("):_HTML.index("function resetQueueFilters(")]
    script = transient + unread + r"""
const assert = require('node:assert/strict');
const document = {hidden:false};
const noteBackgroundRead = () => {};
const QUEUE_MAX_REQUEST_SIZE = 500;
const tickets = Array.from({length: 120}, (_, index) => ({id: index + 1}));
const requests = [];
let failNext = false;
async function api(path) {
  requests.push(path);
  if (failNext) { failNext = false; throw new Error('offline'); }
  const params = new URL(path, 'http://x').searchParams;
  const page = Number(params.get('page')), size = Number(params.get('page_size'));
  return {items: tickets.slice((page - 1) * size, page * size), total: tickets.length};
}
const attributes = {};
const queue = {scrollTop: 0, setAttribute(name, value) {attributes[name] = value}, getAttribute(name) {return attributes[name]}};
const elements = {queue, refreshQueueBtn: {}};
const $ = id => elements[id];
const state = {authenticated: true, view: 'history', page: 1, pageSize: 50, total: 0, queueItems: [],
  queueRequest: 0, queueLoadingMore: false, queueExhausted: false, query: '', pageLeaving: false};
let renders = 0, saved = 0, notices = [];
const renderQueue = () => {renders++};
const renderQueueSkeleton = () => {};
const renderQueueError = () => {};
const updateQueueCount = () => {};
const refreshAllQueueCounts = async () => {};
const renderQueueFilters = () => {};
const saveSupportSelection = () => {saved++};
const notify = (message, tone) => notices.push([message, tone]);
const text = () => {};
""" + _HTML[start:end] + r"""
(async () => {
  await loadQueue();
  assert.equal(state.queueItems.length, 50);
  assert.equal(queueHasMore(), true);

  await loadMoreQueue();
  assert.equal(requests.at(-1), '/api/support/tickets?page=2&page_size=50&q=&view=history');
  assert.equal(state.queueItems.length, 100);
  assert.equal(state.page, 2);
  assert.ok(saved > 0, 'the loaded depth is remembered');

  // New activity moves a ticket to the top: the next page repeats one row,
  // which is dropped instead of shown twice.
  tickets.unshift({id: 999});
  await loadMoreQueue();
  assert.equal(state.queueItems.length, 120);
  assert.equal(new Set(state.queueItems.map(item => item.id)).size, 120);

  // A refresh reloads everything already scrolled into view.
  await loadQueue({silent: true});
  assert.equal(requests.at(-1), '/api/support/tickets?page=1&page_size=150&q=&view=history');
  assert.equal(state.queueItems.length, 121);
  assert.equal(state.queueItems[0].id, 999);

  // Server-side searches continue through the complete matching dataset.
  state.total = 500; state.query = 'ab';
  assert.equal(queueHasMore(), true);
  state.query = '';

  // A failed page stops automatic retries until the next refresh.
  state.total = 500; failNext = true;
  await loadMoreQueue();
  assert.equal(state.queueExhausted, true);
  assert.equal(notices.at(-1)[1], 'error');
  const before = requests.length;
  await loadMoreQueue();
  assert.equal(requests.length, before);

  // Very deep lists refresh in API-sized chunks.
  state.page = 12;
  await loadQueue({silent: true});
  assert.deepEqual(requests.slice(-2), [
    '/api/support/tickets?page=1&page_size=500&q=&view=history',
    '/api/support/tickets?page=2&page_size=500&q=&view=history',
  ]);
  assert.equal(state.queueExhausted, false);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
