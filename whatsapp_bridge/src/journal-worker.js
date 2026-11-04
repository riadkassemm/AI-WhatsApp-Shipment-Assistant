// This worker only operates on a captured durable journal prefix and private
// temporary files. The parent owns the directory lock and fences publication.
import {parentPort, workerData} from 'node:worker_threads';
import fs from 'node:fs/promises';
import {createReadStream} from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import {performance} from 'node:perf_hooks';
import {readJournal, scanJournal, encode, hash, syncDirectory} from './journal-store.js';
import {safeStorageCode} from './journal-maintenance.js';

const {filename, cutoff, maintenance, token} = workerData;
const snapshotTemporary = `${filename}.checkpoint-${token}.snapshot.tmp`;
const journalTemporary = `${filename}.checkpoint-${token}.journal.tmp`;
const gates = new Map();
let gateSequence = 0, copied = cutoff.bytes, sequence = cutoff.seq;
let verifiedBytes = 0, verifiedSequence = cutoff.seq;
let journal, snapshotBytes, compactedAt;

async function stage(name) {
  const id = ++gateSequence;
  await new Promise((resolve, reject) => {
    gates.set(id, {resolve, reject});
    parentPort.postMessage({id, stage: name});
  });
}

async function capacity(bytes, inodes = 0) {
  const disk = await fs.statfs(path.dirname(filename));
  if (disk.bavail * disk.bsize < bytes + maintenance.minFreeBytes) throw new Error('STORAGE_LOW_SPACE');
  // files==0 denotes a filesystem that does not expose meaningful inode data.
  if (disk.files > 0 && disk.ffree < inodes + maintenance.minFreeInodes) throw new Error('STORAGE_LOW_INODES');
}

async function prepare() {
  await capacity(0, 2);
  const state = await readJournal(filename, () => { throw new Error('MISSING_STORAGE_BASE'); }, {
    endBytes: cutoff.bytes, maxRecordBytes: maintenance.maxReplayRecordBytes});
  if (state.seq !== cutoff.seq || state.incompleteTailBytes) throw new Error('INVALID_CHECKPOINT_BOUNDARY');
  await stage('prefix_replayed');
  const body = encode({version: 1, seq: state.seq, data: state.data});
  compactedAt = Date.now();
  // Additional v1 fields are ignored by older readers. The checksum covers the
  // version and sequence as well as data; a damaged seq cannot skip transactions.
  const suffix = `,"checksum":"${hash(body)}","compacted_at":${compactedAt}}`;
  snapshotBytes = Buffer.byteLength(body) - 1 + Buffer.byteLength(suffix);
  if (snapshotBytes > maintenance.maxSnapshotBytes) throw new Error('SNAPSHOT_SIZE_LIMIT');
  await capacity(snapshotBytes, 2);
  const snapshot = await fs.open(snapshotTemporary, 'wx', 0o600);
  const expected = crypto.createHash('sha256').update(body.slice(0, -1)).update(suffix).digest('hex');
  try {
    await snapshot.writeFile(body.slice(0, -1));
    await snapshot.writeFile(suffix);
    await stage('snapshot_written');
    await snapshot.sync();
    await stage('snapshot_synced');
  } finally { await snapshot.close(); }
  const actual = crypto.createHash('sha256');
  for await (const chunk of createReadStream(snapshotTemporary)) actual.update(chunk);
  if (actual.digest('hex') !== expected) throw new Error('SNAPSHOT_CHECKSUM_FAILED');
  journal = await fs.open(journalTemporary, 'wx', 0o600);
  await stage('snapshot_ready');
  return {snapshotBytes};
}

async function catchup(target) {
  if (target.bytes < copied || target.seq < sequence) throw new Error('JOURNAL_SEQUENCE_GAP');
  await capacity(target.bytes - copied);
  const scan = await scanJournal(filename + '.journal', record => {
    if (record.seq !== sequence + 1) throw new Error('JOURNAL_SEQUENCE_GAP');
    sequence = record.seq;
  }, {startBytes: copied, endBytes: target.bytes, maxRecordBytes: maintenance.maxReplayRecordBytes,
    onChunk: chunk => journal.writeFile(chunk)});
  if (scan.incompleteTailBytes || sequence !== target.seq) throw new Error('INVALID_CHECKPOINT_BOUNDARY');
  copied = target.bytes;
  return {copied, sequence};
}

async function publish(target) {
  const started = performance.now();
  if (target.bytes - copied > maintenance.finalCatchupBytes) throw new Error('COMPACTION_BUSY');
  await catchup(target);
  // A missing/unexpected active size is an external writer or damaged store.
  let active;
  try { active = await fs.stat(filename + '.journal'); }
  catch (error) { if (error.code === 'ENOENT') throw new Error('JOURNAL_MISSING'); throw error; }
  if (active.size !== target.bytes) throw new Error('JOURNAL_SIZE_CHANGED');
  await journal.sync();
  await verifyShadow();
  await stage('journal_synced');
  await journal.close(); journal = null;
  await stage('before_snapshot_rename');
  await fs.rename(snapshotTemporary, filename + '.snapshot');
  await stage('snapshot_renamed');
  await syncDirectory(filename);
  await stage('snapshot_directory_synced');
  // Snapshot+old journal and snapshot+new suffix are both valid for v1 readers.
  // Never replace the journal until the snapshot's directory entry is durable.
  await fs.rename(journalTemporary, filename + '.journal');
  await stage('journal_renamed');
  await syncDirectory(filename);
  await stage('journal_directory_synced');
  return {snapshotBytes, compactedAt, finalPauseMs: Math.round(performance.now() - started)};
}

async function backgroundCatchup(target) {
  const result = await catchup(target);
  await stage('before_background_journal_sync');
  await journal.sync();
  await verifyShadow();
  await stage('suffix_synced');
  await stage('suffix_copied');
  return result;
}

async function verifyShadow() {
  const endBytes = copied - cutoff.bytes;
  const scan = await scanJournal(journalTemporary, record => {
    if (record.seq !== verifiedSequence + 1) throw new Error('JOURNAL_SEQUENCE_GAP');
    verifiedSequence = record.seq;
  }, {startBytes: verifiedBytes, endBytes, maxRecordBytes: maintenance.maxReplayRecordBytes});
  if (scan.incompleteTailBytes || verifiedSequence !== sequence || (await fs.stat(journalTemporary)).size !== endBytes) {
    throw new Error('INVALID_CHECKPOINT_BOUNDARY');
  }
  verifiedBytes = endBytes;
}

parentPort.on('message', async message => {
  if (message.resume) {
    const gate = gates.get(message.resume);
    gates.delete(message.resume);
    if (message.error) gate?.reject(new Error(message.error)); else gate?.resolve();
    return;
  }
  try {
    const result = await ({prepare, catchup: backgroundCatchup, publish}[message.command])(message.target);
    parentPort.postMessage({id: message.id, result});
  } catch (error) { parentPort.postMessage({id: message.id, error: safeStorageCode(error)}); }
});
