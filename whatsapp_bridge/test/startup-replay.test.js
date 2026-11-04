import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {JournalStore, scanJournal, readJournal, encode, hash} from '../src/journal-store.js';

async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'startup-replay-'));
  t.after(() => fs.rm(directory, {recursive: true, force: true}));
  const filename = path.join(directory, 'state.json');
  await fs.writeFile(filename, '{"messages":{}}', {mode: 0o600});
  return {directory, filename};
}
const record = seq => {
  const body = encode({version: 1, seq, ops: [{path: ['messages', String(seq)], value: 'x'.repeat(1024)}]});
  return encode({record: body, checksum: hash(body)}) + '\n';
};

test('journal replay cancellation stops between chunks without repairing or modifying stored bytes', async t => {
  const {filename} = await fixture(t);
  const content = Array.from({length: 200}, (_, i) => record(i + 1)).join('') + 'incomplete-evidence';
  await fs.writeFile(filename + '.journal', content);
  const controller = new AbortController();
  let visited = 0;
  await assert.rejects(scanJournal(filename + '.journal', () => { visited++; }, {
    signal: controller.signal, onChunk: () => controller.abort(),
  }), {name: 'AbortError'});
  assert.ok(visited > 0 && visited < 200);
  assert.equal(await fs.readFile(filename + '.journal', 'utf8'), content);
  const restored = await readJournal(filename, () => { throw new Error('Must preserve base'); });
  assert.equal(restored.seq, 200);
  assert.equal(restored.incompleteTailBytes, Buffer.byteLength('incomplete-evidence'));
});

test('cancelled snapshot load does not report storage corruption or create fresh credentials', async t => {
  const {filename} = await fixture(t);
  await fs.writeFile(filename + '.snapshot', encode({version: 1, seq: 0, data: {messages: {saved: true}}}));
  const controller = new AbortController(), readFile = fs.readFile;
  const store = new JournalStore(filename, () => { throw new Error('Must not initialize'); });
  fs.readFile = async (name, options) => {
    const result = await readFile(name, options);
    if (name === filename + '.snapshot') controller.abort();
    return result;
  };
  try { await assert.rejects(store.load({signal: controller.signal}), {name: 'AbortError'}); }
  finally { fs.readFile = readFile; }
  assert.equal(store.failed, false);
  assert.equal(store.recoveryErrors, 0);
  await assert.rejects(fs.stat(filename + '.health.json'), {code: 'ENOENT'});
  await store.close();
  assert.equal((await readJournal(filename, () => ({}))).data.messages.saved, true);
});

test('cancellation after tail quarantine begins completes evidence synchronization before stopping', async t => {
  const {directory, filename} = await fixture(t);
  const tail = 'incomplete-private-evidence';
  await fs.writeFile(filename + '.journal', record(1) + tail);
  const controller = new AbortController(), open = fs.open;
  fs.open = async (name, ...args) => {
    if (String(name).includes('.journal.tail-') && String(name).endsWith('.bin')) controller.abort();
    return open(name, ...args);
  };
  const store = new JournalStore(filename, () => ({}));
  try { await assert.rejects(store.load({signal: controller.signal}), {name: 'AbortError'}); }
  finally { fs.open = open; }
  await store.close();
  const files = await fs.readdir(directory);
  const evidence = files.find(name => name.endsWith('.bin'));
  assert.ok(evidence);
  assert.equal(await fs.readFile(path.join(directory, evidence), 'utf8'), tail);
  const metadata = JSON.parse(await fs.readFile(path.join(directory, evidence + '.json'), 'utf8'));
  assert.equal(metadata.checksum, hash(tail));
  assert.equal(metadata.bytes, Buffer.byteLength(tail));
  const recovered = await readJournal(filename, () => ({}));
  assert.equal(recovered.seq, 1);
  assert.equal(recovered.incompleteTailBytes, 0);
});
