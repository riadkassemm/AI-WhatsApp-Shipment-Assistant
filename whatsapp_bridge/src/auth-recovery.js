import { isDeepStrictEqual } from 'node:util';
import crypto from 'node:crypto';

const sources = new Set(['get', 'set', 'transaction', 'pipeline', 'messageMutex', 'notificationMutex',
  'receiptMutex', 'appStatePatchMutex', 'bridgeTask', 'bridgeDetachedTask']);
const outcomes = new Set(['completed', 'rejected', 'unresolved', 'not_started']);

// Pure assessment, deliberately not an authorization token or a repair function.
// Private objects enter; only fixed labels, booleans and counts may leave.
export function assessAuthRecovery({failure, active, intents, journal, live} = {}) {
  const blockers = new Set();
  const r = failure?.version === 2 && failure?.recovery?.version === 1 ? failure.recovery : null;
  const operations = Array.isArray(r?.operations) ? r.operations : [];
  if (!r || !Array.isArray(failure?.failures) || !failure.failures.length) blockers.add('FINAL_EVIDENCE_MISSING');
  if (!Array.isArray(r?.operations) || !Number.isSafeInteger(r?.revision) || r.revision < 1 ||
      !Number.isSafeInteger(r?.connection_generation) || r.connection_generation < 1 ||
      typeof r?.owner_id !== 'string' || !r.owner_id || !Number.isSafeInteger(r?.sequence) || r.sequence < 0) {
    blockers.add('FINAL_EVIDENCE_MISSING');
  }
  if (r?.retired !== true) blockers.add('OWNER_NOT_RETIRED');
  if (r?.transport_state !== 'closed') blockers.add('TRANSPORT_RELEASE_UNCONFIRMED');
  if (r?.events_released !== true) blockers.add('EVENT_SUBSCRIPTION_NOT_RELEASED');
  if (r?.settled !== true || r?.pending_operations !== 0 || r?.pending_credential_writes !== 0 || r?.pending_journal_writes !== 0) {
    blockers.add('WORK_NOT_SETTLED');
  }
  if (r?.evidence_write_failed !== false || (live && live.evidence_persisted !== true)) blockers.add('EVIDENCE_DURABILITY_UNCONFIRMED');
  const ids = new Set();
  for (const op of operations) {
    if (typeof op?.id !== 'string' || ids.has(op.id) || !sources.has(op.source) ||
        !outcomes.has(op.provider_outcome) || !outcomes.has(op.wrapper_outcome) ||
        !Number.isSafeInteger(op.sequence_at_start) || !Number.isSafeInteger(op.sequence_at_end) ||
        op.sequence_at_end < op.sequence_at_start || op.sequence_at_end > r.sequence) blockers.add('OPERATION_ACCOUNTING_INCOMPLETE');
    ids.add(op?.id);
    if (op?.provider_outcome === 'unresolved' || op?.wrapper_outcome === 'unresolved') blockers.add('WORK_NOT_SETTLED');
    if (op?.mutation_batches !== 0 || op?.credentials_changed !== false) blockers.add('AUTH_MUTATIONS_REQUIRE_RECONCILIATION');
  }
  if (!Array.isArray(r?.intent_ids) || new Set(r.intent_ids).size !== r.intent_ids.length || r.intent_ids.some(id => !ids.has(id))) blockers.add('OPERATION_ACCOUNTING_INCOMPLETE');
  // Be deliberately conservative: a changed/uncertain write requires a separate
  // reviewed reconciliation, even when later bytes happen to match the journal.
  for (const entry of Array.isArray(failure?.failures) ? failure.failures : []) {
    if (entry?.intent_id && !ids.has(entry.intent_id)) blockers.add('OPERATION_ACCOUNTING_INCOMPLETE');
    if (!['drain_timeout', 'pipeline', 'transaction', 'get'].includes(entry?.operation) ||
        !Array.isArray(entry?.mutations) || entry.mutations.length) blockers.add('AUTH_MUTATIONS_REQUIRE_RECONCILIATION');
  }
  if (!journal) blockers.add('OFFLINE_JOURNAL_VALIDATION_REQUIRED');
  else {
    if (journal.incompleteTailBytes !== 0 || !Number.isSafeInteger(r?.sequence) || journal.seq !== r.sequence) blockers.add('JOURNAL_STATE_MISMATCH');
    if (!r?.credentials || !isDeepStrictEqual(r.credentials, journal.data?.creds) ||
        (live && live.credentials_match_committed !== true)) blockers.add('FINAL_CREDENTIALS_MISMATCH');
    if (!active?.id || active.id !== r?.owner_id) blockers.add('OWNER_EVIDENCE_MISMATCH');
    const intentOperations = intents?.operations;
    if (!Array.isArray(intentOperations) || !Array.isArray(r?.intent_ids) ||
        intentOperations.length !== r.intent_ids.length || new Set(intentOperations.map(x => x?.id)).size !== intentOperations.length ||
        intentOperations.some(op => !ids.has(op?.id) || !r.intent_ids.includes(op.id) ||
          !Array.isArray(op.mutations) || op.mutations.length || !isDeepStrictEqual(op.creds, journal.data?.creds))) {
      blockers.add('INTENT_EVIDENCE_MISMATCH');
    }
  }
  if (live && live.credentials_match_committed !== true) blockers.add('FINAL_CREDENTIALS_MISMATCH');
  if (live && live.current_owner !== true) blockers.add('OWNER_EVIDENCE_MISMATCH');
  return { version: 1, status: blockers.size ? 'BLOCKED' : 'OPERATOR_REVIEW_REQUIRED',
    authentication_evidence_complete: blockers.size === 0, recovery_allowed: false,
    blockers: [...blockers], required_external_checks: ['CURRENT_EXCLUSIVE_OWNERSHIP', 'MESSAGE_CALLBACK_DEDUP_CONSISTENCY',
      'UNCERTAIN_DELIVERIES_PRESERVED', 'REVIEWED_RECOVERY_PROCEDURE', 'EXPLICIT_RECOVERY_APPROVAL'],
    sequence: Number.isSafeInteger(r?.sequence) ? r.sequence : null,
    // Correlates the private-copy result with a fresh live observation. This
    // hashes a random owner epoch and counters, never credentials or keys.
    evidence_reference: r?.owner_id && Number.isSafeInteger(r?.revision) && Number.isSafeInteger(r?.sequence)
      ? crypto.createHash('sha256').update(`${r.owner_id}:${r.revision}:${r.sequence}`).digest('hex') : null,
    connection_generation: Number.isSafeInteger(r?.connection_generation) ? r.connection_generation : null,
    operations: operations.map(op => ({ source: sources.has(op?.source) ? op.source : 'unknown',
      provider_outcome: outcomes.has(op?.provider_outcome) ? op.provider_outcome : 'unresolved',
      wrapper_outcome: outcomes.has(op?.wrapper_outcome) ? op.wrapper_outcome : 'unresolved' })) };
}
