import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, readFile, writeFile, appendFile, readdir, rm, stat} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import {performance} from 'node:perf_hooks';
import {JournalStore, readJournal, readSnapshot, encode, hash} from '../src/journal-store.js';
import {checkpointWorker} from '../src/journal-maintenance.js';
import {storageTool} from '../src/storage-tool.js';

const gate = () => { let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve}; };
async function fixture(t, options = {}) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-checkpoint-'));
  const filename = path.join(directory, 'outbox.json');
  const store = await new JournalStore(filename, () => ({creds: {paired: true, key: Buffer.from([1, 2, 3])},
    messages: {}, entries: [], dead_letters: []}), {maintenance: {enabled: false}, ...options}).load();
  t.after(async () => { await store.close().catch(() => {}); await rm(directory, {recursive: true, force: true}); });
  return {directory, filename, store};
}
const request = index => ({id: index, pending_send: true, delivery_uncertain: true, ack_confirmed: false});

test('automatic size, transaction and idle-age thresholds checkpoint without rewriting legacy data', async t => {
  for (const maintenance of [
    {maxJournalBytes: 1, maxTransactions: 10000, maxAgeMs: 86400000},
    {maxJournalBytes: 1e9, maxTransactions: 2, maxAgeMs: 86400000},
    {maxJournalBytes: 1e9, maxTransactions: 10000, maxAgeMs: 1},
  ]) {
    const {filename, store} = await fixture(t, {maintenance: {enabled: true, ...maintenance}});
    const legacy = await readFile(filename);
    await store.update(data => {data.messages.first = request(1);});
    if (maintenance.maxTransactions === 2) await store.update(data => {data.messages.second = request(2);});
    if (maintenance.maxAgeMs === 1) store.journalStartedAt = Date.now() - 1000;
    await store.maintain();
    await store.compactionPromise;
    assert(store.stats().last_compaction_at > 0);
    assert(store.stats().journal_bytes < store.stats().total_written_bytes);
    assert.deepEqual((await readJournal(filename, () => ({}))).data, store.data);
    assert.deepEqual(await readFile(filename), legacy);
  }
});

test('online snapshot contains only durable facts and preserves writes arriving while worker is busy', async t => {
  const ready = gate(), release = gate();
  const {store, filename} = await fixture(t, {compactionHook: async stage => {
    if (stage === 'snapshot_ready') { ready.resolve(); await release.promise; }
  }});
  await store.update(data => {data.messages.before = request('before');});
  // Mimic an accidental external credential mutation. Workers must read disk,
  // never serialize live memory whose changes have not committed.
  store.data.creds.paired = false;
  const compacting = store.compact();
  await ready.promise;
  await Promise.all(Array.from({length: 40}, (_, index) => store.update(data => {
    data.messages[index] = request(index); data.entries.push({id: index, event: {id: index}});
  })));
  assert.equal(store.data.entries.length, 40, 'writes continue while snapshot is prepared');
  release.resolve(); await compacting;
  const recovered = await readJournal(filename, () => ({}));
  assert.equal(recovered.seq, 41);
  assert.equal(recovered.data.creds.paired, true);
  assert.deepEqual(recovered.data.messages, store.data.messages);
  assert.deepEqual(recovered.data.entries, store.data.entries);
  assert.equal(recovered.data.messages.before.delivery_uncertain, true);
  assert.equal(recovered.data.messages.before.pending_send, true);
  const snapshot = JSON.parse(await readFile(filename + '.snapshot', 'utf8'));
  assert.equal(snapshot.seq, 1);
  assert.equal(Object.keys(snapshot.data.messages).length, 1);
});

test('oversized final catch-up retries outside writer fence, and overlapping compactions share one promise', async t => {
  let copies = 0, store;
  ({store} = await fixture(t, {maintenance: {enabled: false, finalCatchupBytes: 128}, compactionHook: async stage => {
    if (stage === 'suffix_copied' && copies++ === 0) {
      await store.update(data => {data.messages.during = {body: 'synthetic'.repeat(1024)};});
    }
  }}));
  await store.update(data => {data.messages.before = request(1);});
  const first = store.compact(), second = store.compact();
  assert.equal(first, second);
  await first;
  assert(copies >= 2);
  assert.deepEqual((await readJournal(store.filename, () => ({}))).data, store.data);
});

test('one global worker limits memory while other stores keep accepting commits', async t => {
  const entered = gate(), release = gate();
  const {store: first} = await fixture(t, {compactionHook: async stage => {
    if (stage === 'snapshot_ready') {entered.resolve(); await release.promise;}
  }});
  let secondEntered = false;
  const {store: second} = await fixture(t, {compactionHook: stage => {if (stage === 'prefix_replayed') secondEntered = true;}});
  await first.update(data => {data.messages.a = request(1);});
  await second.update(data => {data.messages.a = request(2);});
  const firstTask = first.compact(); await entered.promise;
  const secondTask = second.compact();
  await second.update(data => {data.messages.b = request(3);});
  assert.equal(secondEntered, false);
  release.resolve(); await Promise.all([firstTask, secondTask]);
  assert.equal(secondEntered, true);
});

test('background suffix synchronization permits writes before the bounded final fence', async t => {
  let store, synchronized = 0;
  ({store} = await fixture(t, {compactionHook: async stage => {
    if (stage === 'snapshot_ready') await store.update(data => {data.messages.suffix = request(2);});
    if (stage === 'before_background_journal_sync') {
      await store.update(data => {data.messages.whileSyncing = request(3);});
      synchronized++;
    }
  }}));
  await store.update(data => {data.messages.before = request(1);});
  await store.compact();
  assert(synchronized > 0);
  assert.deepEqual((await readJournal(store.filename, () => ({}))).data, store.data);
  assert.equal(store.seq, 3);
});

test('capacity and prepublication failures preserve authoritative files and permit later writes', async t => {
  for (const scenario of ['space', 'inodes', 'snapshot-size', 'write', 'journal-sync']) {
    const {filename, store} = await fixture(t, {
      maintenance: {enabled: false,
        ...(scenario === 'space' ? {minFreeBytes: Number.MAX_SAFE_INTEGER} : {}),
        ...(scenario === 'inodes' ? {minFreeInodes: Number.MAX_SAFE_INTEGER} : {}),
        ...(scenario === 'snapshot-size' ? {maxSnapshotBytes: 1} : {})},
      compactionHook: stage => {
        if ((scenario === 'write' && stage === 'snapshot_written') ||
            (scenario === 'journal-sync' && stage === 'journal_synced')) throw Object.assign(new Error('ENOSPC'), {code: 'ENOSPC'});
      },
    });
    await store.update(data => {data.messages.before = request(1);});
    const original = await readFile(filename + '.journal');
    await assert.rejects(store.compact(), /STORAGE_LOW_SPACE|STORAGE_LOW_INODES|SNAPSHOT_SIZE_LIMIT|ENOSPC/);
    assert.deepEqual(await readFile(filename + '.journal'), original);
    assert.equal(store.failed, false);
    assert.equal(store.stats().compaction_failures, 1);
    assert(store.stats().next_compaction_retry_at > Date.now());
    await store.update(data => {data.messages.after = request(2);});
    assert.deepEqual((await readJournal(filename, () => ({}))).data, store.data);
  }
});

test('ambiguous publication failures poison the writer and all completed transactions recover', async t => {
  for (const failureStage of ['before_snapshot_rename', 'snapshot_renamed', 'snapshot_directory_synced', 'journal_renamed', 'journal_directory_synced']) {
    const {filename, store} = await fixture(t, {compactionHook: stage => {
      if (stage === failureStage) throw Object.assign(new Error('EIO'), {code: 'EIO'});
    }});
    await store.update(data => {data.messages.durable = request(1);});
    const expected = (await readJournal(filename, () => ({}))).data;
    await assert.rejects(store.compact(), /EIO/);
    await assert.rejects(store.update(data => {data.messages.unsafe = request(2);}), /RESTART_REQUIRED/);
    assert.deepEqual((await readJournal(filename, () => ({}))).data, expected);
  }
});

test('process crashes at every checkpoint stage recover without losing credentials or uncertain sends', async t => {
  const moduleUrl = new URL('../src/journal-store.js', import.meta.url).href;
  const stages = ['prefix_replayed', 'snapshot_written', 'snapshot_synced', 'snapshot_ready',
    'before_background_journal_sync', 'suffix_synced', 'suffix_copied',
    'journal_synced', 'before_snapshot_rename', 'snapshot_renamed', 'snapshot_directory_synced',
    'journal_renamed', 'journal_directory_synced'];
  for (const crashStage of stages) {
    const {filename, store} = await fixture(t);
    await store.update(data => {data.messages.durable = request(crashStage); data.entries.push({id: crashStage});});
    const expected = await readJournal(filename, () => ({}));
    if (stages.indexOf(crashStage) >= stages.indexOf('snapshot_ready')) {
      expected.data.messages.duringCheckpoint = request('suffix');
      expected.data.entries.push({id: 'suffix'});
      expected.seq++;
    }
    const script = `import {JournalStore} from ${JSON.stringify(moduleUrl)};
      const store = await new JournalStore(process.argv[2], () => ({}), {maintenance:{enabled:false},
        compactionHook:async stage=>{
          if(stage==='snapshot_ready')await store.update(data=>{
            data.messages.duringCheckpoint=${JSON.stringify(request('suffix'))};data.entries.push({id:'suffix'});
          });
          if(stage===process.argv[3])process.kill(process.pid,'SIGKILL');
        }}).load();
      await store.compact(); process.exit(9);`;
    const childScript = path.join(path.dirname(filename), 'crash-child.mjs');
    await writeFile(childScript, script, {mode: 0o600});
    const result = await new Promise((resolve, reject) => {
      const child = spawn(process.execPath, [childScript, filename, crashStage], {stdio: ['ignore', 'ignore', 'pipe']});
      let stderr = '';
      child.stderr.on('data', data => {stderr += data;});
      child.once('error', reject);
      child.once('exit', (code, signal) => resolve({code, signal, stderr}));
    });
    assert.equal(result.signal, 'SIGKILL', `${crashStage}: ${result.stderr}`);
    const restored = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
    assert.deepEqual(restored.data, expected.data, crashStage);
    assert.equal(restored.seq, expected.seq, crashStage);
    await restored.update(data => {data.messages.afterRestart = request(2);});
    await restored.compact();
    assert.deepEqual((await readJournal(filename, () => ({}))).data, restored.data);
    assert(!(await readdir(path.dirname(filename))).some(name => name.includes('.checkpoint-')));
    await restored.close();
  }
});

test('repeated growing journal and restart cycles preserve snapshot checksums and contiguous sequences', async t => {
  const {filename, store} = await fixture(t);
  let current = store;
  for (let cycle = 0; cycle < 5; cycle++) {
    for (let index = 0; index < 20; index++) await current.update(data => {
      data.messages.latest = {body: 'synthetic'.repeat(12000), cycle, index, key: Buffer.from([cycle, index])};
    });
    const before = (await stat(filename + '.journal')).size;
    await current.compact();
    assert.equal((await stat(filename + '.journal')).size, 0);
    assert(current.stats().last_compaction_reclaimed_bytes === before);
    const expected = current.data;
    await current.close();
    current = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
    assert.equal(current.seq, (cycle + 1) * 20);
    assert.deepEqual(current.data, expected);
  }
  await current.close();
});

test('snapshot sequence corruption and journal corruption fail closed before reclaiming evidence', async t => {
  const {filename, store} = await fixture(t);
  await store.update(data => {data.messages.durable = request(1);});
  await store.compact();
  const snapshot = JSON.parse(await readFile(filename + '.snapshot', 'utf8'));
  snapshot.seq += 1;
  await writeFile(filename + '.snapshot', JSON.stringify(snapshot));
  await assert.rejects(readJournal(filename, () => ({})), /SNAPSHOT_CHECKSUM_FAILED/);
  snapshot.seq -= 1;
  await writeFile(filename + '.snapshot', JSON.stringify(snapshot));
  await store.update(data => {data.messages.second = request(2);});
  const line = JSON.parse((await readFile(filename + '.journal', 'utf8')).trim());
  line.checksum = '0'.repeat(64);
  await writeFile(filename + '.journal', JSON.stringify(line) + '\n');
  const damaged = await readFile(filename + '.journal');
  await assert.rejects(store.compact(), /JOURNAL_CHECKSUM_FAILED/);
  assert.equal(store.failed, true);
  assert.deepEqual(await readFile(filename + '.journal'), damaged);
});

test('incomplete tails are preserved privately with verified metadata; offline transforms refuse them', async t => {
  const {directory, filename, store} = await fixture(t);
  await store.update(data => {data.messages.durable = request(1);});
  const tail = Buffer.from('{"record":"synthetic-partial-🦦');
  await appendFile(filename + '.journal', tail);
  const before = await readFile(filename + '.journal');
  for (const command of ['compact', 'import', 'export']) {
    await assert.rejects(storageTool(command, directory, directory + '-export'), /INCOMPLETE_JOURNAL_TAIL/);
    assert.deepEqual(await readFile(filename + '.journal'), before);
  }
  const diagnostics = [];
  const recovered = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}, diagnostics: event => diagnostics.push(event)}).load();
  const evidence = (await readdir(directory)).find(name => name.endsWith('.bin'));
  assert(evidence);
  assert.deepEqual(await readFile(path.join(directory, evidence)), tail);
  assert.equal((await stat(path.join(directory, evidence))).mode & 0o777, 0o600);
  const metadata = JSON.parse(await readFile(path.join(directory, evidence + '.json'), 'utf8'));
  assert.equal(metadata.checksum, crypto.createHash('sha256').update(tail).digest('hex'));
  assert.equal(metadata.bytes, tail.length);
  assert(diagnostics.some(event => event.event === 'storage_tail_quarantined'));
  await recovered.compact();
  assert.deepEqual(await readFile(path.join(directory, evidence)), tail, 'compaction must retain quarantined evidence');
  await recovered.close();
  const restarted = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
  assert.equal(restarted.stats().quarantined_tail_bytes, tail.length, 'unreviewed evidence stays observable across restarts');
  await restarted.close();
});

test('unexpected appended bytes during checkpoint are never truncated or silently discarded', async t => {
  let filename;
  const result = await fixture(t, {compactionHook: async stage => {
    if (stage === 'snapshot_ready') await appendFile(filename + '.journal', '{"incomplete":');
  }});
  filename = result.filename;
  await result.store.update(data => {data.messages.durable = request(1);});
  await assert.rejects(result.store.compact(), /JOURNAL_SIZE_CHANGED/);
  assert((await readFile(filename + '.journal', 'utf8')).endsWith('{"incomplete":'));
  assert.equal(result.store.failed, true);
});

test('oversized replay records and partial tails fail closed with configurable limits', async t => {
  const {filename, store} = await fixture(t);
  await store.update(data => {data.messages.large = {body: 'synthetic'.repeat(4096)};});
  const bytes = await readFile(filename + '.journal');
  await assert.rejects(readJournal(filename, () => ({}), {maxRecordBytes: 1024}), /JOURNAL_RECORD_TOO_LARGE/);
  assert.deepEqual((await readJournal(filename, () => ({}), {maxRecordBytes: bytes.length})).data, store.data);
  await appendFile(filename + '.journal', 'x'.repeat(bytes.length * 2));
  const damaged = await readFile(filename + '.journal');
  await assert.rejects(readJournal(filename, () => ({}), {repairTail: true, maxRecordBytes: bytes.length}), /JOURNAL_RECORD_TOO_LARGE/);
  assert.deepEqual(await readFile(filename + '.journal'), damaged, 'oversized evidence must remain intact');
});

test('failed atomic serialization removes only its own unpublished temporary file', async t => {
  const {directory, filename} = await fixture(t);
  const {atomicWrite} = await import('../src/storage.js');
  const before = await readFile(filename);
  const unrelated = path.join(directory, 'unrelated.tmp');
  await writeFile(unrelated, 'keep');
  const circular = {}; circular.self = circular;
  await assert.rejects(atomicWrite(filename, circular));
  assert.deepEqual(await readFile(filename), before);
  assert.deepEqual((await readdir(directory)).filter(name => name.endsWith('.tmp')), ['unrelated.tmp']);
});

test('offline validation honors explicitly configured legacy replay limits', async t => {
  const {directory, store} = await fixture(t);
  await store.update(data => {data.messages.large = {body: 'synthetic'.repeat(1024)};});
  await assert.rejects(storageTool('validate', directory, undefined, {maintenance: {maxReplayRecordBytes: 128}}), /JOURNAL_RECORD_TOO_LARGE/);
  assert.equal((await storageTool('validate', directory, undefined, {maintenance: {maxReplayRecordBytes: 65536}})).stores, 1);
});

test('checksummed sequence gaps fail closed during compaction without removing journal evidence', async t => {
  const {filename, store} = await fixture(t);
  await store.update(data => {data.messages.first = request(1);});
  await store.update(data => {data.messages.second = request(2);});
  const lines = (await readFile(filename + '.journal', 'utf8')).trimEnd().split('\n');
  const envelope = JSON.parse(lines[1]), record = JSON.parse(envelope.record);
  record.seq = 3;
  envelope.record = JSON.stringify(record);
  envelope.checksum = crypto.createHash('sha256').update(envelope.record).digest('hex');
  const damaged = lines[0] + '\n' + JSON.stringify(envelope) + '\n';
  await writeFile(filename + '.journal', damaged);
  await assert.rejects(store.compact(), /JOURNAL_SEQUENCE_GAP/);
  assert.equal(store.failed, true);
  assert.equal(await readFile(filename + '.journal', 'utf8'), damaged);
});

test('disappearing authoritative journals poison the store rather than recreating a sequence gap', async t => {
  const {filename, store} = await fixture(t);
  await store.update(data => {data.messages.durable = request(1);});
  await rm(filename + '.journal');
  await assert.rejects(store.compact(), /JOURNAL_MISSING/);
  await assert.rejects(store.update(data => {data.messages.after = request(2);}), /RESTART_REQUIRED/);
  await assert.rejects(stat(filename + '.journal'), {code: 'ENOENT'});
});

test('continuously growing writes trigger repeated automatic checkpoints and recover every receipt fingerprint', async t => {
  const checkpoints = Array.from({length: 3}, () => ({entered: gate(), release: gate()}));
  let started = 0, transactions = 0;
  const {filename, store} = await fixture(t, {maintenance: {enabled: true, maxJournalBytes: 32768,
    maxTransactions: 10000, maxAgeMs: 86400000}, compactionHook: async stage => {
    if (stage !== 'snapshot_ready') return;
    const checkpoint = checkpoints[started++];
    if (checkpoint) {checkpoint.entered.resolve(); await checkpoint.release.promise;}
  }});
  const fingerprints = {};
  const write = async () => {
    const id = ++transactions, fingerprint = crypto.createHash('sha256').update(String(id)).digest('hex');
    fingerprints[id] = fingerprint;
    await store.update(data => {
      data.messages.latest = {body: 'synthetic'.repeat(1024), id};
      data.messages[id] = {fingerprint, delivery_uncertain: true, ack_confirmed: false};
    });
  };
  for (const checkpoint of checkpoints) {
    for (let index = 0; index < 8; index++) await write();
    await checkpoint.entered.promise;
    // These commit while the corresponding automatic checkpoint is paused.
    for (let index = 0; index < 3; index++) await write();
    const finishing = store.compactionPromise;
    checkpoint.release.resolve();
    await finishing;
  }
  // This is the same threshold/age poll used by the automatic monitor.
  await store.maintain();
  assert(started >= 3);
  assert(store.stats().journal_bytes < 32768);
  assert(store.stats().total_written_bytes > 3 * 32768);
  const restored = await readJournal(filename, () => ({}));
  assert.equal(restored.seq, transactions);
  assert.deepEqual(restored.data, store.data);
  for (const [id, fingerprint] of Object.entries(fingerprints)) {
    assert.equal(restored.data.messages[id].fingerprint, fingerprint);
    assert.equal(restored.data.messages[id].delivery_uncertain, true);
  }
});

test('shadow journal readback detects write corruption before authoritative history is reclaimed', async t => {
  let store, directory;
  ({store, directory} = await fixture(t, {compactionHook: async stage => {
    if (stage === 'snapshot_ready') await store.update(data => {data.messages.suffix = request(2);});
    if (stage === 'before_background_journal_sync') {
      const temporary = (await readdir(directory)).find(name => name.includes('.checkpoint-') && name.endsWith('.journal.tmp'));
      const filename = path.join(directory, temporary);
      const envelope = JSON.parse((await readFile(filename, 'utf8')).trim());
      envelope.checksum = '0'.repeat(64);
      await writeFile(filename, JSON.stringify(envelope) + '\n');
    }
  }}));
  await store.update(data => {data.messages.before = request(1);});
  await assert.rejects(store.compact(), /JOURNAL_CHECKSUM_FAILED/);
  assert.equal(store.failed, true);
  const original = await readJournal(store.filename, () => ({}));
  assert.equal(original.seq, 2);
  assert.deepEqual(original.data, store.data);
  assert.equal((await readFile(store.filename + '.journal', 'utf8')).trim().split('\n').length, 2);
});

test('snapshot byte verification protects seq/data and rejects appended root fields after the checksum', async t => {
  const {filename, store} = await fixture(t);
  await store.update(data => {
    data.messages.durable = request(1);
    data.messages.metadata = {checksum: 'f'.repeat(64), body: ',"checksum":"synthetic", "seq":999'};
  });
  await store.compact();
  const snapshotPath = filename + '.snapshot';
  const original = await readFile(snapshotPath, 'utf8');
  assert.deepEqual((await readSnapshot(snapshotPath)).data, store.data);
  for (const damaged of [
    original.replace('"seq":1,', '"seq":2,'),
    original.replace('"paired":true', '"paired":false'),
    original.slice(0, -1) + ',"seq":999}',
    original.slice(0, -1) + ',"data":{"creds":{"paired":false}}}',
    original.replace(/"checksum":"[0-9a-f]{64}"(?=,"compacted_at":)/, '"checksum":null').replace('"seq":1,', '"seq":999,'),
    original.slice(0, -1) + ',"checksum":null,"seq":999}',
  ]) {
    assert.notEqual(damaged, original);
    await writeFile(snapshotPath, damaged);
    await assert.rejects(readSnapshot(snapshotPath), /SNAPSHOT_CHECKSUM_FAILED/);
  }
  await writeFile(snapshotPath, original);
});

test('snapshot canonical fallback accepts reordered and formatted legacy checksummed documents', async t => {
  const {filename, store} = await fixture(t);
  await store.update(data => {data.messages.durable = request(1);});
  const body = {version: 1, seq: store.seq, data: store.data};
  const checksum = hash(encode(body));
  const documents = [
    encode({...body, checksum}),
    encode({data: body.data, seq: body.seq, version: body.version, checksum}),
    encode({checksum, ...body}),
    JSON.stringify(JSON.parse(encode({...body, checksum})), null, 2) + '\n',
    encode(body),
  ];
  for (const document of documents) {
    await writeFile(filename + '.snapshot', document);
    const recovered = await readSnapshot(filename + '.snapshot');
    assert.equal(recovered.seq, store.seq);
    assert.deepEqual(recovered.data, store.data);
  }
});

test('writer-produced snapshot verification does not reserialize decoded state', async t => {
  const {filename, store} = await fixture(t);
  await store.update(data => {data.messages.large = {body: 'synthetic'.repeat(8192)};});
  await store.compact();
  // A second full JSON.stringify is the measured large-auth startup memory
  // regression. Raw-byte verification of normal snapshots must avoid it.
  t.mock.method(JSON, 'stringify', () => {throw new Error('Unexpected decoded snapshot serialization');});
  const recovered = await readSnapshot(filename + '.snapshot');
  assert.equal(recovered.seq, store.seq);
  assert.deepEqual(recovered.data, store.data);
});

// Intercept the real fs/promises calls in the worker, rather than throwing from
// an after-the-fact stage hook. This exercises handles, partial effects, rename
// ambiguity, error propagation, termination, and parent cleanup together.
function faultWorker(scenario) {
  const preload = `import fs from 'node:fs/promises';
    const scenario = ${JSON.stringify(scenario)};
    const open = fs.open.bind(fs), rename = fs.rename.bind(fs);
    let renamed = 0, tripped = false;
    function fail(code='EIO') { tripped=true; throw Object.assign(new Error(code), {code}); }
    fs.open = async (...args) => {
      const handle = await open(...args), name = String(args[0]);
      const snapshot = name.endsWith('.snapshot.tmp'), journal = name.endsWith('.journal.tmp');
      const originalWrite = handle.writeFile.bind(handle), originalSync = handle.sync.bind(handle);
      handle.writeFile = async (...values) => {
        if (!tripped && ((scenario==='snapshot-write' && snapshot) || (scenario==='journal-write' && journal))) {
          await originalWrite(Buffer.from(String(values[0])).subarray(0, 7)); fail('ENOSPC');
        }
        return originalWrite(...values);
      };
      handle.sync = async () => {
        if (!tripped && ((scenario==='snapshot-sync' && snapshot) || (scenario==='journal-sync' && journal) ||
            (scenario==='snapshot-directory-sync' && renamed===1 && args[1]==='r') ||
            (scenario==='journal-directory-sync' && renamed===2 && args[1]==='r'))) fail();
        await originalSync();
        if (snapshot && scenario==='worker-exit') process.exit(37);
        if (snapshot && scenario==='worker-timeout') await new Promise(() => {});
      };
      return handle;
    };
    fs.rename = async (...args) => {
      if ((scenario==='snapshot-rename' && String(args[0]).endsWith('.snapshot.tmp')) ||
          (scenario==='journal-rename' && String(args[0]).endsWith('.journal.tmp'))) fail();
      const result = await rename(...args); renamed++; return result;
    };`;
  const preloadUrl = 'data:text/javascript,' + encodeURIComponent(preload);
  return (filename, cutoff, maintenance, onStage) => checkpointWorker(filename, cutoff, maintenance, onStage,
    {execArgv: ['--import', preloadUrl]});
}

test('filesystem write, sync, rename and directory-sync failures recover every durable transaction', async t => {
  for (const scenario of ['snapshot-write', 'snapshot-sync', 'journal-write', 'journal-sync',
    'snapshot-rename', 'snapshot-directory-sync', 'journal-rename', 'journal-directory-sync']) {
    let store;
    ({store} = await fixture(t, {workerFactory: faultWorker(scenario), compactionHook: async stage => {
      if (stage === 'snapshot_ready') await store.update(data => {data.messages.suffix = request(2);});
    }}));
    await store.update(data => {data.messages.durable = request(1);});
    await assert.rejects(store.compact(), /ENOSPC|EIO/, scenario);
    const restored = await readJournal(store.filename, () => {throw new Error('Fresh credentials forbidden');});
    assert.deepEqual(restored.data, store.data, scenario);
    assert.equal(restored.seq, store.seq, scenario);
    assert.equal(store.failed, scenario.includes('rename') || scenario.includes('directory-sync'), scenario);
    assert(!(await readdir(path.dirname(store.filename))).some(name => name.includes('.checkpoint-')), scenario);
  }
});

test('repeated actual worker exits and timeouts remove owned temporaries without restarting the store', async t => {
  for (const scenario of ['worker-exit', 'worker-timeout']) {
    const {store, directory, filename} = await fixture(t, {
      workerFactory: faultWorker(scenario), maintenance: {enabled: false, timeoutMs: 2500},
    });
    await store.update(data => {data.messages.durable = request(1);});
    const unrelated = filename + '.checkpoint-not-an-attempt.snapshot.tmp';
    await writeFile(unrelated, 'keep');
    const original = await readFile(filename + '.journal');
    for (let attempt = 1; attempt <= 3; attempt++) {
      await assert.rejects(store.compact(), scenario === 'worker-exit' ? /COMPACTION_WORKER_EXITED/ : /COMPACTION_TIMEOUT/);
      assert.deepEqual((await readdir(directory)).filter(name => name.includes('.checkpoint-')), [path.basename(unrelated)]);
      assert.deepEqual(await readFile(filename + '.journal'), original);
      assert.equal(store.failed, false);
      assert.equal(store.stats().compaction_failures, attempt);
    }
    store.workerFactory = checkpointWorker;
    await store.compact();
    assert.equal(store.stats().journal_bytes, 0);
    assert.equal(await readFile(unrelated, 'utf8'), 'keep');
  }
});

test('shutdown cancels active and queued checkpoints promptly while essential writes still drain', async t => {
  const ready = gate();
  const {store: first} = await fixture(t, {compactionHook: stage => {
    if (stage === 'snapshot_ready') {ready.resolve(); return new Promise(() => {});}
  }});
  const {store: second} = await fixture(t);
  for (const store of [first, second]) await store.update(data => {data.messages.durable = request(1);});
  const firstTask = first.compact(); await ready.promise;
  const secondTask = second.compact();
  const started = performance.now();
  await Promise.all([first.stopMaintenance(), second.stopMaintenance()]);
  assert(performance.now() - started < 5000);
  assert.deepEqual(await Promise.all([firstTask, secondTask]), [false, false]);
  for (const store of [first, second]) {
    assert.equal(store.failed, false);
    await store.update(data => {data.messages.acceptedBeforeShutdown = request(2);});
    assert.deepEqual((await readJournal(store.filename, () => ({}))).data, store.data);
    assert(!(await readdir(path.dirname(store.filename))).some(name => name.includes('.checkpoint-')));
  }
});

test('publication failure fences already queued writes before releasing the journal switch', async t => {
  const ready = gate(), release = gate();
  const {store} = await fixture(t, {compactionHook: async stage => {
    if (stage === 'snapshot_renamed') {ready.resolve(); await release.promise; throw Object.assign(new Error('EIO'), {code: 'EIO'});}
  }});
  await store.update(data => {data.messages.durable = request(1);});
  const compacting = store.compact(); compacting.catch(() => {});
  await ready.promise;
  const queued = store.update(data => {data.messages.unsafe = request(2);}); queued.catch(() => {});
  release.resolve();
  await assert.rejects(compacting, /EIO/);
  await assert.rejects(queued, /STORAGE_RESTART_REQUIRED/);
  assert.equal((await readJournal(store.filename, () => ({}))).data.messages.unsafe, undefined);
});

test('missing bases fail closed before fresh credentials or evidence cleanup; valid snapshots recover alone', async t => {
  for (const artifact of ['.journal', '.journal.tail-1-00000000-0000-0000-0000-000000000000.bin',
    '.checkpoint-00000000-0000-0000-0000-000000000000.snapshot.tmp']) {
    const {filename, directory, store} = await fixture(t);
    await store.close(); await rm(filename);
    await writeFile(filename + artifact, 'existing evidence');
    let initialized = 0;
    const recovered = new JournalStore(filename, () => {initialized++; return {};});
    await assert.rejects(recovered.load(), /MISSING_STORAGE_BASE/);
    assert.equal(initialized, 0);
    await assert.rejects(stat(filename), {code: 'ENOENT'});
    assert.equal(await readFile(filename + artifact, 'utf8'), 'existing evidence');
    await assert.rejects(storageTool('validate', directory), /MISSING_STORAGE_BASE/);
    await assert.rejects(storageTool('export', directory, directory + '-export'), /MISSING_STORAGE_BASE/);
    await assert.rejects(stat(directory + '-export'), {code: 'ENOENT'});
  }
  const {filename, directory, store} = await fixture(t);
  await store.update(data => {data.messages.durable = request(1);});
  await store.compact(); await store.close(); await rm(filename);
  const recovered = await new JournalStore(filename, () => {throw new Error('Fresh credentials forbidden');},
    {maintenance: {enabled: false}}).load();
  assert.deepEqual(recovered.data, store.data);
  await assert.rejects(stat(filename), {code: 'ENOENT'});
  assert.equal((await storageTool('validate', directory)).stores, 1);
  await recovered.close();
});

test('quarantine write, fsync, metadata rename and directory fsync failures retain the authoritative tail', async t => {
  const {filename, store} = await fixture(t);
  await store.update(data => {data.messages.durable = request(1);});
  await appendFile(filename + '.journal', '{"incomplete":');
  const original = await readFile(filename + '.journal');
  for (const scenario of ['write', 'sync', 'rename', 'directory-sync']) {
    const originalOpen = fs.open.bind(fs), originalRename = fs.rename.bind(fs);
    let tripped = false, evidenceOpened = false;
    const fail = () => {tripped = true; throw Object.assign(new Error('ENOSPC'), {code: 'ENOSPC'});};
    const openMock = t.mock.method(fs, 'open', async (...args) => {
      const handle = await originalOpen(...args), name = String(args[0]);
      if (name.includes('.tail-') && name.endsWith('.bin') && args[1] === 'wx') {
        evidenceOpened = true;
        const write = handle.writeFile.bind(handle), sync = handle.sync.bind(handle);
        handle.writeFile = async (...values) => {if (scenario === 'write' && !tripped) fail(); return write(...values);};
        handle.sync = async () => {if (scenario === 'sync' && !tripped) fail(); return sync();};
      }
      if (scenario === 'directory-sync' && evidenceOpened && name === path.dirname(filename)) {
        const sync = handle.sync.bind(handle);
        handle.sync = async () => {if (!tripped) fail(); return sync();};
      }
      return handle;
    });
    const renameMock = t.mock.method(fs, 'rename', async (...args) => {
      if (scenario === 'rename' && String(args[1]).includes('.tail-') && !tripped) fail();
      return originalRename(...args);
    });
    try {
      await assert.rejects(readJournal(filename, () => ({}), {repairTail: true}), /ENOSPC/);
      assert(tripped);
      assert.deepEqual(await readFile(filename + '.journal'), original, scenario);
    } finally {openMock.mock.restore(); renameMock.mock.restore();}
  }
  assert.equal((await readJournal(filename, () => ({}), {repairTail: true})).incompleteTailBytes, 14);
});

test('compaction and recovery counters persist across restarts without making telemetry authoritative', async t => {
  const {filename, store} = await fixture(t, {maintenance: {enabled: false, minFreeBytes: Number.MAX_SAFE_INTEGER}});
  await store.update(data => {data.messages.durable = request(1);});
  await assert.rejects(store.compact(), /STORAGE_LOW_SPACE/);
  await store.close();
  let recovered = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
  assert.equal(recovered.stats().compaction_failures, 1);
  assert.equal(recovered.stats().last_compaction_error, 'STORAGE_LOW_SPACE');
  await recovered.compact(); await recovered.close();
  recovered = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
  assert.equal(recovered.stats().compaction_failures, 1);
  assert.equal(recovered.stats().last_compaction_error, null);
  await recovered.close();
  await writeFile(filename + '.health.json', 'damaged metrics');
  recovered = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
  assert.equal(recovered.failed, false);
  assert.equal(recovered.stats().health_persistence_failed, true);
  await recovered.update(data => {data.messages.after = request(2);});
  await recovered.compact(); await recovered.close();
  recovered = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
  assert.equal(recovered.stats().health_persistence_failed, true, 'lost counter history must stay explicit across restarts');
  assert.deepEqual((await readJournal(filename, () => ({}))).data, recovered.data);
  await recovered.close();
});

test('append syscall failures stop writes and preserve partial or fully written transaction evidence', async t => {
  for (const scenario of ['write', 'sync', 'directory-sync']) {
    const {store, filename, directory} = await fixture(t);
    const originalOpen = fs.open.bind(fs);
    let tripped = false;
    const fail = () => {tripped = true; throw Object.assign(new Error('ENOSPC'), {code: 'ENOSPC'});};
    const mock = t.mock.method(fs, 'open', async (...args) => {
      const handle = await originalOpen(...args), name = String(args[0]);
      if (name === filename + '.journal' && args[1] === 'a') {
        const write = handle.writeFile.bind(handle), sync = handle.sync.bind(handle);
        handle.writeFile = async (...values) => {
          if (!tripped && scenario === 'write') {await write(Buffer.from(values[0]).subarray(0, 13)); fail();}
          return write(...values);
        };
        handle.sync = async () => {if (!tripped && scenario === 'sync') fail(); return sync();};
      }
      if (name === directory && scenario === 'directory-sync') {
        const sync = handle.sync.bind(handle);
        handle.sync = async () => {if (!tripped) fail(); return sync();};
      }
      return handle;
    });
    try {
      await assert.rejects(store.update(data => {data.messages.maybeCommitted = request(1);}), /ENOSPC/);
      assert(tripped);
      assert.equal(store.failed, true);
      await assert.rejects(store.update(data => {data.messages.unsafe = request(2);}), /RESTART_REQUIRED/);
    } finally {mock.mock.restore();}
    const before = await readFile(filename + '.journal');
    const recovered = await readJournal(filename, () => ({}), {repairTail: true});
    if (scenario === 'write') {
      assert.equal(recovered.seq, 0);
      const evidence = (await readdir(directory)).find(name => name.endsWith('.bin'));
      assert.deepEqual(await readFile(path.join(directory, evidence)), before);
    } else {
      assert.equal(recovered.seq, 1, 'complete failed-fsync transaction remains recoverable');
      assert.equal(recovered.data.messages.maybeCommitted.delivery_uncertain, true);
      assert.deepEqual(await readFile(filename + '.journal'), before);
    }
  }
});

test('cleanup retries only the terminated attempt before automatically resuming compaction', async t => {
  let workers = 0;
  const factory = faultWorker('worker-exit');
  const {store, filename, directory} = await fixture(t, {workerFactory: (...args) => {workers++; return factory(...args);}});
  await store.update(data => {data.messages.durable = request(1);});
  const originalUnlink = fs.unlink.bind(fs);
  const mock = t.mock.method(fs, 'unlink', async name => {
    if (String(name).includes('.checkpoint-')) throw Object.assign(new Error('EACCES'), {code: 'EACCES'});
    return originalUnlink(name);
  });
  try {
    await assert.rejects(store.compact(), /EACCES/);
    assert.equal(store.failed, false);
    const leftovers = (await readdir(directory)).filter(name => name.includes('.checkpoint-'));
    assert.equal(leftovers.length, 1);
    await assert.rejects(store.compact(), /EACCES/);
    assert.equal(workers, 1, 'failed cleanup must not allocate another worker');
    assert.equal(await store.maintain(), false);
    assert.deepEqual((await readdir(directory)).filter(name => name.includes('.checkpoint-')), leftovers);
    await store.update(data => {data.messages.essential = request(2);});
  } finally {mock.mock.restore();}
  store.workerFactory = checkpointWorker;
  store.maintenance.enabled = true;
  store.nextCompactionRetryAt = 0;
  assert.equal(await store.maintain(), true, 'same-attempt cleanup and compaction resume automatically');
  assert(!(await readdir(directory)).some(name => name.includes('.checkpoint-')));
  await store.close();
  const recovered = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
  assert.deepEqual(recovered.data, store.data);
  assert(!(await readdir(directory)).some(name => name.includes('.checkpoint-')));
  await recovered.close();
});

test('shutdown allows a publication already in progress to finish and retains all accepted transactions', async t => {
  const ready = gate(), release = gate();
  const {store} = await fixture(t, {compactionHook: async stage => {
    if (stage === 'snapshot_renamed') {ready.resolve(); await release.promise;}
  }});
  await store.update(data => {data.messages.durable = request(1);});
  const compacting = store.compact(); await ready.promise;
  const stopping = store.stopMaintenance();
  const queued = store.update(data => {data.messages.acceptedBeforeShutdown = request(2);});
  release.resolve();
  assert.equal(await compacting, true);
  await Promise.all([stopping, queued]);
  assert.equal(store.failed, false);
  assert.deepEqual((await readJournal(store.filename, () => ({}))).data, store.data);
});

test('a stalled publication stops within the maintenance shutdown budget and recovers committed data', async t => {
  const ready = gate();
  const {store, directory} = await fixture(t, {compactionHook: stage => {
    if (stage === 'snapshot_renamed') {ready.resolve(); return new Promise(() => {});}
  }});
  await store.update(data => {data.messages.durable = request(1);});
  const compacting = store.compact(); compacting.catch(() => {});
  await ready.promise;
  const started = performance.now();
  await store.stopMaintenance();
  assert(performance.now() - started < 8000);
  await assert.rejects(compacting, /COMPACTION_CANCELLED/);
  assert.equal(store.failed, true, 'publication ambiguity must not permit a stale in-process writer');
  assert.deepEqual((await readJournal(store.filename, () => ({}))).data, store.data);
  assert(!(await readdir(directory)).some(name => name.includes('.checkpoint-')));
});

test('failed health-sidecar writes degrade telemetry without undoing checkpoints or blocking essential writes', async t => {
  const {store, filename} = await fixture(t);
  await store.update(data => {data.messages.durable = request(1);});
  const originalRename = fs.rename.bind(fs);
  const mock = t.mock.method(fs, 'rename', async (...args) => {
    if (String(args[1]) === filename + '.health.json') throw Object.assign(new Error('ENOSPC'), {code: 'ENOSPC'});
    return originalRename(...args);
  });
  try {
    assert.equal(await store.compact(), true);
    assert.equal(store.stats().health_persistence_failed, true);
    assert.equal(store.failed, false);
    await store.update(data => {data.messages.essential = request(2);});
  } finally {mock.mock.restore();}
  await store.compact();
  assert.equal(store.stats().health_persistence_failed, false);
  assert.deepEqual((await readJournal(filename, () => ({}))).data, store.data);
});

test('cleanup directory synchronization is retried even after all owned names were already removed', async t => {
  const {store, directory} = await fixture(t, {workerFactory: faultWorker('worker-exit')});
  await store.update(data => {data.messages.durable = request(1);});
  const originalOpen = fs.open.bind(fs);
  let failures = 2, syncs = 0;
  const mock = t.mock.method(fs, 'open', async (...args) => {
    const handle = await originalOpen(...args);
    if (String(args[0]) === directory) {
      const sync = handle.sync.bind(handle);
      handle.sync = async () => {
        syncs++;
        if (failures-- > 0) throw Object.assign(new Error('EIO'), {code: 'EIO'});
        return sync();
      };
    }
    return handle;
  });
  try {
    await assert.rejects(store.compact(), /EIO/);
    assert(!(await readdir(directory)).some(name => name.includes('.checkpoint-')));
    const before = syncs;
    store.workerFactory = checkpointWorker;
    await store.compact();
    assert(syncs > before, 'a removed name does not remove the pending directory-sync obligation');
    assert.equal(store.maintenanceCleanupFailed, false);
  } finally {mock.mock.restore();}
});

test('quarantine recovery counters survive restart and oversized telemetry remains nonauthoritative', async t => {
  const {store, filename} = await fixture(t);
  await store.update(data => {data.messages.durable = request(1);});
  await store.close();
  await appendFile(filename + '.journal', '{');
  let recovered = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
  assert.equal(recovered.stats().recovery_errors, 1);
  await recovered.close();
  recovered = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
  assert.equal(recovered.stats().recovery_errors, 1);
  await recovered.close();
  await writeFile(filename + '.health.json', 'x'.repeat(65536));
  recovered = await new JournalStore(filename, () => ({}), {maintenance: {enabled: false}}).load();
  assert.equal(recovered.stats().health_persistence_failed, true);
  assert.equal(recovered.failed, false);
  assert.deepEqual(recovered.data, store.data);
  await recovered.close();
});

test('offline validation and every transform refuse unresolved authentication intents, failures, or active sessions', async t => {
  for (const suffix of ['.auth-intent', '.auth-failure', '.auth-active']) {
    const {directory, filename, store} = await fixture(t);
    await store.update(data => {data.messages.durable = request(1);});
    const originalBase = await readFile(filename), originalJournal = await readFile(filename + '.journal');
    const lineDirectory = path.join(directory, '90001');
    await mkdir(lineDirectory, {mode: 0o700});
    const authFilename = path.join(lineDirectory, 'auth.json');
    const auth = await new JournalStore(authFilename, () => ({creds: {registered: true}, keys: {}}),
      {maintenance: {enabled: false}}).load();
    await auth.close();
    const marker = authFilename + suffix;
    // Empty/malformed evidence still blocks; never attempt to classify or
    // disclose the potentially sensitive buffered credentials and mutations.
    for (const content of ['', '{private unresolved evidence']) {
      await writeFile(marker, content, {mode: 0o600});
      for (const command of ['validate', 'export', 'compact', 'import']) {
        await assert.rejects(storageTool(command, directory, directory + '-export'), {code: 'AUTH_RECOVERY_REQUIRED'});
        assert.equal(await readFile(marker, 'utf8'), content);
        assert.deepEqual(await readFile(filename), originalBase);
        assert.deepEqual(await readFile(filename + '.journal'), originalJournal);
        await assert.rejects(stat(filename + '.snapshot'), {code: 'ENOENT'});
        await assert.rejects(stat(directory + '-export'), {code: 'ENOENT'});
      }
    }
  }
});
