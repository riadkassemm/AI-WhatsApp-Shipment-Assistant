import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, readdir, writeFile, mkdir, access } from 'node:fs/promises';
import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JournalStore } from '../src/journal-store.js';
import { splitSharedOutbox, mergeLineOutboxes } from '../src/outbox-migration.js';
import { LineSupervisor, createSupervisorServer } from '../src/supervisor.js';
import { createSupervisorHost } from '../src/worker-ipc.js';
import { readConfig, readLineWorkerConfig, readProcessMode, workerSocketPath } from '../src/config.js';
import { coordinateCheckpoint, setCheckpointGate } from '../src/journal-maintenance.js';
import { Bridge } from '../src/bridge.js';

const SECRET = 'a-test-secret-that-is-long-enough-123456';
const FAKE_WORKER = fileURLToPath(new URL('../fixtures/fake-line-worker.mjs', import.meta.url));
const healthyStorage = async () => ({bsize: 4096, blocks: 10_000_000, bavail: 5_000_000, files: 1_000_000, ffree: 900_000});
const exists = filename => access(filename).then(() => true, () => false);
const entry = (lineId, id) => ({ event: { event_id: id, line_id: lineId, type: 'message' }, attempts: 0, next_try: 0, created_at: 1 });

async function tempDir(t) {
  // Unix socket paths must stay short: use the system temp directory directly.
  const root = await mkdtemp(path.join(os.tmpdir(), 'wa-pl-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const dataDir = path.join(root, 'data');
  await mkdir(dataDir, { recursive: true, mode: 0o700 });
  return { root, dataDir };
}
async function writeOutbox(filename, data) {
  const store = new JournalStore(filename, () => ({ entries: [], dead_letters: [] }), { maintenance: { enabled: false } });
  await store.load();
  await store.update(draft => { draft.entries.push(...data.entries); draft.dead_letters.push(...(data.dead_letters ?? [])); });
  await store.close();
}
async function readOutbox(filename) {
  const store = new JournalStore(filename, () => ({ entries: [], dead_letters: [] }), { maintenance: { enabled: false } });
  await store.load();
  const data = JSON.parse(JSON.stringify(store.data));
  await store.close();
  return data;
}

test('configuration: process mode defaults to single and worker settings are validated', () => {
  assert.equal(readProcessMode({}), 'single');
  assert.equal(readProcessMode({ WHATSAPP_LINKED_DEVICE_PROCESS_MODE: 'per_line' }), 'per_line');
  assert.throws(() => readProcessMode({ WHATSAPP_LINKED_DEVICE_PROCESS_MODE: 'cluster' }), /single or per_line/);
  const config = readConfig({ WHATSAPP_LINKED_DEVICE_SECRET: SECRET });
  assert.equal(config.processMode, 'single');
  assert.deepEqual(config.workers, { stopTimeoutMs: 80_000, startTimeoutMs: 120_000, statusTimeoutMs: 2_000 });
  assert.throws(() => readConfig({ WHATSAPP_LINKED_DEVICE_SECRET: SECRET, WHATSAPP_LINKED_DEVICE_WORKER_STOP_TIMEOUT_MS: '10' }), /at least 5000/);
  const worker = readLineWorkerConfig({ WHATSAPP_LINKED_DEVICE_SECRET: SECRET, WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY: '/var/lib/x',
    WHATSAPP_LINKED_DEVICE_WORKER_LINE_ID: '90047', WHATSAPP_LINKED_DEVICE_WORKER_EXPECTED_PHONE: '+961 70 123456' });
  assert.deepEqual(worker.lines, [{ line_id: '90047', expected_phone: '96170123456' }]);
  assert.equal(worker.socketPath, '/var/lib/x/.workers/90047.sock');
  assert.equal(worker.worker.lockPath, '/var/lib/x/.workers/90047');
  assert.throws(() => readLineWorkerConfig({ WHATSAPP_LINKED_DEVICE_SECRET: SECRET, WHATSAPP_LINKED_DEVICE_WORKER_LINE_ID: '../x' }), /numeric line ID/);
  assert.throws(() => workerSocketPath('/' + 'x'.repeat(90), '90047'), /too long/);
});

test('outbox split moves each number\'s callbacks to its folder and archives the shared store', async t => {
  const { root, dataDir } = await tempDir(t);
  await writeOutbox(path.join(dataDir, 'outbox.json'), {
    entries: [entry('90001', 'a'), entry('90002', 'b'), entry('90001', 'c'), entry('90099', 'gone')],
    dead_letters: [{ ...entry('90002', 'dead'), failed_at: 1, reason: 'x' }],
  });
  // A previous partial run already copied one record: it is not duplicated.
  await mkdir(path.join(dataDir, '90001'), { mode: 0o700 });
  await writeOutbox(path.join(dataDir, '90001', 'outbox.json'), { entries: [entry('90001', 'a')] });
  const logs = [];
  const result = await splitSharedOutbox(dataDir, ['90001', '90002'], { log: item => logs.push(item) });
  assert.deepEqual(result, { moved: 3, orphaned: 1 });
  assert.deepEqual((await readOutbox(path.join(dataDir, '90001', 'outbox.json'))).entries.map(item => item.event.event_id), ['a', 'c']);
  const second = await readOutbox(path.join(dataDir, '90002', 'outbox.json'));
  assert.deepEqual(second.entries.map(item => item.event.event_id), ['b']);
  assert.deepEqual(second.dead_letters.map(item => item.event.event_id), ['dead']);
  assert.deepEqual((await readdir(dataDir)).filter(name => name.startsWith('outbox')), [], 'shared store and marker are gone');
  const [archive] = await readdir(path.join(root, 'linked-device-archive'));
  assert.match(archive, /outbox-split$/);
  assert.ok((await readdir(path.join(root, 'linked-device-archive', archive))).includes('outbox.json'));
  assert.equal(logs[0].event, 'outbox_split_for_per_line_mode');
  assert.deepEqual(await splitSharedOutbox(dataDir, ['90001']), { moved: 0, orphaned: 0 }, 'nothing left to do');
});

test('outbox split resumed after its marker only finishes moving files and never rereads them', async t => {
  const { root, dataDir } = await tempDir(t);
  await writeOutbox(path.join(dataDir, 'outbox.json'), { entries: [entry('90001', 'a')] });
  const archive = path.join(root, 'linked-device-archive', 'resume-outbox-split');
  await writeFile(path.join(dataDir, 'outbox-migration.json'), JSON.stringify({ version: 1, direction: 'split', archive }));
  await splitSharedOutbox(dataDir, ['90001']);
  assert.equal(await exists(path.join(dataDir, '90001', 'outbox.json')), false, 'marker means the copy already happened');
  assert.equal(await exists(path.join(dataDir, 'outbox-migration.json')), false);
  assert.ok((await readdir(archive)).includes('outbox.json'));
});

test('single mode merges per-line outboxes back into the shared one (rollback)', async t => {
  const { dataDir } = await tempDir(t);
  await writeOutbox(path.join(dataDir, 'outbox.json'), { entries: [entry('90001', 'kept')] });
  for (const [id, events] of [['90001', ['kept', 'x']], ['90002', ['y']]]) {
    await mkdir(path.join(dataDir, id), { mode: 0o700 });
    await writeOutbox(path.join(dataDir, id, 'outbox.json'), { entries: events.map(event => entry(id, event)) });
  }
  assert.deepEqual(await mergeLineOutboxes(dataDir), { moved: 2 });
  assert.deepEqual((await readOutbox(path.join(dataDir, 'outbox.json'))).entries.map(item => item.event.event_id).sort(), ['kept', 'x', 'y']);
  for (const id of ['90001', '90002']) assert.deepEqual(await readdir(path.join(dataDir, id)), []);
});

test('checkpoint gate wraps every in-process checkpoint and can be reset', async () => {
  const calls = [];
  setCheckpointGate(async action => { calls.push('acquire'); try { return await action(); } finally { calls.push('release'); } });
  try {
    assert.equal(await coordinateCheckpoint(async () => { calls.push('work'); return 7; }), 7);
    assert.deepEqual(calls, ['acquire', 'work', 'release']);
  } finally { setCheckpointGate(null); }
  assert.equal(await coordinateCheckpoint(async () => 8), 8);
});

test('worker IPC host matches replies, maps errors and fails pending calls when the supervisor leaves', async () => {
  const channel = new EventEmitter();
  channel.connected = true;
  const sent = [];
  channel.send = (message, callback) => { sent.push(message); callback(); };
  const host = createSupervisorHost(channel);
  const first = host.call('retiredPhone', { phone: '1' });
  const second = host.call('tombstone', { line_id: '90001' });
  channel.emit('message', { type: 'rpc_result', id: sent[1].id, error: { status: 403, code: 'FORBIDDEN_LINE', message: 'no' } });
  channel.emit('message', { type: 'rpc_result', id: sent[0].id, result: { offline_since: 5 } });
  assert.deepEqual(await first, { offline_since: 5 });
  await assert.rejects(second, error => error.status === 403 && error.code === 'FORBIDDEN_LINE');
  const pending = host.call('claimPhone', { phone: '2' });
  channel.connected = false;
  channel.emit('disconnect');
  await assert.rejects(pending, error => error.code === 'SUPERVISOR_UNAVAILABLE');
  await assert.rejects(host.call('claimPhone', {}), error => error.code === 'SUPERVISOR_UNAVAILABLE');
});

// ---- Supervisor against real child processes (fake workers, no WhatsApp)
async function supervisorFixture(t, { lines = ['90001', '90002'], workers = {}, manifest } = {}) {
  const { root, dataDir } = await tempDir(t);
  if (manifest) await writeFile(path.join(dataDir, 'lines.json'), JSON.stringify(manifest));
  const config = { secret: SECRET, dataDir, lines: lines.map(id => ({ line_id: id, expected_phone: '' })),
    workers: { stopTimeoutMs: 5_000, startTimeoutMs: 10_000, statusTimeoutMs: 2_000 } };
  const logs = [];
  const supervisor = new LineSupervisor(config, { workerEntry: FAKE_WORKER, restartDelaysMs: [50, 100], claimStartupWaitMs: 2_000,
    log: item => logs.push(item),
    workerEnv: { ...process.env, WHATSAPP_LINKED_DEVICE_SECRET: SECRET, WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY: dataDir,
      FAKE_WORKER_CONFIG: JSON.stringify(workers) } });
  const server = createSupervisorServer(supervisor, config);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    await supervisor.close().catch(() => {});
    await new Promise(resolve => server.close(resolve));
  });
  await supervisor.initialize();
  supervisor.startWorkers();
  const base = `http://127.0.0.1:${server.address().port}`;
  const call = async (method, pathname, body) => {
    const response = await fetch(base + pathname, { method, headers: { authorization: `Bearer ${SECRET}`,
      ...(body === undefined ? {} : { 'content-type': 'application/json' }) }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: response.status, body: await response.json() };
  };
  const ready = async (id, timeoutMs = 10_000) => {
    const deadline = Date.now() + timeoutMs;
    while (supervisor.records.get(id)?.state !== 'ready') {
      if (Date.now() > deadline) throw new Error(`worker ${id} not ready`);
      await new Promise(resolve => setTimeout(resolve, 20));
    }
  };
  return { supervisor, dataDir, root, call, ready, logs, base };
}

test('supervisor routes each number to its own process and aggregates status in the single-process shape', async t => {
  const { call, ready, base } = await supervisorFixture(t, { workers: { '90002': { p99: 9, loopAgeMs: 5000 } } });
  await ready('90001'); await ready('90002');
  const first = await call('POST', '/v1/lines/90001/messages', { client_message_id: 'm1' });
  const second = await call('POST', '/v1/lines/90002/messages', { client_message_id: 'm2' });
  assert.equal(first.status, 200);
  assert.equal(first.body.message_id, 'ld:90001:m1');
  assert.notEqual(first.body.pid, second.body.pid, 'separate processes');
  const status = (await call('GET', '/v1/status')).body;
  assert.deepEqual(status.lines.map(line => [line.line_id, line.status, line.process.state]), [['90001', 'connected', 'ready'], ['90002', 'connected', 'ready']]);
  assert.equal(status.storage.level, 'warning');
  assert.deepEqual(status.storage.issues.map(issue => [issue.code, issue.line_id ?? null]),
    [['DISK_SPACE_LOW', null], ['JOURNAL_LIMIT', '90001'], ['JOURNAL_LIMIT', '90002']], 'shared disk issue reported once');
  assert.equal(status.storage.stores.length, 2);
  assert.equal(status.storage.pending_optional_write_bytes, 2);
  assert.equal(status.event_loop.p99_ms, 9, 'worst process');
  assert.ok(status.event_loop.sampled_at <= Date.now() - 4000, 'oldest sample');
  assert.equal(status.runtime.ready, true);
  assert.deepEqual(status.runtime.line_processes, { ready: 2, total: 2 });
  assert.equal((await call('GET', '/v1/health/ready')).status, 200);
  assert.equal((await call('GET', '/v1/storage')).body.stores.length, 2);
  assert.equal((await call('POST', '/v1/lines/90009/messages', {})).body.error.code, 'UNKNOWN_LINE');
  const unauthorized = await fetch(`${base}/v1/status`);
  assert.equal(unauthorized.status, 401);
});

test('a crashing number is restarted alone; the other keeps serving and requests in between are retryable', async t => {
  const { supervisor, call, ready, logs } = await supervisorFixture(t);
  await ready('90001'); await ready('90002');
  const before = supervisor.records.get('90001').pid;
  const crashed = await call('POST', '/v1/lines/90001/crash', {});
  assert.equal(crashed.status, 502);
  assert.equal(crashed.body.delivery_uncertain, true, 'the worker had the request when it died');
  assert.equal(crashed.body.error.code, 'LINE_PROCESS_FAILED');
  // Immediately afterwards the number is restarting: not sent, safe to retry.
  const waiting = await call('POST', '/v1/lines/90001/messages', { client_message_id: 'm' });
  assert.equal(waiting.status, 503);
  assert.equal(waiting.body.delivery_uncertain, false);
  assert.equal(waiting.body.error.code, 'LINE_PROCESS_UNAVAILABLE');
  const status = (await call('GET', '/v1/status')).body;
  const line = status.lines.find(item => item.line_id === '90001');
  assert.equal(line.status, 'disconnected');
  assert.match(line.error, /restarting/);
  assert.equal(line.retry_scheduled, true);
  assert.equal(status.runtime.ready, false);
  assert.equal(status.runtime.phase, 'line_process_unavailable');
  assert.equal((await call('POST', '/v1/lines/90002/messages', { client_message_id: 'n' })).status, 200, 'other number unaffected');
  await ready('90001');
  assert.notEqual(supervisor.records.get('90001').pid, before);
  assert.equal(supervisor.records.get('90001').restarts, 1);
  assert.equal((await call('POST', '/v1/lines/90001/messages', { client_message_id: 'm' })).status, 200);
  assert.ok(logs.some(item => item.event === 'line_worker_exited' && item.line_id === '90001' && item.exit_code === 1));
});

test('restart-process restarts only the requested number', async t => {
  const { supervisor, call, ready } = await supervisorFixture(t);
  await ready('90001'); await ready('90002');
  const [one, two] = ['90001', '90002'].map(id => supervisor.records.get(id).pid);
  const restarted = await call('POST', '/v1/lines/90001/restart-process', {});
  assert.equal(restarted.status, 200);
  assert.equal(restarted.body.restarting, true);
  await ready('90001');
  assert.notEqual(supervisor.records.get('90001').pid, one);
  assert.equal(supervisor.records.get('90002').pid, two);
  assert.equal(supervisor.records.get('90001').restarts, 0, 'a requested restart is not a failure');
});

test('registration starts a new worker before publishing the number, and repeats are idempotent', async t => {
  const { supervisor, dataDir, call } = await supervisorFixture(t);
  const requestId = randomUUID();
  const created = await call('POST', '/v1/lines', { request_id: requestId, expected_phone: '' });
  assert.equal(created.status, 200);
  assert.equal(created.body.line_id, '90003');
  assert.equal(created.body.created, true);
  assert.equal(supervisor.records.get('90003').state, 'ready');
  const manifest = JSON.parse(await readFile(path.join(dataDir, 'lines.json'), 'utf8'));
  assert.equal(manifest.lines['90003'], '');
  assert.equal(manifest.requests[requestId].line_id, '90003');
  const again = await call('POST', '/v1/lines', { request_id: requestId, expected_phone: '' });
  assert.equal(again.body.created, false);
  assert.equal(again.body.line_id, '90003');
  const conflict = await call('POST', '/v1/lines', { request_id: requestId, expected_phone: '96170123456' });
  assert.equal(conflict.status, 409);
});

test('a registration whose worker cannot start is not published', async t => {
  const { supervisor, dataDir, call } = await supervisorFixture(t, { workers: { '90003': { neverReady: true } } });
  supervisor.workerConfig.startTimeoutMs = 300;
  const failed = await call('POST', '/v1/lines', { request_id: randomUUID() });
  assert.equal(failed.status, 504);
  assert.equal(failed.body.error.code, 'LINE_PROCESS_START_TIMEOUT');
  assert.equal(supervisor.records.has('90003'), false);
  assert.equal(JSON.parse(await readFile(path.join(dataDir, 'lines.json'), 'utf8')).lines['90003'], undefined);
});

test('removal unlinks through the worker, tombstones in the supervisor, stops the process and purges the folder', async t => {
  const { supervisor, dataDir, call, ready } = await supervisorFixture(t, { workers: { '90002': { boundPhone: '96170222222' } } });
  await ready('90001'); await ready('90002');
  const pid = supervisor.records.get('90002').pid;
  const removed = await call('DELETE', '/v1/lines/90002');
  assert.equal(removed.status, 200);
  assert.deepEqual(removed.body, { line_id: '90002', removed: true, unlinked: true });
  const manifest = JSON.parse(await readFile(path.join(dataDir, 'lines.json'), 'utf8'));
  assert.deepEqual(manifest.removed_line_ids, ['90002']);
  assert.equal(supervisor.records.has('90002'), false);
  assert.throws(() => process.kill(pid, 0), 'worker stopped');
  assert.equal(await exists(path.join(dataDir, '90002')), false);
  assert.equal(JSON.parse(await readFile(path.join(dataDir, 'line-phones.json'), 'utf8')).phones['90002'], undefined);
  assert.equal((await call('POST', '/v1/lines/90002/messages', {})).body.error.code, 'UNKNOWN_LINE');
  assert.equal((await call('DELETE', '/v1/lines/90002')).body.removed, true, 'repeat is idempotent');
  assert.equal((await call('POST', '/v1/lines/90001/messages', {})).status, 200);
});

test('force remove works on a crash-looping number: archive, tombstone and retire its known phone', async t => {
  const { supervisor, dataDir, root, call, ready } = await supervisorFixture(t, { workers: { '90002': { boundPhone: '96170222222' } } });
  await ready('90002');
  // From now on the number's worker exits at every start.
  await writeFile(path.join(dataDir, 'fake-fail-90002'), '1000');
  await call('POST', '/v1/lines/90002/crash', {});
  await new Promise(resolve => setTimeout(resolve, 300));
  assert.notEqual(supervisor.records.get('90002').state, 'ready');
  const removed = await call('POST', '/v1/lines/90002/force-remove', {});
  assert.equal(removed.status, 200);
  assert.deepEqual(removed.body, { line_id: '90002', removed: true, forced: true });
  assert.equal(supervisor.records.has('90002'), false);
  assert.equal(await exists(path.join(dataDir, '90002')), false);
  const archived = (await readdir(path.join(root, 'linked-device-archive'))).find(name => name.endsWith('-remove-90002'));
  assert.ok(archived);
  assert.ok((await readdir(path.join(root, 'linked-device-archive', archived))).includes('state.json'));
  assert.deepEqual(JSON.parse(await readFile(path.join(dataDir, 'lines.json'), 'utf8')).removed_line_ids, ['90002']);
  const retired = JSON.parse(await readFile(path.join(dataDir, 'retired-phones.json'), 'utf8')).phones['96170222222'];
  assert.ok(retired.offline_since <= Date.now() && retired.removed_at <= Date.now());
});

test('one phone cannot be bound by two numbers across processes; reservations and claims are per number', async t => {
  const { supervisor, dataDir, call, ready } = await supervisorFixture(t, { workers: { '90001': { boundPhone: '96170111111' } } });
  await ready('90001'); await ready('90002');
  const rpc = (id, method, params) => call('POST', `/v1/lines/${id}/rpc`, { method, params });
  assert.deepEqual((await rpc('90002', 'claimPhone', { phone: '96170111111' })).body.result, { owned_elsewhere: true });
  assert.deepEqual((await rpc('90002', 'claimPhone', { phone: '96170333333' })).body.result, { owned_elsewhere: false });
  assert.deepEqual((await rpc('90001', 'claimPhone', { phone: '96170333333' })).body.result, { owned_elsewhere: true }, 'reserved by 90002');
  await rpc('90002', 'settlePhone', { phone: '96170333333', bound: true });
  assert.equal(supervisor.phoneReservations.size, 0);
  assert.deepEqual(JSON.parse(await readFile(path.join(dataDir, 'line-phones.json'), 'utf8')).phones,
    { '90001': '96170111111', '90002': '96170333333' });
  assert.deepEqual((await rpc('90001', 'claimPhone', { phone: '96170333333' })).body.result, { owned_elsewhere: true });
  const forbidden = await rpc('90002', 'tombstone', { line_id: '90001' });
  assert.equal(forbidden.status, 403);
  assert.equal(JSON.parse(await readFile(path.join(dataDir, 'lines.json'), 'utf8')).removed_line_ids, undefined);
  // Retired phones round-trip through the supervisor.
  await rpc('90002', 'retirePhone', { phone: '96170444444', offline_since: 1234 });
  assert.equal((await rpc('90001', 'retiredPhone', { phone: '96170444444' })).body.result.offline_since, 1234);
  await rpc('90001', 'dropRetiredPhone', { phone: '96170444444' });
  assert.equal((await rpc('90001', 'retiredPhone', { phone: '96170444444' })).body.result, null);
});

test('one storage checkpoint at a time across workers; a dead holder releases the token', async t => {
  const { supervisor, call, ready } = await supervisorFixture(t);
  await ready('90001'); await ready('90002');
  const rpc = (id, method) => call('POST', `/v1/lines/${id}/rpc`, { method, params: {} });
  assert.deepEqual((await rpc('90001', 'checkpointAcquire')).body.result, { granted: true });
  let granted = false;
  const waiting = rpc('90002', 'checkpointAcquire').then(result => { granted = true; return result; });
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(granted, false, 'second worker waits');
  await call('POST', '/v1/lines/90001/crash', {});
  assert.deepEqual((await waiting).body.result, { granted: true });
  assert.equal(supervisor.checkpoint.holder, supervisor.records.get('90002').child);
  await rpc('90002', 'checkpointRelease');
  assert.equal(supervisor.checkpoint.holder, null);
});

test('the global media upload slot is shared by all numbers', async t => {
  const { call, ready } = await supervisorFixture(t);
  await ready('90001'); await ready('90002');
  const slow = call('POST', '/v1/lines/90001/media', { slow_ms: 400 });
  await new Promise(resolve => setTimeout(resolve, 100));
  const busy = await call('POST', '/v1/lines/90002/media', {});
  assert.equal(busy.status, 503);
  assert.equal(busy.body.error.code, 'MEDIA_UPLOAD_BUSY');
  assert.equal(busy.body.delivery_uncertain, false);
  assert.equal((await call('POST', '/v1/lines/90002/messages', {})).status, 200, 'text is not blocked');
  assert.equal((await slow).status, 200);
  assert.equal((await call('POST', '/v1/lines/90002/media', {})).status, 200, 'slot released');
});

test('shutdown stops every worker and refuses new work', async t => {
  const { supervisor, call, ready } = await supervisorFixture(t);
  await ready('90001'); await ready('90002');
  const pids = ['90001', '90002'].map(id => supervisor.records.get(id).pid);
  await supervisor.close();
  for (const pid of pids) assert.throws(() => process.kill(pid, 0));
  const refused = await call('POST', '/v1/lines/90001/messages', {});
  assert.equal(refused.status, 503);
  assert.equal(refused.body.error.code, 'STOPPING');
  assert.equal(refused.body.delivery_uncertain, false);
});

test('a worker that ignores SIGTERM is killed after the stop timeout', async t => {
  const { supervisor, ready, logs } = await supervisorFixture(t, { workers: { '90001': { ignoreSigterm: true } } });
  await ready('90001');
  const record = supervisor.records.get('90001');
  const pid = record.pid;
  const started = Date.now();
  await supervisor.stopWorker(record, 300);
  assert.ok(Date.now() - started >= 250);
  assert.throws(() => process.kill(pid, 0));
  assert.ok(logs.some(item => item.event === 'line_worker_stop_timeout' && item.line_id === '90001'));
});

test('startup purges tombstoned folders, honours the manifest and splits a shared outbox', async t => {
  const { root, dataDir } = await tempDir(t);
  await writeOutbox(path.join(dataDir, 'outbox.json'), { entries: [entry('90005', 'p'), entry('90004', 'removed')] });
  await mkdir(path.join(dataDir, '90004'), { mode: 0o700 });
  await writeFile(path.join(dataDir, 'lines.json'), JSON.stringify({ version: 1, lines: { '90005': '' }, requests: {}, removed_line_ids: ['90004'] }));
  const supervisor = new LineSupervisor({ secret: SECRET, dataDir, lines: [], workers: { stopTimeoutMs: 5000 } },
    { workerEntry: FAKE_WORKER, log: () => {}, workerEnv: { ...process.env, WHATSAPP_LINKED_DEVICE_SECRET: SECRET, WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY: dataDir } });
  t.after(() => supervisor.close());
  await supervisor.initialize();
  assert.deepEqual([...supervisor.records.keys()], ['90005']);
  assert.equal(await exists(path.join(dataDir, '90004')), false);
  assert.deepEqual((await readOutbox(path.join(dataDir, '90005', 'outbox.json'))).entries.map(item => item.event.event_id), ['p']);
  assert.ok(await exists(path.join(root, 'linked-device-archive')));
});

// ---- The ordinary Bridge restricted to one number (worker mode)
function fakeHost() {
  const calls = [];
  const answers = { claimPhone: { owned_elsewhere: false }, retiredPhone: null };
  return { calls, answers, call: async (method, params) => {
    calls.push([method, params]);
    if (answers[method] instanceof Error) throw answers[method];
    return answers[method] ?? { ok: true };
  } };
}
async function workerBridge(t, host, socket) {
  const { dataDir } = await tempDir(t);
  const config = { secret: SECRET, lines: [{ line_id: '90007', expected_phone: '' }], dataDir, callbackUrl: 'http://127.0.0.1/callback',
    maxMediaBytes: 1024, ackTimeoutMs: 30, worker: { lineId: '90007', expectedPhone: '' } };
  const bridge = await new Bridge(config, { host, makeSocket: () => socket, onError: () => {}, log: () => {}, storageFilesystem: healthyStorage })
    .initialize({ autoConnect: false });
  t.after(async () => { if (!bridge.closed) await bridge.close().catch(() => {}); });
  return { bridge, dataDir };
}
function openSocket() {
  const ws = new EventEmitter();
  ws.isOpen = true;
  return { ws, user: { id: '96170111111:1@s.whatsapp.net' }, ev: { process: () => () => {} }, end() { this.ended = true; },
    logout: async () => {}, query: async () => ({ tag: 'iq', attrs: { type: 'result' } }) };
}

test('worker-mode bridge keeps its outbox in its own folder and never writes cross-line files', async t => {
  const host = fakeHost();
  const { bridge, dataDir } = await workerBridge(t, host, openSocket());
  assert.deepEqual([...bridge.lines.keys()], ['90007']);
  assert.equal(bridge.outbox.filename, path.join(dataDir, '90007', 'outbox.json'));
  for (const name of ['lines.json', 'retired-phones.json', 'outbox.json']) assert.equal(await exists(path.join(dataDir, name)), false, name);
  assert.ok(bridge.storageMonitor.status().stores.some(store => store.kind === 'outbox' && store.line_id === '90007'));
  await assert.rejects(bridge.addLine({ request_id: randomUUID() }), error => error.code === 'REGISTRATION_IN_SUPERVISOR');
  await bridge.removeLine('90007');
  assert.deepEqual(host.calls.find(([method]) => method === 'tombstone'), ['tombstone', { line_id: '90007' }]);
  assert.equal(await exists(path.join(dataDir, '90007')), false);
  assert.equal(await exists(path.join(dataDir, 'lines.json')), false);
  await bridge.close();
});

test('worker-mode pairing asks the supervisor before binding a phone', async t => {
  const host = fakeHost();
  const socket = openSocket();
  const { bridge } = await workerBridge(t, host, socket);
  await bridge.connect('90007');
  const line = bridge.getLine('90007');
  await bridge.connectionUpdate(line, socket, { connection: 'open' });
  assert.equal(line.status, 'connected');
  assert.equal(line.store.data.bound_phone, '96170111111');
  assert.deepEqual(host.calls.filter(([method]) => ['claimPhone', 'settlePhone'].includes(method)),
    [['claimPhone', { phone: '96170111111' }], ['settlePhone', { phone: '96170111111', bound: true }]]);
});

test('worker-mode pairing refuses a phone bound to another number and retries when the supervisor is unreachable', async t => {
  const host = fakeHost();
  host.answers.claimPhone = { owned_elsewhere: true };
  const socket = openSocket();
  const { bridge } = await workerBridge(t, host, socket);
  await bridge.connect('90007');
  const line = bridge.getLine('90007');
  await bridge.connectionUpdate(line, socket, { connection: 'open' });
  assert.equal(line.status, 'error');
  assert.match(line.error, /already linked/);
  assert.equal(line.store.data.enabled, false);

  const other = fakeHost();
  other.answers.claimPhone = new Error('channel closed');
  const second = openSocket();
  const { bridge: retrying } = await workerBridge(t, other, second);
  await retrying.connect('90007');
  const pending = retrying.getLine('90007');
  await retrying.connectionUpdate(pending, second, { connection: 'open' });
  assert.equal(pending.status, 'disconnected');
  assert.match(pending.error, /Retrying shortly/);
  assert.equal(pending.store.data.enabled, true, 'not disabled: only unverifiable');
  assert.equal(pending.store.data.bound_phone, '');
  assert.ok(second.ended);
  assert.ok(pending.reconnectTimer, 'reconnect scheduled');
});

test('worker-mode retired phones go through the supervisor', async t => {
  const host = fakeHost();
  const { bridge } = await workerBridge(t, host, openSocket());
  const line = bridge.getLine('90007');
  await line.store.update(data => { data.bound_phone = '96170111111'; });
  line.status = 'disconnected';
  line.disconnectedSince = 5000;
  await bridge.retirePhone(line);
  assert.deepEqual(host.calls.at(-1), ['retirePhone', { phone: '96170111111', offline_since: 5000 }]);
  await line.store.update(data => { data.bound_phone = ''; });
  line.auth.state.creds.me = { id: '96170111111:2@s.whatsapp.net' };
  host.answers.retiredPhone = { offline_since: 100_000, removed_at: Date.now() };
  await bridge.adoptRetiredPhone(line);
  assert.equal(line.store.data.recover_since, 100_000 - 60_000);
  assert.deepEqual(host.calls.slice(-2).map(([method]) => method), ['retiredPhone', 'dropRetiredPhone']);
});
