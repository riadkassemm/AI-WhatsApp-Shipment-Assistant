import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile, mkdir, symlink, link, chmod, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createAuth, atomicWrite } from '../src/storage.js';
import { JournalStore } from '../src/journal-store.js';
import { storageRehearsal, verifyRehearsalCopy } from '../src/storage-rehearsal.js';

async function copy(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-rehearsal-test-'));
  await chmod(directory, 0o700);
  t.after(() => rm(directory, { recursive: true, force: true }));
  return directory;
}

const mark = directory => writeFile(path.join(directory, '.storage-rehearsal-copy'), 'WHATSAPP_STORAGE_REHEARSAL_COPY\n', { mode: 0o600 });

test('rehearsal refuses unmarked, permissive, linked, and declared live directories', async t => {
  const directory = await copy(t);
  await assert.rejects(verifyRehearsalCopy(directory), { code: 'REHEARSAL_MARKER_REQUIRED' });
  await mark(directory);
  await chmod(directory, 0o755);
  await assert.rejects(verifyRehearsalCopy(directory), { code: 'REHEARSAL_PRIVATE_DIRECTORY_REQUIRED' });
  await chmod(directory, 0o700);
  const outside = await copy(t);
  await writeFile(path.join(outside, 'private'), 'synthetic-private', { mode: 0o600 });
  await symlink(path.join(outside, 'private'), path.join(directory, 'alias'));
  await assert.rejects(verifyRehearsalCopy(directory), { code: 'REHEARSAL_ALIASED_FILE_FORBIDDEN' });
  await rm(path.join(directory, 'alias'));
  await link(path.join(outside, 'private'), path.join(directory, 'alias'));
  await assert.rejects(verifyRehearsalCopy(directory), { code: 'REHEARSAL_ALIASED_FILE_FORBIDDEN' });
  await rm(path.join(directory, 'alias'));
  const previous = process.env.WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY;
  process.env.WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY = directory;
  t.after(() => {
    if (previous === undefined) delete process.env.WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY;
    else process.env.WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY = previous;
  });
  await assert.rejects(verifyRehearsalCopy(directory), { code: 'REHEARSAL_LIVE_DIRECTORY_FORBIDDEN' });
});

test('private-copy compaction and mocked reconnect preserve auth and uncertainty without printing state', { timeout: 120000 }, async t => {
  const directory = await copy(t);
  await mark(directory);
  await mkdir(path.join(directory, '90001'), { mode: 0o700 });
  await atomicWrite(path.join(directory, 'lines.json'), { version: 1, lines: { '90001': '' }, requests: {} });
  const auth = await createAuth(path.join(directory, '90001'), { maintenance: { enabled: false } });
  auth.state.creds.registered = true;
  auth.state.creds.me = { id: '96170111111:1@s.whatsapp.net' };
  await auth.saveCreds();
  await auth.state.keys.set({ session: { synthetic: Buffer.from([7, 8, 9]) } });
  await auth.store.close();
  const state = await new JournalStore(path.join(directory, '90001', 'state.json'), () => ({
    messages: {}, media: {}, operations: {}, send_requests: {}, enabled: true, bound_phone: '96170111111',
  }), { maintenance: { enabled: false } }).load();
  await state.update(data => {
    data.messages.synthetic = { body: 'PRIVATE-SYNTHETIC-REHEARSAL-BODY', pending_send: true, delivery_uncertain: false };
  });
  await state.close();
  const outbox = await new JournalStore(path.join(directory, 'outbox.json'), () => ({ entries: [], dead_letters: [] }),
    { maintenance: { enabled: false } }).load();
  await outbox.update(data => { data.entries.push({ event: { event_id: 'synthetic-callback', line_id: '90001' } }); });
  await outbox.close();
  const before = await storageRehearsal('inspect', directory, 1);
  assert.equal(before.stores.length, 3);
  const compacted = await storageRehearsal('compact', directory, 1);
  assert.equal(compacted.state_preserved, true);
  assert.equal(compacted.after.reduce((sum, store) => sum + store.journal_bytes, 0), 0);
  const reconnected = await storageRehearsal('reconnect', directory, 1);
  assert.equal(reconnected.state_preserved, true);
  assert.equal(reconnected.verification.mock_connected_lines, 1);
  assert.equal(reconnected.verification.sends, 0);
  assert.equal(reconnected.verification.network_attempts, 0);
  assert.equal(reconnected.verification.interrupted_sends_reclassified_uncertain, 1);
  const afterState = reconnected.after.find(store => store.kind === 'state');
  assert.equal(afterState.counts.pending_sends, 0);
  assert.equal(afterState.counts.uncertain_sends, 1);
  const output = JSON.stringify({ before, compacted, reconnected });
  for (const privateValue of ['PRIVATE-SYNTHETIC-REHEARSAL-BODY', '96170111111', 'fingerprint', 'noiseKey']) assert.equal(output.includes(privateValue), false);
  const baseline = JSON.parse(await readFile(path.join(directory, '.storage-rehearsal-baseline.json'), 'utf8'));
  assert.equal(typeof baseline.stores[0].fingerprint, 'string');
});
