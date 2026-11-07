import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {EventEmitter} from 'node:events';
import {Bridge} from '../src/bridge.js';
import {createServer} from '../src/server.js';
import {createRuntime} from '../src/runtime-lifecycle.js';
import {storageTool} from '../src/storage-tool.js';
import {acquireDataDirectory, createAuth, atomicWrite} from '../src/storage.js';
import {JournalStore, decode} from '../src/journal-store.js';
import {assessAuthRetention, inspectAuthRetention, retainAuth} from '../src/fenced-auth.js';

const ID = '90035', HEALTHY = ['90012', '90013', '90015', '90017', '90024', '90026'];
const gate = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
const disk = async () => ({bsize: 4096, blocks: 10_000_000, bavail: 5_000_000, files: 1_000_000, ffree: 900_000});
const write = (file, data) => atomicWrite(file, data);
const hashes = async dir => Object.fromEntries(await Promise.all((await fs.readdir(dir)).filter(name => !name.startsWith('media')).sort()
  .map(async name => [name, crypto.createHash('sha256').update(await fs.readFile(path.join(dir, name))).digest('hex')])));

function evidence(count = 477) {
  const operations = Array.from({length: count}, (_, i) => ({id: `synthetic-${i}`, source: 'messageMutex',
    operation: 'pipeline', sequence_at_start: 0, sequence_at_end: 0, provider_outcome: i % 2 ? 'completed' : 'rejected',
    wrapper_outcome: 'rejected', mutation_batches: i < 3 ? 4 : 0, credentials_changed: i < 3}));
  return {failure: {version: 2, failures: [{operation: 'drain_timeout', mutations: []}], recovery: {
    version: 1, revision: 2, owner_id: 'synthetic-owner', connection_generation: 2, sequence: 0,
    retired: true, transport_state: 'closed', events_released: true, settled: true,
    pending_operations: 0, pending_credential_writes: 0, pending_journal_writes: 0, evidence_write_failed: false,
    credentials: {registered: true, uncertain: 'synthetic-not-production'}, intent_ids: operations.map(x => x.id), operations}},
    active: {version: 1, id: 'synthetic-owner'}, intents: {version: 1, operations: operations.map((op, i) => ({id: op.id,
      mutations: i < 3 ? [{synthetic: {key: 'retained'}}] : []}))}, journal: {seq: 0, incompleteTailBytes: 0}};
}

function socket(index) {
  const ws = new EventEmitter(), closed = gate(); ws.isOpen = true;
  const s = {ws, user: {id: `96170000${100 + index}:1@s.whatsapp.net`}, ends: 0, sends: 0,
    bridgeTransportClosed: closed.promise, bridgePrepareRetirement() {},
    ev: {process(handler) {s.handler = handler; return () => {}; }},
    end() {s.ends++; ws.isOpen = false; closed.resolve(); ws.emit('close');},
    sendMessage: async () => {s.sends++; throw new Error('Unexpected mock send');}};
  return s;
}

async function fixture(t, change = () => {}) {
  const dataDir = await fs.mkdtemp(path.join(os.tmpdir(), 'fenced-identity-mock-'));
  const release = await acquireDataDirectory(dataDir), sockets = [];
  const config = {dataDir, secret: 'isolated-mock-secret-longer-than-32-bytes',
    lines: HEALTHY.map(line_id => ({line_id, expected_phone: ''})), callbackUrl: 'http://127.0.0.1/unused',
    ackTimeoutMs: 30, storage: {maintenance: {enabled: false}}};
  const deps = {assertExclusiveOwnership: release.assertHeld, storageFilesystem: disk, log() {}, onError() {},
    makeSocket: () => {const s = socket(sockets.length); sockets.push(s); return s;}, fetch: async () => {throw new Error('Unexpected callback');}};
  const seed = await new Bridge(config, deps).initialize({autoConnect: false});
  for (const line of seed.lines.values()) await line.store.update(data => {data.enabled = true;});
  await seed.close();
  const directory = path.join(dataDir, ID); await fs.mkdir(directory, {mode: 0o700});
  const e = evidence(); change(e);
  await write(path.join(directory, 'auth.json'), {creds: {registered: true}, keys: {}});
  await write(path.join(directory, 'auth.json.auth-active'), e.active);
  await write(path.join(directory, 'auth.json.auth-intent'), e.intents);
  await write(path.join(directory, 'auth.json.auth-failure'), e.failure);
  await write(path.join(directory, 'state.json'), {enabled: true, bound_phone: '96170000999', messages: {
    protected: {pending_send: true, delivery_uncertain: true, client_message_id: 'unchanged-client-id'}}, operations: {}, media: {}, send_requests: {}});
  config.lines.push({line_id: ID, expected_phone: ''});
  const before = await hashes(directory);
  const bridge = await new Bridge(config, deps).initialize();
  for (const [i, id] of HEALTHY.entries()) await bridge.connectionUpdate(bridge.getLine(id), sockets[i], {connection: 'open'});
  t.after(async () => {
    // Only disposable mocks: remove the deliberately invalid gate from teardown,
    // never teach the product to ignore it. Clean the six real mock owners.
    bridge.lines.delete(ID); bridge.storageMonitor.unregister(ID);
    bridge.outbox.failed = false;
    if (!bridge.closed) await bridge.close();
    await release(); await fs.rm(dataDir, {recursive: true, force: true});
  });
  return {bridge, directory, before, sockets, config, deps, release, dataDir};
}

test('477 accounted historical intents with three mutations permit retention, never recovery', () => {
  const result = assessAuthRetention(evidence());
  assert.equal(result.isolation_safe, true); assert.equal(result.historical_intents, 477);
  assert.equal(result.recovery_allowed, false);
  assert.equal(JSON.stringify(result).includes('synthetic-not-production'), false);
});

for (const [name, change] of [
  ['legacy final evidence', e => {e.failure.version = 1;}],
  ['open transport', e => {e.failure.recovery.transport_state = 'open';}],
  ['unreleased events', e => {e.failure.recovery.events_released = false;}],
  ['executing work', e => {e.failure.recovery.pending_operations = 1;}],
  ['missing accounting', e => {e.failure.recovery.operations.pop();}],
  ['unresolved outcome', e => {e.failure.recovery.operations[0].provider_outcome = 'unresolved';}],
  ['failed fsync', e => {e.failure.recovery.evidence_write_failed = true;}],
  ['incomplete journal', e => {e.journal.incompleteTailBytes = 1;}],
  ['wrong owner', e => {e.active.id = 'other-owner';}],
]) test(`${name} prevents retirement`, () => {
  const e = evidence(); change(e); assert.equal(assessAuthRetention(e).isolation_safe, false);
});

test('six healthy identities drain and restart; fenced state, callbacks and uncertain messages remain byte-identical', async t => {
  const {bridge, directory, before, sockets, config, deps} = await fixture(t);
  assert.equal(sockets.length, 6, 'no Beirut transport created');
  assert.equal(bridge.runtimeHealth().ready, true);
  assert.equal(bridge.runtimeHealth().eligible_lines, 6);
  assert.equal(bridge.storageMonitor.status().level, 'critical', 'raw auth failure stays visible');
  assert.equal(bridge.status().lines.find(line => line.line_id === ID).lifecycle, 'fenced_retained');
  await bridge.outbox.update(data => data.entries.push({event: {line_id: ID, event_id: 'pending-callback'}, next_try: 0}));
  await bridge.deliverCallbacks(); await bridge.recoverPending();
  assert.equal(bridge.outbox.data.entries.length, 1);
  for (const action of [() => bridge.connect(ID), () => bridge.disconnect(ID), () => bridge.removeLine(ID),
    () => bridge.uploadMedia(ID, {})]) await assert.rejects(action, {code: 'AUTH_RECOVERY_REQUIRED'});
  await assert.rejects(bridge.send(ID, {client_message_id: 'unchanged-client-id', to: '96170123456', type: 'text', text: {body: 'synthetic'}}), {code: 'AUTH_RECOVERY_REQUIRED'});
  await Promise.all([bridge.prepareShutdown(), bridge.prepareShutdown()]);
  await bridge.close();
  assert.ok(sockets.every(s => s.ends === 1 && s.sends === 0));
  assert.deepEqual(await hashes(directory), before);
  const resumed = await new Bridge(config, deps).initialize();
  try {
    assert.equal(sockets.length, 12); assert.equal(resumed.getLine(ID).fencedAuth.isolation_safe, true);
    assert.equal(resumed.outbox.data.entries.length, 1);
    assert.equal(resumed.getLine(ID).store.data.messages.protected.pending_send, true);
    assert.equal(resumed.getLine(ID).store.data.messages.protected.delivery_uncertain, true);
    assert.equal(resumed.runtimeHealth().ready, true);
  } finally {await resumed.close();}
  assert.deepEqual(await hashes(directory), before);
});

test('legacy Beirut evidence blocks activation and shutdown without modifying journals', async t => {
  const {bridge, sockets, directory, before} = await fixture(t, e => {e.failure.version = 1; delete e.failure.recovery;});
  assert.equal(bridge.runtimeHealth().ready, false);
  assert.equal(bridge.getLine(ID).fencedAuth.state, 'fenced_unresolved');
  assert.equal(sockets.length, 0, 'unknown retirement evidence cannot authorize any new provider owner');
  await assert.rejects(bridge.connect(HEALTHY[0]), {code: 'AUTH_ISOLATION_UNPROVEN'});
  await assert.rejects(bridge.close(), /AUTH_ISOLATION_UNPROVEN/);
  assert.equal(bridge.stopping, false);
  assert.ok(sockets.every(s => s.ends === 0 && s.ws.isOpen));
  assert.deepEqual(await hashes(directory), before);
});

test('changed evidence and shared storage failures remain hard gates', async t => {
  const {bridge, sockets, directory} = await fixture(t);
  bridge.outbox.failed = true;
  assert.equal(bridge.runtimeHealth().ready, false);
  await assert.rejects(bridge.prepareShutdown(), /UNISOLATED_STORE_FAILURE/);
  bridge.outbox.failed = false;
  await fs.appendFile(path.join(directory, 'auth.json.auth-intent'), '\n');
  await assert.rejects(bridge.close(), /AUTH_EVIDENCE_CHANGED/);
  assert.equal(bridge.runtimeHealth().ready, false);
  assert.ok(sockets.every(s => s.ends === 0));
});

test('ownership must be held; no boolean configuration or historical record substitutes for it', async t => {
  const {directory, dataDir} = await fixture(t);
  await assert.rejects(inspectAuthRetention(directory, () => {throw new Error('DIRECTORY_OWNERSHIP_UNPROVEN');}), /DIRECTORY_OWNERSHIP_UNPROVEN/);
  await assert.rejects(acquireDataDirectory(dataDir), {code: 'ELOCKED'});
});

test('read-only startup never repairs a partial fenced journal or removes checkpoint evidence', async t => {
  const {directory, bridge, before, config, deps} = await fixture(t);
  await bridge.close();
  await fs.writeFile(path.join(directory, 'auth.json.journal'), '{incomplete', {mode: 0o600});
  await fs.writeFile(path.join(directory, 'auth.json.checkpoint-11111111-1111-1111-1111-111111111111.snapshot.tmp'), 'evidence', {mode: 0o600});
  const after = await hashes(directory);
  const resumed = await new Bridge(config, deps).initialize();
  try {
    assert.equal(resumed.runtimeHealth().ready, false);
    assert.deepEqual(await hashes(directory), after);
    assert.equal(after['auth.json'], before['auth.json']);
    await assert.rejects(resumed.prepareShutdown());
  } finally {resumed.lines.delete(ID); resumed.storageMonitor.unregister(ID); await resumed.close();}
});

test('live stalled work cannot qualify from files; settled changed credentials may only be sealed offline', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'live-auth-retention-mock-'));
  const release = await acquireDataDirectory(directory);
  const state = await new JournalStore(path.join(directory, 'state.json'), () => ({messages: {}, media: {}, operations: {}})).load();
  await state.close();
  const auth = await createAuth(directory); await auth.saveCreds(); await auth.armSession();
  const closed = gate(); auth.trackRecoveryTransport(closed.promise, 1);
  const entered = gate(), finish = gate();
  const pending = auth.trackProviderTask(async () => {entered.resolve(); await finish.promise; auth.state.creds.nextPreKeyId += 3;});
  const settled = Promise.allSettled([pending]); await entered.promise;
  auth.retire(); await assert.rejects(auth.drain({timeoutMs: 5}), /AUTH_TRANSACTION_DRAIN_TIMEOUT/);
  closed.resolve(); auth.recoveryEventsReleased(); await auth.assessRecovery();
  try {
    assert.equal((await retainAuth(directory, {assertOwnership: release.assertHeld, auth, stateStore: state})).isolation_safe, false);
    finish.resolve(); await settled; await auth.assessRecovery();
    const before = await hashes(directory);
    const retained = await retainAuth(directory, {assertOwnership: release.assertHeld, auth, stateStore: state});
    assert.equal(retained.isolation_safe, true);
    await retained.close();
    await assert.rejects(auth.saveCreds(), /AUTH_SESSION_RETIRED/);
    await assert.rejects(auth.renew(), /AUTH_SESSION_RETIRED/);
    await assert.rejects(auth.drain(), /AUTH_RETAINED_OFFLINE/);
    await assert.rejects(createAuth(directory), /AUTH_RECOVERY_REQUIRED/);
    assert.deepEqual(await hashes(directory), before, 'including all markers and journals');
    const saved = decode(await fs.readFile(path.join(directory, 'auth.json.auth-failure'), 'utf8'));
    assert.notEqual(saved.recovery.credentials.nextPreKeyId, auth.store.data.creds.nextPreKeyId);
    assert.equal(retained.recovery_allowed, false);
  } finally {finish.resolve(); await settled; await release(); await fs.rm(directory, {recursive: true, force: true});}
});

test('runtime aborts failed signal preflight without closing listener, healthy identities, or releasing ownership', async () => {
  const processOwner = new EventEmitter(), messages = []; let safe = false, ends = 0, releases = 0;
  const server = new EventEmitter(); let closes = 0;
  server.listen = () => queueMicrotask(() => server.emit('listening'));
  server.close = done => {closes++; done();};
  const unlock = async () => {releases++;}; unlock.assertHeld = () => {};
  const bridge = {initialized: false, async initialize() {this.initialized = true;}, startWorkers() {},
    async prepareShutdown() {if (!safe) throw new Error('AUTH_ISOLATION_UNPROVEN');}, async close() {ends++;}};
  const runtime = createRuntime({process: processOwner, logger: {info() {}, error: m => messages.push(m)},
    prepare: async () => ({config: {}, acquireDataDirectory: async () => unlock, createBridge: () => bridge, createServer: () => server})});
  await runtime.start();
  const a = runtime.stop(), b = runtime.stop(); assert.equal(a, b);
  assert.deepEqual(await a, {aborted: true});
  assert.equal(runtime.stopping, false); assert.equal(ends, 0); assert.equal(closes, 0); assert.equal(releases, 0);
  assert.ok(messages.some(m => m.includes('shutdown_preflight_blocked')));
  safe = true; assert.deepEqual(await runtime.stop(), {exitCode: 0}); assert.equal(releases, 1);
});

test('live failed owner is isolated only after its provider work settles; no failed-store flush or close is called', async t => {
  const {bridge, sockets, dataDir} = await fixture(t);
  const line = bridge.getLine(HEALTHY[0]), auth = line.auth;
  const entered = gate(), finish = gate();
  const work = auth.trackProviderTask(async () => {
    await auth.state.keys.set({session: {synthetic: Buffer.from('uncertain-mock-key')}});
    entered.resolve(); await finish.promise;
    throw new Error('Synthetic failure after key mutation');
  });
  const settled = Promise.allSettled([work]); await entered.promise;
  auth.retire(); await assert.rejects(auth.drain({timeoutMs: 5}), /AUTH_TRANSACTION_DRAIN_TIMEOUT/);
  await assert.rejects(bridge.prepareShutdown(), /LIVE_OWNER_OR_WORK_UNCONFIRMED/);
  assert.ok(sockets.slice(1).every(s => s.ends === 0));
  finish.resolve(); await settled; await auth.assessRecovery();
  const directory = path.join(dataDir, HEALTHY[0]), before = await hashes(directory);
  const flush = t.mock.method(auth.store, 'flush', () => {throw new Error('Failed store must not flush');});
  const close = t.mock.method(auth.store, 'close', () => {throw new Error('Failed store must not close normally');});
  await Promise.all([bridge.prepareShutdown(), bridge.prepareShutdown()]);
  assert.equal(line.fencedAuth.isolation_safe, true);
  assert.equal(bridge.runtimeHealth().eligible_lines, 5);
  await bridge.close();
  assert.equal(flush.mock.callCount(), 0); assert.equal(close.mock.callCount(), 0);
  assert.deepEqual(await hashes(directory), before);
});

test('shutdown waits for healthy physical transport release and rejects missing release evidence', async t => {
  const {bridge, sockets} = await fixture(t);
  const line = bridge.getLine(HEALTHY[0]), original = line.transportClosed;
  line.transportClosed = undefined;
  await assert.rejects(bridge.prepareShutdown(), /TRANSPORT_RELEASE_UNCONFIRMED/);
  assert.ok(sockets.every(s => s.ends === 0));
  const released = gate(); line.transportClosed = released.promise;
  let finished = false;
  const closing = bridge.close().then(() => {finished = true;});
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(finished, false, 'auth drain alone cannot release directory ownership');
  released.resolve(); await closing; line.transportClosed = original;
});

test('a second identity failing during asynchronous preflight aborts before healthy admission or transports close', async t => {
  const {bridge, sockets} = await fixture(t);
  const preflight = bridge.prepareShutdown.bind(bridge), line = bridge.getLine(HEALTHY[0]);
  bridge.prepareShutdown = async () => {await preflight(); line.auth.store.failed = true;};
  try {
    await assert.rejects(bridge.beginShutdown(), /AUTH_ISOLATION_UNPROVEN/);
    assert.equal(bridge.stopping, false);
    assert.ok(sockets.every(s => s.ends === 0));
  } finally {line.auth.store.failed = false; bridge.prepareShutdown = preflight;}
});

test('authenticated preflight endpoint never starts a signal or a transport', async t => {
  const {bridge, sockets, config} = await fixture(t);
  const server = createServer(bridge, config);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => {server.close(resolve); server.closeAllConnections();}));
  const url = `http://127.0.0.1:${server.address().port}/v1/shutdown-preflight`;
  assert.equal((await fetch(url, {method: 'POST'})).status, 401);
  const response = await fetch(url, {method: 'POST', headers: {Authorization: `Bearer ${config.secret}`}});
  assert.equal(response.status, 200); assert.equal((await response.json()).shutdown_safe, true);
  assert.ok(sockets.every(s => s.ends === 0)); assert.equal(bridge.stopping, false);
  bridge.outbox.failed = true;
  const blocked = await fetch(url, {method: 'POST', headers: {Authorization: `Bearer ${config.secret}`}});
  assert.equal(blocked.status, 503); assert.equal((await blocked.json()).code, 'UNISOLATED_STORE_FAILURE');
  assert.ok(sockets.every(s => s.ends === 0)); bridge.outbox.failed = false;
});

test('offline retained validation is explicit; exports and compaction still refuse every auth fence', async t => {
  const {bridge, directory, before, dataDir} = await fixture(t);
  await bridge.close();
  const copy = await fs.mkdtemp(path.join(os.tmpdir(), 'retained-offline-mock-'));
  t.after(() => fs.rm(copy, {recursive: true, force: true}));
  await fs.cp(dataDir, copy, {recursive: true});
  const result = await storageTool('validate-retained', copy);
  assert.equal(result.stores, 15); assert.deepEqual(result.eligible_lines, HEALTHY);
  assert.deepEqual(result.retained_lines, [ID]); assert.equal(result.recovery_allowed, false);
  for (const command of ['validate', 'compact', 'import', 'export']) {
    await assert.rejects(storageTool(command, copy, path.join(copy, '..', 'unused-export-' + crypto.randomUUID())), /AUTH_RECOVERY_REQUIRED/);
  }
  assert.deepEqual(await hashes(path.join(copy, ID)), before);
  assert.deepEqual(await hashes(directory), before);
  const filename = path.join(copy, ID, 'auth.json.auth-failure');
  const failure = decode(await fs.readFile(filename, 'utf8')); delete failure.recovery; failure.version = 1;
  await write(filename, failure);
  await assert.rejects(storageTool('validate-retained', copy), /AUTH_ISOLATION_UNPROVEN/);
});
