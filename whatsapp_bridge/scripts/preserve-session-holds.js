// One-time upgrade from bridge versions that kept terminal failures in memory.
// Capture /v1/status after stopping API admission, stop the bridge, then run
// with its usual environment: node scripts/preserve-session-holds.js snapshot.json
// Never reads auth files, connects to WhatsApp, or rewrites message records.
import { readFile, lstat } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { acquireDataDirectory } from '../src/file-store.js';
import { JournalStore } from '../src/journal-store.js';
import { readConfig } from '../src/config.js';

const categories = ['authentication_failure', 'session_conflict', 'protocol_failure'];
export async function preserveSessionHolds(dataDir, snapshot, now = Date.now()) {
  if (!Number.isSafeInteger(snapshot?.captured_at) || now - snapshot.captured_at < 0 ||
      now - snapshot.captured_at > 300_000 || !Array.isArray(snapshot.lines)) throw Error('INVALID_OR_STALE_SNAPSHOT');
  const lines = snapshot.lines.filter(line => line.status === 'error' && categories.includes(line.recovery_category));
  if (lines.some(line => !/^\d{5,32}$/.test(line.line_id) || !Number.isSafeInteger(line.disconnected_since) ||
      line.disconnected_since <= 0 || line.disconnected_since > snapshot.captured_at) ||
      new Set(lines.map(line => line.line_id)).size !== lines.length) throw Error('INVALID_HOLD_LINE');
  const release = await acquireDataDirectory(dataDir);
  const changed = [];
  try {
    for (const line of lines) {
      const directory = path.join(dataDir, line.line_id);
      const filename = path.join(directory, 'state.json');
      if (!(await lstat(directory)).isDirectory() || !(await lstat(filename)).isFile()) throw Error('INVALID_STATE_PATH');
      const releaseWorker = await acquireDataDirectory(path.join(dataDir, '.workers', line.line_id));
      const store = new JournalStore(filename, () => { throw Error('MISSING_STATE'); });
      try {
        await store.load();
        if (store.data.last_disconnect?.at > snapshot.captured_at) throw Error('STATE_NEWER_THAN_SNAPSHOT');
        if (!store.data.enabled || store.data.connection_hold === line.recovery_category) continue;
        await store.update(data => {
          data.connection_hold = line.recovery_category;
          data.last_disconnect ??= {at: line.disconnected_since, category: line.recovery_category,
            provider_status: null, reason: 'unknown', source: 'upgrade'};
        });
        changed.push(line.line_id);
      } finally {
        try { await store.close(); } finally { await releaseWorker(); }
      }
    }
  } finally { await release(); }
  return changed;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  process.umask(0o077);
  try {
    if (process.argv.length !== 3) throw Error('SNAPSHOT_REQUIRED');
    const snapshot = JSON.parse(await readFile(process.argv[2], 'utf8'));
    const changed = await preserveSessionHolds(readConfig().dataDir, snapshot);
    console.info(JSON.stringify({event: 'upgrade_session_holds_preserved', lines: changed}));
  } catch (error) {
    console.error(JSON.stringify({event: 'upgrade_session_holds_failed', code: error.code ?? 'HOLD_MIGRATION_FAILED'}));
    process.exitCode = 1;
  }
}
