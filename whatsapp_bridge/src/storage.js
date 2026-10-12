import { mkdir, readFile, open, rename, chmod } from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import lockfile from 'proper-lockfile';
import { BufferJSON, initAuthCreds, proto } from '@whiskeysockets/baileys';

export async function atomicWrite(filename, value) {
  const temporary = `${filename}.${crypto.randomUUID()}.tmp`;
  const handle = await open(temporary, 'wx', 0o600);
  try {
    await handle.writeFile(JSON.stringify(value, BufferJSON.replacer));
    await handle.sync();
  } finally { await handle.close(); }
  await rename(temporary, filename);
  const directory = await open(path.dirname(filename), 'r');
  try { await directory.sync(); } finally { await directory.close(); }
}

export class FileStore {
  constructor(filename, initial) { this.filename = filename; this.initial = initial; this.tail = Promise.resolve(); }
  async load() {
    try { this.data = JSON.parse(await readFile(this.filename, 'utf8'), BufferJSON.reviver); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      this.data = this.initial();
      await atomicWrite(this.filename, this.data);
    }
    return this;
  }
  update(change) {
    const task = this.tail.then(async () => {
      // Copy first: failed writes must never leave uncommitted success in memory.
      const next = JSON.parse(JSON.stringify(this.data, BufferJSON.replacer), BufferJSON.reviver);
      const result = await change(next);
      await atomicWrite(this.filename, next);
      this.data = next;
      return result;
    });
    this.tail = task.catch(() => {});
    return task;
  }
  async flush() { await this.tail; }
}

export async function acquireDataDirectory(directory) {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await chmod(directory, 0o700);
  return lockfile.lock(directory, { realpath: true, stale: 30_000, update: 10_000, retries: 0 });
}

export async function createAuth(directory) {
  const store = await new FileStore(path.join(directory, 'auth.json'), () => ({ creds: initAuthCreds(), keys: {} })).load();
  const creds = store.data.creds;
  return {
    store,
    state: {
      creds,
      keys: {
        get: async (type, ids) => Object.fromEntries(ids.map(id => {
          let value = store.data.keys[type]?.[id];
          if (type === 'app-state-sync-key' && value) value = proto.Message.AppStateSyncKeyData.fromObject(value);
          return [id, value];
        })),
        set: async updates => store.update(data => {
          for (const [type, values] of Object.entries(updates)) {
            data.keys[type] ??= {};
            for (const [id, value] of Object.entries(values)) {
              if (value === null || value === undefined) delete data.keys[type][id];
              else data.keys[type][id] = value;
            }
          }
        }),
      },
    },
    saveCreds: async () => store.update(data => { data.creds = creds; }),
    reset: async () => store.update(data => { data.creds = initAuthCreds(); data.keys = {}; }),
  };
}
