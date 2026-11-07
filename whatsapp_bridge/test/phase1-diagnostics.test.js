import test from 'node:test';
import assert from 'node:assert/strict';
import { classifyDisconnect, reconnectDelay, newConnectionDiagnostics, observeHeartbeat, beginInterruption, finishInterruption, resetConnectionDiagnostics } from '../src/connection-diagnostics.js';
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
  observeHeartbeat(diagnostics, {event: 'heartbeat_request', query_sequence: 1}, 25);
  observeHeartbeat(diagnostics, {event: 'heartbeat_result', query_sequence: 1, rtt_ms: 35}, 30);
  observeHeartbeat(diagnostics, {event: 'heartbeat_timeout'}, 40);
  assert.equal(diagnostics.last_frame_at, 10);
  assert.equal(diagnostics.heartbeat_timer_delay_ms, 1200);
  assert.equal(diagnostics.heartbeat_rtt_ms, null);
  assert.equal(diagnostics.heartbeat_successes, 1);
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


test('undefined query timeout never creates RTT or success, and late results cannot downgrade current evidence', () => {
  const d = newConnectionDiagnostics();
  observeHeartbeat(d, {event: 'heartbeat_request', query_sequence: 1}, 100);
  observeHeartbeat(d, {event: 'heartbeat_query_timeout', query_sequence: 1, query_elapsed_ms: 20000}, 20100);
  assert.equal(d.heartbeat_successes, 0);
  assert.equal(d.heartbeat_query_timeouts, 1);
  assert.equal(d.heartbeat_rtt_ms, null);
  assert.equal(d.recovery_started_at, 20100);
  observeHeartbeat(d, {event: 'heartbeat_request', query_sequence: 2}, 30100);
  observeHeartbeat(d, {event: 'heartbeat_result', query_sequence: 2, rtt_ms: 20}, 30120);
  observeHeartbeat(d, {event: 'heartbeat_error', query_sequence: 1}, 30130);
  observeHeartbeat(d, {event: 'heartbeat_result', query_sequence: 2, rtt_ms: 500}, 30140);
  assert.equal(d.heartbeat_successes, 1);
  assert.equal(d.heartbeat_failures, 0);
  assert.equal(d.heartbeat_rtt_ms, 20);
  assert.equal(d.heartbeat_outcome, 'success');
});

test('recovery spans first failure through new-generation readiness using monotonic time', () => {
  const d = newConnectionDiagnostics();
  beginInterruption(d, 10000, 100);
  beginInterruption(d, 30000, 20100);
  Object.assign(d, {socket_error_code: 'ECONNRESET', socket_end_at: 30100,
    socket_closed_at: 30110, socket_close_had_error: true});
  resetConnectionDiagnostics(d, 2);
  assert.equal(d.socket_error_code, null); assert.equal(d.socket_end_at, null);
  assert.equal(d.socket_closed_at, null); assert.equal(d.socket_close_had_error, null);
  assert.equal(d.recovery_started_at, 10000);
  assert.equal(d.heartbeat_success_at, null);
  assert.equal(finishInterruption(d, 25100), 25);
  assert.equal(d.recovery_seconds_last, 25);
  assert.equal(finishInterruption(d, 25200), null);
});
