import test from 'node:test';
import assert from 'node:assert/strict';
import { classifyDisconnect, reconnectDelay, newConnectionDiagnostics, observeHeartbeat } from '../src/connection-diagnostics.js';
import { Bridge } from '../src/bridge.js';

const gate = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const turn = () => new Promise(resolve => setImmediate(resolve));

test('transport recovery is distinct from revocation, session conflict and storage fencing', () => {
  for (const [code, category, retry] of [[408, 'temporary_transport', true], [515, 'restart_required', true],
    [401, 'logged_out', false], [403, 'authentication_failure', false], [440, 'session_conflict', false],
    [411, 'protocol_failure', false], [500, 'protocol_failure', false]]) {
    assert.deepEqual(classifyDisconnect({output: {statusCode: code}}), {category, retry});
  }
  assert.deepEqual(classifyDisconnect({bridgePersistenceFailure: true, output: {statusCode: 408}}),
    {category: 'storage_failure', retry: false});
  assert.equal(reconnectDelay(0, false, () => 0), 1000);
  assert.equal(reconnectDelay(0, false, () => 1), 2000);
  assert.equal(reconnectDelay(100, false, () => 1), 60000);
  assert.equal(reconnectDelay(0, true, () => 0), 500);
  assert.equal(reconnectDelay(2, true, () => 1), 4000);
  assert.equal(reconnectDelay(100, true, () => 1), 60000);
});

test('diagnostics keep bounded timing fields without copying provider payloads', () => {
  const diagnostics = newConnectionDiagnostics();
  observeHeartbeat(diagnostics, {event: 'decoded_frame', credentials: 'secret'}, 10);
  observeHeartbeat(diagnostics, {event: 'heartbeat_tick', frame_age_ms: 35100, timer_delay_ms: 1200, body: 'private'}, 20);
  observeHeartbeat(diagnostics, {event: 'heartbeat_result', rtt_ms: 35}, 30);
  observeHeartbeat(diagnostics, {event: 'heartbeat_timeout'}, 40);
  assert.equal(diagnostics.last_frame_at, 10);
  assert.equal(diagnostics.heartbeat_timer_delay_ms, 1200);
  assert.equal(diagnostics.heartbeat_rtt_ms, 35);
  assert.equal(diagnostics.heartbeat_timeouts, 1);
  assert.doesNotMatch(JSON.stringify(diagnostics), /secret|private|credentials/);
});

test('nearby reconnects are staggered without waiting behind another line long backoff', t => {
  t.mock.timers.enable({apis: ['setTimeout', 'Date'], now: 10000});
  const bridge = Object.assign(Object.create(Bridge.prototype), {
    stopping: false, reconnectSlots: new Map([['long', 70000]]), random: () => 0, trace: () => {},
  });
  const line = id => ({line_id: id, generation: 1, store: {data: {enabled: true}}, status: 'disconnected'});
  const one = line('one'), two = line('two');
  bridge.scheduleReconnect(one); bridge.scheduleReconnect(two);
  assert.equal(bridge.reconnectSlots.get('one'), 11000);
  assert.equal(bridge.reconnectSlots.get('two'), 11250);
  one.retryBlocked = true; one.reconnectTimer = null;
  bridge.scheduleReconnect(one);
  assert.equal(one.reconnectAttempts, 1);
});

test('durable enqueue wakes callbacks immediately and never overlaps a line owner', async () => {
  const commit = gate(), active = gate(); let calls = 0, running = 0, maximum = 0;
  const data = {entries: []};
  const bridge = Object.assign(Object.create(Bridge.prototype), {
    stopping: false, workerTimer: true, callbackWakeups: new Set(), callbackTasks: new Map(),
    lines: new Map(), manifest: {data: {removed_line_ids: []}}, onError: error => { throw error; },
    outbox: {data, update: async work => { await commit.promise; work(data); }},
    deliverLineCallbacks: async () => { calls++; maximum = Math.max(maximum, ++running); await active.promise; running--; },
  });
  const first = bridge.enqueue({line_id: '90001', event_id: 'first'});
  await turn(); assert.equal(calls, 0, 'no callback before journal commit');
  commit.resolve(); await first; await turn(); assert.equal(calls, 1);
  await bridge.enqueue({line_id: '90001', event_id: 'second'});
  await turn(); assert.equal(calls, 1, 'active owner retains its line');
  active.resolve(); await turn(); await turn();
  assert.equal(calls, 2, 'enqueue racing the running task is not forgotten');
  assert.equal(maximum, 1);
  bridge.stopping = true; clearImmediate(bridge.callbackWakeHandle);
});
