import path from 'node:path';
import { mkdir, readdir, readFile, rename, unlink, open as openFile } from 'node:fs/promises';
import { JournalStore } from './journal-store.js';
import { atomicWrite } from './storage.js';

// The callback outbox is one shared store in single-process mode
// (<data>/outbox.json) and one store per number in per-line mode
// (<data>/<line>/outbox.json). Switching modes moves pending callbacks and
// dead letters between the two layouts before any bridge loads them:
//
// 1. Merge the source into the destination store(s), skipping event IDs the
//    destination already has. Repeating this after a crash is harmless.
// 2. Write a marker next to the source. From here on the source is never read
//    again, so a crash while moving its files cannot replay a partial store.
// 3. Move every source file into linked-device-archive/, then delete the marker.
//
// Nothing is deleted: the archived source keeps every original record.
const OUTBOX = 'outbox.json';
const MARKER = 'outbox-migration.json';
const LINE_ID = /^\d{5,32}$/;
const emptyOutbox = () => ({ entries: [], dead_letters: [] });

async function outboxFiles(directory) {
  try {
    return (await readdir(directory)).filter(name => name === OUTBOX || name.startsWith(OUTBOX + '.'));
  } catch (error) {
    if (error.code === 'ENOENT') return [];
    throw error;
  }
}

async function syncDirectory(directory) {
  const handle = await openFile(directory, 'r');
  try { await handle.sync(); } finally { await handle.close(); }
}

async function readMarker(directory) {
  try { return JSON.parse(await readFile(path.join(directory, MARKER), 'utf8')); }
  catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

async function openOutbox(filename) {
  // Maintenance stays off: these stores live only for the copy.
  const store = new JournalStore(filename, emptyOutbox, { maintenance: { enabled: false } });
  await store.load();
  return store;
}

function mergeRecords(data, entries, deadLetters) {
  const known = list => new Set(list.map(item => item?.event?.event_id).filter(Boolean));
  data.entries ??= [];
  data.dead_letters ??= [];
  const pending = known(data.entries), failed = known(data.dead_letters);
  let added = 0;
  for (const entry of entries) if (!pending.has(entry?.event?.event_id)) { data.entries.push(entry); pending.add(entry?.event?.event_id); added++; }
  for (const entry of deadLetters) if (!failed.has(entry?.event?.event_id)) { data.dead_letters.push(entry); failed.add(entry?.event?.event_id); added++; }
  return added;
}

// One journal transaction is limited to 8 MiB; copy in bounded batches.
const BATCH = 200;
async function mergeInto(store, entries, deadLetters) {
  let added = 0;
  for (let offset = 0; offset < entries.length; offset += BATCH) {
    const batch = entries.slice(offset, offset + BATCH);
    added += await store.update(data => mergeRecords(data, batch, []));
  }
  for (let offset = 0; offset < deadLetters.length; offset += BATCH) {
    const batch = deadLetters.slice(offset, offset + BATCH);
    added += await store.update(data => mergeRecords(data, [], batch));
  }
  return added;
}

async function finishMove(directory, archive) {
  const names = await outboxFiles(directory);
  if (names.length) {
    await mkdir(archive, { recursive: true, mode: 0o700 });
    for (const name of names) await rename(path.join(directory, name), path.join(archive, name));
    await syncDirectory(archive);
  }
  try { await unlink(path.join(directory, MARKER)); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  await syncDirectory(directory);
  return names.length;
}

function archivePath(dataDir, label) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  return path.join(path.dirname(dataDir), 'linked-device-archive', `${stamp}-${label}`);
}

// Single → per-line. Records of numbers that are not active stay in the archive.
export async function splitSharedOutbox(dataDir, lineIds, { log = () => {} } = {}) {
  const active = new Set(lineIds);
  let marker = await readMarker(dataDir);
  let moved = 0, orphaned = 0;
  if (!marker) {
    if (!(await outboxFiles(dataDir)).length) return { moved: 0, orphaned: 0 };
    const source = await openOutbox(path.join(dataDir, OUTBOX));
    try {
      const groups = new Map();
      const group = id => { if (!groups.has(id)) groups.set(id, { entries: [], deadLetters: [] }); return groups.get(id); };
      for (const entry of source.data.entries ?? []) group(String(entry?.event?.line_id)).entries.push(entry);
      for (const entry of source.data.dead_letters ?? []) group(String(entry?.event?.line_id)).deadLetters.push(entry);
      for (const [id, records] of groups) {
        if (!active.has(id) || !LINE_ID.test(id)) { orphaned += records.entries.length + records.deadLetters.length; continue; }
        await mkdir(path.join(dataDir, id), { recursive: true, mode: 0o700 });
        const target = await openOutbox(path.join(dataDir, id, OUTBOX));
        try { moved += await mergeInto(target, records.entries, records.deadLetters); }
        finally { await target.close(); }
      }
    } finally { await source.close(); }
    marker = { version: 1, direction: 'split', archive: archivePath(dataDir, 'outbox-split') };
    await atomicWrite(path.join(dataDir, MARKER), marker);
  }
  const files = await finishMove(dataDir, marker.archive);
  log({ component: 'whatsapp_bridge', event: 'outbox_split_for_per_line_mode', moved_records: moved,
    archived_inactive_records: orphaned, archived_files: files });
  return { moved, orphaned };
}

// Per-line → single. Runs when the single-process bridge starts.
export async function mergeLineOutboxes(dataDir, { log = () => {} } = {}) {
  let names;
  try { names = await readdir(dataDir, { withFileTypes: true }); }
  catch (error) { if (error.code === 'ENOENT') return { moved: 0 }; throw error; }
  let shared = null, moved = 0;
  try {
    for (const item of names) {
      if (!item.isDirectory() || !LINE_ID.test(item.name)) continue;
      const directory = path.join(dataDir, item.name);
      let marker = await readMarker(directory);
      if (!marker) {
        if (!(await outboxFiles(directory)).length) continue;
        shared ??= await openOutbox(path.join(dataDir, OUTBOX));
        const source = await openOutbox(path.join(directory, OUTBOX));
        try {
          moved += await mergeInto(shared, source.data.entries ?? [], source.data.dead_letters ?? []);
        } finally { await source.close(); }
        marker = { version: 1, direction: 'merge', archive: archivePath(dataDir, `outbox-merge-${item.name}`) };
        await atomicWrite(path.join(directory, MARKER), marker);
      }
      await finishMove(directory, marker.archive);
    }
  } finally { await shared?.close(); }
  if (moved) log({ component: 'whatsapp_bridge', event: 'outbox_merged_for_single_mode', moved_records: moved });
  return { moved };
}
