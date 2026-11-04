import {Worker} from 'node:worker_threads';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

export const maintenanceDefaults = Object.freeze({enabled: true, maxJournalBytes: 64 * 1024 * 1024,
  maxTransactions: 10000, maxAgeMs: 6 * 3600000, retryMs: 60000, finalCatchupBytes: 1024 * 1024,
  minFreeBytes: 128 * 1024 * 1024, minFreeInodes: 64, workerMemoryMb: 512,
  maxSnapshotBytes: 128 * 1024 * 1024, maxReplayRecordBytes: 64 * 1024 * 1024, timeoutMs: 300000});

export function safeStorageCode(error) {
  const code = error?.code ?? error?.message;
  return typeof code === 'string' && /^[A-Z][A-Z0-9_]{1,80}$/.test(code) ? code : 'STORAGE_IO_ERROR';
}

// At most one checkpoint worker and extra materialized store in this process.
// Stores waiting here retain ordinary independent write queues.
let checkpointTail = Promise.resolve();
export function coordinateCheckpoint(action) {
  const task = checkpointTail.then(action);
  checkpointTail = task.catch(() => {});
  return task;
}

export function checkpointWorker(filename, cutoff, maintenance, onStage, workerOptions = {}) {
  const token = crypto.randomUUID();
  const worker = new Worker(new URL('./journal-worker.js', import.meta.url), {
    ...workerOptions,
    workerData: {filename, cutoff, maintenance, token},
    resourceLimits: {maxOldGenerationSizeMb: maintenance.workerMemoryMb},
  });
  const pending = new Map();
  let sequence = 0, failure = null, closing = false, closed, termination, cleanupNeedsSync = false;
  const fail = error => {
    failure ??= error;
    for (const {reject} of pending.values()) reject(error);
    pending.clear();
  };
  const terminate = () => termination ??= worker.terminate();
  const timer = setTimeout(() => {
    fail(new Error('COMPACTION_TIMEOUT'));
    terminate().catch(() => {});
  }, maintenance.timeoutMs);
  timer.unref();
  worker.on('message', message => {
    if (message.stage) {
      Promise.resolve().then(() => { if (!failure && !closing) return onStage(message.stage); }).then(
        () => { if (!failure && !closing) worker.postMessage({resume: message.id}); },
        error => { if (!failure && !closing) worker.postMessage({resume: message.id, error: safeStorageCode(error)}); });
      return;
    }
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    if (message.error) request.reject(Object.assign(new Error(message.error), {code: message.error}));
    else request.resolve(message.result);
  });
  worker.on('error', error => fail(Object.assign(new Error(safeStorageCode(error)), {code: safeStorageCode(error)})));
  worker.on('exit', () => { if (!closing && !failure) fail(new Error('COMPACTION_WORKER_EXITED')); });
  const call = (command, target) => {
    if (failure) return Promise.reject(failure);
    if (closing) return Promise.reject(new Error('COMPACTION_CANCELLED'));
    const id = ++sequence;
    return new Promise((resolve, reject) => {
      pending.set(id, {resolve, reject});
      worker.postMessage({id, command, target});
    });
  };
  return {call, cancel() {
    fail(new Error('COMPACTION_CANCELLED'));
    return terminate();
  }, close() {
    if (closed) return closed;
    closing = true; clearTimeout(timer);
    closed = (async () => {
      // Termination first: a timed-out worker may still be creating/writing its
      // files. Only the parent can prove it has stopped before reclaiming them.
      await terminate();
      let cleanupError;
      for (const suffix of ['snapshot', 'journal']) {
        try { await fs.unlink(`${filename}.checkpoint-${token}.${suffix}.tmp`); cleanupNeedsSync = true; }
        catch (error) { if (error.code !== 'ENOENT') cleanupError ??= error; }
      }
      if (cleanupNeedsSync) {
        const directory = await fs.open(path.dirname(filename), 'r');
        try { await directory.sync(); } finally { await directory.close(); }
        cleanupNeedsSync = false;
      }
      if (cleanupError) throw cleanupError;
    })();
    // A retry only touches this already-terminated attempt's paths; it cannot
    // allocate another worker or another set of temporary files.
    closed.catch(() => { closed = null; });
    return closed;
  }};
}
