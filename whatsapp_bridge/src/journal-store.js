// Incremental durable transactions. Legacy JSON is an immutable import source;
// snapshots/exports run only in the offline storage tool, never on this loop.
import { readFile, open, stat, truncate } from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { BufferJSON } from '@whiskeysockets/baileys';
import { atomicWrite } from './storage.js';

const encode = value => JSON.stringify(value, BufferJSON.replacer);
const decode = value => {
  try { return JSON.parse(value, BufferJSON.reviver); }
  catch (_) { throw new Error('INVALID_STORAGE_JSON'); }
};
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
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

export async function readJournal(filename, initial, { repairTail = false } = {}) {
  let data, seq = 0;
  try {
    const snapshot = decode(await readFile(filename + '.snapshot', 'utf8'));
    if (snapshot.version !== 1 || !Number.isSafeInteger(snapshot.seq) || snapshot.seq < 0 || !object(snapshot.data)) throw new Error('INVALID_SNAPSHOT');
    ({data, seq} = snapshot);
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    try { data = decode(await readFile(filename, 'utf8')); }
    catch (error) { if (error.code !== 'ENOENT') throw error; data = initial(); }
  }
  const snapshotSeq = seq;
  let previousSeq = null;
  let journal;
  try { journal = await readFile(filename + '.journal'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; return {data, seq}; }
  const boundary = journal.lastIndexOf(10) + 1;
  if (boundary !== journal.length && repairTail) {
    await truncate(filename + '.journal', boundary);
    const fd = await open(filename + '.journal', 'r+');
    try { await fd.sync(); } finally { await fd.close(); }
  }
  for (const line of journal.subarray(0, boundary).toString('utf8').split('\n')) {
    if (!line) continue;
    const envelope = decode(line);
    if (typeof envelope.record !== 'string' || hash(envelope.record) !== envelope.checksum) throw new Error('JOURNAL_CHECKSUM_FAILED');
    const record = decode(envelope.record);
    if (record.version !== 1 || !Number.isSafeInteger(record.seq) || record.seq < 1 || !Array.isArray(record.ops)) throw new Error('INVALID_JOURNAL_RECORD');
    if (previousSeq !== null && record.seq !== previousSeq + 1) throw new Error('JOURNAL_SEQUENCE_GAP');
    previousSeq = record.seq;
    if (record.seq <= snapshotSeq) continue; // Snapshot committed before old journal rotation.
    if (record.seq !== seq + 1) throw new Error('JOURNAL_SEQUENCE_GAP');
    applyOperations(data, record.ops); seq = record.seq;
  }
  return {data, seq, incompleteTailBytes: journal.length - boundary};
}

export class JournalStore {
  constructor(filename, initial, { maxPending = 256, maxRecordBytes = 8 * 1024 * 1024, diagnostics = () => {}, append } = {}) {
    Object.assign(this, {filename, initial, maxPending, maxRecordBytes, diagnostics});
    this.tail = Promise.resolve(); this.pending = 0; this.failed = false;
    this.append = append ?? this.appendRecord.bind(this);
  }
  async load() {
    try { await stat(this.filename); }
    catch (error) { if (error.code !== 'ENOENT') throw error; await atomicWrite(this.filename, this.initial()); }
    Object.assign(this, await readJournal(this.filename, this.initial, {repairTail: true}));
    return this;
  }
  async appendRecord(bytes) {
    const handle = await open(this.filename + '.journal', 'a', 0o600);
    try { await handle.writeFile(bytes); await handle.sync(); }
    finally { await handle.close(); }
    if (!this.journalSynced) {
      const directory = await open(path.dirname(this.filename), 'r');
      try { await directory.sync(); this.journalSynced = true; } finally { await directory.close(); }
    }
  }
  update(change) {
    if (this.failed || this.pending >= this.maxPending) {
      const error = new Error(this.failed ? 'STORAGE_RESTART_REQUIRED' : 'STORAGE_QUEUE_FULL');
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
      const record = encode({version: 1, seq: this.seq + 1, ops: transaction.ops});
      const bytes = JSON.stringify({record, checksum: hash(record)}) + '\n';
      if (Buffer.byteLength(bytes) > this.maxRecordBytes) throw Object.assign(new Error('STORAGE_TRANSACTION_TOO_LARGE'), {bridgePersistenceFailure:true});
      try { await this.append(bytes); }
      catch (error) { this.failed = true; error.bridgePersistenceFailure = true; throw error; }
      applyOperations(this.data, decode(record).ops); this.seq++;
      try { this.diagnostics({event: 'storage_commit', queue_wait_ms: Math.round(started - queued),
        persist_ms: Math.round(performance.now() - started), bytes: Buffer.byteLength(bytes), pending: this.pending}); } catch (_) { /* Diagnostics cannot undo a durable commit. */ }
      return result;
    }).finally(() => { this.pending--; });
    this.tail = task.catch(() => {});
    return task;
  }
  async flush() { await this.tail; if (this.failed) throw new Error('STORAGE_RESTART_REQUIRED'); }
}
