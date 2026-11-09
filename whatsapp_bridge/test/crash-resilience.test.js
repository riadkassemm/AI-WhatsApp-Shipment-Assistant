import test from 'node:test';
import assert from 'node:assert/strict';
import { appendFile, cp, mkdtemp, readdir, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { performance } from 'node:perf_hooks';
import { Bridge } from '../src/bridge.js';
import { readAuthConfig } from '../src/config.js';
import { DEFAULT_AUTH_DRAIN_TIMEOUT_MS } from '../src/storage.js';

const SECRET = 'a-test-secret-that-is-long-enough-123456';
const LINES = ['90001', '90002'];
const healthyStorage = async () => ({ bsize: 4096, blocks: 10_000_000, bavail: 5_000_000, files: 1_000_000, ffree: 900_000 });

let phones = 0;
function mockSocket() {
  const ws = new EventEmitter();
  ws.isOpen = true;
  // Each line is a distinct phone: the bridge refuses one phone on two lines.
  const socket = {
    ws, user: { id: `9617011${String(++phones).padStart(4, '0')}:1@s.whatsapp.net` },
    ev: { process: handler => { socket.eventHandler = handler; return () => {}; } },
    end: () => {}, logout: async () => {},
    query: async () => ({ tag: 'iq', attrs: { type: 'result' } }),
  };
  return socket;
}

const exists = file => stat(file).then(() => true, error => { if (error.code === 'ENOENT') return false; throw error; });

async function start(t, dataDir, { auth, logs = [] } = {}) {
  const config = { secret: SECRET, lines: LINES.map(line_id => ({ line_id, expected_phone: '' })), dataDir,
    callbackUrl: 'http://127.0.0.1/callback', maxMediaBytes: 1024 * 1024, ackTimeoutMs: 30, ...(auth ? { auth } : {}) };
  const bridge = await new Bridge(config, { makeSocket: () => mockSocket(), onError: () => {}, log: entry => logs.push(entry),
    storageFilesystem: healthyStorage }).initialize({ autoConnect: false });
  t.after(async () => { if (!bridge.closed) await bridge.close().catch(() => {}); });
  return { bridge, logs };
}

async function connectAll(bridge) {
  for (const id of LINES) {
    await bridge.connect(id);
    const line = bridge.getLine(id);
    await bridge.connectionUpdate(line, line.socket, { connection: 'open' });
  }
}

async function tempDir(t, prefix) {
  const directory = await mkdtemp(path.join(os.tmpdir(), prefix));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return directory;
}

// Copy the live data directory: exactly what a SIGKILL or power loss leaves.
async function crashSnapshot(t, bridge, dataDir) {
  for (const id of LINES) await bridge.getLine(id).auth.store.flush();
  const crashed = await tempDir(t, 'bridge-crashed-');
  await cp(dataDir, crashed, { recursive: true });
  return crashed;
}

test('auth drain deadline outlasts provider query timeouts and is configurable but never short', () => {
  assert.ok(DEFAULT_AUTH_DRAIN_TIMEOUT_MS >= 60_000);
  assert.deepEqual(readAuthConfig({}), { drainTimeoutMs: undefined, reconnectDrainTimeoutMs: undefined, resumeAfterUncleanExit: true });
  assert.equal(readAuthConfig({ WHATSAPP_LINKED_DEVICE_AUTH_RECONNECT_DRAIN_TIMEOUT_MS: '20000' }).reconnectDrainTimeoutMs, 20_000);
  assert.throws(() => readAuthConfig({ WHATSAPP_LINKED_DEVICE_AUTH_RECONNECT_DRAIN_TIMEOUT_MS: '1000' }), /at least 5000/);
  assert.equal(readAuthConfig({ WHATSAPP_LINKED_DEVICE_AUTH_DRAIN_TIMEOUT_MS: '90000' }).drainTimeoutMs, 90_000);
  assert.throws(() => readAuthConfig({ WHATSAPP_LINKED_DEVICE_AUTH_DRAIN_TIMEOUT_MS: '20000' }), /at least 30000/);
  assert.equal(readAuthConfig({ WHATSAPP_LINKED_DEVICE_AUTH_RESUME_AFTER_UNCLEAN_EXIT: 'false' }).resumeAfterUncleanExit, false);
  assert.throws(() => readAuthConfig({ WHATSAPP_LINKED_DEVICE_AUTH_RESUME_AFTER_UNCLEAN_EXIT: 'yes' }), /true or false/);
});

test('after an unclean exit, every line whose only marker is auth-active resumes its saved session without re-pairing', async t => {
  const dataDir = await tempDir(t, 'bridge-live-');
  const { bridge } = await start(t, dataDir);
  await connectAll(bridge);
  for (const [index, id] of LINES.entries()) {
    await bridge.getLine(id).auth.state.keys.set({ session: { peer: Buffer.from([index, 1, 2]) } });
    assert.equal(await exists(path.join(dataDir, id, 'auth.json.auth-active')), true);
  }
  const crashed = await crashSnapshot(t, bridge, dataDir);
  const { bridge: restarted, logs } = await start(t, crashed);
  for (const [index, id] of LINES.entries()) {
    const line = restarted.getLine(id);
    assert.notEqual(line.status, 'error', line.error);
    assert.deepEqual(line.auth.store.data.keys.session.peer, Buffer.from([index, 1, 2]));
    assert.equal(await exists(path.join(crashed, id, 'auth.json.auth-active')), false);
    const retained = (await readdir(path.join(crashed, id))).filter(name => name.startsWith('auth.json.auth-active.resumed-'));
    assert.equal(retained.length, 1, 'the marker is kept as evidence, not deleted');
  }
  assert.deepEqual(logs.filter(entry => entry.event === 'auth_session_resumed_after_unclean_exit').map(entry => entry.line_id).sort(), LINES);
  await restarted.connect(LINES[0]);
  assert.equal(await exists(path.join(crashed, LINES[0], 'auth.json.auth-active')), true, 'the new session arms a fresh marker');
});

const listing = async (directory, id) => readdir(path.join(directory, id));

test('a change in flight at the crash is discarded and the line resumes from its committed state', async t => {
  const dataDir = await tempDir(t, 'bridge-live-');
  const { bridge } = await start(t, dataDir);
  await connectAll(bridge);
  await bridge.getLine(LINES[1]).auth.state.keys.set({ session: { committed: Buffer.from([1]) } });
  const crashed = await crashSnapshot(t, bridge, dataDir);
  await writeFile(path.join(crashed, LINES[1], 'auth.json.auth-intent'), '{"version":1,"operations":[]}', { mode: 0o600 });
  const { bridge: restarted, logs } = await start(t, crashed);
  const line = restarted.getLine(LINES[1]);
  assert.notEqual(line.status, 'error', line.error);
  assert.deepEqual(line.auth.store.data.keys.session.committed, Buffer.from([1]));
  const names = await listing(crashed, LINES[1]);
  assert.ok(!names.includes('auth.json.auth-intent'));
  assert.equal(names.filter(name => name.startsWith('auth.json.auth-intent.abandoned-')).length, 1, 'kept as evidence');
  assert.ok(logs.some(entry => entry.event === 'auth_session_resumed_after_unclean_exit' && entry.line_id === LINES[1] &&
    entry.reason === 'in_flight_change_discarded'));
});

test('a journal write torn by the crash is quarantined and the line resumes from the last complete write', async t => {
  const dataDir = await tempDir(t, 'bridge-live-');
  const { bridge } = await start(t, dataDir);
  await connectAll(bridge);
  await bridge.getLine(LINES[1]).auth.state.keys.set({ session: { committed: Buffer.from([1]) } });
  const crashed = await crashSnapshot(t, bridge, dataDir);
  await appendFile(path.join(crashed, LINES[1], 'auth.json.journal'), '{"record":"partial');
  const { bridge: restarted, logs } = await start(t, crashed);
  const line = restarted.getLine(LINES[1]);
  assert.notEqual(line.status, 'error', line.error);
  assert.deepEqual(line.auth.store.data.keys.session.committed, Buffer.from([1]));
  assert.ok((await listing(crashed, LINES[1])).some(name => name.startsWith('auth.json.journal.tail-')), 'torn bytes kept as evidence');
  assert.ok(logs.some(entry => entry.event === 'auth_session_resumed_after_unclean_exit' && entry.line_id === LINES[1] &&
    entry.reason === 'journal_tail_quarantined'));
});

test('a failure that began with a drain timeout resumes; a real write failure keeps only that line fenced', async t => {
  const dataDir = await tempDir(t, 'bridge-live-');
  const { bridge } = await start(t, dataDir);
  await connectAll(bridge);
  const crashed = await crashSnapshot(t, bridge, dataDir);
  const evidence = operation => JSON.stringify({ version: 1, failures: [{ operation, mutations: [] }, { operation: 'nested_transaction' }] });
  await writeFile(path.join(crashed, LINES[0], 'auth.json.auth-failure'), evidence('drain_timeout'), { mode: 0o600 });
  await writeFile(path.join(crashed, LINES[1], 'auth.json.auth-failure'), evidence('keys'), { mode: 0o600 });
  const { bridge: restarted, logs } = await start(t, crashed);
  assert.notEqual(restarted.getLine(LINES[0]).status, 'error');
  assert.ok((await listing(crashed, LINES[0])).some(name => name.startsWith('auth.json.auth-failure.abandoned-')));
  assert.equal(restarted.getLine(LINES[1]).status, 'error');
  assert.ok((await listing(crashed, LINES[1])).includes('auth.json.auth-failure'), 'a real write failure keeps its fence');
  assert.deepEqual(logs.filter(entry => entry.event === 'auth_session_fenced_after_unclean_exit')
    .map(({ line_id, reason }) => ({ line_id, reason })), [{ line_id: LINES[1], reason: 'auth_failure_present' }]);
});

test('operators can keep the strict fence after an unclean exit', async t => {
  const dataDir = await tempDir(t, 'bridge-live-');
  const { bridge } = await start(t, dataDir);
  await connectAll(bridge);
  const crashed = await crashSnapshot(t, bridge, dataDir);
  const { bridge: restarted } = await start(t, crashed, { auth: { resumeAfterUncleanExit: false } });
  for (const id of LINES) assert.equal(restarted.getLine(id).status, 'error');
});

test('a failed auth store stays isolated: other lines keep committing and a graceful stop retires their sessions', async t => {
  const dataDir = await tempDir(t, 'bridge-live-');
  const { bridge } = await start(t, dataDir);
  await connectAll(bridge);
  const [healthyId, failedId] = LINES;
  const healthy = bridge.getLine(healthyId), failed = bridge.getLine(failedId);
  const failedAuth = failed.auth;
  failedAuth.store.maxPending = 0;
  await assert.rejects(failedAuth.state.keys.set({ session: { first: Buffer.from([1]) } }), error => error.bridgePersistenceFailure);
  assert.equal(failed.status, 'error');
  // An admitted provider backlog keeps writing into the failed store.
  const started = performance.now();
  await Promise.all(Array.from({ length: 300 }, (_, index) =>
    failedAuth.state.keys.set({ session: { ['peer' + (index % 25)]: Buffer.alloc(20_000, index % 251) } }).catch(() => {})));
  await healthy.auth.state.keys.set({ session: { still_working: Buffer.from([7]) } });
  assert.ok(performance.now() - started < 3_000, 'the failed line must not starve the healthy one');
  assert.deepEqual(healthy.auth.store.data.keys.session.still_working, Buffer.from([7]));
  await bridge.close().catch(() => {});
  assert.equal(await exists(path.join(dataDir, healthyId, 'auth.json.auth-active')), false, 'graceful stop retired the healthy session');
  assert.equal(await exists(path.join(dataDir, failedId, 'auth.json.auth-failure')), true, 'the failed line keeps its own evidence');
  const { bridge: restarted } = await start(t, dataDir);
  assert.notEqual(restarted.getLine(healthyId).status, 'error');
  assert.equal(restarted.getLine(failedId).status, 'error');
});

test('text sends give up after 30 s, media keeps 120 s, and test overrides still shorten both', async t => {
  const dataDir = await tempDir(t, 'bridge-live-');
  const { bridge } = await start(t, dataDir);
  assert.equal(bridge.submissionTimeout(false), 30_000);
  assert.equal(bridge.submissionTimeout(true), 120_000);
  bridge.config.submissionTimeoutMs = 100;
  assert.equal(bridge.submissionTimeout(false), 100);
  assert.equal(bridge.submissionTimeout(true), 100);
});

test('after a dead transport, the reconnect abandons stuck work at a short deadline instead of the full one', async t => {
  const dataDir = await tempDir(t, 'bridge-live-');
  const { bridge, logs } = await start(t, dataDir, { auth: { reconnectDrainTimeoutMs: 100 } });
  await connectAll(bridge);
  const line = bridge.getLine(LINES[0]);
  const auth = line.auth, never = new Promise(() => {});
  // Work stuck on the dead socket while holding an uncommitted change.
  const stuck = auth.trackProviderTask(async () => {
    await auth.state.keys.set({ session: { in_flight: Buffer.from([1]) } });
    await never;
  });
  stuck.catch(() => {});
  await new Promise(resolve => setTimeout(resolve, 50));
  bridge.restartStaleSocket(line, line.socket);
  assert.equal(line.transportLost, true);
  const started = performance.now();
  await bridge.connect(LINES[0]);
  assert.ok(performance.now() - started < 3_000, 'reconnect did not wait the full 60 s deadline');
  assert.notEqual(line.auth, auth, 'a new auth owner took over');
  assert.equal(line.auth.store.failed, false);
  assert.equal(line.transportLost, false);
  assert.ok(logs.some(entry => entry.event === 'auth_work_abandoned' && entry.line_id === LINES[0] && entry.reason === 'drain_timeout'));
  await line.auth.state.keys.set({ session: { after: Buffer.from([2]) } });
  assert.deepEqual(line.auth.store.data.keys.session.after, Buffer.from([2]));
});

async function blockedLine(t) {
  const dataDir = await tempDir(t, 'bridge-live-');
  const { bridge } = await start(t, dataDir);
  await connectAll(bridge);
  await bridge.getLine(LINES[1]).store.update(data => { data.messages.KEEP = { phone: '96170000000', text: 'kept', timestamp: Date.now(), notified: true, message: { key: { id: 'KEEP' } } }; });
  const crashed = await crashSnapshot(t, bridge, dataDir);
  // A real write failure: the only marker that still blocks a number.
  await writeFile(path.join(crashed, LINES[1], 'auth.json.auth-failure'), JSON.stringify({ version: 1, failures: [{ operation: 'keys', mutations: [] }] }), { mode: 0o600 });
  const { bridge: restarted, logs } = await start(t, crashed);
  assert.equal(restarted.getLine(LINES[1]).status, 'error');
  return { crashed, bridge: restarted, logs };
}
const archived = async (crashed, pattern) => (await readdir(path.join(path.dirname(crashed), 'linked-device-archive')).catch(() => []))
  .filter(name => name.includes(pattern));

test('admin reset of a blocked number archives only its session and leaves it unpaired with its history', async t => {
  const { crashed, bridge, logs } = await blockedLine(t);
  t.after(() => rm(path.join(path.dirname(crashed), 'linked-device-archive'), { recursive: true, force: true }));
  const status = await bridge.resetSession(LINES[1]);
  assert.equal(status.is_paired, false);
  assert.equal(status.error, undefined);
  const line = bridge.getLine(LINES[1]);
  assert.equal(line.store.data.messages.KEEP.text, 'kept', 'the conversation state stays');
  const names = await readdir(path.join(crashed, LINES[1]));
  assert.equal(names.some(name => name.startsWith('auth.json.auth-')), false, 'no markers remain');
  const [archive] = await archived(crashed, `-reset-${LINES[1]}`);
  assert.ok(archive, 'the old session moved to the archive');
  assert.ok((await readdir(path.join(path.dirname(crashed), 'linked-device-archive', archive))).includes('auth.json.auth-failure'));
  assert.ok(logs.some(entry => entry.event === 'auth_session_reset' && entry.line_id === LINES[1]));
  await line.auth.state.keys.set({ session: { fresh: Buffer.from([1]) } });
  assert.equal(line.auth.store.failed, false, 'the fresh session is writable');
});

test('admin force-remove archives a number that cannot be unlinked and tombstones it durably', async t => {
  const { crashed, bridge, logs } = await blockedLine(t);
  t.after(() => rm(path.join(path.dirname(crashed), 'linked-device-archive'), { recursive: true, force: true }));
  const result = await bridge.forceRemoveLine(LINES[1]);
  assert.deepEqual(result, { line_id: LINES[1], removed: true, forced: true });
  assert.equal(await exists(path.join(crashed, LINES[1])), false);
  const [archive] = await archived(crashed, `-remove-${LINES[1]}`);
  assert.ok((await readdir(path.join(path.dirname(crashed), 'linked-device-archive', archive))).includes('state.json'), 'the whole folder is kept');
  assert.throws(() => bridge.getLine(LINES[1]), { code: 'UNKNOWN_LINE' });
  assert.deepEqual(await bridge.removeLine(LINES[1]), { line_id: LINES[1], removed: true, unlinked: true }, 'the normal removal then completes');
  assert.ok(logs.some(entry => entry.event === 'line_force_removed'));
  await bridge.close();
  const { bridge: restarted } = await start(t, crashed);
  assert.throws(() => restarted.getLine(LINES[1]), { code: 'UNKNOWN_LINE' }, 'stays removed after a restart');
  assert.equal(restarted.getLine(LINES[0]).store.failed, false);
});

test('admin reconnect replaces only that number connection', async t => {
  const dataDir = await tempDir(t, 'bridge-live-');
  const { bridge } = await start(t, dataDir);
  await connectAll(bridge);
  const [first, second] = LINES.map(id => bridge.getLine(id).socket);
  const status = await bridge.reconnectLine(LINES[0]);
  assert.notEqual(bridge.getLine(LINES[0]).socket, first, 'a new connection was opened');
  assert.equal(status.status, 'connecting');
  assert.equal(bridge.getLine(LINES[1]).socket, second, 'the other number is untouched');
  await assert.rejects(bridge.resetSession(LINES[1]), { code: 'LINE_CONNECTED' }, 'a connected number is never reset');
});
