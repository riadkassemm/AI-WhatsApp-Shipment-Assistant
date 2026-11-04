import test from 'node:test';
import assert from 'node:assert/strict';
import { StorageMonitor } from '../src/storage-monitor.js';
import { readStorageConfig } from '../src/config.js';
import { createServer } from '../src/server.js';

const GiB = 1024 ** 3;
const disk = (bytes = 20 * GiB, freeInodes = 1000) => ({bsize: 1, blocks: 100 * GiB, bavail: bytes, files: 1000, ffree: freeInodes});
function store(values = {}) {
  const stats = {journal_bytes: 0, total_written_bytes: 0, ...values};
  return {values: stats, checks: 0, stats: () => stats, maintain() { this.checks++; return Promise.resolve(); }};
}
function fixture(options = {}) {
  let time = 100000;
  const events = [];
  const monitor = new StorageMonitor('/private/session/path', {filesystem: async () => disk(), now: () => time,
    log: event => events.push(event), ...options});
  return {monitor, events, advance: value => {time += value;}};
}

test('storage defaults enable bounded maintenance and reject conflicting pressure thresholds', () => {
  const config = readStorageConfig({});
  assert.equal(config.maintenance.enabled, true);
  assert.equal(config.maintenance.maxReplayRecordBytes, 64 * 1024 ** 2);
  assert.ok(config.maintenance.maxJournalBytes <= config.monitoring.journalWarnBytes);
  assert.ok(config.monitoring.journalWarnBytes < config.monitoring.journalCriticalBytes);
  assert.ok(config.maintenance.minFreeBytes < config.monitoring.diskCriticalBytes);
  const env = key => `WHATSAPP_LINKED_DEVICE_STORAGE_${key}`;
  assert.throws(() => readStorageConfig({[env('AUTO_COMPACT')]: 'yes'}), /must be true or false/);
  assert.throws(() => readStorageConfig({[env('COMPACT_BYTES')]: '-1'}), /integer/);
  assert.throws(() => readStorageConfig({[env('COMPACT_TRANSACTIONS')]: 'NaN'}), /integer/);
  assert.throws(() => readStorageConfig({[env('MAX_REPLAY_RECORD_BYTES')]: '0'}), /integer/);
  assert.throws(() => readStorageConfig({[env('MONITOR_TIMEOUT_MS')]: '31000'}), /thresholds/);
  assert.throws(() => readStorageConfig({[env('JOURNAL_WARN_BYTES')]: '268435456'}), /thresholds/);
  assert.throws(() => readStorageConfig({[env('SNAPSHOT_WARN_BYTES')]: '125829120'}), /thresholds/);
  assert.throws(() => readStorageConfig({[env('MAX_SNAPSHOT_BYTES')]: '104857600'}), /thresholds/);
  assert.throws(() => readStorageConfig({[env('DISK_CRITICAL_PERCENT')]: '5'}), /thresholds/);
  assert.throws(() => readStorageConfig({[env('INODE_WARN_PERCENT')]: '101'}), /thresholds/);
  assert.throws(() => readStorageConfig({[env('RESERVE_BYTES')]: '536870912'}), /thresholds/);
});

test('monitor samples every registered scope and growth remains meaningful after rotation', async () => {
  const {monitor, advance} = fixture();
  const auth = store({journal_bytes: 100, total_written_bytes: 100, replay_ms: 30, replay_records: 9});
  const outbox = store();
  monitor.register(auth, {lineId: '90001', kind: 'auth'});
  monitor.register(outbox, {kind: 'outbox'});
  advance(5000);
  await monitor.sample();
  assert.equal(auth.checks, 1);
  assert.equal(outbox.checks, 1);
  assert.equal(monitor.status().stores[0].journal_growth_bytes_per_second, 0, 'startup history is not new growth');
  advance(1000);
  Object.assign(auth.values, {journal_bytes: 10, total_written_bytes: 300, last_compaction_at: 100500,
    last_compaction_ms: 35, last_compaction_reclaimed_bytes: 290});
  await monitor.sample();
  const result = monitor.status();
  assert.equal(result.level, 'ok');
  assert.equal(result.stores[0].journal_bytes, 10);
  assert.equal(result.stores[0].journal_growth_bytes_per_second, 200);
  assert.equal(result.stores[0].last_compaction_at, 100500);
  assert.equal(result.stores[0].last_compaction_ms, 35);
  assert.equal(result.stores[0].last_compaction_reclaimed_bytes, 290);
  assert.equal(result.stores[0].replay_ms, 30);
  monitor.unregister('90001');
  assert.equal(monitor.status().stores.length, 1);
  assert.equal(monitor.status().stores[0].kind, 'outbox');
  await monitor.close();
});

test('idle monitoring starts independently and never waits for a pending compaction worker', async () => {
  const {monitor} = fixture();
  let complete;
  const pending = new Promise(resolve => {complete = resolve;});
  const auth = store();
  auth.maintain = () => pending;
  const outbox = store();
  monitor.register(auth, {lineId: '90001', kind: 'auth'});
  monitor.register(outbox, {kind: 'outbox'});
  await monitor.start();
  assert.ok(monitor.timer);
  assert.equal(outbox.checks, 1);
  assert.equal(monitor.status().level, 'ok');
  await monitor.close();
  complete();
  await monitor.sample();
  assert.equal(outbox.checks, 1, 'closed monitors cannot initiate maintenance');
});

test('critical journal pressure is scoped, preserves evidence, and clears automatically', async () => {
  const {monitor} = fixture();
  const first = store({journal_bytes: 300 * 1024 ** 2, pending_sends: 5});
  const second = store();
  monitor.register(first, {lineId: '90001', kind: 'state'});
  monitor.register(second, {lineId: '90002', kind: 'state'});
  await monitor.sample();
  assert.throws(() => monitor.assertCanSubmit('90001'), error => error.code === 'STORAGE_PRESSURE' && error.delivery_uncertain === false);
  assert.doesNotThrow(() => monitor.assertCanSubmit('90002'));
  assert.equal(first.values.pending_sends, 5);
  first.values.journal_bytes = 0;
  assert.doesNotThrow(() => monitor.assertCanSubmit('90001'));
  const outbox = store({failed: true, recovery_errors: 1});
  monitor.register(outbox, {kind: 'outbox'});
  assert.throws(() => monitor.assertCanSubmit('90002'), {code: 'STORAGE_PRESSURE'});
  await monitor.close();
});

test('disk pressure uses available blocks, handles unknown inode counts, and fails closed on stale probes', async () => {
  let current = disk(300 * 1024 ** 2);
  const {monitor, advance} = fixture({filesystem: async () => current});
  await monitor.sample();
  assert.equal(monitor.status().level, 'critical');
  assert.equal(monitor.status().issues[0].code, 'DISK_SPACE_LOW');
  current = {...disk(), bfree: 0, files: 0, ffree: 0};
  await monitor.sample();
  assert.equal(monitor.status().level, 'ok', 'bavail controls service-user free space; unknown inodes are not zero free');
  assert.equal(monitor.status().disk.available_inode_percent, null);
  current = disk(20 * GiB, 4);
  await monitor.sample();
  assert.equal(monitor.status().issues[0].code, 'INODES_LOW');
  assert.throws(() => monitor.assertCanSubmit('90001'), {code: 'STORAGE_PRESSURE'});
  current = disk();
  await monitor.sample();
  monitor.filesystem = async () => {throw Object.assign(new Error('/private/secret'), {code: 'EACCES'});};
  await monitor.sample();
  assert.equal(monitor.status().level, 'warning');
  advance(monitor.config.intervalMs * 3 + 1);
  assert.equal(monitor.status().level, 'critical');
  assert.throws(() => monitor.assertCanSubmit('90001'), {code: 'STORAGE_PRESSURE'});
  monitor.filesystem = async () => disk();
  await monitor.sample();
  assert.equal(monitor.status().level, 'ok');
  await monitor.close();
});

test('snapshot headroom warns and blocks new submissions before the checkpoint size cap', async () => {
  const {monitor} = fixture();
  const auth = store({snapshot_bytes: 100 * 1024 ** 2});
  monitor.register(auth, {lineId: '90001', kind: 'auth'});
  await monitor.sample();
  assert.equal(monitor.status().level, 'warning');
  assert.equal(monitor.status().issues[0].code, 'SNAPSHOT_LIMIT');
  assert.doesNotThrow(() => monitor.assertCanSubmit('90001'));
  auth.values.snapshot_bytes = 121 * 1024 ** 2;
  assert.throws(() => monitor.assertCanSubmit('90001'), {code: 'STORAGE_PRESSURE'});
  assert.doesNotThrow(() => monitor.assertCanSubmit('90002'));
  assert.equal(auth.values.snapshot_bytes, 121 * 1024 ** 2, 'authoritative state is never pruned');
  await monitor.close();
});

test('invalid filesystem metrics become unavailable instead of reporting false headroom', async () => {
  let current = disk();
  const {monitor, advance} = fixture({filesystem: async () => current});
  await monitor.sample();
  const previous = monitor.status().disk;
  for (const invalid of [{...disk(), bavail: Infinity}, {...disk(), bavail: 101 * GiB},
    {...disk(), ffree: NaN}, {...disk(), ffree: 1001}, {...disk(), files: -1}]) {
    current = invalid;
    await monitor.sample();
    assert.equal(monitor.status().issues[0].code, 'MONITOR_UNAVAILABLE');
    assert.deepEqual(monitor.status().disk, previous, 'invalid samples never replace valid cached metrics');
  }
  advance(monitor.config.intervalMs * 3 + 1);
  assert.equal(monitor.status().level, 'critical');
  await monitor.close();
});

test('failure counter persistence degradation is visible without blocking essential storage', async () => {
  const {monitor, events} = fixture();
  const auth = store({compaction_failures: 8, recovery_errors: 0, health_persistence_failed: true});
  monitor.register(auth, {lineId: '90001', kind: 'auth'});
  await monitor.sample();
  assert.equal(monitor.status().stores[0].compaction_failures, 8);
  assert.equal(monitor.status().stores[0].failure_counters_persistence, 'degraded');
  assert.equal(monitor.status().issues[0].code, 'STORAGE_HEALTH_UNAVAILABLE');
  assert.equal(events[0].level, 'warning');
  assert.doesNotThrow(() => monitor.assertCanSubmit('90001'));
  auth.values.health_persistence_failed = false;
  await monitor.sample();
  assert.equal(monitor.status().stores[0].failure_counters_persistence, 'durable');
  assert.equal(events.at(-1).event, 'storage_recovered');
  await monitor.close();
});

test('optional uploads reserve free capacity synchronously and release it on failure or completion', async () => {
  const {monitor} = fixture({filesystem: async () => disk(2 * GiB)});
  await monitor.sample();
  const release = monitor.reserveOptionalWrite('90001', 600 * 1024 ** 2);
  assert.equal(monitor.status().pending_optional_write_bytes, 600 * 1024 ** 2 + 16 * 1024);
  assert.throws(() => monitor.reserveOptionalWrite('90002', 600 * 1024 ** 2), error =>
    error.code === 'STORAGE_PRESSURE' && error.delivery_uncertain === false);
  assert.doesNotThrow(() => monitor.assertCanSubmit('90002'), 'essential small persistence is not reserved/blocked by this admission API');
  release();
  release();
  assert.equal(monitor.status().pending_optional_write_bytes, 0);
  await monitor.sample();
  const next = monitor.reserveOptionalWrite('90002', 600 * 1024 ** 2);
  next();
  assert.equal(monitor.status().pending_optional_write_bytes, 0);
  await monitor.close();
});

test('optional uploads fail closed on unknown/stale disk metrics and preserve byte and percentage reserves', async () => {
  const {monitor, advance} = fixture({filesystem: async () => ({...disk(600 * 1024 ** 2), blocks: 10 * GiB})});
  assert.throws(() => monitor.reserveOptionalWrite('90001', 1), {code: 'STORAGE_PRESSURE'});
  await monitor.sample();
  assert.throws(() => monitor.reserveOptionalWrite('90001', 88 * 1024 ** 2), {code: 'STORAGE_PRESSURE'}, 'bytes remain strictly above critical reserve');
  const release = monitor.reserveOptionalWrite('90001', 25 * 1024 ** 2);
  release();
  advance(monitor.config.intervalMs * 3 + 1);
  assert.throws(() => monitor.reserveOptionalWrite('90001', 1), {code: 'STORAGE_PRESSURE'});
  await monitor.close();
});

test('parallel tiny uploads cannot consume the inode reserve from one cached sample', async () => {
  const {monitor} = fixture({filesystem: async () => disk(20 * GiB, 52)});
  await monitor.sample();
  const release = monitor.reserveOptionalWrite('90001', 1);
  assert.equal(monitor.status().pending_optional_write_inodes, 1);
  assert.throws(() => monitor.reserveOptionalWrite('90002', 1), {code: 'STORAGE_PRESSURE'});
  release();
  release();
  assert.equal(monitor.status().pending_optional_write_inodes, 0);
  await monitor.sample();
  monitor.reserveOptionalWrite('90002', 1)();
  await monitor.close();
});

test('tiny uploads reserve filesystem allocation blocks and separate metadata headroom', async () => {
  const blockSize = 4096, critical = 512 * 1024 ** 2;
  const {monitor} = fixture({filesystem: async () => ({bsize: blockSize, blocks: GiB / blockSize,
    bavail: (critical + 32 * 1024) / blockSize, files: 1000, ffree: 900})});
  await monitor.sample();
  assert.equal(monitor.status().disk.block_size, blockSize);
  const release = monitor.reserveOptionalWrite('90001', 1);
  assert.equal(monitor.status().pending_optional_write_bytes, 20 * 1024, 'one payload block plus four metadata blocks');
  assert.throws(() => monitor.reserveOptionalWrite('90002', 1), {code: 'STORAGE_PRESSURE'},
    'two tiny uploads must not share the same free allocation blocks');
  release();
  assert.equal(monitor.status().unobserved_optional_write_bytes, 20 * 1024);
  assert.throws(() => monitor.reserveOptionalWrite('90002', 1), {code: 'STORAGE_PRESSURE'});
  await monitor.sample();
  const next = monitor.reserveOptionalWrite('90002', 4097);
  assert.equal(monitor.status().pending_optional_write_bytes, 24 * 1024, 'a partial second payload block is charged in full');
  next();
  await monitor.close();
});

test('completed uploads stay charged until a successful disk probe begins after their completion', async () => {
  let resolve, pending = false, current = disk(2 * GiB);
  const {monitor} = fixture({filesystem: () => pending ? new Promise(done => {resolve = done;}) : Promise.resolve(current)});
  await monitor.sample();
  const release = monitor.reserveOptionalWrite('90001', 600 * 1024 ** 2);
  pending = true;
  const staleSample = monitor.sample();
  await Promise.resolve();
  release();
  resolve(disk(2 * GiB));
  await staleSample;
  assert.equal(monitor.status().pending_optional_write_bytes, 0);
  assert.equal(monitor.status().unobserved_optional_write_bytes, 600 * 1024 ** 2 + 16 * 1024);
  assert.throws(() => monitor.reserveOptionalWrite('90002', 600 * 1024 ** 2), {code: 'STORAGE_PRESSURE'},
    'late result from a pre-completion probe must not expose the consumed capacity again');
  const filesystem = monitor.filesystem;
  monitor.filesystem = async () => { throw Object.assign(new Error('EIO'), {code: 'EIO'}); };
  await monitor.sample();
  assert.equal(monitor.status().unobserved_optional_write_bytes, 600 * 1024 ** 2 + 16 * 1024, 'failed probes cannot reclaim completed debits');
  monitor.filesystem = filesystem;
  pending = false;
  current = disk(2 * GiB - 600 * 1024 ** 2);
  await monitor.sample();
  assert.equal(monitor.status().unobserved_optional_write_bytes, 0);
  const next = monitor.reserveOptionalWrite('90002', 100 * 1024 ** 2);
  next();
  await monitor.close();
});

test('compaction and recovery alerts repeat at a bounded rate and contain safe fields only', async () => {
  const {monitor, events, advance} = fixture();
  const auth = store({compaction_failures: 1, last_compaction_error: 'ENOSPC', secret: 'credentials', filename: '/private/auth.json'});
  monitor.register(auth, {lineId: '90001', kind: 'auth'});
  await monitor.sample();
  assert.equal(events.length, 1);
  assert.equal(events[0].event, 'storage_alert');
  assert.equal(events[0].issues[0].code, 'COMPACTION_FAILED');
  await monitor.sample();
  assert.equal(events.length, 1);
  advance(monitor.config.alertRepeatMs);
  await monitor.sample();
  assert.equal(events.length, 2);
  auth.values.last_compaction_error = null;
  await monitor.sample();
  assert.equal(events.at(-1).event, 'storage_recovered');
  auth.values.quarantined_tail_bytes = 200;
  await monitor.sample();
  assert.equal(events.at(-1).issues[0].code, 'RECOVERY_ATTENTION');
  auth.values.last_compaction_error = '/private/secret';
  const serialized = JSON.stringify({events, status: monitor.status()});
  assert.ok(!serialized.includes('/private'));
  assert.ok(!serialized.includes('credentials'));
  assert.equal(monitor.status().stores[0].last_compaction_error, 'STORAGE_MAINTENANCE_FAILED');
  await monitor.close();
});

test('a hung filesystem probe has a deadline without duplicate probes or blocked health/shutdown', async () => {
  let expire, release, probes = 0, deadlines = 0;
  const {monitor, advance} = fixture({
    filesystem: () => {probes++; return new Promise(resolve => {release = resolve;});},
    scheduleTimeout: callback => {deadlines++; expire = callback; return 1;}, cancelTimeout: () => {},
  });
  const auth = store();
  monitor.register(auth, {lineId: '90001', kind: 'auth'});
  const first = monitor.sample();
  await Promise.resolve();
  assert.equal(probes, 1);
  assert.equal(monitor.status().level, 'warning', 'status never waits for a filesystem probe');
  expire();
  await first;
  assert.equal(auth.checks, 1, 'maintenance continues after probe timeout');
  advance(monitor.config.intervalMs * 3 + 1);
  assert.equal(monitor.status().level, 'critical');
  const second = monitor.sample();
  const closing = monitor.close();
  assert.equal(probes, 1, 'one underlying kernel probe at a time');
  assert.equal(deadlines, 1, 'a stuck probe cannot accumulate promise/deadline listeners');
  await second;
  await closing;
  release(disk());
});

test('storage endpoint requires bridge authorization and contains no session details', async t => {
  const {monitor} = fixture();
  monitor.register(store({secret: 'private-auth'}), {lineId: '90001', kind: 'auth'});
  await monitor.sample();
  const secret = 'test-secret-with-more-than-thirty-two-characters';
  const server = createServer({storageMonitor: monitor}, {secret});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    await monitor.close();
    await new Promise(resolve => {server.close(resolve); server.closeAllConnections();});
  });
  const endpoint = `http://127.0.0.1:${server.address().port}/v1/storage`;
  assert.equal((await fetch(endpoint)).status, 401);
  const response = await fetch(endpoint, {headers: {authorization: `Bearer ${secret}`}});
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.equal(JSON.parse(body).stores[0].kind, 'auth');
  assert.ok(!body.includes('private-auth'));
  assert.ok(!body.includes('/private/session/path'));
});
