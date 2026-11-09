// Live-update reconnection, connection notice and read-retry backoff.
// Runs the shipped events.js and refresh.js with an injected clock, a fake
// EventSource and a fake API; no browser, network or services.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const eventsSource = fs.readFileSync('app/static/support/events.js', 'utf8');
const refreshSource = fs.readFileSync('app/static/support/refresh.js', 'utf8').replaceAll('__POLL_MS__', '30000');
const microtasks = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

function harness({random = 0} = {}) {
  let now = 1_000_000, nextId = 0;
  const timers = new Map(), listeners = {document: {}, window: {}}, elements = new Map();
  const apiCalls = [], refreshes = [], sources = [], runs = [];
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set(id === 'connectionBanner' ? ['hidden'] : []);
      let content = '', writes = 0;
      elements.set(id, {
        classList: {add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
          toggle: (c, on) => (on ? classes.add(c) : classes.delete(c))},
        get textContent() { return content; }, set textContent(value) { content = value; writes++; },
        get writes() { return writes; }, addEventListener() {},
      });
    }
    return elements.get(id);
  };
  class FakeEventSource {
    constructor(url) { this.url = url; this.closed = false; sources.push(this); }
    close() { this.closed = true; }
  }
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  const on = target => (type, fn) => { (listeners[target][type] ||= []).push(fn); };
  const context = vm.createContext({
    Date: FakeDate, Promise, Map, Set, JSON, Number, String, Boolean, Object, Array, Error, TypeError, DOMException,
    setTimeout: (fn, delay) => { timers.set(++nextId, {at: now + delay, delay, fn}); return nextId; },
    clearTimeout: id => { timers.delete(id); },
    document: {hidden: false, addEventListener: on('document'), querySelectorAll: () => []},
    window: {addEventListener: on('window')},
    EventSource: FakeEventSource,
    $: element, text: (node, value) => { node.textContent = value; },
    state: {authenticated: true, pageLeaving: false, supportEventSource: null, supportEventsRetryTimer: null,
      supportEventsAttempt: 0, supportEventsGeneration: 0, supportEventsProbe: null,
      activeModal: null, busy: false, ticketId: null, view: 'unassigned'},
    invalidateQueueCounts: () => {}, resetDashboardRequests: () => {}, clearAttentionIndicator: () => {},
    loadAiSettings: async () => {}, renderAiSettingsUnavailable: () => {},
    refreshAllQueueCounts: async () => {}, openTicket: async () => {},
    loadQueue: async () => { runs.push(now); },
  });
  context.api = path => new Promise((resolve, reject) => apiCalls.push({path, resolve, reject}));
  context.showLogin = () => { context.state.authenticated = false; context.disconnectSupportEvents(); context.resetConnectionHealth(); };
  vm.runInContext(eventsSource + '\n' + refreshSource, context);
  vm.runInContext(`Math.random = () => ${random};`, context);
  const scheduler = vm.runInContext('dashboardRefresh', context);
  const realRequest = scheduler.request.bind(scheduler);
  scheduler.request = (...scopes) => { refreshes.push(scopes); return realRequest(...scopes); };
  const advance = async ms => {
    const end = now + ms;
    for (;;) {
      const next = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > end) break;
      now = next[1].at; timers.delete(next[0]); next[1].fn(); await microtasks();
    }
    now = end; await microtasks();
  };
  const dispatch = async (target, type, event = {}) => {
    for (const fn of listeners[target][type] || []) fn(event);
    await microtasks();
  };
  const live = () => sources.filter(source => !source.closed);
  const banner = () => element('connectionBanner');
  const health = vm.runInContext('connectionHealth', context);
  // Timers by owner: the live-update reconnect, the notice re-render, the scheduler.
  const retryDelay = () => timers.get(context.state.supportEventsRetryTimer)?.delay;
  const reconnectTimers = () => [...timers.keys()].filter(id => id !== health.timer && id !== scheduler.timer);
  return {context, state: context.state, timers, apiCalls, refreshes, sources, runs, advance, dispatch, live,
    banner, bannerText: () => element('connectionBannerText').textContent, element,
    now: () => now, scheduler, health, retryDelay, reconnectTimers};
}

const networkError = () => Object.assign(new TypeError('Failed to fetch'), {network: true});
const httpError = status => Object.assign(new Error('HTTP ' + status), {status});

test('repeated failures, including 502 responses, reconnect with one source and one capped, jittered retry', async () => {
  const h = harness();
  h.context.connectSupportEvents();
  assert.equal(h.sources.length, 1);
  const delays = [];
  for (let attempt = 0; attempt < 7; attempt++) {
    const source = h.state.supportEventSource;
    // A 502 from the proxy or a dropped stream both surface as an error event.
    source.onerror(); source.onerror();
    assert.equal(source.closed, true, 'the built-in EventSource retry must not compete');
    assert.equal(h.live().length, 0);
    assert.equal(h.reconnectTimers().length, 1, 'exactly one scheduled reconnect');
    delays.push(h.retryDelay());
    await h.advance(delays.at(-1));
    assert.equal(h.apiCalls.length, attempt + 1, 'one session check per retry');
    // Recovery signals during a pending check do not start a second one.
    await h.dispatch('window', 'online');
    await h.dispatch('document', 'visibilitychange');
    assert.equal(h.apiCalls.length, attempt + 1);
    h.apiCalls.at(-1).resolve({id: '7'});
    await h.advance(0);
    assert.equal(h.live().length, 1);
  }
  // Base 1 s doubling, capped at 30 s; Math.random() = 0 gives the 50% lower jitter bound.
  assert.deepEqual(delays, [500, 1000, 2000, 4000, 8000, 15000, 15000]);
  assert.equal(h.sources.length, 8);
  assert.ok(h.apiCalls.every(call => call.path === '/api/support/me'));
});

test('the upper jitter bound stays at about 30 seconds', async () => {
  const h = harness({random: 0.999});
  h.context.connectSupportEvents();
  for (let attempt = 0; attempt < 8; attempt++) {
    h.state.supportEventSource.onerror();
    const delay = h.retryDelay();
    assert.ok(delay <= 30000, 'delay ' + delay);
    await h.advance(delay);
    h.apiCalls.at(-1).resolve({});
    await h.advance(0);
  }
  h.state.supportEventSource.onerror();
  assert.ok(h.retryDelay() >= 29900);
});

test('a failed session check backs off; success reconciles once and resets the backoff', async () => {
  const h = harness();
  h.context.connectSupportEvents();
  h.state.supportEventSource.onopen();
  assert.deepEqual(h.refreshes.at(-1), ['queue', 'conversation', 'counts'], 'first open keeps the existing refresh');
  h.state.supportEventSource.onerror();
  await h.advance(500);
  h.apiCalls[0].reject(networkError());
  await h.advance(0);
  assert.equal(h.live().length, 0);
  assert.equal(h.retryDelay(), 1000);
  await h.advance(1000);
  h.apiCalls[1].reject(httpError(502));
  await h.advance(0);
  assert.equal(h.retryDelay(), 2000);
  await h.advance(2000);
  h.apiCalls[2].resolve({});
  await h.advance(0);
  const before = h.refreshes.length;
  h.state.supportEventSource.onopen();
  // Missed events are not replayed: one authoritative refresh, including history.
  assert.equal(h.refreshes.length, before + 1);
  assert.deepEqual(h.refreshes.at(-1), ['queue', 'history', 'counts']);
  assert.equal(h.state.supportEventsAttempt, 0);
  h.state.supportEventSource.onerror();
  assert.equal(h.retryDelay(), 500);
});

test('an expired or revoked session follows the sign-in flow and stops reconnecting', async () => {
  const h = harness();
  h.context.connectSupportEvents();
  h.state.supportEventSource.onerror();
  await h.advance(500);
  // api() runs the normal sign-in flow on 401 before rejecting.
  h.context.showLogin();
  h.apiCalls[0].reject(httpError(401));
  await h.advance(0);
  assert.equal(h.state.authenticated, false);
  assert.equal(h.live().length, 0);
  assert.equal(h.reconnectTimers().length, 0);
  assert.equal(h.health.timer, null, 'sign-out clears the notice timer');
  await h.dispatch('window', 'online');
  await h.advance(120000);
  assert.equal(h.apiCalls.length, 1);
  assert.equal(h.sources.length, 1);
  assert.equal(h.banner().classList.contains('hidden'), true);
});

test('a session that loses dashboard access stops retrying and says so', async () => {
  const h = harness();
  h.context.connectSupportEvents();
  h.state.supportEventSource.onerror();
  await h.advance(500);
  h.apiCalls[0].reject(httpError(403));
  await h.advance(0);
  assert.equal(h.reconnectTimers().length, 0, 'no reconnect is scheduled');
  assert.equal(h.state.supportEventsRetryTimer, null);
  assert.equal(h.banner().classList.contains('hidden'), false);
  assert.match(h.bannerText(), /no longer has dashboard access/);
  await h.advance(120000);
  assert.equal(h.apiCalls.length, 1);
});

test('sign-out cancels a scheduled retry and discards a late session check', async () => {
  const h = harness();
  h.context.connectSupportEvents();
  h.state.supportEventSource.onerror();
  h.context.showLogin();
  await h.advance(60000);
  assert.equal(h.apiCalls.length, 0);
  h.state.authenticated = true;
  h.context.connectSupportEvents();
  h.state.supportEventSource.onerror();
  await h.advance(500);
  assert.equal(h.apiCalls.length, 1);
  h.context.disconnectSupportEvents();
  h.apiCalls[0].resolve({});
  await h.advance(0);
  assert.equal(h.live().length, 0, 'a late success after teardown must not reopen');
  assert.equal(h.sources.length, 2);
});

test('network restoration and showing the tab skip the wait; hidden tabs keep retrying', async () => {
  const h = harness();
  h.context.connectSupportEvents();
  h.state.supportEventSource.onerror();
  assert.equal(h.reconnectTimers().length, 1);
  await h.dispatch('window', 'online');
  assert.equal(h.apiCalls.length, 1);
  assert.equal(h.state.supportEventsRetryTimer, null);
  h.apiCalls[0].resolve({});
  await h.advance(0);
  // Background tabs keep their live connection for notification sounds.
  h.context.document.hidden = true;
  h.state.supportEventSource.onerror();
  await h.advance(1000);
  assert.equal(h.apiCalls.length, 2);
  h.apiCalls[1].reject(networkError());
  await h.advance(0);
  h.context.document.hidden = false;
  await h.dispatch('document', 'visibilitychange');
  assert.equal(h.apiCalls.length, 3);
  // Back/forward-cache: pagehide closes, pageshow reconnects.
  h.apiCalls[2].resolve({});
  await h.advance(0);
  await h.dispatch('window', 'pagehide', {persisted: true});
  assert.equal(h.live().length, 0);
  assert.equal(h.reconnectTimers().length, 0);
  await h.dispatch('window', 'pageshow', {persisted: true});
  await h.advance(0);
  assert.equal(h.apiCalls.length, 4);
});

test('the connection notice ignores brief reconnects, escalates extended outages and clears on recovery', async () => {
  const h = harness();
  h.context.connectSupportEvents();
  h.state.supportEventSource.onopen();
  h.context.noteBackgroundRead('queue');
  h.state.supportEventSource.onerror();
  assert.equal(h.banner().classList.contains('hidden'), true, 'brief live-update blip stays quiet');
  await h.advance(4100);
  assert.equal(h.banner().classList.contains('hidden'), false);
  assert.match(h.bannerText(), /Live updates are reconnecting/);
  // A failed background read is shown at once and states data age.
  h.context.noteBackgroundRead('queue', networkError());
  assert.match(h.bannerText(), /^Connection problem\. Reconnecting….*Showing information from/);
  const writes = h.element('connectionBannerText').writes;
  h.context.noteBackgroundRead('counts', httpError(503));
  assert.equal(h.element('connectionBannerText').writes, writes, 'unchanged text is not re-announced');
  assert.equal(h.banner().classList.contains('extended'), false);
  await h.advance(45000);
  assert.equal(h.banner().classList.contains('extended'), true);
  assert.match(h.bannerText(), /^Connection lost for 1 minute\. The information shown may be out of date/);
  // Recovery: every failing scope must succeed and live updates must reopen.
  h.context.noteBackgroundRead('queue');
  assert.equal(h.banner().classList.contains('hidden'), false);
  h.context.noteBackgroundRead('counts');
  h.context.reconnectSupportEventsNow();
  h.apiCalls.at(-1).resolve({});
  await h.advance(0);
  h.state.supportEventSource.onopen();
  assert.equal(h.banner().classList.contains('hidden'), true);
  // A closed conversation can no longer be stale.
  h.context.noteBackgroundRead('conversation', networkError());
  assert.equal(h.banner().classList.contains('hidden'), false);
  h.context.forgetBackgroundRead('conversation');
  assert.equal(h.banner().classList.contains('hidden'), true);
  // Signed-out pages never show it.
  h.context.noteBackgroundRead('queue', networkError());
  h.context.showLogin();
  assert.equal(h.banner().classList.contains('hidden'), true);
});

test('failed background reads retry with bounded backoff through the one scheduler, then resume the interval', async () => {
  const h = harness();
  let failing = true;
  h.context.loadQueue = async () => {
    h.runs.push(h.now());
    h.context.noteBackgroundRead('queue', failing ? networkError() : null);
  };
  h.scheduler.start();
  const gaps = [];
  let last = h.now();
  for (let run = 0; run < 7; run++) {
    await h.advance(h.timers.get(h.scheduler.timer).delay);
    gaps.push(h.runs.at(-1) - last);
    last = h.runs.at(-1);
    assert.equal(h.reconnectTimers().length, 0, 'retries use the one scheduler timer');
  }
  // First run at the 30 s interval; then 1 s, 2 s, 4 s, 8 s, 15 s, 15 s (50% jitter bound).
  assert.deepEqual(gaps, [30000, 1000, 2000, 4000, 8000, 15000, 15000]);
  failing = false;
  await h.advance(15000);
  const recovered = h.runs.at(-1);
  await h.advance(30000);
  assert.equal(h.runs.at(-1) - recovered, 30000, 'healthy reads return to the routine interval');
  // Hidden tabs stay silent even while reads were failing.
  failing = true;
  await h.advance(30000);
  h.context.document.hidden = true;
  h.scheduler.visibilityChanged();
  const count = h.runs.length;
  await h.advance(600000);
  assert.equal(h.runs.length, count);
  h.scheduler.stop();
});
