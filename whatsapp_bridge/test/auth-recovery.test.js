import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {createAuth} from '../src/storage.js';
import {readJournal, decode} from '../src/journal-store.js';
import {assessAuthRecovery} from '../src/auth-recovery.js';
import {inspectPrivateAuthCopy} from '../src/auth-recovery-inspect.js';

const gate = () => { let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve}; };
const read = async file => decode(await fs.readFile(file, 'utf8'));
const digest = async file => crypto.createHash('sha256').update(await fs.readFile(file)).digest('hex');
async function fixture(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'auth-recovery-mock-'));
  const auth = await createAuth(directory);
  await auth.saveCreds();
  await auth.armSession();
  const closed = gate(); auth.trackRecoveryTransport(closed.promise, 5);
  t.after(async () => {
    closed.resolve(); auth.retire(); await auth.drain({timeoutMs: 20}).catch(() => {});
    await auth.assessRecovery(); await auth.store.close().catch(() => {});
    await fs.rm(directory, {recursive: true, force: true});
  });
  return {auth, directory, closed};
}
async function failure(auth) {
  await auth.assessRecovery();
  return read(auth.store.filename + '.auth-failure');
}
async function twoStalled(auth) {
  const entered = [gate(), gate()], release = gate();
  const tasks = entered.map((ready, i) => auth.trackProviderTask(async () => {
    ready.resolve(); await release.promise;
    if (i) throw new Error('synthetic provider rejection');
  }));
  const settled = Promise.allSettled(tasks);
  await Promise.all(entered.map(x => x.promise));
  auth.retire();
  await assert.rejects(auth.drain({timeoutMs: 10}), /AUTH_TRANSACTION_DRAIN_TIMEOUT/);
  return {release, settled};
}

for (const fenced of [false, true]) test(`repeated live assessment preserves every auth file (${fenced ? 'fenced' : 'active'})`, async t => {
  const {auth, directory, closed} = await fixture(t);
  if (fenced) {
    const {release, settled} = await twoStalled(auth);
    release.resolve(); await settled; closed.resolve(); auth.recoveryEventsReleased();
    await auth.assessRecovery(); // Wait for already accepted evidence writes.
  }
  const inventory = async () => {
    const result = {};
    for (const name of (await fs.readdir(directory)).sort()) {
      const file = path.join(directory, name), stat = await fs.stat(file);
      if (stat.isFile()) result[name] = {hash: await digest(file), mode: stat.mode, mtime: stat.mtimeMs};
    }
    return result;
  };
  const before = await inventory(), sequence = auth.store.seq, failed = auth.store.failed;
  const traps = [t.mock.method(auth, 'renew', () => {throw new Error('Unexpected renewal');}),
    t.mock.method(auth, 'saveCreds', () => {throw new Error('Unexpected credential write');}),
    t.mock.method(auth.store, 'flush', () => {throw new Error('Unexpected flush');})];
  try {
    const results = await Promise.all(Array.from({length: 8}, () => auth.assessRecovery()));
    assert.ok(results.every(result => result.recovery_allowed === false));
    assert.deepEqual(await inventory(), before);
    assert.equal(auth.store.seq, sequence); assert.equal(auth.store.failed, failed);
  } finally {for (const trap of traps) trap.mock.restore();}
});

test('two stalled operations retain unresolved and final outcomes without unlocking authentication', async t => {
  const {auth, directory, closed} = await fixture(t);
  const {release, settled} = await twoStalled(auth);
  try {
    let evidence = await failure(auth);
    assert.equal(evidence.recovery.pending_operations, 2);
    assert.deepEqual(evidence.recovery.operations.map(x => x.provider_outcome), ['unresolved', 'unresolved']);
    assert.equal(evidence.recovery.settled, false);
    assert.equal((await inspectPrivateAuthCopy(directory)).authentication_evidence_complete, false);
    release.resolve(); await settled;
    evidence = await failure(auth);
    assert.equal(evidence.recovery.settled, true);
    assert.deepEqual(evidence.recovery.operations.map(x => x.provider_outcome), ['completed', 'rejected']);
    assert.deepEqual(evidence.recovery.operations.map(x => x.wrapper_outcome), ['rejected', 'rejected']);
    assert.equal((await inspectPrivateAuthCopy(directory)).authentication_evidence_complete, false, 'settled tasks are not transport release');
    closed.resolve(); auth.recoveryEventsReleased(); await auth.assessRecovery();
    const assessment = await inspectPrivateAuthCopy(directory);
    assert.equal(assessment.status, 'OPERATOR_REVIEW_REQUIRED');
    assert.equal(assessment.authentication_evidence_complete, true);
    assert.equal(assessment.recovery_allowed, false, 'complete evidence cannot clear a fence or authorize a send');
    const live = await auth.assessRecovery();
    assert.equal(live.authentication_evidence_complete, false, 'live summary never substitutes for journal validation');
    assert.deepEqual(live.blockers, ['OFFLINE_JOURNAL_VALIDATION_REQUIRED']);
    assert.equal(live.evidence_reference, assessment.evidence_reference, 'operator must correlate the same owner epoch and evidence revision');
    assert.equal((await fs.stat(auth.store.filename + '.auth-failure')).mode & 0o777, 0o600);
    assert.equal(auth.store.failed, true);
    await assert.rejects(auth.renew(), /STORAGE_RESTART_REQUIRED/);
    await assert.rejects(createAuth(directory), /AUTH_RECOVERY_REQUIRED/);
    const output = JSON.stringify(assessment);
    for (const word of ['noiseKey', 'advSecretKey', 'credentials', 'owner_id']) assert.equal(output.includes(word), false);
  } finally { release.resolve(); await settled; }
});

test('late rejected keys after timeout remain private evidence and block reconciliation', async t => {
  const {auth, directory, closed} = await fixture(t);
  const entered = gate(), release = gate();
  const task = auth.trackProviderTask(async () => {
    entered.resolve(); await release.promise;
    await auth.state.keys.set({session: {private_late_key: Buffer.from('synthetic-secret')}});
  });
  const settled = Promise.allSettled([task]); await entered.promise;
  auth.retire(); await assert.rejects(auth.drain({timeoutMs: 10}), /AUTH_TRANSACTION_DRAIN_TIMEOUT/);
  release.resolve(); await settled; closed.resolve(); auth.recoveryEventsReleased();
  const evidence = await failure(auth);
  assert.ok(evidence.failures.some(x => x.operation === 'rejected_keys_after_failure' && x.mutations[0].session.private_late_key));
  const assessment = await inspectPrivateAuthCopy(directory);
  assert.ok(assessment.blockers.includes('AUTH_MUTATIONS_REQUIRE_RECONCILIATION'));
  assert.equal(JSON.stringify(assessment).includes('synthetic-secret'), false);
  assert.equal(auth.store.data.keys.session, undefined);
});

test('final credentials changed after timeout are retained but never silently committed', async t => {
  const {auth, directory, closed} = await fixture(t);
  const entered = gate(), release = gate();
  const original = auth.store.data.creds.nextPreKeyId;
  const task = auth.trackProviderTask(async () => {
    entered.resolve(); await release.promise;
    auth.state.creds.nextPreKeyId = original + 100;
  });
  const settled = Promise.allSettled([task]); await entered.promise;
  auth.retire(); await assert.rejects(auth.drain({timeoutMs: 10}), /AUTH_TRANSACTION_DRAIN_TIMEOUT/);
  release.resolve(); await settled; closed.resolve(); auth.recoveryEventsReleased();
  const evidence = await failure(auth);
  assert.equal(evidence.recovery.credentials.nextPreKeyId, original + 100);
  assert.equal(auth.store.data.creds.nextPreKeyId, original);
  const result = await inspectPrivateAuthCopy(directory);
  assert.ok(result.blockers.includes('FINAL_CREDENTIALS_MISMATCH'));
  assert.equal(result.recovery_allowed, false);
});

test('interrupted credential fsync cannot qualify even if complete journal bytes exist', async t => {
  const {auth, directory, closed} = await fixture(t);
  const append = auth.store.append.bind(auth.store);
  auth.store.append = async bytes => { await append(bytes); throw Object.assign(new Error('synthetic fsync uncertainty'), {code: 'EIO'}); };
  auth.state.creds.nextPreKeyId += 10;
  await assert.rejects(auth.saveCreds(), error => error.bridgePersistenceFailure);
  auth.retire(); closed.resolve(); auth.recoveryEventsReleased(); await auth.assessRecovery();
  const journal = await readJournal(auth.store.filename, () => {throw new Error('missing');});
  assert.equal(journal.incompleteTailBytes, 0);
  const result = await inspectPrivateAuthCopy(directory);
  assert.ok(result.blockers.includes('AUTH_MUTATIONS_REQUIRE_RECONCILIATION'));
  assert.equal(result.authentication_evidence_complete, false);
});

test('legacy and incomplete evidence always block; final equality cannot replace operation accounting', async t => {
  const {auth, directory, closed} = await fixture(t);
  const {release, settled} = await twoStalled(auth);
  release.resolve(); await settled; closed.resolve(); auth.recoveryEventsReleased();
  const saved = await failure(auth);
  const input = {failure: saved, journal: await readJournal(auth.store.filename, () => ({})),
    active: await read(auth.store.filename + '.auth-active'), intents: await read(auth.store.filename + '.auth-intent')};
  assert.equal(assessAuthRecovery(input).authentication_evidence_complete, true);
  for (const mutate of [
    f => {f.version = 1; delete f.recovery;},
    f => {delete f.recovery.credentials;},
    f => {f.recovery.operations.pop();},
    f => {f.recovery.operations[0].wrapper_outcome = 'unresolved';},
    f => {f.recovery.events_released = false;},
    f => {f.recovery.transport_state = 'unconfirmed';},
    f => {f.recovery.evidence_write_failed = true;},
    f => {f.recovery.owner_id = 'other-owner';},
  ]) {
    const candidate = structuredClone(saved); mutate(candidate);
    const result = assessAuthRecovery({...input, failure: candidate});
    assert.equal(result.authentication_evidence_complete, false);
    assert.equal(result.recovery_allowed, false);
  }
  const original = await fs.readFile(auth.store.filename + '.auth-failure');
  await fs.writeFile(auth.store.filename + '.auth-failure', '{"truncated":');
  await assert.rejects(inspectPrivateAuthCopy(directory));
  await fs.writeFile(auth.store.filename + '.auth-failure', original);
});

test('concurrent assessments and recovery attempts cannot create a second owner or alter evidence', async t => {
  const {auth, directory, closed} = await fixture(t);
  const {release, settled} = await twoStalled(auth);
  release.resolve(); await settled; closed.resolve(); auth.recoveryEventsReleased(); await auth.assessRecovery();
  const files = ['.journal', '.auth-failure', '.auth-intent', '.auth-active'].map(s => auth.store.filename + s);
  const before = await Promise.all(files.map(digest));
  const results = await Promise.all(Array.from({length: 4}, () => inspectPrivateAuthCopy(directory)));
  assert.ok(results.every(x => x.status === 'OPERATOR_REVIEW_REQUIRED' && x.recovery_allowed === false));
  assert.deepEqual(await Promise.all(files.map(digest)), before);
  const owners = await Promise.allSettled([auth.renew(), auth.renew(), createAuth(directory)]);
  assert.ok(owners.every(x => x.status === 'rejected'));
  assert.equal(auth.store.failed, true);
});

test('private-copy inspection refuses permissive directories and symlinked evidence', async t => {
  const {auth, directory} = await fixture(t);
  await fs.chmod(directory, 0o755);
  await assert.rejects(inspectPrivateAuthCopy(directory), /PRIVATE_EVIDENCE_REQUIRED/);
  await fs.chmod(directory, 0o700);
  await fs.symlink(auth.store.filename + '.auth-active', auth.store.filename + '.auth-failure');
  await assert.rejects(inspectPrivateAuthCopy(directory));
  await fs.unlink(auth.store.filename + '.auth-failure');
});

test('a failed evidence write cannot be hidden by a later queued successful snapshot', async t => {
  const {auth, directory, closed} = await fixture(t);
  const rename = fs.rename;
  let failed = false;
  const mock = t.mock.method(fs, 'rename', async (source, destination) => {
    if (!failed && String(destination).endsWith('.auth-failure')) {
      failed = true; throw Object.assign(new Error('synthetic evidence fsync failure'), {code: 'EIO'});
    }
    return rename(source, destination);
  });
  try {
    const {release, settled} = await twoStalled(auth);
    release.resolve(); await settled; closed.resolve(); auth.recoveryEventsReleased();
    const evidence = await failure(auth);
    assert.equal(evidence.recovery.evidence_write_failed, true);
    assert.ok((await inspectPrivateAuthCopy(directory)).blockers.includes('EVIDENCE_DURABILITY_UNCONFIRMED'));
    assert.ok((await auth.assessRecovery()).blockers.includes('EVIDENCE_DURABILITY_UNCONFIRMED'));
  } finally {mock.mock.restore();}
});

test('incomplete journal tail and checksum corruption cannot qualify for operator review', async t => {
  const {auth, directory, closed} = await fixture(t);
  const {release, settled} = await twoStalled(auth);
  release.resolve(); await settled; closed.resolve(); auth.recoveryEventsReleased(); await auth.assessRecovery();
  const file = auth.store.filename + '.journal', bytes = await fs.readFile(file);
  try {
    await fs.appendFile(file, '{"partial":');
    const result = await inspectPrivateAuthCopy(directory);
    assert.ok(result.blockers.includes('JOURNAL_STATE_MISMATCH'));
    assert.equal((await fs.readFile(file)).length, bytes.length + 11, 'inspection never repairs a tail');
    await fs.writeFile(file, bytes);
    const corrupted = Buffer.from(bytes); corrupted[corrupted.indexOf('checksum') + 12] ^= 1;
    await fs.writeFile(file, corrupted);
    await assert.rejects(inspectPrivateAuthCopy(directory));
  } finally {await fs.writeFile(file, bytes);}
});

test('read-only observation remains bounded when private evidence I/O stalls', async t => {
  const {auth} = await fixture(t);
  const rename = fs.rename, release = gate(), entered = gate();
  const mock = t.mock.method(fs, 'rename', async (source, destination) => {
    if (String(destination).endsWith('.auth-failure')) {entered.resolve(); await release.promise;}
    return rename(source, destination);
  });
  try {
    auth.store.maxPending = 0;
    await assert.rejects(auth.saveCreds(), /STORAGE_QUEUE_FULL/);
    await entered.promise;
    const result = await auth.assessRecovery({timeoutMs: 10});
    assert.equal(result.status, 'BLOCKED');
    assert.ok(result.blockers.includes('EVIDENCE_OBSERVATION_TIMEOUT'));
    assert.equal(result.recovery_allowed, false);
  } finally {release.resolve(); await auth.assessRecovery(); mock.mock.restore();}
});

test('coalesced failure writes retain final recovery changes without requiring another failure', async t => {
  const {auth, closed} = await fixture(t);
  const rename = fs.rename, entered = gate(), release = gate();
  let writes = 0;
  const mock = t.mock.method(fs, 'rename', async (source, destination) => {
    if (String(destination).endsWith('.auth-failure')) {
      writes++;
      if (writes === 1) {entered.resolve(); await release.promise;}
    }
    return rename(source, destination);
  });
  const original = auth.state.creds.nextPreKeyId;
  try {
    auth.store.failed = true;
    await assert.rejects(auth.saveCreds(), /STORAGE_RESTART_REQUIRED/);
    await entered.promise;
    auth.state.creds.nextPreKeyId = original + 100;
    auth.retire(); closed.resolve(); auth.recoveryEventsReleased();
    const pending = await auth.assessRecovery({timeoutMs: 10});
    assert.equal(pending.recovery_allowed, false);
    assert.ok(pending.blockers.includes('EVIDENCE_DURABILITY_UNCONFIRMED'));
    assert.equal(writes, 1);
    release.resolve();
    let evidence = await failure(auth);
    assert.equal(writes, 2);
    assert.equal(evidence.version, 2);
    assert.equal(evidence.failures.length, 1);
    assert.equal(evidence.failures[0].creds.nextPreKeyId, original, 'initial failure evidence is immutable');
    assert.equal(evidence.recovery.credentials.nextPreKeyId, original + 100);
    assert.equal(evidence.recovery.retired, true);
    assert.equal(evidence.recovery.transport_state, 'closed');
    assert.equal(evidence.recovery.events_released, true);
    assert.equal(evidence.recovery.pending_operations, 0);
    assert.equal(evidence.recovery.settled, true);
    const revision = evidence.recovery.revision;
    auth.state.creds.nextPreKeyId = original + 200;
    auth.recoveryEventsReleased();
    evidence = await failure(auth);
    assert.equal(writes, 3, 'a new revision after the writer finishes must start another write');
    assert.ok(evidence.recovery.revision > revision);
    assert.equal(evidence.recovery.credentials.nextPreKeyId, original + 200);
    assert.equal(evidence.failures[0].creds.nextPreKeyId, original);
    assert.equal(auth.store.data.creds.nextPreKeyId, original, 'evidence is never committed as authentication');
  } finally {release.resolve(); await auth.assessRecovery(); mock.mock.restore();}
});
