import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile, stat } from 'node:fs/promises';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createAuth } from '../src/storage.js';
import { readJournal } from '../src/journal-store.js';
import { addTransactionCapability, BufferJSON } from '@whiskeysockets/baileys';
import pino from 'pino';
import { spawn } from 'node:child_process';
import { performance } from 'node:perf_hooks';
import { makeMutex } from '@whiskeysockets/baileys/lib/Utils/make-mutex.js';

const gate = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const retired = error => error.code === 'AUTH_SESSION_RETIRED' && !error.bridgePersistenceFailure;
const canonical = value => JSON.parse(JSON.stringify(value, BufferJSON.replacer), BufferJSON.reviver);

async function fixture(t) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-auth-owner-'));
  const auth = await createAuth(directory);
  t.after(async () => { auth.retire(); await auth.drain().catch(() => {}); await auth.store.close().catch(() => {}); await rm(directory, { recursive: true, force: true }); });
  return { directory, auth };
}

test('provider credentials and key objects never alias committed auth state', async t => {
  const { auth } = await fixture(t);
  auth.state.creds.registered = true;
  auth.state.creds.noiseKey.private[0] ^= 255;
  assert.equal(auth.store.data.creds.registered, false);
  assert.notDeepEqual(auth.state.creds.noiseKey.private, auth.store.data.creds.noiseKey.private);
  await auth.saveCreds();
  assert.equal(auth.store.data.creds.registered, true);
  const key = { bytes: Buffer.from([1, 2, 3]) };
  await auth.state.keys.set({ session: { first: key, second: key, third: key } });
  key.bytes[0] = 9;
  const returned = await auth.state.keys.get('session', ['first']);
  assert.equal(returned.first.bytes[0], 1);
  returned.first.bytes[0] = 8;
  assert.equal(auth.store.data.keys.session.first.bytes[0], 1);
  await auth.state.keys.set({ session: { second: undefined, third: null } });
  assert.deepEqual(Object.keys(auth.store.data.keys.session), ['first']);
  assert.deepEqual((await readJournal(auth.store.filename, () => ({}))).data, canonical(auth.store.data));
});

test('renewal drains admitted writes and permanently fences every old auth method', async t => {
  const { auth } = await fixture(t);
  const entered = gate(), release = gate();
  const append = auth.store.append;
  auth.store.append = async bytes => { entered.resolve(); await release.promise; return append(bytes); };
  const writing = auth.state.keys.set({ session: { admitted: Buffer.from([7]) } });
  await entered.promise;
  let finished = false;
  const renewing = auth.renew().then(value => { finished = true; return value; });
  try {
    await assert.rejects(auth.state.keys.set({ session: { late: Buffer.from([9]) } }), retired);
    await assert.rejects(auth.state.keys.get('session', ['admitted']), retired);
    await assert.rejects(auth.saveCreds(), retired);
    assert.equal(finished, false, 'replacement must wait for the accepted key write');
  } finally { release.resolve(); }
  await writing;
  const replacement = await renewing;
  assert.equal(replacement.store, auth.store);
  assert.deepEqual(await replacement.state.keys.get('session', ['admitted']), { admitted: Buffer.from([7]) });
  await replacement.state.keys.set({ session: { next: Buffer.from([8]) } });
  await assert.rejects(auth.renew(), retired);
  await assert.rejects(auth.reset(), retired);
  assert.deepEqual((await readJournal(auth.store.filename, () => ({}))).data, canonical(replacement.store.data));
});

test('queued credential saves capture their admitted value before later provider mutation', async t => {
  const { auth } = await fixture(t);
  const entered = gate(), release = gate();
  const append = auth.store.append;
  auth.store.append = async bytes => { entered.resolve(); await release.promise; return append(bytes); };
  const first = auth.state.keys.set({ session: { earlier: Buffer.from([1]) } });
  await entered.promise;
  auth.state.creds.registered = true;
  const saving = auth.saveCreds();
  auth.state.creds.registered = false;
  release.resolve();
  await Promise.all([first, saving]);
  assert.equal(auth.store.data.creds.registered, true);
  assert.equal((await readJournal(auth.store.filename, () => ({}))).data.creds.registered, true);
});

test('reset drains old key writes and old callbacks cannot restore reset credentials', async t => {
  const { auth } = await fixture(t);
  auth.state.creds.registered = true;
  await auth.saveCreds();
  const previousIdentity = Buffer.from(auth.store.data.creds.signedIdentityKey.public);
  const entered = gate(), release = gate();
  const append = auth.store.append;
  auth.store.append = async bytes => { entered.resolve(); await release.promise; return append(bytes); };
  const writing = auth.state.keys.set({ session: { beforeReset: Buffer.from([1]) } });
  await entered.promise;
  const resetting = auth.reset();
  try { await assert.rejects(auth.state.keys.set({ session: { late: Buffer.from([2]) } }), retired); }
  finally { release.resolve(); }
  await writing;
  const replacement = await resetting;
  assert.equal(replacement.store, auth.store);
  assert.equal(replacement.state.creds.registered, false);
  assert.deepEqual(replacement.store.data.keys, {});
  assert.notDeepEqual(replacement.state.creds.signedIdentityKey.public, previousIdentity);
  await assert.rejects(auth.saveCreds(), retired);
  assert.deepEqual((await readJournal(auth.store.filename, () => ({}))).data, canonical(replacement.store.data));
});

test('factory reuse of an existing store transfers its facade ownership', async t => {
  const { directory, auth } = await fixture(t);
  await auth.state.keys.set({ session: { saved: Buffer.from([3]) } });
  const replacement = await createAuth(directory, { store: auth.store });
  assert.equal(replacement.store, auth.store);
  await assert.rejects(auth.state.keys.set({ session: { stale: Buffer.from([4]) } }), retired);
  assert.deepEqual(await replacement.state.keys.get('session', ['saved']), { saved: Buffer.from([3]) });
});

test('an uncertain auth append prevents replacement of its failed store', async t => {
  const { auth } = await fixture(t);
  const append = auth.store.append;
  auth.store.append = async bytes => { await append(bytes); throw Object.assign(new Error('synthetic fsync outcome'), { code: 'EIO' }); };
  await assert.rejects(auth.state.keys.set({ session: { uncertain: Buffer.from([5]) } }), error => error.bridgePersistenceFailure);
  await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
  await assert.rejects(auth.state.keys.set({ session: { late: Buffer.from([6]) } }), retired);
  assert.deepEqual((await readJournal(auth.store.filename, () => ({}))).data.keys.session.uncertain, Buffer.from([5]));
});

test('a rejected admitted credential transaction poisons the owner before concurrent renewal can finish', async t => {
  const { auth } = await fixture(t);
  const entered = gate(), release = gate();
  auth.store.maxRecordBytes = 512;
  const append = auth.store.append;
  auth.store.append = async bytes => { entered.resolve(); await release.promise; return append(bytes); };
  const writing = assert.rejects(auth.state.keys.set({ session: { admitted: Buffer.from([1]) } }), error => error.bridgePersistenceFailure);
  await entered.promise;
  auth.state.creds.registered = true;
  const saved = assert.rejects(auth.saveCreds(), error => error.message === 'STORAGE_TRANSACTION_TOO_LARGE' && error.bridgePersistenceFailure);
  const renewed = assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
  release.resolve();
  await Promise.all([writing, saved, renewed]);
  assert.equal(auth.store.failed, true);
  assert.equal(auth.store.data.creds.registered, false);
  assert.deepEqual(auth.store.data.keys.session.admitted, Buffer.from([1]));
});

test('credential queue rejection prevents immediate renewal of stale saved credentials', async t => {
  const { auth } = await fixture(t);
  auth.store.maxPending = 0;
  auth.state.creds.registered = true;
  const saved = assert.rejects(auth.saveCreds(), /STORAGE_QUEUE_FULL/);
  const renewed = assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
  await Promise.all([saved, renewed]);
  assert.equal(auth.store.failed, true);
  assert.equal(auth.store.data.creds.registered, false);
});

function providerKeys(auth, options = {}) {
  const keys = addTransactionCapability(auth.state.keys, pino({ level: 'silent' }), {
    maxCommitRetries: 3, delayBetweenTriesMs: 1, ...options,
  });
  auth.trackProviderKeys(keys);
  return keys;
}

test('rejected Signal-key queue and record writes poison auth and retain private recovery evidence across restart', async t => {
  for (const limit of ['queue', 'record']) {
    const { directory, auth } = await fixture(t);
    if (limit === 'queue') auth.store.maxPending = 0;
    else auth.store.maxRecordBytes = 10;
    const updates = { session: { rejected: Buffer.from([4, 5]) } };
    await assert.rejects(auth.state.keys.set(updates), error => error.bridgePersistenceFailure);
    await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
    const marker = auth.store.filename + '.auth-failure';
    const evidence = JSON.parse(await readFile(marker, 'utf8'), BufferJSON.reviver);
    assert.deepEqual(evidence.failures[0].mutations[0], updates);
    assert.equal((await stat(marker)).mode & 0o777, 0o600);
    assert.equal(auth.store.data.keys.session, undefined);
    await assert.rejects(createAuth(directory), error => error.code === 'AUTH_RECOVERY_REQUIRED');
  }
});

test('renewal drains the installed Baileys wrapper while Signal-key mutations remain buffered', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), buffered = gate(), release = gate();
  const transaction = keys.transaction(async () => {
    await keys.set({ session: { buffered: Buffer.from([8]) } });
    buffered.resolve();
    await release.promise;
    await keys.transaction(async () => keys.set({ session: { nested: Buffer.from([9]) } }), 'nested');
    auth.state.creds.nextPreKeyId = 77;
  }, 'peer');
  await buffered.promise;
  assert.equal(auth.store.data.keys.session, undefined, 'Baileys has not committed its buffer yet');
  let renewed = false;
  const replacement = auth.renew().then(value => { renewed = true; return value; });
  try {
    await assert.rejects(keys.transaction(async () => {}, 'late'), retired);
    await assert.rejects(keys.set({ session: { late: Buffer.from([1]) } }), retired);
    assert.equal(renewed, false);
  } finally { release.resolve(); }
  await transaction;
  const next = await replacement;
  assert.deepEqual(await next.state.keys.get('session', ['buffered', 'nested']), {
    buffered: Buffer.from([8]), nested: Buffer.from([9]),
  });
  assert.equal(next.state.creds.nextPreKeyId, 77, 'retired event subscriptions cannot discard credentials changed by an admitted transaction');
  await assert.rejects(keys.get('session', ['buffered']), retired);
});

test('installed Baileys commit retries cannot race renewal after a rejected key commit', async t => {
  const { auth } = await fixture(t);
  const originalSet = auth.state.keys.set;
  const rejected = gate(), retry = gate();
  let attempts = 0;
  auth.state.keys.set = async updates => {
    attempts++;
    if (attempts === 2) { retry.resolve(); await rejected.promise; }
    return originalSet(updates);
  };
  const keys = providerKeys(auth);
  auth.store.maxPending = 0;
  const transaction = assert.rejects(keys.transaction(async () => {
    await keys.set({ session: { unsaved: Buffer.from([3]) } });
  }, 'peer'), error => error.bridgePersistenceFailure);
  await retry.promise;
  let finished = false;
  const renewing = assert.rejects(auth.renew().finally(() => { finished = true; }), /STORAGE_RESTART_REQUIRED/);
  try { assert.equal(finished, false, 'renewal must include provider retry delays'); }
  finally { rejected.resolve(); }
  await Promise.all([transaction, renewing]);
  assert.equal(attempts, 3, 'exercise the installed three-attempt commit loop');
  assert.equal(auth.store.data.keys.session, undefined);
  assert.equal(auth.store.failed, true);
});

test('a provider transaction abort after buffered mutations fences restart without committing partial keys', async t => {
  const { directory, auth } = await fixture(t);
  const keys = providerKeys(auth);
  await assert.rejects(keys.transaction(async () => {
    await keys.set({ session: { buffered: Buffer.from([7]) } });
    throw new Error('Synthetic failure after provider work');
  }, 'peer'), /Synthetic failure/);
  await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
  assert.equal(auth.store.data.keys.session, undefined);
  const evidence = JSON.parse(await readFile(auth.store.filename + '.auth-failure', 'utf8'), BufferJSON.reviver);
  assert.deepEqual(evidence.failures[0].mutations[0].session.buffered, Buffer.from([7]));
  await assert.rejects(createAuth(directory), /AUTH_RECOVERY_REQUIRED/);
});

test('bounded auth drain fences a hung provider transaction and its detached continuation', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), entered = gate(), release = gate();
  const transaction = assert.rejects(keys.transaction(async () => {
    await keys.set({ session: { buffered: Buffer.from([6]) } });
    entered.resolve(); await release.promise;
  }, 'peer'), error => error.code === 'AUTH_SESSION_RETIRED' || error.bridgePersistenceFailure);
  await entered.promise;
  auth.retire();
  try { await assert.rejects(auth.drain({ timeoutMs: 10 }), /AUTH_TRANSACTION_DRAIN_TIMEOUT/); }
  finally { release.resolve(); }
  await transaction;
  await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
  assert.equal(auth.store.data.keys.session, undefined);
});

test('concurrent rejected provider transactions retain distinct recovery evidence without overwriting earlier failures', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), ready = [gate(), gate()], release = gate();
  const rejected = [0, 1].map(index => assert.rejects(keys.transaction(async () => {
    await keys.set({ session: { ['peer' + index]: Buffer.from([index]) } });
    ready[index].resolve(); await release.promise;
  }, 'peer' + index), error => error.bridgePersistenceFailure));
  await Promise.all(ready.map(item => item.promise));
  auth.store.maxPending = 0;
  release.resolve();
  await Promise.all(rejected);
  await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
  const evidence = JSON.parse(await readFile(auth.store.filename + '.auth-failure', 'utf8'), BufferJSON.reviver);
  const keysSaved = evidence.failures.flatMap(item => item.mutations ?? []).flatMap(item => Object.keys(item.session ?? {}));
  assert.deepEqual(new Set(keysSaved), new Set(['peer0', 'peer1']));
});

test('failure to persist the auth recovery fence remains fail closed and emits a distinct actionable error', async t => {
  const { auth } = await fixture(t);
  const errors = [];
  auth.onFailure = error => errors.push(error);
  const open = fs.open;
  const mocked = t.mock.method(fs, 'open', async (filename, ...args) => {
    if (String(filename).includes('.auth-failure.')) throw Object.assign(new Error('Synthetic full disk'), { code: 'ENOSPC' });
    return open(filename, ...args);
  });
  auth.store.maxPending = 0;
  try {
    await assert.rejects(auth.state.keys.set({ session: { rejected: Buffer.from([1]) } }), error => error.bridgePersistenceFailure);
    await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
    assert.equal(auth.store.failed, true);
    assert.ok(errors.some(error => error.code === 'AUTH_FAILURE_FENCE_UNWRITABLE'));
    assert.ok(errors.some(error => error.authFencePersistenceFailed === true));
    await assert.rejects(auth.state.keys.get('session', ['rejected']), error => error.code === 'AUTH_SESSION_RETIRED');
  } finally { mocked.mock.restore(); }
});

test('provider work cannot start before its intent is durable, and successful commits remove their own intent', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth);
  await keys.transaction(async () => {
    const intent = JSON.parse(await readFile(auth.store.filename + '.auth-intent', 'utf8'), BufferJSON.reviver);
    assert.equal(intent.operations.length, 1);
    assert.equal((await stat(auth.store.filename + '.auth-intent')).mode & 0o777, 0o600);
    await keys.set({ session: { saved: Buffer.from([4]), deleted: undefined } });
    const buffered = JSON.parse(await readFile(auth.store.filename + '.auth-intent', 'utf8'), BufferJSON.reviver);
    assert.equal(buffered.operations[0].mutations[0].session.deleted, null, 'recovery evidence preserves key deletions');
    auth.state.creds.nextPreKeyId = 88;
  }, 'peer');
  assert.equal((await readJournal(auth.store.filename, () => ({}))).data.creds.nextPreKeyId, 88);
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
  await auth.renew();
});

test('a failed intent write prevents all provider work and fails the authentication owner closed', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), open = fs.open;
  let invoked = false;
  const mocked = t.mock.method(fs, 'open', async (filename, ...args) => {
    if (String(filename).includes('.auth-intent.')) throw Object.assign(new Error('Synthetic intent disk exhaustion'), { code: 'ENOSPC' });
    return open(filename, ...args);
  });
  try {
    await assert.rejects(keys.transaction(async () => { invoked = true; }, 'peer'), { code: 'ENOSPC' });
    assert.equal(invoked, false);
    assert.equal(auth.store.seq, 0);
    await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
  } finally { mocked.mock.restore(); }
});

test('SIGKILL before provider work or after simulated wire effects leaves an explicit recovery fence', async t => {
  for (const stage of ['before_work', 'after_wire']) {
    const { directory, auth } = await fixture(t);
    const script = `
      import { createAuth } from ${JSON.stringify(new URL('../src/storage.js', import.meta.url).href)};
      import { addTransactionCapability } from '@whiskeysockets/baileys';
      import pino from 'pino';
      const auth = await createAuth(process.argv[1]);
      const keys = addTransactionCapability(auth.state.keys, pino({level:'silent'}), {maxCommitRetries:2,delayBetweenTriesMs:1});
      auth.trackProviderKeys(keys);
      await keys.transaction(async () => {
        if (process.argv[2] === 'before_work') process.kill(process.pid, 'SIGKILL');
        auth.state.creds.nextPreKeyId = 91;
        await keys.set({session:{wireKey:Buffer.from([6,7])}});
        // This point models an acknowledged stanza while keys remain buffered.
        process.kill(process.pid, 'SIGKILL');
      }, 'peer');
    `;
    const child = spawn(process.execPath, ['--input-type=module', '-e', script, directory, stage], {
      cwd: new URL('..', import.meta.url), stdio: ['ignore', 'ignore', 'pipe'],
    });
    let stderr = '';
    child.stderr.on('data', data => { stderr += data; });
    const exit = await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.once('exit', (code, signal) => resolve({ code, signal }));
    });
    assert.equal(exit.signal, 'SIGKILL', stderr);
    const intent = JSON.parse(await readFile(auth.store.filename + '.auth-intent', 'utf8'), BufferJSON.reviver);
    assert.equal(intent.operations.length, 1);
    if (stage === 'after_wire') {
      assert.deepEqual(intent.operations[0].mutations[0].session.wireKey, Buffer.from([6, 7]));
      assert.equal(intent.operations[0].creds.nextPreKeyId, 91);
    }
    assert.equal((await readJournal(auth.store.filename, () => ({}))).data.keys.session, undefined);
    await assert.rejects(createAuth(directory), /AUTH_RECOVERY_REQUIRED/);
  }
});

test('concurrent successful transactions clear only their own intent after key and credential durability', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), buffered = gate(), release = gate();
  const second = keys.transaction(async () => {
    await keys.set({ session: { second: Buffer.from([2]) } });
    buffered.resolve(); await release.promise;
  }, 'second');
  await buffered.promise;
  try {
    await keys.transaction(async () => keys.set({ session: { first: Buffer.from([1]) } }), 'first');
    const intent = JSON.parse(await readFile(auth.store.filename + '.auth-intent', 'utf8'), BufferJSON.reviver);
    assert.equal(intent.operations.length, 1);
    assert.ok(intent.operations[0].mutations.some(value => value.session.second));
    assert.equal(auth.store.data.keys.session.second, undefined);
  } finally { release.resolve(); }
  await second;
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
  assert.deepEqual(auth.store.data.keys.session, { first: Buffer.from([1]), second: Buffer.from([2]) });
});

test('normal provider query failures with unchanged authentication state clear their intent without poisoning renewal', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth);
  await assert.rejects(keys.transaction(async () => {
    await keys.get('session', ['not-found']);
    throw Object.assign(new Error('Synthetic provider timeout'), { code: 'ETIMEDOUT' });
  }, 'peer'), { code: 'ETIMEDOUT' });
  assert.equal(auth.store.failed, false);
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
  await assert.rejects(readFile(auth.store.filename + '.auth-failure'), { code: 'ENOENT' });
  await auth.renew();
});

test('repeated real provider transactions keep bounded key state and remove all completed intent evidence', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), before = process.memoryUsage(), started = performance.now();
  let peakRss = before.rss, peakHeap = before.heapUsed;
  for (let index = 0; index < 100; index++) {
    await keys.transaction(async () => {
      await keys.set({ session: { rotating: Buffer.from(String(index)) } });
      auth.state.creds.nextPreKeyId = index + 2;
    }, 'peer');
    const usage = process.memoryUsage();
    peakRss = Math.max(peakRss, usage.rss); peakHeap = Math.max(peakHeap, usage.heapUsed);
    await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
  }
  const after = process.memoryUsage();
  assert.deepEqual(Object.keys(auth.store.data.keys.session), ['rotating']);
  assert.equal(auth.store.data.creds.nextPreKeyId, 101);
  assert.equal(auth.store.data.keys.session.rotating.toString(), '99');
  assert.equal(auth.store.failed, false);
  await assert.rejects(readFile(auth.store.filename + '.auth-failure'), { code: 'ENOENT' });
  t.diagnostic(JSON.stringify({ provider_transactions: 100, duration_ms: Math.round(performance.now() - started),
    rss_before: before.rss, rss_after: after.rss, rss_peak: peakRss, heap_before: before.heapUsed,
    heap_after: after.heapUsed, heap_peak: peakHeap, journal_bytes: auth.store.journalBytes }));
});

test('a provider that catches a buffered key intent rename failure cannot clear the fence or renew stale keys', async t => {
  const { directory, auth } = await fixture(t);
  const keys = providerKeys(auth), rename = fs.rename;
  let writes = 0, caught = false;
  const mocked = t.mock.method(fs, 'rename', async (source, destination) => {
    if (String(destination).endsWith('.auth-intent') && ++writes === 2) {
      throw Object.assign(new Error('Synthetic intent publication failure'), { code: 'EIO' });
    }
    return rename(source, destination);
  });
  try {
    await assert.rejects(keys.transaction(async () => {
      try { await keys.set({ session: { mustRetain: Buffer.from([9]) } }); }
      catch (error) { assert.equal(error.code, 'EIO'); caught = true; }
    }, 'peer'), /STORAGE_RESTART_REQUIRED/);
    assert.equal(caught, true);
    assert.equal(auth.store.failed, true);
    assert.equal(auth.store.data.keys.session, undefined);
    await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
    const evidence = JSON.parse(await readFile(auth.store.filename + '.auth-failure', 'utf8'), BufferJSON.reviver);
    assert.ok(evidence.failures.some(item => item.mutations?.some(value => value.session?.mustRetain)));
    await assert.rejects(createAuth(directory), /AUTH_RECOVERY_REQUIRED/);
  } finally { mocked.mock.restore(); }
});

test('three queued real provider direct sets retain their own auth context across retirement and renewal', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), firstEntered = gate(), firstRelease = gate(), secondEntered = gate(), secondRelease = gate();
  const append = auth.store.append;
  let writes = 0;
  auth.store.append = async bytes => {
    if (++writes === 1) { firstEntered.resolve(); await firstRelease.promise; }
    else if (writes === 2) { secondEntered.resolve(); await secondRelease.promise; }
    return append(bytes);
  };
  const first = keys.set({ session: { first: Buffer.from([1]) } });
  await firstEntered.promise;
  const second = keys.set({ session: { second: Buffer.from([2]) } });
  const third = keys.set({ session: { third: Buffer.from([3]) } });
  let renewed = false;
  const renewal = auth.renew().then(value => { renewed = true; return value; });
  try {
    firstRelease.resolve(); await secondEntered.promise; await first;
    assert.equal(renewed, false);
    secondRelease.resolve();
    await Promise.all([second, third]);
    const next = await renewal;
    assert.deepEqual(next.store.data.keys.session, { first: Buffer.from([1]), second: Buffer.from([2]), third: Buffer.from([3]) });
    await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
    assert.equal(auth.store.failed, false);
  } finally { firstRelease.resolve(); secondRelease.resolve(); }
});

test('active-session fence exists before provider use and clears only after a clean retirement drain', async t => {
  const { directory, auth } = await fixture(t);
  await auth.armSession();
  assert.equal((await stat(auth.store.filename + '.auth-active')).mode & 0o777, 0o600);
  await assert.rejects(createAuth(directory), /AUTH_RECOVERY_REQUIRED/);
  await auth.state.keys.set({ session: { saved: Buffer.from([1]) } });
  const next = await auth.renew();
  await assert.rejects(readFile(auth.store.filename + '.auth-active'), { code: 'ENOENT' });
  await next.armSession();
  next.retire(); await next.drain();
  await assert.rejects(readFile(auth.store.filename + '.auth-active'), { code: 'ENOENT' });
  assert.deepEqual((await readJournal(auth.store.filename, () => ({}))).data.keys.session.saved, Buffer.from([1]));
});

test('an unrecordable standalone credential failure followed by SIGKILL cannot silently reconnect stale credentials', async t => {
  const { directory, auth } = await fixture(t);
  const script = `
    import { createAuth } from ${JSON.stringify(new URL('../src/storage.js', import.meta.url).href)};
    import fs from 'node:fs/promises';
    const auth = await createAuth(process.argv[1]);
    await auth.armSession();
    const open = fs.open;
    fs.open = async (filename, ...args) => {
      if(String(filename).includes('.auth-failure.')) throw Object.assign(new Error('Synthetic disk exhaustion'), {code:'ENOSPC'});
      return open(filename,...args);
    };
    // Model a standalone credential update after an acknowledged server action,
    // with no encompassing Signal transaction and no writable failure sidecar.
    auth.state.creds.nextPreKeyId = 99;
    auth.store.maxPending = 0;
    await auth.saveCreds().catch(() => {});
    await auth.drain().catch(() => {});
    process.kill(process.pid, 'SIGKILL');
  `;
  const child = spawn(process.execPath, ['--input-type=module', '-e', script, directory], {
    cwd: new URL('..', import.meta.url), stdio: ['ignore', 'ignore', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', data => { stderr += data; });
  const signal = await new Promise((resolve, reject) => {
    child.once('error', reject); child.once('exit', (_code, signal) => resolve(signal));
  });
  assert.equal(signal, 'SIGKILL', stderr);
  await assert.rejects(readFile(auth.store.filename + '.auth-failure'), { code: 'ENOENT' });
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
  assert.equal((await stat(auth.store.filename + '.auth-active')).isFile(), true);
  assert.notEqual((await readJournal(auth.store.filename, () => ({}))).data.creds.nextPreKeyId, 99);
  await assert.rejects(createAuth(directory), /AUTH_RECOVERY_REQUIRED/);
});

test('renewal drains a real provider mutex pipeline including delayed credentials after its key transaction returns', async t => {
  const { auth } = await fixture(t);
  await auth.armSession();
  const keys = providerKeys(auth), socket = { messageMutex: makeMutex() }, keyDone = gate(), release = gate();
  auth.trackProviderPipelines(socket);
  const processing = socket.messageMutex.mutex(async () => {
    await keys.transaction(async () => keys.set({ session: { saved: Buffer.from([8]) } }), 'peer');
    keyDone.resolve(); await release.promise;
    auth.state.creds.myAppStateKeyId = 'delayed-key-id';
  });
  await keyDone.promise;
  let renewed = false;
  const replacing = auth.renew().then(value => { renewed = true; return value; });
  try {
    await assert.rejects(socket.messageMutex.mutex(async () => {}), { code: 'AUTH_SESSION_RETIRED' });
    assert.equal(renewed, false);
  } finally { release.resolve(); }
  await processing;
  const replacement = await replacing;
  assert.equal(replacement.state.creds.myAppStateKeyId, 'delayed-key-id');
  assert.equal((await readJournal(auth.store.filename, () => ({}))).data.creds.myAppStateKeyId, 'delayed-key-id');
  await assert.rejects(readFile(auth.store.filename + '.auth-active'), { code: 'ENOENT' });
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
});

test('a provider pipeline cannot hide an aborted nested transaction with buffered authentication mutations', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), socket = { messageMutex: makeMutex() };
  auth.trackProviderPipelines(socket);
  await assert.rejects(socket.messageMutex.mutex(async () => {
    try {
      await keys.transaction(async () => {
        await keys.set({ session: { buffered: Buffer.from([4]) } });
        throw new Error('Synthetic nested transaction abort');
      }, 'peer');
    } catch { /* Provider receive handlers can catch cryptographic failures. */ }
  }), /STORAGE_RESTART_REQUIRED/);
  assert.equal(auth.store.failed, true);
  assert.equal(auth.store.data.keys.session, undefined);
  await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
});

test('renewal drains independently tracked native child work started by an already accepted callback', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), parentEntered = gate(), startChild = gate(), childEntered = gate(), finishChild = gate();
  let child;
  const parent = auth.trackProviderTask(async () => {
    parentEntered.resolve(); await startChild.promise;
    child = auth.trackProviderTask(async () => {
      childEntered.resolve(); await finishChild.promise;
      await keys.transaction(async () => {
        await keys.set({ session: { detached: Buffer.from([9]) } });
      }, 'peer');
      auth.state.creds.myAppStateKeyId = 'detached-provider-follow-up';
    });
  });
  await parentEntered.promise;
  let renewed = false;
  const replacing = auth.renew().then(value => { renewed = true; return value; });
  try {
    startChild.resolve(); await childEntered.promise; await parent;
    assert.equal(renewed, false, 'a snapshot of only the original pending parent is insufficient');
    assert.equal(auth.hasProviderWork(), true);
  } finally { startChild.resolve(); finishChild.resolve(); }
  await child;
  const replacement = await replacing;
  assert.equal(replacement.state.creds.myAppStateKeyId, 'detached-provider-follow-up');
  assert.deepEqual(replacement.store.data.keys.session.detached, Buffer.from([9]));
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
});

test('retirement admits a scheduled native index flush once before closing new task admission', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), finishFlush = gate(), entered = gate();
  let flush, retireCalls = 0;
  auth.beforeRetire = () => {
    retireCalls++;
    flush = auth.trackProviderTask(async () => {
      entered.resolve(); await finishFlush.promise;
      await keys.set({ tctoken: { index: Buffer.from([3]) } });
    });
  };
  const renewal = auth.renew();
  auth.retire(); await entered.promise;
  assert.equal(retireCalls, 1);
  finishFlush.resolve(); await flush;
  const replacement = await renewal;
  assert.deepEqual(replacement.store.data.keys.tctoken.index, Buffer.from([3]));
});

test('detached native work escapes the installed provider transaction after its parent commits', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), entered = gate(), finish = gate();
  assert.equal(typeof keys.bridgeRunOutsideTransaction, 'function');
  let child;
  await keys.transaction(async () => {
    await keys.set({ session: { parent: Buffer.from([1]) } });
    child = auth.trackProviderDetachedTask(async () => {
      entered.resolve(); await finish.promise;
      assert.equal(keys.isInTransaction(), false, 'detached task cannot inherit a completed Signal mutation buffer');
      await keys.set({ session: { detached_after_commit: Buffer.from([2]) } });
    });
    await entered.promise;
  }, 'peer');
  assert.equal(auth.store.data.keys.session.detached_after_commit, undefined);
  const replacement = auth.renew();
  finish.resolve(); await child;
  const renewed = await replacement;
  assert.deepEqual(renewed.store.data.keys.session.detached_after_commit, Buffer.from([2]));
  assert.deepEqual((await readJournal(auth.store.filename, () => ({}))).data.keys.session.detached_after_commit, Buffer.from([2]));
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
});

test('an awaited native child cannot erase buffered mutations when its parent provider transaction aborts', async t => {
  const { directory, auth } = await fixture(t);
  const keys = providerKeys(auth);
  await assert.rejects(keys.transaction(async () => {
    await auth.trackProviderTask(async () => {
      assert.equal(keys.isInTransaction(), true, 'ordinary awaited work retains transaction atomicity');
      await keys.set({ session: { nested_buffered: Buffer.from([4]) } });
    });
    assert.equal(auth.store.data.keys.session, undefined, 'the parent has not committed its mutation buffer');
    throw new Error('Synthetic parent transaction abort');
  }, 'peer'), /Synthetic parent transaction abort/);
  await auth.drain().catch(() => {});
  assert.equal(auth.store.failed, true);
  const retained = JSON.parse(await readFile(auth.store.filename + '.auth-failure', 'utf8'), BufferJSON.reviver);
  assert.ok(retained.failures.some(failure => failure.mutations?.some(update => update.session?.nested_buffered)));
  await assert.rejects(createAuth(directory), { code: 'AUTH_RECOVERY_REQUIRED' });
});

test('already admitted callbacks retain later rejected keys and credentials after another auth failure', async t => {
  const { auth } = await fixture(t);
  const keys = providerKeys(auth), entered = gate(), release = gate();
  const late = auth.trackProviderTask(async () => {
    entered.resolve(); await release.promise;
    await assert.rejects(keys.set({ session: { after_failure: Buffer.from([8]) } }), /STORAGE_RESTART_REQUIRED/);
    auth.state.creds.myAppStateKeyId = 'known-after-earlier-failure';
    await assert.rejects(auth.saveCreds(), /STORAGE_RESTART_REQUIRED/);
  });
  late.catch(() => {});
  await entered.promise;
  const append = auth.store.append;
  auth.store.append = async () => { throw Object.assign(new Error('Synthetic initial authentication failure'), { code: 'EIO' }); };
  try {
    await assert.rejects(keys.set({ session: { first_failure: Buffer.from([7]) } }), { code: 'EIO' });
    release.resolve(); await assert.rejects(late, /STORAGE_RESTART_REQUIRED/);
    await auth.drain().catch(() => {});
    const retained = JSON.parse(await readFile(auth.store.filename + '.auth-failure', 'utf8'), BufferJSON.reviver);
    assert.ok(retained.failures.some(failure => failure.mutations?.some(update => update.session?.after_failure)));
    assert.ok(retained.failures.some(failure => failure.creds?.myAppStateKeyId === 'known-after-earlier-failure'));
    await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
  } finally { release.resolve(); auth.store.append = append; }
});

test('a backlog of rejected writes after an auth failure stays cheap, bounded and keeps the latest value per key', async t => {
  const { directory, auth } = await fixture(t);
  const errors = [];
  auth.onFailure = error => errors.push(error);
  auth.store.maxPending = 0;
  await assert.rejects(auth.state.keys.set({ session: { first: Buffer.alloc(16, 1) } }), error => error.bridgePersistenceFailure);
  // Production evidence: an offline backlog kept calling set() on a failed
  // store; every call deep-cloned and rewrote all earlier evidence (MBs).
  const peers = 20, rounds = 400, value = round => Buffer.alloc(20_000, round % 251);
  const started = performance.now();
  let maxBlockedMs = 0;
  for (let round = 0; round < rounds; round++) {
    const before = performance.now();
    await assert.rejects(auth.state.keys.set({ session: { ['peer' + (round % peers)]: value(round) } }), /STORAGE_RESTART_REQUIRED/);
    auth.state.creds.myAppStateKeyId = 'round-' + round;
    await assert.rejects(auth.saveCreds(), /STORAGE_RESTART_REQUIRED/);
    maxBlockedMs = Math.max(maxBlockedMs, performance.now() - before);
  }
  await auth.drain().catch(() => {});
  const elapsed = performance.now() - started;
  assert.ok(elapsed < 5_000, `rejected backlog took ${Math.round(elapsed)} ms`);
  assert.ok(maxBlockedMs < 250, `one rejected write blocked ${Math.round(maxBlockedMs)} ms`);
  const file = auth.store.filename + '.auth-failure';
  assert.ok((await stat(file)).size < peers * 20_000 * 3, 'evidence keeps one value per rejected key');
  const evidence = JSON.parse(await readFile(file, 'utf8'), BufferJSON.reviver);
  assert.deepEqual(evidence.failures[0].mutations[0].session.first, Buffer.alloc(16, 1));
  const merged = evidence.failures.find(failure => failure.operation === 'rejected_after_failure');
  assert.equal(merged.rejections, rounds * 2);
  assert.deepEqual(merged.mutations[0].session['peer' + ((rounds - 1) % peers)], value(rounds - 1));
  assert.equal(merged.creds.myAppStateKeyId, 'round-' + (rounds - 1));
  assert.equal(errors.filter(error => error.code === 'AUTH_FAILURE_FENCE_UNWRITABLE').length, 0);
  await assert.rejects(createAuth(directory), /AUTH_RECOVERY_REQUIRED/);
});

test('a burst of queued provider pipelines shares durable intent writes instead of rewriting every intent per job', async t => {
  const { auth } = await fixture(t);
  const open = fs.open;
  let intentWrites = 0;
  t.mock.method(fs, 'open', async (filename, ...args) => {
    if (String(filename).includes('.auth-intent.')) intentWrites++;
    return open(filename, ...args);
  });
  const release = gate(), jobs = 300;
  const started = [];
  // Baileys enqueues every offline message into its mutex at once; each job's
  // intent must be durable before it runs, but they need not be written alone.
  const tasks = Array.from({ length: jobs }, (_, index) => auth.trackProviderTask(async () => {
    started.push(index);
    await release.promise;
    return index;
  }));
  while (started.length < jobs) await new Promise(resolve => setImmediate(resolve));
  const intents = JSON.parse(await readFile(auth.store.filename + '.auth-intent', 'utf8'), BufferJSON.reviver);
  assert.equal(intents.operations.length, jobs, 'every running job is fenced by a durable intent');
  release.resolve();
  assert.deepEqual(await Promise.all(tasks), [...Array(jobs).keys()]);
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
  assert.ok(intentWrites < 20, `${intentWrites} intent rewrites for ${jobs} queued jobs`);
  assert.equal(auth.store.failed, false);
});

test('a queued provider mutex backlog holds one intent at a time and retirement drops jobs that never started', async t => {
  const { auth } = await fixture(t);
  await auth.armSession();
  const keys = providerKeys(auth), socket = { messageMutex: makeMutex() }, release = gate(), entered = gate();
  auth.trackProviderPipelines(socket);
  const open = fs.open;
  let intentWrites = 0;
  t.mock.method(fs, 'open', async (filename, ...args) => {
    if (String(filename).includes('.auth-intent.')) intentWrites++;
    return open(filename, ...args);
  });
  // Production: 3,074 queued jobs each held an intent from enqueue, and every
  // completion rewrote all remaining ones.
  const first = socket.messageMutex.mutex(async () => {
    await keys.transaction(async () => keys.set({ session: { first: Buffer.from([1]) } }), 'peer');
    entered.resolve(); await release.promise;
  });
  let ran = 0;
  const queued = Array.from({ length: 300 }, (_, index) => socket.messageMutex.mutex(async () => {
    ran++;
    await keys.transaction(async () => keys.set({ session: { ['queued' + index]: Buffer.from([index % 251]) } }), 'peer');
  }));
  queued.forEach(job => job.catch(() => {}));
  await entered.promise;
  const intents = JSON.parse(await readFile(auth.store.filename + '.auth-intent', 'utf8'), BufferJSON.reviver);
  assert.equal(intents.operations.length, 1, 'only the running job is fenced');
  // Let part of the backlog run, then retire mid-backlog.
  release.resolve(); await first;
  while (ran < 50) await new Promise(resolve => setImmediate(resolve));
  auth.retire();
  const started = performance.now();
  await auth.drain();
  assert.ok(performance.now() - started < 2_000, 'drain waits only for the running job');
  const outcomes = await Promise.allSettled(queued);
  const rejected = outcomes.filter(outcome => outcome.status === 'rejected');
  assert.ok(rejected.length > 0 && rejected.every(outcome => outcome.reason.code === 'AUTH_SESSION_RETIRED'));
  assert.equal(auth.store.failed, false);
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
  await assert.rejects(readFile(auth.store.filename + '.auth-active'), { code: 'ENOENT' });
  // Start, mutation and clear: a constant number of small writes per executed job.
  assert.ok(intentWrites <= ran * 4 + 4, `${intentWrites} intent writes for ${ran} executed jobs`);
});

async function resumeFixture(t, options = {}) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-auth-resume-'));
  const events = [];
  const auth = await createAuth(directory, { resume: true, onRecovery: event => events.push(event), ...options });
  t.after(async () => { auth.retire(); await auth.drain().catch(() => {}); await auth.store.close().catch(() => {}); await rm(directory, { recursive: true, force: true }); });
  return { directory, auth, events };
}

test('resume mode: an initial-sync burst writes no intents and, if still running at the deadline, is abandoned without blocking', async t => {
  const { auth, events } = await resumeFixture(t, { drainTimeoutMs: 300 });
  await auth.armSession();
  const release = gate(), jobs = 14_000;
  // Production 2026-11-09: 14,032 provider tasks started within 5 s of pairing.
  const tasks = Array.from({ length: jobs }, () => auth.trackProviderTask(async () => { await release.promise; }));
  tasks.forEach(task => task.catch(() => {}));
  await new Promise(resolve => setImmediate(resolve));
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
  const started = performance.now();
  const next = await auth.renew();
  assert.ok(performance.now() - started < 3_000, 'retirement ends at the deadline');
  assert.equal(auth.store.failed, false);
  assert.ok(events.some(event => event.event === 'auth_work_abandoned' && event.reason === 'drain_timeout' && event.archived_intents === 0));
  release.resolve();
  const outcomes = await Promise.allSettled(tasks);
  assert.ok(outcomes.every(outcome => outcome.status === 'fulfilled' || outcome.reason.code === 'AUTH_SESSION_RETIRED'));
  assert.equal(auth.store.failed, false);
  await next.state.keys.set({ session: { after: Buffer.from([1]) } });
  assert.deepEqual(next.store.data.keys.session.after, Buffer.from([1]));
});

test('resume mode: a provider transaction stuck past the deadline is abandoned with evidence, not failed', async t => {
  const { auth, events } = await resumeFixture(t);
  await auth.armSession();
  const keys = providerKeys(auth), entered = gate(), never = gate();
  const stuck = keys.transaction(async () => {
    await keys.set({ session: { buffered: Buffer.from([6]) } });
    entered.resolve(); await never.promise;
  }, 'peer');
  stuck.catch(() => {});
  await entered.promise;
  auth.retire();
  await auth.drain({ timeoutMs: 50 });
  assert.equal(auth.store.failed, false);
  await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
  await assert.rejects(readFile(auth.store.filename + '.auth-failure'), { code: 'ENOENT' });
  await assert.rejects(readFile(auth.store.filename + '.auth-active'), { code: 'ENOENT' });
  const archived = (await fs.readdir(path.dirname(auth.store.filename))).filter(name => name.startsWith('auth.json.auth-intent.abandoned-'));
  assert.equal(archived.length, 1);
  const evidence = JSON.parse(await readFile(path.join(path.dirname(auth.store.filename), archived[0]), 'utf8'), BufferJSON.reviver);
  assert.deepEqual(evidence.operations[0].mutations[0].session.buffered, Buffer.from([6]));
  assert.ok(events.some(event => event.event === 'auth_work_abandoned' && event.reason === 'drain_timeout' && event.archived_intents === 1));
  assert.equal(auth.store.data.keys.session, undefined, 'the uncommitted change is discarded');
  const next = await createAuth(path.dirname(auth.store.filename), { store: auth.store, resume: true });
  never.resolve();
  await assert.rejects(stuck);
  assert.equal(auth.store.failed, false, 'the abandoned task cannot poison the store when it finally returns');
  await next.state.keys.set({ session: { resumed: Buffer.from([2]) } });
  assert.deepEqual(next.store.data.keys.session.resumed, Buffer.from([2]));
});
