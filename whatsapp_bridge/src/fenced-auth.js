// Retention is deliberately weaker than authentication recovery: changed keys
// may be preserved offline, but may NEVER be handed to a provider by this path.
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import {isDeepStrictEqual} from 'node:util';
import {decode, readJournal} from './journal-store.js';

const blocked = code => Object.assign(new Error(code), {code, bridgePersistenceFailure: true});
const sources = new Set(['get', 'set', 'transaction', 'pipeline', 'messageMutex', 'notificationMutex',
  'receiptMutex', 'appStatePatchMutex', 'bridgeTask', 'bridgeDetachedTask']);

export function assessAuthRetention({failure, active, intents, journal}) {
  const blockers = new Set();
  const r = failure?.version === 2 && failure.recovery?.version === 1 ? failure.recovery : null;
  if (!r || !Array.isArray(failure?.failures) || !failure.failures.length ||
      !Number.isSafeInteger(r.revision) || r.revision < 1 || !Number.isSafeInteger(r.connection_generation) ||
      r.connection_generation < 1 || !r.credentials || typeof r.credentials !== 'object') blockers.add('FINAL_EVIDENCE_MISSING');
  if (active?.version !== 1 || !active?.id || active.id !== r?.owner_id) blockers.add('OWNER_EVIDENCE_MISMATCH');
  if (r?.retired !== true || r?.transport_state !== 'closed' || r?.events_released !== true) blockers.add('TRANSPORT_RELEASE_UNCONFIRMED');
  if (r?.settled !== true || r?.pending_operations !== 0 || r?.pending_credential_writes !== 0 ||
      r?.pending_journal_writes !== 0) blockers.add('WORK_NOT_SETTLED');
  if (r?.evidence_write_failed !== false) blockers.add('EVIDENCE_DURABILITY_UNCONFIRMED');
  if (!journal || journal.incompleteTailBytes !== 0 || !Number.isSafeInteger(r?.sequence) || r.sequence !== journal.seq) blockers.add('JOURNAL_STATE_MISMATCH');
  const operations = Array.isArray(r?.operations) ? r.operations : [];
  const ids = new Set();
  if (!Array.isArray(r?.operations)) blockers.add('OPERATION_ACCOUNTING_INCOMPLETE');
  for (const op of operations) {
    if (typeof op?.id !== 'string' || !op.id || ids.has(op.id) || !sources.has(op.source) ||
        !['completed', 'rejected', 'not_started'].includes(op.provider_outcome) ||
        !['completed', 'rejected'].includes(op.wrapper_outcome) ||
        !Number.isSafeInteger(op.sequence_at_start) || !Number.isSafeInteger(op.sequence_at_end) ||
        op.sequence_at_start < 0 || op.sequence_at_end < op.sequence_at_start || op.sequence_at_end > r.sequence ||
        !Number.isSafeInteger(op.mutation_batches) || op.mutation_batches < 0 || typeof op.credentials_changed !== 'boolean') {
      blockers.add('OPERATION_ACCOUNTING_INCOMPLETE');
    }
    ids.add(op?.id);
  }
  const retained = intents?.operations;
  if (intents?.version !== 1 || !Array.isArray(retained) || !Array.isArray(r?.intent_ids) || new Set(r.intent_ids).size !== r.intent_ids.length ||
      retained.length !== r?.intent_ids?.length ||
      new Set(retained?.map(op => op?.id)).size !== retained?.length ||
      retained?.some(op => !ids.has(op?.id) || !r.intent_ids.includes(op.id) || !Array.isArray(op.mutations))) {
    blockers.add('OPERATION_ACCOUNTING_INCOMPLETE');
  }
  for (const entry of Array.isArray(failure?.failures) ? failure.failures : []) if (entry?.intent_id && !ids.has(entry.intent_id)) blockers.add('OPERATION_ACCOUNTING_INCOMPLETE');
  return {state: blockers.size ? 'fenced_unresolved' : 'fenced_retained', isolation_safe: blockers.size === 0,
    recovery_allowed: false, blockers: [...blockers], historical_intents: Array.isArray(retained) ? retained.length : null};
}

async function inventory(directory) {
  const dir = await fs.lstat(directory);
  if (!dir.isDirectory() || dir.isSymbolicLink() || (dir.mode & 0o077) || dir.uid !== process.getuid()) throw blocked('PRIVATE_EVIDENCE_REQUIRED');
  const result = {};
  for (const name of (await fs.readdir(directory)).filter(name =>
    ['auth.json', 'state.json'].some(base => name === base || name.startsWith(base + '.'))).sort()) {
    const handle = await fs.open(path.join(directory, name), constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const before = await handle.stat();
      if (!before.isFile() || (before.mode & 0o077) || before.uid !== process.getuid()) throw blocked('PRIVATE_EVIDENCE_REQUIRED');
      const hash = crypto.createHash('sha256');
      for await (const chunk of handle.createReadStream({autoClose: false})) hash.update(chunk);
      const after = await handle.stat();
      if (before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs) throw blocked('AUTH_EVIDENCE_CHANGED');
      result[name] = {hash: hash.digest('hex'), size: after.size, inode: after.ino, mtime: after.mtimeMs, ctime: after.ctimeMs};
    } finally { await handle.close(); }
  }
  return result;
}

async function retainedStats(directory, name, journal, files, failed) {
  let health = {}, invalid = false;
  if (files[name + '.health.json']) {
    try {
      if (files[name + '.health.json'].size > 4096) throw new Error('INVALID_HEALTH');
      health = JSON.parse(await fs.readFile(path.join(directory, name + '.health.json'), 'utf8'));
      if (health.version !== 1 || !Number.isSafeInteger(health.compaction_failures) || health.compaction_failures < 0 ||
          !Number.isSafeInteger(health.recovery_errors) || health.recovery_errors < 0 ||
          (health.last_compaction_error !== null && !/^[A-Z][A-Z0-9_]{1,80}$/.test(health.last_compaction_error ?? ''))) throw new Error('INVALID_HEALTH');
    } catch { invalid = true; health = {}; }
  }
  return {failed, sequence: journal.seq, journal_bytes: journal.journalBytes, snapshot_bytes: journal.snapshotBytes,
    replay_records: journal.journalRecords, replay_ms: journal.replayMs, last_compaction_at: journal.compactedAt,
    compaction_failures: health.compaction_failures ?? 0, recovery_errors: health.recovery_errors ?? 0,
    last_compaction_error: health.last_compaction_error ?? null,
    health_persistence_failed: invalid || health.history_unavailable === true};
}

export async function hasAuthFence(directory) {
  for (const suffix of ['auth-failure', 'auth-intent', 'auth-active']) {
    try { await fs.lstat(path.join(directory, `auth.json.${suffix}`)); return true; }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  return false;
}

// No JournalStore.load(), repair, flush, checkpoint, mkdir, write, or unlink.
// The caller must hold the existing process-wide data-directory ownership lock.
export async function inspectAuthRetention(directory, assertOwnership, observeLive) {
  assertOwnership();
  const before = await inventory(directory);
  const optional = async suffix => before[`auth.json.${suffix}`]
    ? decode(await fs.readFile(path.join(directory, `auth.json.${suffix}`), 'utf8')) : null;
  const [failure, active, intents, journal, state] = await Promise.all([
    optional('auth-failure'), optional('auth-active'), optional('auth-intent'),
    readJournal(path.join(directory, 'auth.json'), () => { throw blocked('AUTH_BASE_MISSING'); }, {repairTail: false}),
    readJournal(path.join(directory, 'state.json'), () => { throw blocked('STATE_BASE_MISSING'); }, {repairTail: false}),
  ]);
  if (state.incompleteTailBytes || !state.data?.messages || !state.data?.operations || !state.data?.media) throw blocked('STATE_INTEGRITY_UNPROVEN');
  const result = assessAuthRetention({failure, active, intents: intents ?? {version: 1, operations: []}, journal});
  const stats = await retainedStats(directory, 'auth.json', journal, before, true);
  const stateStats = await retainedStats(directory, 'state.json', state, before, false);
  const after = await inventory(directory);
  if (!isDeepStrictEqual(before, after)) throw blocked('AUTH_EVIDENCE_CHANGED');
  assertOwnership();
  // A file on disk is never proof that this process's promises settled.
  if (observeLive && !observeLive(failure?.recovery)) {
    result.state = 'fenced_unresolved'; result.isolation_safe = false;
    result.blockers.push('LIVE_OWNER_OR_WORK_UNCONFIRMED');
  }
  return {result, fingerprint: after, state, stats, stateStats,
    paired: Boolean(journal.data?.creds?.registered || journal.data?.creds?.me?.id)};
}

export async function retainAuth(directory, {assertOwnership, auth, stateStore} = {}) {
  const observeLive = auth ? recovery => auth.canRetain(recovery) : undefined;
  const evidence = await inspectAuthRetention(directory, assertOwnership, observeLive);
  if (auth && (!stateStore || stateStore.failed || stateStore.pending !== 0 || stateStore.seq !== evidence.state.seq)) {
    throw blocked('LINE_PERSISTENCE_UNCONFIRMED');
  }
  const retained = {
    ...evidence.result,
    paired: evidence.paired,
    stats: () => ({...evidence.stats}),
    maintain: async () => false,
    async verify() {
      try {
        if (!retained.isolation_safe) throw blocked('AUTH_ISOLATION_UNPROVEN');
        assertOwnership();
        if (!isDeepStrictEqual(await inventory(directory), evidence.fingerprint)) throw blocked('AUTH_EVIDENCE_CHANGED');
        assertOwnership();
        if (auth && !auth.retainedIdle()) throw blocked('LIVE_OWNER_OR_WORK_UNCONFIRMED');
      } catch (error) {
        retained.isolation_safe = false; retained.state = 'fenced_unresolved';
        if (!retained.blockers.includes('RETENTION_PROOF_INVALIDATED')) retained.blockers.push('RETENTION_PROOF_INVALIDATED');
        throw error;
      }
    },
    async close() { await retained.verify(); },
  };
  retained.stateStore = {data: evidence.state.data, filename: path.join(directory, 'state.json'),
    stats: () => ({...evidence.stateStats}), maintain: async () => false,
    update: async () => { throw blocked('AUTH_RETAINED_OFFLINE'); },
    flush: async () => { await retained.verify(); }, close: async () => { await retained.verify(); }};
  // Seal the facade synchronously after the final observation; no disk changes.
  if (auth && retained.isolation_safe) auth.sealRetained();
  return retained;
}
