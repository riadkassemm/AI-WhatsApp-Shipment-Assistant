// Repeatable online lifecycle soak on a verified, private /tmp copy only.
// Every store stays resident in one Bridge; every transport/network API fails.
import { fork } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { readdir, lstat, statfs } from 'node:fs/promises';
import { performance } from 'node:perf_hooks';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { acquireDataDirectory, atomicWrite } from './storage.js';
import { readStorageConfig } from './config.js';
import { verifyRehearsalCopy, layout, inspectAll, digest, summary, deliveryFingerprint, blockNetwork } from './storage-rehearsal.js';

const FIELD = '__private_storage_lifecycle_soak_v1__';
const fail = code => { throw Object.assign(new Error(code), {code}); };
const safeCode = error => /^[A-Z][A-Z0-9_]{0,79}$/.test(error?.code ?? error?.message ?? '') ? error.code ?? error.message : 'STORAGE_SOAK_FAILED';
const memory = () => ({...process.memoryUsage(), peak_rss: process.resourceUsage().maxRSS * 1024});
const countEntries = async directory => { try { return (await readdir(directory)).length; } catch { return null; } };

function options(value = {}) {
  const result = {expectedLines: 8, cycles: 6, writesPerCycle: 65, payloadBytes: 1024 ** 2,
    compactBytes: 64 * 1024 ** 2, authBurstTransactions: 25, ...value};
  for (const name of ['expectedLines', 'cycles', 'writesPerCycle', 'payloadBytes', 'compactBytes', 'authBurstTransactions']) {
    if (!Number.isSafeInteger(result[name]) || result[name] < 1) fail('SOAK_INVALID_OPTIONS');
  }
  if (result.payloadBytes < 64 || result.payloadBytes > 2 * 1024 ** 2 || result.cycles > 100 ||
      result.expectedLines > 32 || result.writesPerCycle > 10000 || result.authBurstTransactions > 1000 ||
      result.payloadBytes * result.writesPerCycle < result.compactBytes) fail('SOAK_INVALID_OPTIONS');
  return result;
}

// Always isolate the network guard and process RSS from the invoking test/tool.
export async function storageSoak(directory, config = {}) {
  const verified = await verifyRehearsalCopy(directory), selected = options(config);
  return new Promise((resolve, reject) => {
    const child = fork(fileURLToPath(import.meta.url), ['--worker', verified, JSON.stringify(selected)],
      {stdio: ['ignore', 'ignore', 'ignore', 'ipc']});
    let result;
    child.on('message', message => { result = message; });
    child.on('error', () => reject(new Error('SOAK_CHILD_FAILED')));
    child.on('exit', code => {
      if (code || !result?.ok) reject(Object.assign(new Error(result?.code ?? 'SOAK_CHILD_FAILED'), {code: result?.code ?? 'SOAK_CHILD_FAILED'}));
      else resolve(result.result);
    });
  });
}

async function run(directory, config) {
  directory = await verifyRehearsalCopy(directory);
  const release = await acquireDataDirectory(directory);
  let bridge, timer, sampling;
  try {
    const {manifest, stores: descriptions} = await layout(directory, config.expectedLines);
    const original = await inspectAll(directory, descriptions);
    if (original.some(store => store.incomplete_tail_bytes)) fail('SOAK_INCOMPLETE_TAIL_REQUIRES_REVIEW');
    const networkAttempts = await blockNetwork();
    const {installLibsignalConsoleFilter} = await import('./safe-console.js');
    installLibsignalConsoleFilter();
    const {Bridge} = await import('./bridge.js');
    const {addTransactionCapability, makeEventBuffer} = await import('@whiskeysockets/baileys');
    const {makeMutex} = await import('@whiskeysockets/baileys/lib/Utils/make-mutex.js');
    const {default: pino} = await import('pino');
    let sends = 0, providerCalls = 0, socketCount = 0, compactions = 0, finalFenceMs = 0, writesDuringWorkers = 0;
    let authTransactions = 0, authJournalTransactions = 0, authBurst;
    let maximumWorkers = 0, maximumFd = 0, minimumDisk = Infinity, maximumTemporaryBytes = 0;
    const peakMemory = {rss: 0, heapUsed: 0, external: 0, arrayBuffers: 0};
    const errors = [], perStoreCompactions = new Map(), samples = [];
    const providerForbidden = () => { providerCalls++; fail('SOAK_PROVIDER_CALL_FORBIDDEN'); };
    const sendForbidden = () => { sends++; fail('SOAK_SEND_FORBIDDEN'); };
    const policy = readStorageConfig({});
    policy.maintenance.maxJournalBytes = config.compactBytes;
    const began = performance.now();
    bridge = new Bridge({dataDir: directory, lines: [], ackTimeoutMs: 30,
      callbackUrl: 'http://127.0.0.1/soak-never-requested', storage: policy}, {
      fetch: providerForbidden, downloadMediaMessage: providerForbidden,
      onError: error => errors.push(safeCode(error)),
      log: event => {
        if (event.event === 'storage_compacted') {
          compactions++;
          finalFenceMs = Math.max(finalFenceMs, event.final_pause_ms ?? 0);
          const key = `${event.line_id ?? 'shared'}/${event.kind}`;
          perStoreCompactions.set(key, (perStoreCompactions.get(key) ?? 0) + 1);
        }
        if (['storage_compaction_failed', 'storage_recovery_error', 'storage_health_persistence_failed'].includes(event.event)) errors.push(event.code ?? event.event);
      },
      makeSocket: ({auth, bridgeTask}) => {
        if (!auth.creds.me?.id) fail('SOAK_UNPAIRED_SESSION');
        socketCount++;
        const ws = new EventEmitter(); ws.isOpen = true;
        const logger = pino({level: 'silent'}), ev = makeEventBuffer(logger);
        const keys = addTransactionCapability(auth.keys, logger, {maxCommitRetries: 3, delayBetweenTriesMs: 1});
        return {ws, authState: {creds: auth.creds, keys}, user: {id: auth.creds.me.id}, ev,
          soakProviderTask: bridgeTask(operation => operation()),
          messageMutex: makeMutex(), notificationMutex: makeMutex(), receiptMutex: makeMutex(), appStatePatchMutex: makeMutex(),
          end: () => {ws.isOpen = false; ev.destroy();}, sendMessage: sendForbidden, relayMessage: sendForbidden,
          query: providerForbidden, logout: providerForbidden, onWhatsApp: providerForbidden,
          fetchMessageHistory: providerForbidden, requestPlaceholderResend: providerForbidden};
      },
    });
    await bridge.initialize({autoConnect: false});
    if (bridge.lines.size !== config.expectedLines || errors.length) fail('SOAK_INITIALIZATION_FAILED');
    for (const id of Object.keys(manifest.lines)) {
      const line = bridge.getLine(id);
      await bridge.connect(id);
      await bridge.connectionUpdate(line, line.socket, {connection: 'open'});
    }
    const stores = descriptions.map(description => ({...description, store: description.kind === 'outbox' ? bridge.outbox :
      description.kind === 'auth' ? bridge.getLine(description.relative.split(path.sep)[0]).auth.store : bridge.getLine(description.relative.split(path.sep)[0]).store}));
    const workerCount = () => {
      const count = stores.filter(entry => entry.store.activeCheckpoint).length;
      maximumWorkers = Math.max(maximumWorkers, count);
      return count;
    };
    async function settle() {
      for (let pass = 0; pass < 20; pass++) {
        await Promise.all(stores.map(entry => entry.store.flush()));
        await new Promise(resolve => setImmediate(resolve));
        await bridge.storageMonitor.sample();
        const running = stores.map(entry => entry.store.compactionPromise).filter(Boolean);
        if (!running.length) {
          if (errors.length || stores.some(entry => entry.store.failed)) fail('SOAK_STORAGE_FAILURE');
          return;
        }
        await Promise.all(running);
      }
      fail('SOAK_MAINTENANCE_DID_NOT_QUIESCE');
    }
    await settle();
    const statuses = bridge.status().lines;
    if (statuses.some(line => line.status !== 'connected' || !line.is_paired || line.qr) || socketCount !== config.expectedLines) fail('SOAK_RECONNECT_FAILED');
    const baseline = stores.map((entry, index) => {
      if (Object.hasOwn(entry.store.data, FIELD)) fail('SOAK_RESERVED_FIELD_EXISTS');
      const before = original[index], data = entry.store.data;
      if (entry.kind !== 'state' && digest(data) !== before.fingerprint) fail('SOAK_AUTH_OR_OUTBOX_CHANGED');
      if (entry.kind === 'state' && (deliveryFingerprint(data) !== before.restarted_delivery_fingerprint ||
          summary(data).pending_sends !== 0 || summary(data).uncertain_sends !== before.counts.pending_or_uncertain_sends)) fail('SOAK_DELIVERY_STATE_CHANGED');
      return {label: entry.label, sequence: entry.store.seq, fingerprint: digest(data), counts: summary(data),
        written: entry.store.totalWrittenBytes, compaction_failures: entry.store.compactionFailures};
    });
    await atomicWrite(path.join(directory, '.storage-soak-baseline.json'), {version: 1, config, original, baseline});
    const expectedSequences = baseline.map(entry => entry.sequence);
    async function measure(label, includeDisk = true) {
      const value = {label, elapsed_ms: Math.round(performance.now() - began), memory: memory(),
        active_checkpoint_workers: workerCount(), pending_compactions: stores.filter(entry => entry.store.compactionPromise).length,
        open_fds: await countEntries('/proc/self/fd'), process_threads: await countEntries('/proc/self/task'), compactions};
      for (const key of Object.keys(peakMemory)) peakMemory[key] = Math.max(peakMemory[key], value.memory[key]);
      if (includeDisk) {
        let journalBytes = 0, snapshotBytes = 0, temporaryBytes = 0;
        for (const entry of stores) {
          const filename = path.join(directory, entry.relative);
          for (const [suffix, type] of [['.journal', 'journal'], ['.snapshot', 'snapshot']]) {
            const bytes = await lstat(filename + suffix).then(value => value.size).catch(error => {if (error.code === 'ENOENT') return 0; throw error;});
            if (type === 'journal') journalBytes += bytes; else snapshotBytes += bytes;
          }
        }
        const directories = [...new Set(stores.map(entry => path.dirname(path.join(directory, entry.relative))))];
        for (const current of directories) for (const name of await readdir(current)) if (name.includes('.checkpoint-') && name.endsWith('.tmp')) {
          temporaryBytes += await lstat(path.join(current, name)).then(value => value.size).catch(error => {if (error.code === 'ENOENT') return 0; throw error;});
        }
        const disk = await statfs(directory);
        Object.assign(value, {journal_bytes: journalBytes, snapshot_bytes: snapshotBytes, checkpoint_temporary_bytes: temporaryBytes,
          available_disk_bytes: Number(disk.bavail) * Number(disk.bsize), available_inodes: Number(disk.ffree)});
        minimumDisk = Math.min(minimumDisk, value.available_disk_bytes);
        maximumTemporaryBytes = Math.max(maximumTemporaryBytes, temporaryBytes);
      }
      maximumWorkers = Math.max(maximumWorkers, value.active_checkpoint_workers);
      maximumFd = Math.max(maximumFd, value.open_fds ?? 0);
      return value;
    }
    const initial = await measure('initialized');
    if (globalThis.gc) { globalThis.gc(); initial.post_gc_memory = memory(); }
    samples.push(initial);
    // Peak RSS is also recorded by the OS, including worker memory, even if a
    // brief allocation falls between the lightweight periodic samples.
    timer = setInterval(() => {
      if (!sampling) sampling = measure('active').then(value => {
        maximumWorkers = Math.max(maximumWorkers, value.active_checkpoint_workers);
      }).catch(error => errors.push(safeCode(error))).finally(() => {sampling = null;});
    }, 250);
    timer.unref();
    const authWork = entry => {
      const line = bridge.getLine(entry.relative.split(path.sep)[0]), keys = line.socket.authState.keys;
      const selected = Object.entries(entry.store.data.keys).find(([type, values]) =>
        !['pre-key', 'app-state-sync-key'].includes(type) && Object.keys(values).length);
      return {journalRecords: selected ? 1 : 0,
        run: () => line.socket.soakProviderTask(() => line.socket.messageMutex.mutex(() => keys.transaction(async () => {
          if (selected) {
            const [type, values] = selected, id = Object.keys(values)[0];
            const current = await keys.get(type, [id]);
            await keys.set({[type]: current});
          } else await keys.get('session', ['__soak_missing_read_only__']);
        }, 'storage-soak')))};
    };
    async function burstAuthentication() {
      const started = performance.now(), beforeMemory = memory(), latencies = [];
      let journalBytes = 0, journalTransactions = 0, transactionsDuringWorkers = 0;
      const workersAtStart = workerCount(), concurrency = Math.min(4, config.authBurstTransactions);
      await Promise.all(stores.map(async (entry, index) => {
        if (entry.kind !== 'auth') return;
        const operation = authWork(entry), beforeSequence = entry.store.seq, beforeBytes = entry.store.totalWrittenBytes;
        let next = 0;
        await Promise.all(Array.from({length: concurrency}, async () => {
          while (next < config.authBurstTransactions) {
            next++;
            const admittedAt = performance.now();
            if (workerCount()) transactionsDuringWorkers++;
            await operation.run();
            latencies.push(performance.now() - admittedAt);
          }
        }));
        const added = entry.store.seq - beforeSequence;
        if (added !== operation.journalRecords * config.authBurstTransactions) fail('SOAK_AUTH_BURST_SEQUENCE_CHANGED');
        expectedSequences[index] += added;
        journalBytes += entry.store.totalWrittenBytes - beforeBytes;
        journalTransactions += added;
        authTransactions += config.authBurstTransactions; authJournalTransactions += added;
        for (const suffix of ['.auth-intent', '.auth-failure']) {
          const exists = await lstat(entry.store.filename + suffix).then(() => true).catch(error => {
            if (error.code === 'ENOENT') return false;
            throw error;
          });
          if (exists) fail('SOAK_AUTH_BURST_INTENT_REMAINS');
        }
      }));
      latencies.sort((left, right) => left - right);
      const percentile = fraction => latencies[Math.max(0, Math.ceil(latencies.length * fraction) - 1)];
      return {transactions: latencies.length, transactions_per_line: config.authBurstTransactions,
        maximum_in_flight_per_line: concurrency, journal_transactions: journalTransactions,
        journal_bytes_added: journalBytes, duration_ms: performance.now() - started,
        latency_ms: {p50: percentile(0.5), p95: percentile(0.95), max: latencies.at(-1)},
        memory_before: beforeMemory, memory_after: memory(), active_checkpoint_workers_at_start: workersAtStart,
        transactions_during_active_workers: transactionsDuringWorkers, orphan_intents: 0};
    }
    const payload = 'x'.repeat(config.payloadBytes - 48);
    for (let cycle = 1; cycle <= config.cycles; cycle++) {
      const capacity = await statfs(directory);
      if (Number(capacity.bavail) * Number(capacity.bsize) < 1024 ** 3) fail('SOAK_DISK_HEADROOM_REQUIRED');
      const beforeCompactions = new Map(perStoreCompactions);
      for (let write = 0; write < config.writesPerCycle; write++) {
        const value = `${payload}:${cycle}:${write}:${'y'.repeat(40)}`;
        await Promise.all(stores.map((entry, index) => {
          if (workerCount()) writesDuringWorkers++;
          expectedSequences[index]++;
          return entry.store.update(data => {data[FIELD] = value;});
        }));
      }
      // Keep synthetic appends paused while measuring only authentication
      // amplification. Automatic checkpoint workers continue in the same
      // resident bridge throughout this bounded provider-pipeline burst.
      if (cycle === 1) authBurst = await burstAuthentication();
      // Exercise the installed provider's real buffered transaction/commit
      // wrapper and intent fencing alongside checkpoint workers. Same-value
      // key writes preserve every credential while exercising durable commits.
      await Promise.all(stores.map(async (entry, index) => {
        if (entry.kind !== 'auth') return;
        const operation = authWork(entry);
        const beforeSequence = entry.store.seq;
        await operation.run();
        const added = entry.store.seq - beforeSequence;
        // Unchanged credentials are intentionally not rewritten by the owner.
        // A real key commit adds one record; a read-only transaction adds none.
        if (added !== operation.journalRecords) fail('SOAK_AUTH_TRANSACTION_SEQUENCE_CHANGED');
        expectedSequences[index] += added;
        authTransactions++; authJournalTransactions += added;
      }));
      await settle();
      for (const entry of stores) {
        const key = entry.kind === 'outbox' ? 'shared/outbox' : `${entry.relative.split(path.sep)[0]}/${entry.kind}`;
        if ((perStoreCompactions.get(key) ?? 0) <= (beforeCompactions.get(key) ?? 0)) fail('SOAK_AUTOMATIC_THRESHOLD_NOT_TRIGGERED');
        if (entry.store.journalBytes >= config.compactBytes) fail('SOAK_JOURNAL_NOT_BOUNDED');
      }
      const sample = await measure(`cycle_${cycle}`);
      if (globalThis.gc) { globalThis.gc(); sample.post_gc_memory = memory(); }
      samples.push(sample);
      await atomicWrite(path.join(directory, '.storage-soak-progress.json'), {version: 1, cycle, samples});
    }
    await Promise.all(stores.map((entry, index) => {
      expectedSequences[index]++;
      return entry.store.update(data => {delete data[FIELD];});
    }));
    await settle();
    for (const [index, entry] of stores.entries()) {
      if (digest(entry.store.data) !== baseline[index].fingerprint || entry.store.seq !== expectedSequences[index]) fail('SOAK_STATE_CHANGED');
      if (entry.store.compactionFailures !== baseline[index].compaction_failures) fail('SOAK_COMPACTION_FAILED');
    }
    clearInterval(timer); timer = null;
    await sampling;
    const completed = await measure('completed');
    if (completed.active_checkpoint_workers || completed.checkpoint_temporary_bytes || sends || providerCalls || networkAttempts() || errors.length) fail('SOAK_FINAL_VERIFICATION_FAILED');
    samples.push(completed);
    const totalWritten = stores.reduce((sum, entry, index) => sum + entry.store.totalWrittenBytes - baseline[index].written, 0);
    const shutdownAt = performance.now();
    await bridge.close(); bridge = null;
    for (const [index, entry] of stores.entries()) {
      const increment = entry.store.seq - expectedSequences[index];
      // All same-value provider work already persisted its credentials; drain
      // must neither mutate state nor add another redundant journal record.
      if (increment !== 0) fail('SOAK_SHUTDOWN_SEQUENCE_CHANGED');
      expectedSequences[index] += increment;
    }
    for (const entry of stores.filter(entry => entry.kind === 'auth')) {
      const names = await readdir(path.dirname(path.join(directory, entry.relative)));
      if (['auth.json.auth-intent', 'auth.json.auth-failure', 'auth.json.auth-active'].some(name => names.includes(name))) fail('SOAK_AUTH_RECOVERY_FENCE_REMAINS');
    }
    const shutdownMs = Math.round(performance.now() - shutdownAt);
    const final = await measure('closed');
    if (globalThis.gc) {globalThis.gc(); final.post_gc_memory = memory();}
    samples.push(final);
    const restored = await inspectAll(directory, descriptions);
    for (const [index, entry] of restored.entries()) if (entry.fingerprint !== baseline[index].fingerprint ||
      entry.sequence !== expectedSequences[index]) fail('SOAK_REPLAY_CHANGED_STATE');
    const report = {version: 1, lines: config.expectedLines, stores: stores.length, config,
      maintenance: policy.maintenance, state_preserved: true, paired_lines: statuses.length, qr_codes: 0,
      sends, provider_calls: providerCalls, network_attempts: networkAttempts(), cycles_completed: config.cycles,
      writes_during_active_workers: writesDuringWorkers, total_written_bytes: totalWritten, compactions,
      auth_transactions: authTransactions, auth_journal_transactions: authJournalTransactions, auth_burst: authBurst,
      maximum_active_workers: maximumWorkers, maximum_open_fds: maximumFd,
      sampled_peak_memory: peakMemory,
      minimum_available_disk_bytes: minimumDisk, maximum_checkpoint_temporary_bytes: maximumTemporaryBytes,
      maximum_final_fence_ms: finalFenceMs, shutdown_ms: shutdownMs, elapsed_ms: Math.round(performance.now() - began),
      gc_available: Boolean(globalThis.gc), samples};
    if (!writesDuringWorkers || maximumWorkers > 1 || final.active_checkpoint_workers || final.checkpoint_temporary_bytes) fail('SOAK_CONCURRENCY_VERIFICATION_FAILED');
    await atomicWrite(path.join(directory, '.storage-soak-report.json'), {report, baseline, restored});
    return report;
  } finally {
    clearInterval(timer);
    await sampling;
    try { await bridge?.close(); } finally { await release(); }
  }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  process.umask(0o077);
  if (process.argv[2] === '--worker') {
    run(process.argv[3], options(JSON.parse(process.argv[4]))).then(result => {
      process.send({ok: true, result}); process.disconnect();
    }).catch(error => {process.send?.({ok: false, code: safeCode(error)}); process.disconnect?.(); process.exitCode = 1;});
  } else {
    const [directory, rawOptions = '{}'] = process.argv.slice(2);
    if (!directory) {console.error('Usage: node src/storage-soak.js /tmp/PRIVATE_COPY [JSON_OPTIONS]'); process.exitCode = 1;}
    else storageSoak(directory, JSON.parse(rawOptions)).then(result => console.log(JSON.stringify(result)))
      .catch(error => {console.error(JSON.stringify({error: safeCode(error)})); process.exitCode = 1;});
  }
}
