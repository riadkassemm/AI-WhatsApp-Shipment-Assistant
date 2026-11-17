import { mkdir, readFile, rename, chmod, unlink, realpath, rm } from 'node:fs/promises';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import lockfile from 'proper-lockfile';

// Atomic JSON files and the data-directory lock, without the WhatsApp
// provider. storage.js re-exports these with Baileys' Buffer-aware JSON; the
// per-line supervisor uses them directly and so never loads Baileys.
export async function atomicWrite(filename, value, { replacer } = {}) {
  const temporary = `${filename}.${crypto.randomUUID()}.tmp`;
  let published = false;
  try {
    const handle = await fs.open(temporary, 'wx', 0o600);
    try {
      await handle.writeFile(JSON.stringify(value, replacer));
      await handle.sync();
    } finally { await handle.close(); }
    await fs.rename(temporary, filename);
    published = true;
    const directory = await fs.open(path.dirname(filename), 'r');
    try { await directory.sync(); } finally { await directory.close(); }
  } catch (error) {
    // Only this invocation's unpublished temporary is disposable. A failed
    // directory fsync after rename must never undo the destination.
    if (!published) {
      try {
        await fs.unlink(temporary);
        const directory = await fs.open(path.dirname(filename), 'r');
        try { await directory.sync(); } finally { await directory.close(); }
      } catch (_) { /* Preserve the original write failure; recovery never adopts tmp files. */ }
    }
    throw error;
  }
}

export class FileStore {
  constructor(filename, initial, { reviver, replacer } = {}) {
    this.filename = filename; this.initial = initial; this.tail = Promise.resolve();
    this.json = { reviver, replacer };
  }
  async load() {
    try { this.data = JSON.parse(await readFile(this.filename, 'utf8'), this.json.reviver); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      this.data = this.initial();
      await atomicWrite(this.filename, this.data, this.json);
    }
    return this;
  }
  update(change) {
    const task = this.tail.then(async () => {
      // Copy first: failed writes must never leave uncommitted success in memory.
      const next = JSON.parse(JSON.stringify(this.data, this.json.replacer), this.json.reviver);
      const result = await change(next);
      await atomicWrite(this.filename, next, this.json);
      this.data = next;
      return result;
    });
    this.tail = task.catch(() => {});
    return task;
  }
  async flush() { await this.tail; }
}

// One bridge per data directory. After a crash or SIGKILL the lock directory
// stays behind, and proper-lockfile only treats it as stale after 30 s, so
// every restart attempt in between failed with ELOCKED (2026-11-10: ~20 s
// of extra downtime). The holder is now recorded (PID, that process's start
// time and the boot ID). When the recorded holder no longer exists (its PID is
// gone, now belongs to a different process, or the server rebooted), the lock
// was abandoned and is reclaimed at once. A live holder still refuses a second
// bridge, and a lock without a holder record falls back to the 30 s rule. The
// 30 s stale window itself is unchanged: lowering it would make proper-lockfile
// treat a briefly busy bridge's own lock as compromised.
const LOCK_OPTIONS = { realpath: true, stale: 30_000, update: 10_000, retries: 0 };
async function processStart(pid) {
  try {
    const stat = await readFile(`/proc/${pid}/stat`, 'utf8');
    return stat.slice(stat.lastIndexOf(')') + 2).split(' ')[19] ?? null;
  } catch (_) { return null; }
}
async function currentBootId() {
  try { return (await readFile('/proc/sys/kernel/random/boot_id', 'utf8')).trim(); } catch (_) { return null; }
}
function processExists(pid) {
  try { process.kill(pid, 0); return true; } catch (error) { return error.code === 'EPERM'; }
}
export async function lockHolderGone(holderFile) {
  let holder;
  try { holder = JSON.parse(await readFile(holderFile, 'utf8')); } catch (_) { return false; }
  if (!Number.isSafeInteger(holder?.pid) || holder.pid <= 0) return false;
  const boot = await currentBootId();
  if (holder.boot_id && boot && holder.boot_id !== boot) return true;
  if (!processExists(holder.pid)) return true;
  const start = await processStart(holder.pid);
  return Boolean(holder.start && start && holder.start !== start);
}
export async function acquireDataDirectory(directory, { onReclaimed } = {}) {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await chmod(directory, 0o700);
  const target = await realpath(directory);
  const lockDirectory = `${target}.lock`, holderFile = `${target}.lock-holder.json`;
  let release;
  try { release = await lockfile.lock(directory, LOCK_OPTIONS); }
  catch (error) {
    if (error.code !== 'ELOCKED' || !(await lockHolderGone(holderFile))) throw error;
    // Rename first: only one process can take over an abandoned lock.
    const abandoned = `${lockDirectory}.abandoned-${crypto.randomUUID()}`;
    try { await rename(lockDirectory, abandoned); } catch (renameError) { if (renameError.code !== 'ENOENT') throw renameError; }
    await rm(abandoned, { recursive: true, force: true });
    release = await lockfile.lock(directory, LOCK_OPTIONS);
    onReclaimed?.();
  }
  await atomicWrite(holderFile, { version: 1, pid: process.pid, start: await processStart(process.pid),
    boot_id: await currentBootId(), acquired_at: Date.now() });
  return async () => {
    await unlink(holderFile).catch(() => {});
    await release();
  };
}

