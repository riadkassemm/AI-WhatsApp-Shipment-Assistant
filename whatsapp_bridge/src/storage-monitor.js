import { statfs } from 'node:fs/promises';
import { BridgeError, readStorageConfig } from './config.js';

const rank = {ok: 0, warning: 1, critical: 2};
const finite = value => Number.isFinite(value) && value >= 0 ? value : null;
const count = value => finite(value) ?? 0;
const safeError = value => typeof value === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(value) ? value : null;
const OPTIONAL_WRITE_METADATA_BYTES = 16 * 1024;
const pressureError = () => {
  const error = new BridgeError(503, 'STORAGE_PRESSURE', 'WhatsApp storage needs operator attention before accepting new sends. Existing session and delivery records are retained.');
  error.delivery_uncertain = false;
  return error;
};

// This monitor never changes a store's data or stops evidence/auth writes.
// Outbound admission uses the cached health, before provider submission only.
export class StorageMonitor {
  constructor(directory, {config = {}, log = () => {}, filesystem = statfs, now = Date.now,
    scheduleTimeout = setTimeout, cancelTimeout = clearTimeout} = {}) {
    this.directory = directory;
    this.config = {...readStorageConfig({}).monitoring, ...config};
    this.log = log;
    this.filesystem = filesystem;
    this.now = now;
    this.scheduleTimeout = scheduleTimeout;
    this.cancelTimeout = cancelTimeout;
    this.stores = new Map();
    this.disk = null;
    this.checkedAt = null;
    this.sampleError = false;
    this.createdAt = this.now();
    this.lastAlertAt = 0;
    this.lastAlert = '';
    this.closed = false;
    this.optionalWriteBytes = 0;
    this.optionalWriteInodes = 0;
    this.completedOptionalWriteBytes = 0;
    this.completedOptionalWriteInodes = 0;
    this.observedOptionalWriteBytes = 0;
    this.observedOptionalWriteInodes = 0;
  }

  register(store, {lineId = null, kind}) {
    if (!['auth', 'state', 'outbox'].includes(kind) || (lineId !== null && !/^\d{5,32}$/.test(lineId)) ||
        (kind === 'outbox') !== (lineId === null)) throw new Error('INVALID_STORAGE_SCOPE');
    const key = `${lineId ?? 'shared'}:${kind}`;
    const previous = this.stores.get(key);
    if (previous?.store === store) return;
    this.stores.set(key, {store, lineId, kind, sampledAt: this.now(), written: count(store.stats().total_written_bytes), sampled: false, growth: 0});
  }

  unregister(lineId) {
    for (const [key, entry] of this.stores) if (entry.lineId === lineId) this.stores.delete(key);
  }

  async start() {
    if (this.closed || this.timer) return;
    await this.sample();
    if (this.closed) return;
    this.timer = setInterval(() => { void this.sample(); }, this.config.intervalMs);
    this.timer.unref();
  }

  sample() {
    if (this.closed) return Promise.resolve();
    if (this.sampling) return this.sampling;
    this.sampling = this.collect().finally(() => { this.sampling = null; });
    return this.sampling;
  }

  async probe() {
    // statfs cannot be cancelled. Keep one in flight after a deadline so a stuck
    // filesystem cannot accumulate libuv jobs or hold startup/shutdown forever.
    if (this.probePromise) throw new Error('STORAGE_MONITOR_PENDING');
    const pending = Promise.resolve().then(() => this.filesystem(this.directory));
    this.probePromise = pending;
    const settled = () => { if (this.probePromise === pending) this.probePromise = null; };
    pending.then(settled, settled);
    let timer;
    try {
      return await Promise.race([pending, new Promise((_, reject) => {
        timer = this.scheduleTimeout(() => reject(new Error('STORAGE_MONITOR_TIMEOUT')), this.config.probeTimeoutMs);
      })]);
    } finally { this.cancelTimeout(timer); }
  }

  async collect() {
    // Only completions preceding this probe may be reflected by its result.
    // A write finishing while statfs is in flight remains charged even if the
    // old free-space value arrives after that write releases its active slot.
    const completedBytesBeforeProbe = this.completedOptionalWriteBytes;
    const completedInodesBeforeProbe = this.completedOptionalWriteInodes;
    try {
      const result = await this.probe();
      const blockSize = Number(result.bsize), blocks = Number(result.blocks), available = Number(result.bavail);
      if (![blockSize, blocks, available].every(Number.isSafeInteger) || blockSize <= 0 || blocks <= 0 || available < 0 ||
          available > blocks || !Number.isSafeInteger(blocks * blockSize)) throw new Error('INVALID_FILESYSTEM_METRICS');
      const files = Number(result.files), free = Number(result.ffree);
      if (![files, free].every(Number.isSafeInteger) || files < 0 || free < 0 || free > files) throw new Error('INVALID_FILESYSTEM_METRICS');
      this.disk = {
        block_size: blockSize,
        available_bytes: available * blockSize,
        total_bytes: blocks * blockSize,
        available_percent: available / blocks * 100,
        total_inodes: files > 0 ? files : null,
        available_inodes: files > 0 && free >= 0 ? free : null,
        available_inode_percent: files > 0 && free >= 0 ? free / files * 100 : null,
      };
      this.checkedAt = this.now();
      this.sampleError = false;
      this.observedOptionalWriteBytes = completedBytesBeforeProbe;
      this.observedOptionalWriteInodes = completedInodesBeforeProbe;
    } catch (_) { this.sampleError = true; }
    const now = this.now();
    for (const entry of this.stores.values()) {
      const written = count(entry.store.stats().total_written_bytes);
      const elapsed = now - entry.sampledAt;
      if (entry.sampled && elapsed > 0) entry.growth = Math.max(0, written - entry.written) * 1000 / elapsed;
      entry.written = written;
      entry.sampledAt = now;
      entry.sampled = true;
      // Snapshot construction runs in the store's worker. Never hold up disk
      // sampling or another store's age trigger waiting for that worker.
      if (!this.closed) {
        try { Promise.resolve(entry.store.maintain()).catch(() => {}); }
        catch (_) { /* Store diagnostics and stats retain maintenance failures. */ }
      }
    }
    this.report(this.status());
  }

  status(lineId = null) {
    const issues = [];
    const add = (code, level, entry = null) => issues.push({code, level,
      ...(entry ? {line_id: entry.lineId, kind: entry.kind} : {})});
    const now = this.now(), config = this.config;
    if (this.sampleError || this.checkedAt === null || now - this.checkedAt > config.intervalMs * 3) {
      const unavailableFor = now - (this.checkedAt ?? this.createdAt);
      add('MONITOR_UNAVAILABLE', unavailableFor > config.intervalMs * 3 ? 'critical' : 'warning');
    }
    if (this.disk) {
      const disk = this.disk;
      if (disk.available_bytes <= config.diskCriticalBytes || disk.available_percent <= config.diskCriticalPercent) add('DISK_SPACE_LOW', 'critical');
      else if (disk.available_bytes <= config.diskWarnBytes || disk.available_percent <= config.diskWarnPercent) add('DISK_SPACE_LOW', 'warning');
      if (disk.available_inode_percent !== null && disk.available_inode_percent <= config.inodeCriticalPercent) add('INODES_LOW', 'critical');
      else if (disk.available_inode_percent !== null && disk.available_inode_percent <= config.inodeWarnPercent) add('INODES_LOW', 'warning');
    }
    const stores = [];
    for (const entry of this.stores.values()) {
      if (lineId !== null && entry.lineId !== null && entry.lineId !== lineId) continue;
      const raw = entry.store.stats();
      const stats = {
        line_id: entry.lineId, kind: entry.kind,
        journal_bytes: count(raw.journal_bytes), snapshot_bytes: count(raw.snapshot_bytes),
        journal_growth_bytes_per_second: Math.round(entry.growth),
        transactions_since_compaction: count(raw.transactions_since_compaction),
        sequence: count(raw.sequence), checkpoint_seq: count(raw.checkpoint_seq),
        replay_ms: count(raw.replay_ms), replay_records: count(raw.replay_records),
        total_written_bytes: count(raw.total_written_bytes),
        last_compaction_at: finite(raw.last_compaction_at),
        last_compaction_ms: finite(raw.last_compaction_ms),
        last_compaction_reclaimed_bytes: finite(raw.last_compaction_reclaimed_bytes),
        compaction_failures: count(raw.compaction_failures),
        failure_counters_persistence: raw.health_persistence_failed ? 'degraded' : 'durable',
        last_compaction_error: raw.last_compaction_error ? safeError(raw.last_compaction_error) ?? 'STORAGE_MAINTENANCE_FAILED' : null,
        compaction_running: Boolean(raw.compaction_running), next_compaction_retry_at: finite(raw.next_compaction_retry_at),
        recovery_errors: count(raw.recovery_errors), quarantined_tail_bytes: count(raw.quarantined_tail_bytes),
        failed: Boolean(raw.failed),
      };
      stores.push(stats);
      if (stats.failed) add('STORE_UNAVAILABLE', 'critical', entry);
      if (stats.journal_bytes >= config.journalCriticalBytes) add('JOURNAL_LIMIT', 'critical', entry);
      else if (stats.journal_bytes >= config.journalWarnBytes) add('JOURNAL_LIMIT', 'warning', entry);
      if (stats.snapshot_bytes >= config.snapshotCriticalBytes) add('SNAPSHOT_LIMIT', 'critical', entry);
      else if (stats.snapshot_bytes >= config.snapshotWarnBytes) add('SNAPSHOT_LIMIT', 'warning', entry);
      if (stats.last_compaction_error) add('COMPACTION_FAILED', 'warning', entry);
      if (raw.health_persistence_failed) add('STORAGE_HEALTH_UNAVAILABLE', 'warning', entry);
      if (stats.recovery_errors || stats.quarantined_tail_bytes) add('RECOVERY_ATTENTION', stats.failed ? 'critical' : 'warning', entry);
    }
    const level = issues.reduce((current, issue) => rank[issue.level] > rank[current] ? issue.level : current, 'ok');
    return {level, checked_at: this.checkedAt, disk: this.disk ? {...this.disk} : null,
      pending_optional_write_bytes: this.optionalWriteBytes, pending_optional_write_inodes: this.optionalWriteInodes,
      unobserved_optional_write_bytes: this.completedOptionalWriteBytes - this.observedOptionalWriteBytes,
      unobserved_optional_write_inodes: this.completedOptionalWriteInodes - this.observedOptionalWriteInodes, stores, issues};
  }

  report(status) {
    const signature = JSON.stringify(status.issues);
    const now = this.now();
    if (status.level === 'ok') {
      if (this.lastAlert) this.emit({event: 'storage_recovered', level: 'ok', checked_at: status.checked_at});
      this.lastAlert = '';
      return;
    }
    if (signature === this.lastAlert && now - this.lastAlertAt < this.config.alertRepeatMs) return;
    this.lastAlert = signature;
    this.lastAlertAt = now;
    this.emit({event: 'storage_alert', level: status.level, issues: status.issues, checked_at: status.checked_at,
      available_bytes: status.disk?.available_bytes ?? null, available_inodes: status.disk?.available_inodes ?? null});
  }

  emit(event) {
    try { this.log({component: 'whatsapp_bridge', ...event}); }
    catch (_) { /* Logging cannot block durability or monitoring. */ }
  }

  assertCanSubmit(lineId) {
    if (this.status(lineId).level !== 'critical') return;
    throw pressureError();
  }

  reserveOptionalWrite(lineId, bytes) {
    if (!Number.isSafeInteger(bytes) || bytes < 0) throw new Error('INVALID_STORAGE_RESERVATION');
    this.assertCanSubmit(lineId);
    if (!this.disk || this.sampleError || this.closed) throw pressureError();
    // Tiny files still allocate whole filesystem blocks. Reserve a separate
    // 16 KiB allowance for the metadata journal append and directory growth;
    // round both allocations independently so payload alignment cannot borrow
    // that essential-write headroom. This is a conservative admission budget,
    // not a filesystem quota or a bound on unrelated essential writes.
    const blockSize = this.disk.block_size;
    const reservedBytes = Math.ceil(bytes / blockSize) * blockSize +
      Math.ceil(OPTIONAL_WRITE_METADATA_BYTES / blockSize) * blockSize;
    if (!Number.isSafeInteger(reservedBytes)) throw pressureError();
    // Admission and reservation are synchronous, so concurrent uploads cannot
    // each claim the same free capacity between filesystem samples. A later
    // sample may already include writes still reserved; double-counting them
    // is deliberately conservative until a successful post-completion probe.
    const unobservedBytes = this.completedOptionalWriteBytes - this.observedOptionalWriteBytes;
    const unobservedInodes = this.completedOptionalWriteInodes - this.observedOptionalWriteInodes;
    const remaining = this.disk.available_bytes - this.optionalWriteBytes - unobservedBytes - reservedBytes;
    const inodes = this.disk.available_inodes === null ? null : this.disk.available_inodes - this.optionalWriteInodes - unobservedInodes - 1;
    if (remaining <= this.config.diskCriticalBytes || remaining / this.disk.total_bytes * 100 <= this.config.diskCriticalPercent ||
        (inodes !== null && inodes / this.disk.total_inodes * 100 <= this.config.inodeCriticalPercent)) {
      throw pressureError();
    }
    this.optionalWriteBytes += reservedBytes;
    this.optionalWriteInodes++;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      // Treat completion conservatively as a write even when a caller failed
      // before publication. Only a subsequent successful probe can reclaim
      // this debit; essential persistence never uses these reservations.
      this.completedOptionalWriteBytes += reservedBytes;
      this.completedOptionalWriteInodes++;
      this.optionalWriteBytes -= reservedBytes;
      this.optionalWriteInodes--;
    };
  }

  async close() {
    this.closed = true;
    clearInterval(this.timer);
    await this.sampling;
  }
}
