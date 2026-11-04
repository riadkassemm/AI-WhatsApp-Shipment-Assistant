import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, readFile, rm, readdir, chmod} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createAuth, atomicWrite} from '../src/storage.js';
import {JournalStore, readJournal} from '../src/journal-store.js';
import {storageSoak} from '../src/storage-soak.js';

test('online soak retains every loaded store through repeated automatic compactions and exact recovery', {timeout: 120000}, async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-online-soak-'));
  await chmod(directory, 0o700);
  t.after(() => rm(directory, {recursive: true, force: true}));
  await writeFile(path.join(directory, '.storage-rehearsal-copy'), 'WHATSAPP_STORAGE_REHEARSAL_COPY\n', {mode: 0o600});
  await mkdir(path.join(directory, '90001'), {mode: 0o700});
  await atomicWrite(path.join(directory, 'lines.json'), {version: 1, lines: {'90001': ''}, requests: {}});
  const auth = await createAuth(path.join(directory, '90001'), {maintenance: {enabled: false}});
  auth.state.creds.registered = true;
  auth.state.creds.me = {id: '96170111111:1@s.whatsapp.net'};
  await auth.saveCreds();
  await auth.state.keys.set({session: {private: Buffer.from([6, 7, 8])}});
  await auth.store.close();
  const initialState = {messages: {pending: {body: 'PRIVATE-SOAK-BODY', pending_send: true}}, media: {},
    operations: {private: {delivery_uncertain: true}}, send_requests: {request: {native_id: 'pending'}}, enabled: true, bound_phone: '96170111111'};
  const state = await new JournalStore(path.join(directory, '90001', 'state.json'), () => initialState,
    {maintenance: {enabled: false}}).load();
  await state.close();
  const initialOutbox = {entries: [{event: {event_id: 'private-callback', line_id: '90001'}}], dead_letters: []};
  const outbox = await new JournalStore(path.join(directory, 'outbox.json'), () => initialOutbox,
    {maintenance: {enabled: false}}).load();
  await outbox.close();
  const result = await storageSoak(directory, {expectedLines: 1, cycles: 3, writesPerCycle: 5, payloadBytes: 1024,
    compactBytes: 3 * 1024, authBurstTransactions: 3});
  assert.equal(result.state_preserved, true);
  assert.equal(result.stores, 3);
  assert.equal(result.cycles_completed, 3);
  assert.equal(result.paired_lines, 1);
  assert.equal(result.maintenance.enabled, true);
  assert.equal(result.maintenance.maxTransactions, 10000);
  assert.equal(result.maintenance.maxAgeMs, 6 * 3600000);
  assert.ok(result.compactions >= 9);
  assert.ok(result.writes_during_active_workers > 0);
  assert.equal(result.maximum_active_workers, 1);
  assert.equal(result.network_attempts, 0);
  assert.equal(result.sends, 0);
  assert.equal(result.provider_calls, 0);
  assert.equal(result.auth_transactions, 6);
  assert.equal(result.auth_journal_transactions, 6);
  assert.equal(result.auth_burst.transactions, 3);
  assert.equal(result.auth_burst.journal_transactions, 3);
  assert.equal(result.auth_burst.maximum_in_flight_per_line, 3);
  assert.equal(result.auth_burst.orphan_intents, 0);
  assert.ok(result.auth_burst.journal_bytes_added > 0);
  assert.ok(result.auth_burst.duration_ms > 0);
  assert.ok(result.auth_burst.latency_ms.p50 > 0);
  assert.ok(result.auth_burst.latency_ms.p95 >= result.auth_burst.latency_ms.p50);
  assert.ok(result.auth_burst.latency_ms.max >= result.auth_burst.latency_ms.p95);
  assert.ok(result.auth_burst.memory_before.rss > 0 && result.auth_burst.memory_after.rss > 0);
  assert.equal(result.qr_codes, 0);
  assert.ok(result.shutdown_ms < 35000);
  for (const sample of result.samples.filter(sample => sample.label.startsWith('cycle_'))) {
    assert.equal(sample.active_checkpoint_workers, 0);
    assert.equal(sample.checkpoint_temporary_bytes, 0);
    assert.ok(sample.journal_bytes < 3 * result.config.compactBytes);
    assert.ok(sample.memory.rss > 0);
    assert.ok(sample.available_disk_bytes > 0);
  }
  assert.equal(result.samples.at(-1).active_checkpoint_workers, 0);
  const publicOutput = JSON.stringify(result);
  for (const privateValue of ['PRIVATE-SOAK-BODY', '96170111111', 'fingerprint', 'noiseKey']) assert.equal(publicOutput.includes(privateValue), false);
  const privateReport = JSON.parse(await readFile(path.join(directory, '.storage-soak-report.json'), 'utf8'));
  assert.equal(typeof privateReport.baseline[0].fingerprint, 'string');
  const restored = await readJournal(path.join(directory, '90001', 'state.json'), () => assert.fail('Missing original state'));
  assert.equal(restored.data.messages.pending.body, 'PRIVATE-SOAK-BODY');
  assert.equal(restored.data.messages.pending.pending_send, false);
  assert.equal(restored.data.messages.pending.delivery_uncertain, true);
  assert.deepEqual(restored.data.operations, initialState.operations);
  assert.deepEqual(restored.data.send_requests, initialState.send_requests);
  assert.ok(!Object.hasOwn(restored.data, '__private_storage_lifecycle_soak_v1__'));
  for (const subdirectory of [directory, path.join(directory, '90001')]) {
    assert.ok(!(await readdir(subdirectory)).some(name => name.includes('.checkpoint-')));
  }
});

test('soak rejects missing copy marker before starting any bridge or write', async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-online-soak-unmarked-'));
  await chmod(directory, 0o700);
  t.after(() => rm(directory, {recursive: true, force: true}));
  await assert.rejects(storageSoak(directory, {expectedLines: 1}), {code: 'REHEARSAL_MARKER_REQUIRED'});
  assert.deepEqual(await readdir(directory), []);
});
