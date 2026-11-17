import { fork as forkProcess } from 'node:child_process';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdir, rm, rename, unlink, readdir, open as openFile } from 'node:fs/promises';
import { BridgeError, validateLineManifest, tombstoneManifest, phoneDigits, workerSocketPath, UUID_PATTERN, MANIFEST_MAX_BYTES } from './config.js';
// Plain JSON helpers: the supervisor itself never loads the WhatsApp provider.
import { FileStore } from './file-store.js';
import { EventLoopDiagnostics } from './connection-diagnostics.js';
import { errorLabel } from './journal-maintenance.js';
import { authorize, readJson } from './server.js';

// Per-line process mode (WHATSAPP_LINKED_DEVICE_PROCESS_MODE=per_line).
//
// The supervisor keeps everything the Python API talks to: the loopback port,
// the shared secret, the /v1 routes, lines.json and retired-phones.json. Each
// number runs in its own worker process (line-worker.js) with its own Baileys
// socket, journals, callback outbox and freeze watchdog. A worker that
// crashes, freezes or is killed is restarted on its own after a short backoff;
// requests for that number get a "not sent, retry" 503 meanwhile, and every
// other number keeps sending and receiving.
//
// What spans numbers stays here and is reached over IPC: removal tombstones,
// retired phones, the "already linked to another line" check, and the single
// storage checkpoint token (memory bound across all workers).
const WORKER_ENTRY = fileURLToPath(new URL('./line-worker.js', import.meta.url));
const RESTART_DELAYS_MS = [1_000, 2_000, 5_000, 10_000, 30_000];
const STABLE_RUN_MS = 120_000;
const RETIRED_PHONE_MS = 7 * 24 * 60 * 60_000;
const CLAIM_STARTUP_WAIT_MS = 30_000;
const STATUS_RESPONSE_LIMIT = 4 * 1024 * 1024;
const LINE_ID = /^\d{5,32}$/;
const RANK = { ok: 0, warning: 1, critical: 2 };
const RECOVERY_RANK = { none: 0, automatic_after_capacity_recovers: 1, operator_attention: 2, review_required: 3 };

function unavailableError(record) {
  const error = new BridgeError(503, 'LINE_PROCESS_UNAVAILABLE', record?.state === 'stopping'
    ? "This number's WhatsApp process is stopping."
    : "This number's WhatsApp process is starting. Replies will wait and retry.");
  error.delivery_uncertain = false;
  return error;
}

async function syncDirectory(directory) {
  const handle = await openFile(directory, 'r');
  try { await handle.sync(); } finally { await handle.close(); }
}

export class LineSupervisor {
  constructor(config, dependencies = {}) {
    this.config = config;
    this.workerConfig = { stopTimeoutMs: 80_000, startTimeoutMs: 120_000, statusTimeoutMs: 2_000, ...config.workers };
    this.fork = dependencies.fork ?? forkProcess;
    this.workerEntry = dependencies.workerEntry ?? WORKER_ENTRY;
    this.workerEnv = dependencies.workerEnv ?? process.env;
    this.restartDelays = dependencies.restartDelaysMs ?? RESTART_DELAYS_MS;
    this.claimStartupWaitMs = dependencies.claimStartupWaitMs ?? CLAIM_STARTUP_WAIT_MS;
    this.log = dependencies.log ?? (entry => console.info(JSON.stringify(entry)));
    this.records = new Map();
    this.registrationTail = Promise.resolve();
    this.phoneReservations = new Map();
    this.checkpoint = { holder: null, queue: [] };
    this.stopping = false;
    this.initialized = false;
    this.startupPhase = 'created';
    this.startedAt = Date.now();
    this.eventLoop = new EventLoopDiagnostics();
  }

  logError(event, error, lineId = null) {
    this.log({ component: 'whatsapp_bridge', event, ...(lineId ? { line_id: lineId } : {}), code: errorLabel(error) });
  }

  initialize() {
    this.initializePromise ??= this.initializeOnce();
    return this.initializePromise;
  }
  async initializeOnce() {
    const dataDir = this.config.dataDir;
    this.startupPhase = 'storage_replay';
    this.manifest = await new FileStore(path.join(dataDir, 'lines.json'), () => ({ version: 1, lines: {}, requests: {} })).load();
    this.retiredPhones = await new FileStore(path.join(dataDir, 'retired-phones.json'), () => ({ version: 1, phones: {} })).load();
    // Bound phone per number, so the duplicate-link check does not have to
    // wait for every worker's journal replay after a restart.
    this.linePhones = await new FileStore(path.join(dataDir, 'line-phones.json'), () => ({ version: 1, phones: {} })).load();
    const manifest = validateLineManifest(this.manifest.data);
    for (const id of manifest.removed_line_ids) {
      if (this.stopping) return this;
      try { await this.purgeLineFolder(id); } catch (error) { this.logError('removed_line_purge_failed', error, id); }
    }
    // Same routing rule as the single-process bridge.
    const definitions = new Map(this.config.lines
      .filter(line => !manifest.removed_line_ids.includes(line.line_id) &&
        !(line.line_id === this.config.fallbackLineId && (Object.keys(manifest.lines).length || manifest.removed_line_ids.length)))
      .map(line => [line.line_id, line]));
    for (const [id, expected] of Object.entries(manifest.lines)) {
      if (definitions.has(id) && definitions.get(id).expected_phone !== expected) throw new Error('The linked-device manifest conflicts with a configured line.');
      definitions.set(id, { line_id: id, expected_phone: expected });
    }
    if (this.stopping) return this;
    // Only a data directory last used in single mode has a shared outbox. The
    // migration (journal store, provider JSON) is loaded only to split it.
    const names = await readdir(dataDir);
    if (names.some(name => name === 'outbox-migration.json' || name === 'outbox.json' || name.startsWith('outbox.json.'))) {
      this.startupPhase = 'outbox_migration';
      const { splitSharedOutbox } = await import('./outbox-migration.js');
      await splitSharedOutbox(dataDir, [...definitions.keys()], { log: this.log });
    }
    await mkdir(path.join(dataDir, '.workers'), { recursive: true, mode: 0o700 });
    this.startupPhase = 'starting_workers';
    for (const definition of definitions.values()) {
      if (this.stopping) return this;
      const record = this.createRecord(definition.line_id, definition.expected_phone);
      this.records.set(record.lineId, record);
      this.spawn(record);
    }
    // Route at once: a number answers as soon as its own worker is ready.
    this.initialized = true;
    this.startupPhase = 'ready';
    return this;
  }
  startWorkers() { if (!this.stopping) this.eventLoop.start(); }

  createRecord(lineId, expectedPhone) {
    return { lineId, expectedPhone, socketPath: workerSocketPath(this.config.dataDir, lineId),
      child: null, pid: null, state: 'stopped', restarts: 0, failures: 0, startedAt: null, restartAt: null,
      lastExit: null, restartTimer: null, readyWaiters: [], exitWaiters: [], report: null, reported: false,
      lastStatus: null, unavailableSince: Date.now(), removing: false };
  }
  recordOf(id) {
    const record = this.records.get(id);
    if (!record) throw new BridgeError(404, 'UNKNOWN_LINE', 'This linked-device line is not configured.');
    return record;
  }

  // ---- Worker processes
  spawn(record) {
    if (this.stopping) return;
    clearTimeout(record.restartTimer);
    record.restartTimer = null;
    const env = { ...this.workerEnv, WHATSAPP_LINKED_DEVICE_WORKER_LINE_ID: record.lineId,
      WHATSAPP_LINKED_DEVICE_WORKER_EXPECTED_PHONE: record.expectedPhone };
    let child;
    record.state = 'starting';
    record.startedAt = Date.now();
    try {
      child = this.fork(this.workerEntry, [], { env, execArgv: [], serialization: 'json', stdio: ['ignore', 'inherit', 'inherit', 'ipc'] });
    } catch (error) {
      this.workerExited(record, null, null, null, error);
      return;
    }
    record.child = child;
    record.pid = child.pid ?? null;
    child.on('message', message => this.onWorkerMessage(record, child, message));
    child.once('exit', (code, signal) => this.workerExited(record, child, code, signal));
    // A failed spawn reports only 'error'; a running child's IPC errors are not exits.
    child.on('error', error => { if (child.pid === undefined) this.workerExited(record, child, null, null, error); });
    this.log({ component: 'whatsapp_bridge', event: 'line_worker_started', line_id: record.lineId, pid: record.pid });
  }
  workerExited(record, child, code, signal, error = null) {
    if (child && record.child !== child) return;
    record.child = null;
    record.pid = null;
    if (child) this.dropCheckpoint(child);
    for (const [phone, owner] of this.phoneReservations) if (owner === record.lineId) this.phoneReservations.delete(phone);
    record.lastExit = { code, signal, at: Date.now() };
    record.unavailableSince ??= Date.now();
    for (const resolve of record.exitWaiters.splice(0)) resolve();
    for (const waiter of record.readyWaiters.splice(0)) waiter.reject(unavailableError(record));
    if (record.state === 'stopping' || record.removing || this.stopping) {
      record.state = 'stopped';
      this.log({ component: 'whatsapp_bridge', event: 'line_worker_stopped', line_id: record.lineId, exit_code: code, signal });
      return;
    }
    if (record.startedAt && Date.now() - record.startedAt >= STABLE_RUN_MS) record.failures = 0;
    const delay = this.restartDelays[Math.min(record.failures, this.restartDelays.length - 1)];
    record.failures++;
    record.restarts++;
    record.state = 'backoff';
    record.restartAt = Date.now() + delay;
    this.log({ component: 'whatsapp_bridge', event: 'line_worker_exited', line_id: record.lineId, exit_code: code, signal,
      ...(error ? { code: errorLabel(error) } : {}), restart_in_ms: delay, restarts: record.restarts });
    record.restartTimer = setTimeout(() => {
      record.restartTimer = null;
      if (!this.stopping && record.state === 'backoff' && this.records.get(record.lineId) === record) this.spawn(record);
    }, delay);
  }
  async stopWorker(record, timeoutMs = this.workerConfig.stopTimeoutMs) {
    clearTimeout(record.restartTimer);
    record.restartTimer = null;
    const child = record.child;
    if (!child) { record.state = 'stopped'; return; }
    record.state = 'stopping';
    const exited = new Promise(resolve => record.exitWaiters.push(resolve));
    try { child.kill('SIGTERM'); } catch { /* exit is still observed */ }
    // Same as systemd's stop: a worker that cannot drain in time is killed.
    // Its saved session resumes on the next start (unclean-exit resume).
    const timer = setTimeout(() => {
      this.log({ component: 'whatsapp_bridge', event: 'line_worker_stop_timeout', line_id: record.lineId, timeout_ms: timeoutMs });
      try { child.kill('SIGKILL'); } catch { /* already gone */ }
    }, timeoutMs);
    try { await exited; } finally { clearTimeout(timer); }
  }
  waitReady(record, timeoutMs) {
    if (record.state === 'ready') return Promise.resolve();
    if (!record.child) return Promise.reject(unavailableError(record));
    return new Promise((resolve, reject) => {
      const waiter = { resolve: () => { clearTimeout(timer); resolve(); }, reject: error => { clearTimeout(timer); reject(error); } };
      const timer = setTimeout(() => {
        record.readyWaiters = record.readyWaiters.filter(item => item !== waiter);
        reject(new BridgeError(504, 'LINE_PROCESS_START_TIMEOUT', "This number's WhatsApp process did not start in time."));
      }, timeoutMs);
      record.readyWaiters.push(waiter);
    });
  }

  onWorkerMessage(record, child, message) {
    if (record.child !== child || !message || typeof message !== 'object') return;
    if (message.type === 'ready' || message.type === 'report') {
      this.applyReport(record, message.state);
      if (message.type === 'ready' && record.state === 'starting') {
        record.state = 'ready';
        record.unavailableSince = null;
        for (const waiter of record.readyWaiters.splice(0)) waiter.resolve();
        this.log({ component: 'whatsapp_bridge', event: 'line_worker_ready', line_id: record.lineId, pid: record.pid,
          startup_ms: Date.now() - record.startedAt });
      }
      return;
    }
    if (message.type === 'rpc' && Number.isSafeInteger(message.id)) void this.handleRpc(record, child, message);
  }
  applyReport(record, state) {
    if (!state || typeof state !== 'object' || state.line_id !== record.lineId) return;
    let bound = null;
    try { bound = state.bound_phone ? phoneDigits(state.bound_phone) : null; } catch { bound = null; }
    record.report = { bound_phone: bound, status: typeof state.status === 'string' ? state.status : null,
      disconnected_since: Number.isFinite(state.disconnected_since) ? state.disconnected_since : null,
      recover_since: Number.isFinite(state.recover_since) ? state.recover_since : null,
      runtime: state.runtime && typeof state.runtime === 'object' ? state.runtime : null };
    record.reported = true;
    if (bound) this.rememberPhone(record.lineId, bound).catch(error => this.logError('line_phone_persist_failed', error, record.lineId));
  }

  async handleRpc(record, child, { id, method, params }) {
    if (record.child !== child) return;
    let reply;
    try { reply = { result: await this.rpc(record, child, method, params && typeof params === 'object' ? params : {}) ?? null }; }
    catch (error) {
      const known = error instanceof BridgeError;
      if (!known) this.logError('supervisor_rpc_failed', error, record.lineId);
      reply = { error: { status: known ? error.status : 500, code: known ? error.code : 'SUPERVISOR_ERROR',
        message: known ? error.message : 'The bridge supervisor could not complete this operation.' } };
    }
    if (record.child !== child || !child.connected) return;
    try { child.send({ type: 'rpc_result', id, ...reply }, () => {}); } catch { /* the exit handler cleans up */ }
  }
  async rpc(record, child, method, params) {
    const lineId = record.lineId;
    switch (method) {
      case 'tombstone':
        // A worker can only remove its own number.
        if (params.line_id !== lineId) throw new BridgeError(403, 'FORBIDDEN_LINE', 'A worker can only remove its own number.');
        await this.manifest.update(data => tombstoneManifest(data, lineId));
        record.removing = true;
        await this.forgetPhone(lineId);
        return { removed: true };
      case 'retirePhone': {
        const offlineSince = Number(params.offline_since);
        if (!Number.isFinite(offlineSince)) throw new BridgeError(400, 'INVALID_TIME', 'A numeric offline time is required.');
        await this.retirePhone(phoneDigits(params.phone), offlineSince);
        return { retired: true };
      }
      case 'retiredPhone':
        return this.retiredPhones.data.phones[phoneDigits(params.phone)] ?? null;
      case 'dropRetiredPhone': {
        const phone = phoneDigits(params.phone);
        await this.retiredPhones.update(data => { delete data.phones[phone]; });
        return { dropped: true };
      }
      case 'claimPhone':
        return this.claimPhone(lineId, phoneDigits(params.phone));
      case 'settlePhone': {
        const phone = phoneDigits(params.phone);
        if (this.phoneReservations.get(phone) === lineId) this.phoneReservations.delete(phone);
        if (params.bound === true) {
          record.report = { ...record.report, bound_phone: phone };
          await this.rememberPhone(lineId, phone);
        }
        return { settled: true };
      }
      case 'checkpointAcquire':
        await this.acquireCheckpoint(child);
        return { granted: true };
      case 'checkpointRelease':
        this.releaseCheckpoint(child);
        return { released: true };
      default:
        throw new BridgeError(404, 'UNKNOWN_OPERATION', 'Unknown supervisor operation.');
    }
  }

  // ---- Phones
  async retirePhone(phone, offlineSince) {
    await this.retiredPhones.update(data => {
      const cutoff = Date.now() - RETIRED_PHONE_MS;
      for (const [known, entry] of Object.entries(data.phones)) if (!(entry?.removed_at > cutoff)) delete data.phones[known];
      data.phones[phone] = { offline_since: Math.min(data.phones[phone]?.offline_since ?? Infinity, offlineSince), removed_at: Date.now() };
    });
  }
  async rememberPhone(lineId, phone) {
    if (this.linePhones.data.phones[lineId] === phone) return;
    await this.linePhones.update(data => { data.phones[lineId] = phone; });
  }
  async forgetPhone(lineId) {
    if (!Object.hasOwn(this.linePhones.data.phones, lineId)) return;
    await this.linePhones.update(data => { delete data.phones[lineId]; });
  }
  boundPhoneOf(record) {
    // Once its worker has reported, the live journal value wins.
    return record.reported ? record.report?.bound_phone ?? null : this.linePhones.data.phones[record.lineId] ?? null;
  }
  async claimPhone(lineId, phone) {
    // After a restart, a slow worker may not have reported its bound phone yet.
    // line-phones.json covers numbers this supervisor has seen before.
    const deadline = this.startedAt + this.claimStartupWaitMs;
    while (!this.stopping && Date.now() < deadline &&
           [...this.records.values()].some(record => record.lineId !== lineId && !record.removing && !record.reported)) {
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    if (this.stopping) throw new BridgeError(503, 'STOPPING', 'The bridge is shutting down.');
    const owner = [...this.records.values()].find(record => record.lineId !== lineId && !record.removing && this.boundPhoneOf(record) === phone);
    const reserved = this.phoneReservations.get(phone);
    if (owner || (reserved && reserved !== lineId)) return { owned_elsewhere: true };
    this.phoneReservations.set(phone, lineId);
    return { owned_elsewhere: false };
  }

  // ---- One storage checkpoint at a time across every worker
  acquireCheckpoint(child) {
    if (this.stopping) return Promise.reject(new BridgeError(503, 'STOPPING', 'The bridge is shutting down.'));
    if (!this.checkpoint.holder) { this.checkpoint.holder = child; return Promise.resolve(); }
    return new Promise((resolve, reject) => this.checkpoint.queue.push({ child, resolve, reject }));
  }
  releaseCheckpoint(child) {
    if (this.checkpoint.holder !== child) return;
    this.checkpoint.holder = null;
    const next = this.checkpoint.queue.shift();
    if (next) { this.checkpoint.holder = next.child; next.resolve(); }
  }
  dropCheckpoint(child) {
    const dropped = this.checkpoint.queue.filter(waiter => waiter.child === child);
    this.checkpoint.queue = this.checkpoint.queue.filter(waiter => waiter.child !== child);
    for (const waiter of dropped) waiter.reject(new BridgeError(503, 'LINE_PROCESS_EXITED', 'The worker exited.'));
    this.releaseCheckpoint(child);
  }

  // ---- Talking to one worker
  request(record, method, pathname, { body, timeoutMs } = {}) {
    return new Promise((resolve, reject) => {
      const json = body === undefined ? null : JSON.stringify(body);
      const request = http.request({ socketPath: record.socketPath, method, path: pathname, agent: false,
        headers: { authorization: `Bearer ${this.config.secret}`,
          ...(json === null ? {} : { 'content-type': 'application/json', 'content-length': Buffer.byteLength(json) }) } });
      let timer;
      if (timeoutMs) timer = setTimeout(() => request.destroy(Object.assign(new Error('WORKER_TIMEOUT'), { code: 'WORKER_TIMEOUT' })), timeoutMs);
      request.on('error', error => { clearTimeout(timer); reject(error); });
      request.on('response', response => {
        const chunks = [];
        let size = 0;
        response.on('data', chunk => {
          size += chunk.length;
          if (size > STATUS_RESPONSE_LIMIT) { request.destroy(Object.assign(new Error('WORKER_RESPONSE_TOO_LARGE'), { code: 'WORKER_RESPONSE_TOO_LARGE' })); return; }
          chunks.push(chunk);
        });
        response.on('end', () => {
          clearTimeout(timer);
          try { resolve({ status: response.statusCode, payload: JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') }); }
          catch { reject(Object.assign(new Error('INVALID_WORKER_RESPONSE'), { code: 'INVALID_WORKER_RESPONSE' })); }
        });
        response.on('error', error => { clearTimeout(timer); reject(error); });
      });
      request.end(json ?? undefined);
    });
  }
  async workerStatus(record, timeoutMs = this.workerConfig.statusTimeoutMs) {
    if (record.state !== 'ready') return null;
    try {
      const { status, payload } = await this.request(record, 'GET', '/v1/status', { timeoutMs });
      return status === 200 && payload && typeof payload === 'object' ? payload : null;
    } catch { return null; }
  }
  processInfo(record) {
    return { state: record.state, pid: record.pid, restarts: record.restarts,
      started_at: record.startedAt, restart_at: record.state === 'backoff' ? record.restartAt : null,
      last_exit_code: record.lastExit?.code ?? null, last_exit_signal: record.lastExit?.signal ?? null, last_exit_at: record.lastExit?.at ?? null };
  }
  unavailableLineStatus(record) {
    const bound = this.boundPhoneOf(record);
    const base = record.lastStatus ?? { line_id: record.lineId, display_phone_number: bound || record.expectedPhone,
      is_paired: Boolean(bound), recovery_category: null, session_blocked: false, saved_login: Boolean(bound),
      diagnostics: {}, oldest_callback_age_seconds: 0, pending_callbacks: 0, waiting_history_reactions: 0,
      failed_callbacks: 0, uncertain_sends: 0, logout_reason: null, logged_out_at: null, recovering_history: false };
    const { qr: _qr, qr_data_url: _qrData, ...rest } = base;
    return { ...rest, status: 'disconnected', retry_scheduled: ['starting', 'backoff'].includes(record.state),
      disconnected_since: record.unavailableSince ?? record.lastStatus?.disconnected_since ?? Date.now(), recovering_history: false,
      error: record.state === 'stopping' ? "This number's WhatsApp process is stopping."
        : record.state === 'ready' ? "This number's WhatsApp process is not responding. It restarts on its own if it stays frozen."
        : "This number's WhatsApp process is restarting. Replies will wait until it is back.",
      process: this.processInfo(record) };
  }
  async lineStatusOf(record) {
    const status = await this.workerStatus(record);
    const line = status?.lines?.find?.(item => item?.line_id === record.lineId);
    if (!line) return this.unavailableLineStatus(record);
    record.lastStatus = line;
    return { ...line, process: this.processInfo(record) };
  }

  // ---- Aggregated views (same shapes as the single-process bridge)
  runtimeHealth(workerHealth = null) {
    const records = [...this.records.values()];
    const healths = workerHealth ?? records.map(record => record.state === 'ready' ? record.report?.runtime : null);
    const recoveryRequired = healths.flatMap(health => Array.isArray(health?.recovery_required) ? health.recovery_required : []);
    const recoveryMode = healths.reduce((mode, health) =>
      (RECOVERY_RANK[health?.recovery_mode] ?? 0) > RECOVERY_RANK[mode] ? health.recovery_mode : mode, 'none');
    const unavailable = records.filter(record => record.state !== 'ready').length;
    const phase = this.stopping ? 'stopping' : !this.initialized ? this.startupPhase :
      unavailable ? 'line_process_unavailable' :
      healths.some(health => health?.phase === 'storage_degraded') ? 'storage_degraded' :
      healths.some(health => health && health.ready === false) ? 'line_process_degraded' : 'ready';
    return { live: !this.stopping, ready: phase === 'ready', phase,
      startup_elapsed_ms: this.initialized ? null : Date.now() - this.startedAt,
      recovery_required: recoveryRequired, recovery_mode: recoveryMode,
      connected_lines: records.filter(record => record.state === 'ready' && record.report?.status === 'connected').length,
      configured_lines: records.length,
      process_mode: 'per_line', line_processes: { ready: records.length - unavailable, total: records.length } };
  }
  mergeStorage(storages) {
    const present = storages.filter(item => item && typeof item === 'object');
    const issues = [], seen = new Set();
    for (const storage of present) for (const issue of storage.issues ?? []) {
      // Disk and monitor issues without a number are reported by every worker.
      const key = issue.line_id == null ? `${issue.code}:${issue.level}` : null;
      if (key && seen.has(key)) continue;
      if (key) seen.add(key);
      issues.push(issue);
    }
    const sum = name => present.reduce((total, storage) => total + (Number.isFinite(storage[name]) ? storage[name] : 0), 0);
    const checked = present.map(storage => storage.checked_at).filter(Number.isFinite);
    return { level: issues.reduce((level, issue) => (RANK[issue.level] ?? 0) > RANK[level] ? issue.level : level, 'ok'),
      checked_at: checked.length ? Math.min(...checked) : null,
      disk: present.find(storage => storage.disk)?.disk ?? null,
      pending_optional_write_bytes: sum('pending_optional_write_bytes'), pending_optional_write_inodes: sum('pending_optional_write_inodes'),
      unobserved_optional_write_bytes: sum('unobserved_optional_write_bytes'), unobserved_optional_write_inodes: sum('unobserved_optional_write_inodes'),
      stores: present.flatMap(storage => storage.stores ?? []), issues };
  }
  mergeEventLoop(snapshots) {
    const present = snapshots.filter(item => item && Number.isFinite(item.sampled_at));
    if (!present.length) return { sampled_at: null, p99_ms: null, max_ms: null };
    const max = name => Math.max(...present.map(item => Number.isFinite(item[name]) ? item[name] : 0));
    // Oldest sample, so a stalled observer in any process stays visible.
    return { sampled_at: Math.min(...present.map(item => item.sampled_at)), p99_ms: max('p99_ms'), max_ms: max('max_ms'),
      lag_spikes_total: present.reduce((total, item) => total + (Number.isFinite(item.lag_spikes_total) ? item.lag_spikes_total : 0), 0) };
  }
  async collect() {
    const records = [...this.records.values()];
    return Promise.all(records.map(async record => ({ record, status: await this.workerStatus(record) })));
  }
  async status() {
    const results = await this.collect();
    const lines = results.map(({ record, status }) => {
      const line = status?.lines?.find?.(item => item?.line_id === record.lineId);
      if (!line) return this.unavailableLineStatus(record);
      record.lastStatus = line;
      return { ...line, process: this.processInfo(record) };
    });
    return { lines, storage: this.mergeStorage(results.map(item => item.status?.storage)),
      runtime: this.runtimeHealth(results.map(({ record, status }) => status?.runtime ?? (record.state === 'ready' ? record.report?.runtime : null))),
      event_loop: this.mergeEventLoop([this.eventLoop.snapshot(), ...results.map(item => item.status?.event_loop)]) };
  }
  async storageStatus() {
    return this.mergeStorage((await this.collect()).map(item => item.status?.storage));
  }

  // ---- Registration, removal and restarts (one at a time)
  serialize(action) {
    const task = this.registrationTail.then(() => {
      if (this.stopping) throw new BridgeError(503, 'STOPPING', 'The bridge is shutting down.');
      return action();
    });
    this.registrationTail = task.catch(() => {});
    return task;
  }
  addLine(payload) {
    if (!payload || typeof payload.request_id !== 'string' || !UUID_PATTERN.test(payload.request_id)) throw new BridgeError(400, 'INVALID_REQUEST_ID', 'A UUID registration request ID is required.');
    const requestId = payload.request_id.toLowerCase();
    if (payload.expected_phone !== undefined && typeof payload.expected_phone !== 'string') throw new BridgeError(400, 'INVALID_PHONE', 'Expected phone must be an international phone string or empty.');
    const phone = phoneDigits(payload.expected_phone ?? '', true);
    const reserved = payload.reserved_line_ids ?? [];
    if (!Array.isArray(reserved) || reserved.length > 10000 || reserved.some(id => typeof id !== 'string' || !/^\d{5,64}$/.test(id))) throw new BridgeError(400, 'INVALID_RESERVED_LINES', 'Reserved line IDs must be an array of at most 10000 numeric IDs.');
    return this.serialize(async () => {
      const manifest = validateLineManifest(this.manifest.data);
      const prior = manifest.requests[requestId];
      if (manifest.removed_request_ids.includes(requestId)) throw new BridgeError(409, 'REGISTRATION_REMOVED', 'This registration request belongs to a removed number. Start a new registration request.');
      if (prior) {
        if (prior.expected_phone !== phone) throw new BridgeError(409, 'REGISTRATION_CONFLICT', 'This registration request was already used for a different expected phone.');
        return { ...await this.lineStatusOf(this.recordOf(prior.line_id)), created: false };
      }
      const occupied = new Set([...this.records.keys(), ...Object.keys(manifest.lines), ...manifest.removed_line_ids, ...reserved]);
      let numericId = 90001;
      while (occupied.has(String(numericId))) numericId++;
      const id = String(numericId);
      const record = this.createRecord(id, phone);
      // The worker prepares private storage first; only a ready worker is published.
      this.records.set(id, record);
      try {
        this.spawn(record);
        await this.waitReady(record, this.workerConfig.startTimeoutMs);
        if (this.stopping) throw new BridgeError(503, 'STOPPING', 'The bridge is shutting down.');
        await this.manifest.update(data => {
          data.version = 1;
          data.requests ??= {};
          // Keep a legacy fallback line routable once Python switches to manifest routing.
          const fallback = !manifest.removed_line_ids.includes(this.config.fallbackLineId) && this.records.get(this.config.fallbackLineId);
          if (fallback) data.lines[fallback.lineId] = fallback.expectedPhone;
          data.lines[id] = phone;
          data.requests[requestId] = { line_id: id, expected_phone: phone };
          if (Buffer.byteLength(JSON.stringify(data), 'utf8') > MANIFEST_MAX_BYTES) throw new BridgeError(413, 'MANIFEST_TOO_LARGE', 'The linked-device line manifest exceeds its size limit.');
          validateLineManifest(data);
        });
      } catch (error) {
        // Unpublished: stop the worker, keep whatever it prepared for a retry.
        record.removing = true;
        await this.stopWorker(record).catch(() => {});
        this.records.delete(id);
        throw error;
      }
      return { ...await this.lineStatusOf(record), created: true };
    });
  }
  removeLine(id) {
    if (typeof id !== 'string' || !LINE_ID.test(id)) throw new BridgeError(400, 'INVALID_LINE_ID', 'A numeric linked-device line ID is required.');
    return this.serialize(async () => {
      if (validateLineManifest(this.manifest.data).removed_line_ids.includes(id)) {
        const record = this.records.get(id);
        if (record) await this.retireRecord(record);
        await this.purgeLineFolder(id);
        return { status: 200, payload: { line_id: id, removed: true, unlinked: true } };
      }
      const record = this.recordOf(id);
      // Unlinking needs the number's live session: only its worker can do it.
      if (record.state !== 'ready') throw unavailableError(record);
      let result;
      try { result = await this.request(record, 'DELETE', `/v1/lines/${id}`); }
      catch {
        // The tombstone RPC may have landed before the worker died.
        if (!validateLineManifest(this.manifest.data).removed_line_ids.includes(id)) {
          const error = new BridgeError(502, 'LINE_PROCESS_FAILED', "This number's WhatsApp process stopped during removal. Retry removal.");
          error.delivery_uncertain = true;
          throw error;
        }
        result = { status: 200, payload: { line_id: id, removed: true, unlinked: true } };
      }
      if (result.status === 200 && result.payload?.removed) {
        await this.retireRecord(record);
        await this.purgeLineFolder(id);
      }
      return result;
    });
  }
  forceRemoveLine(id) {
    if (typeof id !== 'string' || !LINE_ID.test(id)) throw new BridgeError(400, 'INVALID_LINE_ID', 'A numeric linked-device line ID is required.');
    return this.serialize(async () => {
      if (validateLineManifest(this.manifest.data).removed_line_ids.includes(id)) return { line_id: id, removed: true, forced: true };
      const record = this.recordOf(id);
      // Works even when the worker is crash-looping: stop it, then act on disk.
      const live = await this.workerStatus(record, 2_000);
      const line = live?.lines?.find?.(item => item?.line_id === id);
      record.removing = true;
      try {
        await this.stopWorker(record);
        const phone = this.boundPhoneOf(record) ?? (() => { try { return phoneDigits(line?.display_phone_number); } catch { return null; } })();
        if (phone) {
          const report = record.report;
          const offlineSince = report?.recover_since ?? (report?.status === 'connected' || line?.status === 'connected'
            ? Date.now() : report?.disconnected_since ?? line?.disconnected_since ?? Date.now());
          await this.retirePhone(phone, offlineSince).catch(error => this.logError('retire_phone_failed', error, id));
        }
        const directory = path.join(this.config.dataDir, id);
        const stamp = new Date().toISOString().replace(/[:.]/g, '-');
        const archive = path.join(path.dirname(this.config.dataDir), 'linked-device-archive', `${stamp}-remove-${id}`);
        await mkdir(path.dirname(archive), { recursive: true, mode: 0o700 });
        try { await rename(directory, archive); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
        await syncDirectory(this.config.dataDir);
        await this.manifest.update(data => tombstoneManifest(data, id));
        await this.forgetPhone(id);
        this.records.delete(id);
        await this.cleanupWorkerFiles(id);
        this.log({ component: 'whatsapp_bridge', event: 'line_force_removed', line_id: id, archive: path.basename(archive) });
        return { line_id: id, removed: true, forced: true };
      } catch (error) {
        record.removing = false;
        if (!this.stopping && this.records.get(id) === record && !record.child) this.spawn(record);
        throw error;
      }
    });
  }
  restartLine(id) {
    if (typeof id !== 'string' || !LINE_ID.test(id)) throw new BridgeError(400, 'INVALID_LINE_ID', 'A numeric linked-device line ID is required.');
    return this.serialize(async () => {
      const record = this.recordOf(id);
      if (record.removing) throw new BridgeError(503, 'LINE_BUSY', 'This number is being changed. Try again shortly.');
      this.log({ component: 'whatsapp_bridge', event: 'line_worker_restart_requested', line_id: id });
      await this.stopWorker(record);
      record.failures = 0;
      this.spawn(record);
      return { line_id: id, restarting: true, process: this.processInfo(record) };
    });
  }
  async retireRecord(record) {
    record.removing = true;
    await this.stopWorker(record);
    this.records.delete(record.lineId);
    await this.forgetPhone(record.lineId);
    await this.cleanupWorkerFiles(record.lineId);
  }
  async purgeLineFolder(id) {
    if (!LINE_ID.test(id)) throw new Error('Invalid removal cleanup line ID.');
    await rm(path.join(this.config.dataDir, id), { recursive: true, force: true });
    await this.forgetPhone(id);
    await this.cleanupWorkerFiles(id);
  }
  async cleanupWorkerFiles(id) {
    const directory = path.join(this.config.dataDir, '.workers');
    await unlink(path.join(directory, `${id}.sock`)).catch(() => {});
    await rm(path.join(directory, id), { recursive: true, force: true }).catch(() => {});
  }

  // ---- Shutdown
  close() {
    this.stopping = true;
    this.closePromise ??= this.closeOnce().finally(() => { this.closed = true; });
    return this.closePromise;
  }
  async closeOnce() {
    this.eventLoop.close();
    await this.initializePromise?.catch(() => {});
    for (const record of this.records.values()) { clearTimeout(record.restartTimer); record.restartTimer = null; }
    for (const waiter of this.checkpoint.queue.splice(0)) waiter.reject(new BridgeError(503, 'STOPPING', 'The bridge is shutting down.'));
    // Every worker drains its own accepted work in parallel (its own SIGTERM path).
    await Promise.allSettled([...this.records.values()].map(record => this.stopWorker(record)));
    await this.registrationTail;
    const failures = [];
    for (const result of await Promise.allSettled([this.manifest, this.retiredPhones, this.linePhones]
      .filter(Boolean).map(store => store.flush()))) if (result.status === 'rejected') failures.push(result.reason);
    if (failures.length) throw new AggregateError(failures, 'BRIDGE_SHUTDOWN_FAILED');
  }
}

// ---- HTTP API in front of the workers (same routes as server.js)
export function createSupervisorServer(supervisor, config) {
  const activeRequests = new Set();
  let activeMediaUploads = 0;
  let requestFailed = false, requestFailure;
  const send = (response, status, payload) => {
    response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
    response.end(JSON.stringify(payload));
  };
  const sendError = (request, response, error) => {
    if (response.headersSent) return response.destroy();
    const known = error instanceof BridgeError;
    const status = known ? error.status : 500;
    if (status >= 500) {
      console.warn(JSON.stringify({ component: 'whatsapp_bridge', event: 'request_failed',
        route: `${request.method} ${String(request.url ?? '').split('?')[0].replace(/\/(messages|media)\/[^/]+/, '/$1/:id')}`,
        http_status: status, error_code: known ? error.code : 'BRIDGE_ERROR', cause_code: errorLabel(error), supervisor: true }));
    }
    const deliveryUncertain = error.delivery_uncertain ?? !known;
    send(response, status, { delivery_uncertain: deliveryUncertain,
      error: { code: known ? error.code : 'BRIDGE_ERROR', message: known ? error.message : 'The bridge could not complete this operation.' } });
  };
  // Stream the request to the number's worker and its answer back unchanged.
  const proxy = (request, response, record) => new Promise(resolve => {
    let connected = false, settled = false;
    const finish = () => { if (!settled) { settled = true; resolve(); } };
    const upstream = http.request({ socketPath: record.socketPath, method: request.method, path: request.url,
      headers: request.headers, agent: false });
    upstream.on('socket', socket => socket.once('connect', () => { connected = true; }));
    upstream.on('response', reply => {
      response.writeHead(reply.statusCode, reply.headers);
      reply.pipe(response);
      reply.on('end', finish);
      reply.on('error', () => { response.destroy(); finish(); });
      response.on('close', finish);
    });
    upstream.on('error', error => {
      if (response.headersSent) { response.destroy(); finish(); return; }
      // Refused before connecting: nothing reached the worker. After that, a
      // POST may have been submitted to WhatsApp before the worker died.
      const failure = connected
        ? new BridgeError(502, 'LINE_PROCESS_FAILED', "This number's WhatsApp process stopped while handling the request.")
        : unavailableError(record);
      failure.delivery_uncertain = connected && request.method === 'POST';
      failure.cause = error;
      sendError(request, response, failure);
      finish();
    });
    // An unfinished client body must not become a complete worker request.
    request.on('close', () => { if (!request.complete) upstream.destroy(); });
    request.pipe(upstream);
  });
  const handleRequest = async (request, response) => {
    let ownsMediaSlot = false;
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    try {
      if (!authorize(request, config.secret)) throw new BridgeError(401, 'UNAUTHORIZED', 'Bridge authentication is required.');
      const pathname = new URL(request.url, 'http://localhost').pathname;
      if (request.method === 'GET' && ['/v1/health/live', '/v1/health/ready'].includes(pathname)) {
        const health = supervisor.runtimeHealth();
        return send(response, (pathname.endsWith('/live') ? health.live : health.ready) ? 200 : 503, health);
      }
      if (supervisor.stopping || supervisor.initialized === false) {
        const error = new BridgeError(503, supervisor.stopping ? 'STOPPING' : 'BRIDGE_INITIALIZING',
          supervisor.stopping ? 'The bridge is shutting down.' : 'The bridge is loading saved sessions. Replies will wait until it is ready.');
        error.delivery_uncertain = false;
        throw error;
      }
      let parts;
      try { parts = pathname.split('/').filter(Boolean).map(decodeURIComponent); }
      catch { throw new BridgeError(400, 'INVALID_PATH', 'The bridge endpoint contains invalid URL encoding.'); }
      if (request.method === 'GET' && pathname === '/v1/status') return send(response, 200, await supervisor.status());
      if (request.method === 'GET' && pathname === '/v1/storage') return send(response, 200, await supervisor.storageStatus());
      if (request.method === 'POST' && parts.length === 2 && parts[0] === 'v1' && parts[1] === 'lines') {
        return send(response, 200, await supervisor.addLine(await readJson(request, 1024 * 1024)));
      }
      if (parts[0] !== 'v1' || parts[1] !== 'lines' || !/^\d{5,32}$/.test(parts[2] ?? '')) throw new BridgeError(404, 'NOT_FOUND', 'Unknown bridge endpoint.');
      const id = parts[2];
      if (request.method === 'DELETE' && parts.length === 3) {
        const { status, payload } = await supervisor.removeLine(id);
        return send(response, status, payload);
      }
      if (request.method === 'POST' && parts.length === 4 && parts[3] === 'force-remove') {
        await readJson(request, 64 * 1024);
        return send(response, 200, await supervisor.forceRemoveLine(id));
      }
      if (request.method === 'POST' && parts.length === 4 && parts[3] === 'restart-process') {
        await readJson(request, 64 * 1024);
        return send(response, 200, await supervisor.restartLine(id));
      }
      const record = supervisor.recordOf(id);
      if (record.state !== 'ready' || record.removing) throw unavailableError(record);
      if (request.method === 'POST' && parts.length === 4 && parts[3] === 'media') {
        // Bound whole-body uploads across all numbers, as the single process does.
        if (activeMediaUploads >= (config.maxConcurrentMediaUploads ?? 1)) {
          const error = new BridgeError(503, 'MEDIA_UPLOAD_BUSY', 'An attachment upload is in progress. This reply will wait and retry.');
          error.delivery_uncertain = false;
          throw error;
        }
        activeMediaUploads++; ownsMediaSlot = true;
      }
      return await proxy(request, response, record);
    } catch (error) {
      sendError(request, response, error);
    } finally {
      if (ownsMediaSlot) activeMediaUploads--;
    }
  };
  const server = http.createServer((request, response) => {
    const task = handleRequest(request, response);
    activeRequests.add(task);
    task.then(() => activeRequests.delete(task), error => {
      activeRequests.delete(task);
      if (!requestFailed) { requestFailed = true; requestFailure = error; }
      try { response.destroy(); } catch { /* keep the handler failure */ }
    });
  });
  server.drainRequests = async () => {
    while (activeRequests.size) await Promise.allSettled([...activeRequests]);
    if (requestFailed) throw new Error('HTTP_REQUEST_DRAIN_FAILED', { cause: requestFailure });
  };
  server.requestTimeout = 60_000;
  server.headersTimeout = 10_000;
  server.keepAliveTimeout = 5000;
  return server;
}
