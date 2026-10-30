// OFFLINE ONLY. Own the same directory lock as the bridge for import/compaction
// and export, including validation, so snapshots are never made over live writes.
import { readdir, mkdir, stat, cp, open } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { acquireDataDirectory, atomicWrite } from './storage.js';
import { readJournal } from './journal-store.js';

export async function storageTool(command, directory, destination) {
  if (!['validate', 'import', 'compact', 'export'].includes(command)) throw new Error('INVALID_COMMAND');
  directory = path.resolve(directory);
  if (command === 'export') {
    if (!destination || path.resolve(destination).startsWith(directory + path.sep) || path.resolve(destination) === directory) throw new Error('INVALID_EXPORT_DESTINATION');
    try { await stat(destination); throw new Error('EXPORT_DESTINATION_EXISTS'); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  const release = await acquireDataDirectory(directory);
  try {
    const files = [];
    const entries = await readdir(directory, {withFileTypes: true});
    if (entries.some(item => item.name === 'outbox.json')) files.push('outbox.json');
    for (const entry of entries) if (entry.isDirectory() && /^\d{5,64}$/.test(entry.name)) {
      for (const name of ['state.json', 'auth.json']) {
        try { await stat(path.join(directory, entry.name, name)); files.push(path.join(entry.name, name)); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
      }
    }
    // Validate every store before creating an export or changing any checkpoint.
    const states = [];
    for (const relative of files) {
      const filename = path.join(directory, relative);
      const state = await readJournal(filename, () => ({}));
      if (!state.data || typeof state.data !== 'object') throw new Error('INVALID_STORAGE');
      states.push({filename, relative, ...state});
    }
    if (command === 'export') {
      await mkdir(destination, {mode: 0o700});
      await cp(directory, destination, {recursive: true, filter: source => !/\.(journal|snapshot|lock|tmp)$/.test(source)});
      for (const state of states) await atomicWrite(path.join(destination, state.relative), state.data);
    }
    if (command === 'compact' || command === 'import') {
      for (const state of states) {
        await atomicWrite(state.filename + '.snapshot', {version: 1, seq: state.seq, data: state.data});
        // Commit the snapshot FIRST. A crash before journal truncation is safe:
        // replay skips records whose sequence is already in the snapshot.
        if (command === 'compact') {
          try {
            const handle = await open(state.filename + '.journal', 'r+');
            try { await handle.truncate(0); await handle.sync(); } finally { await handle.close(); }
          }
          catch (error) { if (error.code !== 'ENOENT') throw error; }
        }
      }
    }
    return {stores: states.length, messages: states.reduce((n, state) => n + Object.keys(state.data.messages ?? {}).length, 0),
      incomplete_tail_bytes: states.reduce((n, state) => n + (state.incompleteTailBytes ?? 0), 0)};
  } finally { await release(); }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const [command, directory, destination] = process.argv.slice(2);
  if (!directory) { console.error('Usage: node src/storage-tool.js validate|import|compact|export DATA_DIRECTORY [NEW_EXPORT_DIRECTORY]'); process.exitCode = 1; }
  else storageTool(command, directory, destination).then(result => console.log(JSON.stringify(result)))
    .catch(() => { console.error('Storage operation failed. Directory must be offline, readable, valid, and privately writable. Original history was retained.'); process.exitCode = 1; });
}
