// OFFLINE ONLY. Own the same directory lock as the bridge for import/compaction
// and export, including validation, so snapshots are never made over live writes.
import { readdir, mkdir, stat, cp } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { acquireDataDirectory, atomicWrite, assertAuthRecoveryClear } from './storage.js';
import { JournalStore, readJournal, encode, hash } from './journal-store.js';
import { readStorageConfig } from './config.js';
import { hasAuthFence, inspectAuthRetention } from './fenced-auth.js';

export async function storageTool(command, directory, destination, {maintenance = readStorageConfig().maintenance} = {}) {
  if (!['validate', 'validate-retained', 'import', 'compact', 'export'].includes(command)) throw new Error('INVALID_COMMAND');
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
    const evidence = (names, base) => names.some(name => name === base || name.startsWith(base + '.'));
    if (evidence(entries.map(item => item.name), 'outbox.json')) files.push('outbox.json');
    for (const entry of entries) if (entry.isDirectory() && /^\d{5,64}$/.test(entry.name)) {
      const names = await readdir(path.join(directory, entry.name));
      for (const name of ['state.json', 'auth.json']) {
        if (evidence(names, name)) files.push(path.join(entry.name, name));
      }
    }
    // Older bridges cannot interpret provider transaction intents/failures.
    // No command may report a rollback-ready export or rewrite another store
    // while unresolved authentication evidence remains anywhere in this copy.
    const retainedLines = [], eligibleLines = [];
    for (const relative of files) {
      if (path.basename(relative) !== 'auth.json') continue;
      const identityDirectory = path.dirname(path.join(directory, relative));
      if (command === 'validate-retained' && await hasAuthFence(identityDirectory)) {
        const evidence = await inspectAuthRetention(identityDirectory, release.assertHeld);
        if (!evidence.result.isolation_safe) throw new Error('AUTH_ISOLATION_UNPROVEN');
        retainedLines.push(path.dirname(relative));
      } else {
        await assertAuthRecoveryClear(path.join(directory, relative));
        eligibleLines.push(path.dirname(relative));
      }
    }
    // Validate every store before creating an export or changing any checkpoint.
    const states = [];
    for (const relative of files) {
      const filename = path.join(directory, relative);
      const state = await readJournal(filename, () => ({}), {maxRecordBytes: maintenance.maxReplayRecordBytes});
      if (!state.data || typeof state.data !== 'object') throw new Error('INVALID_STORAGE');
      // Validation may report an incomplete final record. Every transforming
      // command refuses to omit that evidence; normal recovery quarantines it
      // durably before repairing, after a complete private backup.
      if (command !== 'validate' && state.incompleteTailBytes) throw new Error('INCOMPLETE_JOURNAL_TAIL');
      states.push({filename, relative, ...state});
    }
    if (command === 'export') {
      await mkdir(destination, {mode: 0o700});
      await cp(directory, destination, {recursive: true, filter: source => !/\.(journal|snapshot|lock|tmp)$/.test(source)});
      for (const state of states) await atomicWrite(path.join(destination, state.relative), state.data);
    }
    if (command === 'compact' || command === 'import') {
      for (const state of states) {
        if (command === 'compact') {
          const store = await new JournalStore(state.filename, () => ({}), {maintenance: {...maintenance, enabled: false}}).load();
          try { await store.compact(); } finally { await store.close(); }
        } else {
          const snapshot = {version: 1, seq: state.seq, data: state.data};
          await atomicWrite(state.filename + '.snapshot', {...snapshot, checksum: hash(encode(snapshot))});
        }
      }
    }
    return {stores: states.length, messages: states.reduce((n, state) => n + Object.keys(state.data.messages ?? {}).length, 0),
      incomplete_tail_bytes: states.reduce((n, state) => n + (state.incompleteTailBytes ?? 0), 0),
      ...(command === 'validate-retained' ? {retained_lines: retainedLines.sort(), eligible_lines: eligibleLines.sort(), recovery_allowed: false} : {})};
  } finally { await release(); }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  const [command, directory, destination] = process.argv.slice(2);
  if (!directory) { console.error('Usage: node src/storage-tool.js validate|validate-retained|import|compact|export DATA_DIRECTORY [NEW_EXPORT_DIRECTORY]'); process.exitCode = 1; }
  else storageTool(command, directory, destination).then(result => console.log(JSON.stringify(result)))
    .catch(() => { console.error('Storage operation failed. Directory must be offline, readable, valid, and privately writable. Original history was retained.'); process.exitCode = 1; });
}
