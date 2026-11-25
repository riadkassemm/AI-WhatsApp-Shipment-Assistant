import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { preserveSessionHolds } from '../scripts/preserve-session-holds.js';
import { acquireDataDirectory, atomicWrite } from '../src/file-store.js';
import { readJournal } from '../src/journal-store.js';

test('upgrade preserves only terminal holds without changing delivery evidence or enabled state', async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'session-holds-'));
  t.after(() => rm(directory, {recursive: true, force: true}));
  const original = {enabled: true, messages: {uncertain: {delivery_uncertain: true}}, bound_phone: 'synthetic'};
  for (const id of ['90062', '90063']) {
    await mkdir(path.join(directory, id));
    await atomicWrite(path.join(directory, id, 'state.json'), original);
  }
  const snapshot = {captured_at: 10000, lines: [
    {line_id: '90062', status: 'error', recovery_category: 'authentication_failure', disconnected_since: 5000},
    {line_id: '90063', status: 'connected', recovery_category: null},
  ]};
  assert.deepEqual(await preserveSessionHolds(directory, snapshot, 10001), ['90062']);
  const read = id => readJournal(path.join(directory, id, 'state.json'), () => ({}));
  const saved = (await read('90062')).data;
  assert.equal(saved.connection_hold, 'authentication_failure');
  assert.deepEqual(saved.messages, original.messages);
  assert.equal(saved.enabled, true);
  assert.equal(saved.bound_phone, original.bound_phone);
  assert.equal(saved.last_disconnect.reason, 'unknown');
  assert.deepEqual((await read('90063')).data, original);
  assert.deepEqual(await preserveSessionHolds(directory, snapshot, 10002), [], 'idempotent');
  const release = await acquireDataDirectory(directory);
  try { await assert.rejects(preserveSessionHolds(directory, snapshot, 10003), {code: 'ELOCKED'}); }
  finally { await release(); }
  const workerRelease = await acquireDataDirectory(path.join(directory, '.workers', '90062'));
  try { await assert.rejects(preserveSessionHolds(directory, snapshot, 10003), {code: 'ELOCKED'}); }
  finally { await workerRelease(); }
});

test('upgrade rejects stale snapshots and unsafe line paths before accessing state', async () => {
  await assert.rejects(preserveSessionHolds('/unused', {captured_at: 1, lines: []}, 400000), /STALE_SNAPSHOT/);
  await assert.rejects(preserveSessionHolds('/unused', {captured_at: 100, lines: [
    {line_id: '../90062', status: 'error', recovery_category: 'authentication_failure', disconnected_since: 50},
  ]}, 101), /INVALID_HOLD_LINE/);
});
