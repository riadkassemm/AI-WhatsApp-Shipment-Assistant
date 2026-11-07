// Permanent local abandonment. NEVER repair, flush, compact or recover auth.
import path from 'node:path';
import crypto from 'node:crypto';
import {isDeepStrictEqual} from 'node:util';
import {inventory} from './fenced-auth.js';
import {readJournal} from './journal-store.js';
import {validateLineManifest} from './config.js';

const blocked = code => Object.assign(new Error(code), {code});
const stable = files => Object.fromEntries(Object.entries(files).map(([name, file]) => [name, {hash: file.hash, size: file.size}]));
export const unlinkInstruction = 'On the primary phone for this number, open WhatsApp > Settings (or the three-dot menu) > Linked devices, select only this application’s device, and tap Log out. Leave other offices’ devices linked.';
export function removalResult(record) {
  return {line_id: record.line_id, removed: true, quarantined: true, reuse_allowed: false,
    unlinked: record.remote_unlinked, remote_unlink_state: record.remote_unlinked ? 'confirmed' : 'action_required',
    message: record.remote_unlinked ? 'Removed from this application; WhatsApp device unlinking confirmed.' :
      'Removed from this application; WhatsApp device unlinking requires action.',
    ...(record.remote_unlinked ? {} : {unlink_instruction: unlinkInstruction})};
}

export async function inspectQuarantine(directory, assertStopped) {
  assertStopped();
  const before = await inventory(directory);
  // Authentication may be incomplete/corrupt: preserve its bytes, never parse
  // or approve them. Business state must still replay completely and read-only.
  const state = await readJournal(path.join(directory, 'state.json'), () => {throw blocked('STATE_BASE_MISSING');}, {repairTail: false});
  if (state.incompleteTailBytes || !state.data?.messages || !state.data?.media || !state.data?.operations) throw blocked('STATE_INTEGRITY_UNPROVEN');
  const after = await inventory(directory);
  assertStopped();
  if (!isDeepStrictEqual(before, after)) throw blocked('QUARANTINE_EVIDENCE_CHANGED');
  return {fingerprint: stable(after), state};
}

export async function publishQuarantine({manifest, id, directory, actor, proof, remoteUnlinked = false, assertStopped}) {
  const old = validateLineManifest(manifest.data).quarantined_lines[id];
  if (old) return old;
  const evidence = await inspectQuarantine(directory, assertStopped);
  const record = {version: 1, audit_id: crypto.randomUUID(), line_id: id, created_at: Date.now(),
    actor: String(actor).slice(0,128), state: 'permanently_decommissioned', reuse_allowed: false,
    credential_outcome: 'incomplete_abandoned', remote_unlinked: remoteUnlinked === true,
    proof, fingerprint: evidence.fingerprint};
  await manifest.update(data => {
    assertStopped();
    data.quarantined_lines ??= {};
    if (data.quarantined_lines[id]) throw blocked('QUARANTINE_ALREADY_PUBLISHED');
    data.quarantined_lines[id] = record;
    delete data.lines[id];
    data.removed_request_ids ??= [];
    for (const [requestId, request] of Object.entries(data.requests ?? {})) if (request.line_id === id) {
      delete data.requests[requestId];
      if (!data.removed_request_ids.includes(requestId)) data.removed_request_ids.push(requestId);
    }
    // Deliberately NOT removed_line_ids: that is the destructive purge queue.
    validateLineManifest(data);
  });
  return record;
}

export async function loadQuarantine(directory, record, assertStopped) {
  const evidence = await inspectQuarantine(directory, assertStopped);
  if (!isDeepStrictEqual(evidence.fingerprint, record.fingerprint)) throw blocked('QUARANTINE_EVIDENCE_CHANGED');
  const retained = {state: 'decommissioned', isolation_safe: true, recovery_allowed: false, paired: false,
    blockers: [], historical_intents: null, record,
    stats: () => ({failed: true, journal_bytes: record.fingerprint['auth.json.journal']?.size ?? 0,
      snapshot_bytes: record.fingerprint['auth.json.snapshot']?.size ?? record.fingerprint['auth.json']?.size ?? 0}), maintain: async () => false,
    async verify() {
      try {
        assertStopped();
        if (!isDeepStrictEqual(stable(await inventory(directory)), record.fingerprint)) throw blocked('QUARANTINE_EVIDENCE_CHANGED');
        assertStopped();
      } catch (error) {retained.isolation_safe = false; throw error;}
    }};
  retained.stateStore = {data: evidence.state.data, filename: path.join(directory, 'state.json'),
    stats: () => ({failed: false, sequence: evidence.state.seq, journal_bytes: evidence.state.journalBytes,
      snapshot_bytes: evidence.state.snapshotBytes, replay_records: evidence.state.journalRecords}),
    maintain: async () => false, update: async () => {throw blocked('IDENTITY_DECOMMISSIONED');},
    flush: async () => retained.verify(), close: async () => retained.verify()};
  return retained;
}
