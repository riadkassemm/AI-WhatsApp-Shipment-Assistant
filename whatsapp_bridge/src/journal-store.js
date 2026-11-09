// Incremental durable transactions. Checkpoints rebuild committed disk state in
// a worker; only the bounded final journal switch shares the transaction queue.
import { createReadStream } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { BufferJSON } from '@whiskeysockets/baileys';
import { atomicWrite } from './storage.js';
import { checkpointWorker, coordinateCheckpoint, maintenanceDefaults, safeStorageCode } from './journal-maintenance.js';

export const encode = value => JSON.stringify(value, BufferJSON.replacer);
export const decode = value => {
  try { return JSON.parse(value, BufferJSON.reviver); }
  catch (_) { throw new Error('INVALID_STORAGE_JSON'); }
};
export const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const forbidden = key => ['__proto__', 'constructor', 'prototype'].includes(key);
const object = value => value && typeof value === 'object' && !ArrayBuffer.isView(value) && !(value instanceof Date);

export function applyOperations(data, ops) {
  for (const op of ops) {
    if (!Array.isArray(op.path) || !op.path.length || op.path.some(key => typeof key !== 'string' || forbidden(key))) throw new Error('INVALID_JOURNAL_PATH');
    let parent = data;
    for (const key of op.path.slice(0, -1)) {
      if (!Object.hasOwn(parent, key) || !object(parent[key])) throw new Error('INVALID_JOURNAL_PARENT');
      parent = parent[key];
    }
    const key = op.path.at(-1);
    if (op.remove) delete parent[key];
    else parent[key] = op.value;
  }
}

// Overlay reads/writes without cloning the root or a messages/keys collection.
// Only assigned values are serialized; failed writes never publish the overlay.
function draft(data) {
  const ops = [];
  const proxies = new WeakMap();
  const unwrap = value => proxies.has(value) ? proxies.get(value)() : value;
  const wrap = (base, location) => {
    const changes = new Map(), children = new Map();
    const current = key => changes.has(key) ? changes.get(key) : Reflect.get(base, key);
    const keys = () => [...new Set([...Reflect.ownKeys(base), ...changes.keys()])].filter(key => current(key) !== removed);
    const materialize = () => {
      const result = Array.isArray(base) ? [] : {};
      for (const key of keys()) if (key !== 'length') result[key] = unwrap(proxy[key]);
      if (Array.isArray(base)) result.length = current('length');
      return result;
    };
    const proxy = new Proxy(Array.isArray(base) ? [] : {}, {
      get(_target, key) {
        const value = current(key);
        if (value === removed) return undefined;
        if (typeof key === 'symbol' || !object(value)) return value;
        if (!children.has(key)) children.set(key, wrap(value, [...location, key]));
        return children.get(key);
      },
      set(_target, key, value) {
        if (typeof key !== 'string' || forbidden(key)) throw new Error('INVALID_JOURNAL_PATH');
        value = unwrap(value);
        changes.set(key, value); children.delete(key);
        ops.push(value === undefined ? {path: [...location, key], remove: true} : {path: [...location, key], value});
        if (Array.isArray(base) && /^\d+$/.test(key) && Number(key) >= current('length')) changes.set('length', Number(key) + 1);
        return true;
      },
      deleteProperty(_target, key) { changes.set(key, removed); children.delete(key); ops.push({path: [...location, key], remove: true}); return true; },
      ownKeys: keys,
      has(_target, key) { return current(key) !== removed && (changes.has(key) || key in base); },
      getOwnPropertyDescriptor(_target, key) {
        if (key === 'length' && Array.isArray(base)) return {value: current(key), writable: true, enumerable: false, configurable: false};
        if (current(key) !== removed && (changes.has(key) || Object.hasOwn(base, key))) return {value: proxy[key], writable: true, enumerable: true, configurable: true};
      },
    });
    proxies.set(proxy, materialize);
    return proxy;
  };
  const removed = Symbol('removed');
  return {value: wrap(data, []), ops};
}

export function journalRecord(line) {
  const envelope = decode(line);
  if (typeof envelope.record !== 'string' || hash(envelope.record) !== envelope.checksum) throw new Error('JOURNAL_CHECKSUM_FAILED');
  const record = decode(envelope.record);
  if (record.version !== 1 || !Number.isSafeInteger(record.seq) || record.seq < 1 || !Array.isArray(record.ops)) throw new Error('INVALID_JOURNAL_RECORD');
  return record;
}

export async function syncDirectory(filename) {
  const directory = await fs.open(path.dirname(filename), 'r');
  try { await directory.sync(); } finally { await directory.close(); }
}

// endBytes is exclusive and captured under the writer queue. The worker never
// mistakes a concurrent append or an incomplete transaction for its checkpoint.
export async function scanJournal(filename, visit, {startBytes = 0, endBytes, onChunk,
  maxRecordBytes = maintenanceDefaults.maxReplayRecordBytes, signal} = {}) {
  signal?.throwIfAborted();
  if (endBytes === startBytes) return {bytes: 0, boundary: 0, incompleteTailBytes: 0, records: 0};
  const stream = createReadStream(filename, {highWaterMark: 64 * 1024, start: startBytes, signal,
    ...(endBytes === undefined ? {} : {end: endBytes - 1})});
  let bytes = 0, boundary = 0, pendingBytes = 0, records = 0;
  let pieces = [];
  try {
    for await (const chunk of stream) {
      signal?.throwIfAborted();
      let start = 0, newline;
      while ((newline = chunk.indexOf(10, start)) !== -1) {
        const piece = chunk.subarray(start, newline);
        if (pendingBytes + piece.length > maxRecordBytes) throw new Error('JOURNAL_RECORD_TOO_LARGE');
        const line = pieces.length ? Buffer.concat([...pieces, piece], pendingBytes + piece.length) : piece;
        if (line.length) { visit(journalRecord(line.toString('utf8'))); records++; }
        pieces = []; pendingBytes = 0;
        boundary = bytes + newline + 1;
        start = newline + 1;
      }
      if (start < chunk.length) {
        const piece = chunk.subarray(start);
        if (pendingBytes + piece.length > maxRecordBytes) throw new Error('JOURNAL_RECORD_TOO_LARGE');
        pieces.push(piece); pendingBytes += piece.length;
      }
      if (onChunk) await onChunk(chunk);
      bytes += chunk.length;
    }
  } catch (error) {
    if (error.code === 'ENOENT' && (startBytes || endBytes)) throw new Error('JOURNAL_MISSING');
    if (error.code !== 'ENOENT') throw error;
  } finally { stream.destroy(); }
  if (endBytes !== undefined && bytes !== endBytes - startBytes) throw new Error('JOURNAL_SIZE_CHANGED');
  return {bytes, boundary, incompleteTailBytes: bytes - boundary, records};
}

export async function readSnapshot(filename, {signal} = {}) {
  let bytes = await fs.readFile(filename, {signal});
  signal?.throwIfAborted();
  let verifiedChecksum;
  const checksumOffset = bytes.lastIndexOf(',"checksum":');
  // Our v1 writer hashes the exact JSON body before appending this small suffix.
  // Verify those bytes before parsing, avoiding a second serialization of a
  // potentially large decoded auth state. Never trust arbitrary fields after
  // the checksum: an appended seq/data field could otherwise bypass the hash.
  if (checksumOffset >= 0 && bytes.length - checksumOffset <= 160) {
    const suffix = /^,"checksum":"([0-9a-f]{64})"(?:,"compacted_at":\d+)?}$/.exec(bytes.subarray(checksumOffset).toString('utf8'));
    if (suffix && crypto.createHash('sha256').update(bytes.subarray(0, checksumOffset)).update('}').digest('hex') === suffix[1]) {
      verifiedChecksum = suffix[1];
    }
  }
  const text = bytes.toString('utf8');
  bytes = null;
  const snapshot = decode(text);
  if (snapshot.version !== 1 || !Number.isSafeInteger(snapshot.seq) || snapshot.seq < 0 || !object(snapshot.data)) throw new Error('INVALID_SNAPSHOT');
  // Existing v1 snapshots may use different field order/whitespace. Preserve
  // their original canonical decoded verification instead of rejecting them.
  if (snapshot.checksum !== undefined && snapshot.checksum !== verifiedChecksum &&
      snapshot.checksum !== hash(encode({version: 1, seq: snapshot.seq, data: snapshot.data}))) throw new Error('SNAPSHOT_CHECKSUM_FAILED');
  return snapshot;
}

async function preserveTail(filename, boundary, length) {
  const evidence = `${filename}.tail-${Date.now()}-${crypto.randomUUID()}.bin`;
  const source = await fs.open(filename, 'r');
  let destination;
  const checksum = crypto.createHash('sha256');
  try {
    destination = await fs.open(evidence, 'wx', 0o600);
    const buffer = Buffer.alloc(64 * 1024);
    let remaining = length, position = boundary;
    while (remaining) {
      const {bytesRead} = await source.read(buffer, 0, Math.min(buffer.length, remaining), position);
      if (!bytesRead) throw new Error('JOURNAL_SIZE_CHANGED');
      const chunk = buffer.subarray(0, bytesRead);
      await destination.writeFile(chunk); checksum.update(chunk);
      remaining -= bytesRead; position += bytesRead;
    }
    await destination.sync();
  } finally { try { await source.close(); } finally { await destination?.close(); } }
  // The metadata's directory fsync also persists the evidence filename. If any
  // preservation step fails the authoritative journal is left untouched.
  await atomicWrite(evidence + '.json', {version: 1, source: path.basename(filename), offset: boundary,
    bytes: length, checksum: checksum.digest('hex'), recovered_at: Date.now()});
  const journal = await fs.open(filename, 'r+');
  try { await journal.truncate(boundary); await journal.sync(); } finally { await journal.close(); }
}

async function hasStorageEvidence(filename) {
  const base = path.basename(filename);
  // A journal, checkpoint, quarantine, or previous initialization temporary is
  // evidence of an existing store, even if empty. Never synthesize credentials
  // underneath such evidence. A valid snapshot is itself a sufficient base.
  return (await fs.readdir(path.dirname(filename))).some(name => name.startsWith(base + '.'));
}

export async function readJournal(filename, initial, { repairTail = false, endBytes,
  maxRecordBytes = maintenanceDefaults.maxReplayRecordBytes, signal } = {}) {
  signal?.throwIfAborted();
  const started = performance.now();
  let data, seq = 0, snapshotBytes = 0, compactedAt = null;
  try {
    const snapshot = await readSnapshot(filename + '.snapshot', {signal});
    ({data, seq} = snapshot);
    snapshotBytes = (await fs.stat(filename + '.snapshot')).size;
    compactedAt = Number.isFinite(snapshot.compacted_at) ? snapshot.compacted_at : null;
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    try { data = decode(await fs.readFile(filename, {encoding: 'utf8', signal})); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      if (await hasStorageEvidence(filename)) throw new Error('MISSING_STORAGE_BASE');
      signal?.throwIfAborted();
      data = initial();
    }
  }
  const snapshotSeq = seq;
  let previousSeq = null;
  const scan = await scanJournal(filename + '.journal', record => {
    if (previousSeq !== null && record.seq !== previousSeq + 1) throw new Error('JOURNAL_SEQUENCE_GAP');
    previousSeq = record.seq;
    if (record.seq <= snapshotSeq) return;
    if (record.seq !== seq + 1) throw new Error('JOURNAL_SEQUENCE_GAP');
    applyOperations(data, record.ops); seq = record.seq;
  }, {endBytes, maxRecordBytes, signal});
  signal?.throwIfAborted();
  if (scan.incompleteTailBytes && repairTail) {
    // Once evidence preservation starts, finish its fsync/truncate protocol.
    // Cancellation must never interrupt a durability operation halfway through.
    await preserveTail(filename + '.journal', scan.boundary, scan.incompleteTailBytes);
  }
  return {data, seq, snapshotSeq, snapshotBytes, compactedAt,
    incompleteTailBytes: scan.incompleteTailBytes, journalBytes: repairTail ? scan.boundary : scan.bytes,
    journalRecords: scan.records, replayMs: Math.round(performance.now() - started)};
}

export class JournalStore {
  constructor(filename, initial, { maxPending = 256, maxRecordBytes = 8 * 1024 * 1024,
    diagnostics = () => {}, append, maintenance = {}, compactionHook, workerFactory = checkpointWorker } = {}) {
    Object.assign(this, {filename, initial, maxPending, maxRecordBytes, diagnostics, compactionHook});
    this.maintenance = {...maintenanceDefaults, ...maintenance};
    this.tail = Promise.resolve(); this.pending = 0; this.failed = false; this.closed = false;
    this.compactionFailures = 0; this.consecutiveCompactionFailures = 0; this.recoveryErrors = 0; this.nextCompactionRetryAt = 0;
    this.lastCompactionError = null; this.compactionPromise = null; this.totalWrittenBytes = 0;
    this.append = append ?? this.appendRecord.bind(this);
    this.workerFactory = workerFactory;
    this.healthTail = Promise.resolve(); this.healthPersistenceFailed = false; this.healthHistoryUnavailable = false;
  }
  report(event) { try { this.diagnostics(event); } catch (_) { /* Metrics never undo committed state. */ } }
  async loadHealth() {
    try {
      if ((await fs.stat(this.filename + '.health.json')).size > 4096) throw new Error('INVALID_STORAGE_HEALTH');
      const health = JSON.parse(await fs.readFile(this.filename + '.health.json', 'utf8'));
      if (health.version !== 1 || !Number.isSafeInteger(health.compaction_failures) || health.compaction_failures < 0 ||
          !Number.isSafeInteger(health.recovery_errors) || health.recovery_errors < 0 ||
          (health.last_compaction_error !== null && safeStorageCode({code: health.last_compaction_error}) !== health.last_compaction_error) ||
          (health.history_unavailable !== undefined && typeof health.history_unavailable !== 'boolean')) {
        throw new Error('INVALID_STORAGE_HEALTH');
      }
      this.compactionFailures = health.compaction_failures;
      this.recoveryErrors = health.recovery_errors;
      this.lastCompactionError = health.last_compaction_error;
      this.healthHistoryUnavailable = health.history_unavailable === true;
      this.healthPersistenceFailed = this.healthHistoryUnavailable;
    } catch (error) {
      if (error.code !== 'ENOENT') {
        this.healthPersistenceFailed = true;
        this.healthHistoryUnavailable = true;
        this.report({event: 'storage_health_persistence_failed', code: 'STORAGE_HEALTH_PERSISTENCE_FAILED'});
      }
    }
  }
  persistHealth() {
    const task = this.healthTail.then(async () => {
      try {
        await atomicWrite(this.filename + '.health.json', {version: 1,
          compaction_failures: this.compactionFailures, recovery_errors: this.recoveryErrors,
          last_compaction_error: this.lastCompactionError, history_unavailable: this.healthHistoryUnavailable});
        this.healthPersistenceFailed = this.healthHistoryUnavailable;
      } catch (_) {
        this.healthPersistenceFailed = true;
        this.report({event: 'storage_health_persistence_failed', code: 'STORAGE_HEALTH_PERSISTENCE_FAILED'});
      }
    });
    this.healthTail = task;
    return task;
  }
  async load({signal} = {}) {
    signal?.throwIfAborted();
    await this.loadHealth();
    try {
      let fresh = false;
      Object.assign(this, await readJournal(this.filename, () => { fresh = true; return this.initial(); }, {repairTail: true,
        maxRecordBytes: this.maintenance.maxReplayRecordBytes, signal}));
      signal?.throwIfAborted();
      if (fresh) await atomicWrite(this.filename, this.data);
      this.journalRecordsAtLoad = this.journalRecords;
      this.totalWrittenBytes = this.journalBytes;
      this.lastCompactionAt = this.compactedAt;
      this.journalStartedAt = Date.now();
      try { const info = await fs.stat(this.filename + '.journal'); this.journalStartedAt = info.birthtimeMs || info.mtimeMs; }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      // Only this implementation's nonauthoritative checkpoint files are
      // disposable. Evidence/quarantine and arbitrary tmp files are never swept.
      const prefix = path.basename(this.filename) + '.checkpoint-';
      const evidencePrefix = path.basename(this.filename) + '.journal.tail-';
      let removed = false;
      this.quarantinedTailBytes = 0;
      for (const name of await fs.readdir(path.dirname(this.filename))) {
        if (name.startsWith(prefix) && /^[0-9a-f-]{36}\.(snapshot|journal)\.tmp$/.test(name.slice(prefix.length))) {
          await fs.unlink(path.join(path.dirname(this.filename), name)); removed = true;
        }
        if (name.startsWith(evidencePrefix) && /^\d+-[0-9a-f-]{36}\.bin$/.test(name.slice(evidencePrefix.length))) {
          this.quarantinedTailBytes += (await fs.stat(path.join(path.dirname(this.filename), name))).size;
        }
      }
      if (removed) await syncDirectory(this.filename);
      if (this.incompleteTailBytes) {
        this.recoveryErrors++;
        this.report({event: 'storage_tail_quarantined', bytes: this.incompleteTailBytes});
        await this.persistHealth();
      }
      this.report({event: 'storage_replay', duration_ms: this.replayMs, records: this.journalRecords, bytes: this.journalBytes});
      return this;
    } catch (error) {
      if (signal?.aborted && (error === signal.reason || error.name === 'AbortError')) {
        this.maintenanceStopped = true;
        throw error;
      }
      this.recoveryErrors++; this.failed = true;
      this.report({event: 'storage_recovery_error', code: safeStorageCode(error)});
      await this.persistHealth();
      throw error;
    }
  }
  stats() {
    return {journal_bytes: this.journalBytes ?? 0, transactions_since_compaction: this.journalRecords ?? 0,
      checkpoint_seq: this.snapshotSeq ?? 0, sequence: this.seq ?? 0, snapshot_bytes: this.snapshotBytes ?? 0,
      replay_ms: this.replayMs ?? 0, replay_records: this.journalRecordsAtLoad ?? this.journalRecords ?? 0,
      total_written_bytes: this.totalWrittenBytes, last_compaction_at: this.lastCompactionAt ?? null,
      last_compaction_ms: this.lastCompactionMs ?? null, last_compaction_reclaimed_bytes: this.lastCompactionReclaimedBytes ?? null,
      compaction_failures: this.compactionFailures, last_compaction_error: this.lastCompactionError,
      health_persistence_failed: this.healthPersistenceFailed,
      compaction_running: !!this.compactionPromise, next_compaction_retry_at: this.nextCompactionRetryAt,
      recovery_errors: this.recoveryErrors, quarantined_tail_bytes: this.quarantinedTailBytes ?? this.incompleteTailBytes ?? 0, failed: this.failed};
  }
  async appendRecord(bytes) {
    const handle = await fs.open(this.filename + '.journal', 'a', 0o600);
    try { await handle.writeFile(bytes); await handle.sync(); }
    finally { await handle.close(); }
    if (!this.journalSynced) { await syncDirectory(this.filename); this.journalSynced = true; }
  }
  update(change) {
    if (this.failed || this.closed || this.pending >= this.maxPending) {
      const error = new Error(this.failed ? 'STORAGE_RESTART_REQUIRED' : this.closed ? 'STORAGE_CLOSED' : 'STORAGE_QUEUE_FULL');
      error.bridgePersistenceFailure = true;
      return Promise.reject(error);
    }
    this.pending++; const queued = performance.now();
    const task = this.tail.then(async () => {
      if (this.failed) throw Object.assign(new Error('STORAGE_RESTART_REQUIRED'), {bridgePersistenceFailure:true});
      const started = performance.now();
      const transaction = draft(this.data);
      const result = await change(transaction.value);
      if (!transaction.ops.length) return result;
      if (!Number.isSafeInteger(this.seq + 1)) throw new Error('STORAGE_SEQUENCE_EXHAUSTED');
      const record = encode({version: 1, seq: this.seq + 1, ops: transaction.ops});
      const bytes = JSON.stringify({record, checksum: hash(record)}) + '\n';
      const length = Buffer.byteLength(bytes);
      if (length > this.maxRecordBytes) throw Object.assign(new Error('STORAGE_TRANSACTION_TOO_LARGE'), {bridgePersistenceFailure:true});
      try { await this.append(bytes); }
      catch (error) { this.failed = true; error.bridgePersistenceFailure = true; throw error; }
      applyOperations(this.data, decode(record).ops); this.seq++;
      this.journalBytes += length; this.journalRecords++; this.totalWrittenBytes += length;
      this.report({event: 'storage_commit', queue_wait_ms: Math.round(started - queued),
        persist_ms: Math.round(performance.now() - started), bytes: length, pending: this.pending});
      // Schedule after this transaction settles; never await a checkpoint from
      // inside the same queue that its final switch must acquire.
      queueMicrotask(() => { this.maintain().catch(() => {}); });
      return result;
    }).finally(() => { this.pending--; });
    this.tail = task.catch(() => {});
    return task;
  }
  fence(action) {
    const task = this.tail.then(() => {
      if (this.failed) throw new Error('STORAGE_RESTART_REQUIRED');
      return action();
    });
    this.tail = task.catch(() => {});
    return task;
  }
  async maintain() {
    const config = this.maintenance, now = Date.now();
    if (!config.enabled || this.closed || this.failed || this.maintenanceStopped ||
        now < this.nextCompactionRetryAt || !this.journalBytes) return false;
    if (this.maintenanceCleanupFailed) return this.compact();
    if (this.journalBytes < config.maxJournalBytes && this.journalRecords < config.maxTransactions &&
      now - (this.lastCompactionAt ?? this.journalStartedAt) < config.maxAgeMs) return false;
    return this.compact();
  }
  compact() {
    if (this.compactionPromise) return this.compactionPromise;
    if (this.closed || this.failed || this.maintenanceStopped) return Promise.reject(new Error('STORAGE_RESTART_REQUIRED'));
    const task = coordinateCheckpoint(async () => {
      if (this.closed || this.maintenanceStopped) return false;
      let worker, publishing = false;
      const started = performance.now();
      try {
        try {
          if (this.cleanupCheckpoint) {
            await this.cleanupCheckpoint.close();
            this.cleanupCheckpoint = null; this.maintenanceCleanupFailed = false;
          }
          const cutoff = await this.fence(() => ({seq: this.seq, bytes: this.journalBytes}));
          if (!cutoff.bytes) return false;
          if (this.maintenanceStopped) return false;
          worker = this.workerFactory(this.filename, cutoff, this.maintenance, async stage => {
            if (stage === 'before_snapshot_rename') {
              if (this.maintenanceStopped) throw new Error('COMPACTION_CANCELLED');
              publishing = true; this.compactionPublishing = true;
            }
            if (this.compactionHook) await this.compactionHook(stage);
          });
          this.activeCheckpoint = worker;
          await worker.call('prepare');
          let completed = false;
          for (let pass = 0; pass < 32 && !completed; pass++) {
            const target = await this.fence(() => ({seq: this.seq, bytes: this.journalBytes}));
            await worker.call('catchup', target);
            completed = await this.fence(async () => {
              if (this.maintenanceStopped) throw new Error('COMPACTION_CANCELLED');
              if (this.journalBytes - target.bytes > this.maintenance.finalCatchupBytes) return false;
              const final = {seq: this.seq, bytes: this.journalBytes};
              let result;
              try { result = await worker.call('publish', final); }
              catch (error) {
                // Poison before releasing the fence: queued writes must not run
                // against a journal whose publication outcome is uncertain.
                if (publishing) { this.failed = true; error.bridgePersistenceFailure = true; }
                throw error;
              }
              this.journalBytes = final.bytes - cutoff.bytes;
              this.journalRecords = final.seq - cutoff.seq;
              this.snapshotSeq = cutoff.seq; this.snapshotBytes = result.snapshotBytes;
              this.lastCompactionAt = result.compactedAt; this.journalStartedAt = result.compactedAt;
              this.lastCompactionMs = Math.round(performance.now() - started);
              this.lastCompactionReclaimedBytes = cutoff.bytes;
              this.journalSynced = true; this.lastCompactionError = null; this.nextCompactionRetryAt = 0;
              this.consecutiveCompactionFailures = 0;
              this.compactionPublishing = false;
              this.report({event: 'storage_compacted', duration_ms: Math.round(performance.now() - started),
                reclaimed_bytes: cutoff.bytes, journal_bytes: this.journalBytes, snapshot_bytes: this.snapshotBytes,
                checkpoint_seq: cutoff.seq, final_pause_ms: result.finalPauseMs});
              return true;
            });
          }
          if (!completed) throw new Error('COMPACTION_BUSY');
          await this.persistHealth();
          return true;
        } catch (error) {
          if (publishing || /^(INVALID_|JOURNAL_|SNAPSHOT_CHECKSUM_FAILED|MISSING_STORAGE_BASE)/.test(safeStorageCode(error))) {
            this.failed = true; error.bridgePersistenceFailure = true;
          }
          throw error;
        } finally {
          try { await worker?.close(); }
          catch (error) { this.maintenanceCleanupFailed = true; this.cleanupCheckpoint = worker; throw error; }
          finally { this.activeCheckpoint = null; this.compactionPublishing = false; }
        }
      } catch (error) {
        const integrityError = /^(INVALID_|JOURNAL_|SNAPSHOT_CHECKSUM_FAILED|MISSING_STORAGE_BASE)/.test(safeStorageCode(error));
        if (integrityError) this.recoveryErrors++;
        if (publishing || integrityError) { this.failed = true; error.bridgePersistenceFailure = true; }
        if (safeStorageCode(error) === 'COMPACTION_CANCELLED' && !publishing && !this.maintenanceCleanupFailed) return false;
        this.compactionFailures++; this.lastCompactionError = safeStorageCode(error);
        this.consecutiveCompactionFailures++;
        this.nextCompactionRetryAt = Date.now() + Math.min(this.maintenance.retryMs * 2 ** Math.min(this.consecutiveCompactionFailures - 1, 6), 3600000);
        this.report({event: 'storage_compaction_failed', code: this.lastCompactionError, restart_required: this.failed});
        await this.persistHealth();
        throw error;
      }
    });
    this.compactionPromise = task.finally(() => { this.compactionPromise = null; });
    return this.compactionPromise;
  }
  async flush() { await this.tail; if (this.failed) throw new Error('STORAGE_RESTART_REQUIRED'); }
  stopMaintenance() {
    this.maintenanceStopped = true;
    if (this.stopMaintenancePromise) return this.stopMaintenancePromise;
    this.stopMaintenancePromise = (async () => {
      let timer;
      try {
        if (this.compactionPublishing) {
          // Give the small atomic publication time to finish, reserving most of
          // systemd's 35-second budget for accepted events and credential drains.
          await Promise.race([this.compactionPromise?.catch(() => {}), new Promise(resolve => {
            timer = setTimeout(resolve, 5000); timer.unref();
          })]);
        }
        await this.activeCheckpoint?.cancel();
        await this.compactionPromise?.catch(() => {});
      } finally { clearTimeout(timer); }
    })();
    return this.stopMaintenancePromise;
  }
  async close() {
    this.closed = true;
    await this.stopMaintenance();
    await this.flush();
    await this.healthTail;
  }
}
