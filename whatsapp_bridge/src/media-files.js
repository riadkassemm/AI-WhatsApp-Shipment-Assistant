import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';

// Only unpublished files created by this writer have this exact double-UUID
// name. Authoritative .bin files and unrelated temporary files are never swept.
const uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const temporaryName = new RegExp(`^${uuid}\\.bin\\.${uuid}\\.tmp$`);

export class MediaFiles {
  constructor(filesystem = fs) { this.fs = filesystem; this.directories = new Map(); }
  serial(directory, operation) {
    let state = this.directories.get(directory);
    if (!state) this.directories.set(directory, state = {tail: Promise.resolve(), scanned: false, pending: new Set()});
    const task = state.tail.then(() => operation(state));
    state.tail = task.catch(() => {});
    return task;
  }
  async sync(directory) {
    const handle = await this.fs.open(directory, 'r');
    try { await handle.sync(); } finally { await handle.close(); }
  }
  async cleanup(directory, state) {
    if (!state.scanned) {
      for (const name of await this.fs.readdir(directory)) if (temporaryName.test(name)) state.pending.add(path.join(directory, name));
      state.scanned = true;
    }
    if (!state.pending.size) return;
    // Keep the complete pending set through directory sync. A failed unlink or
    // sync prevents allocating another attempt, including after a partial sweep.
    for (const filename of state.pending) {
      try { await this.fs.unlink(filename); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
    await this.sync(directory);
    state.pending.clear();
  }
  prepare(directory) { return this.serial(directory, state => this.cleanup(directory, state)); }
  write(filename, bytes) {
    const directory = path.dirname(filename);
    return this.serial(directory, async state => {
      await this.cleanup(directory, state);
      const temporary = `${filename}.${crypto.randomUUID()}.tmp`;
      let created = false, published = false;
      try {
        const handle = await this.fs.open(temporary, 'wx', 0o600);
        created = true;
        try { await handle.writeFile(bytes); await handle.sync(); } finally { await handle.close(); }
        await this.fs.rename(temporary, filename);
        published = true;
        await this.sync(directory);
      } catch (error) {
        if (created && !published) {
          state.pending.add(temporary);
          try { await this.cleanup(directory, state); } catch { /* Retry before another allocation. */ }
        }
        throw error;
      }
    });
  }
}
