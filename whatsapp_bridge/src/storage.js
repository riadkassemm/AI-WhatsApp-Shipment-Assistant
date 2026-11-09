import { JournalStore } from './journal-store.js';
import { mkdir, readFile, open, rename, chmod, unlink, access } from 'node:fs/promises';
import { AsyncLocalStorage } from 'node:async_hooks';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import lockfile from 'proper-lockfile';
import { BufferJSON, initAuthCreds, proto } from '@whiskeysockets/baileys';
import { assessAuthRecovery } from './auth-recovery.js';

export async function atomicWrite(filename, value) {
  const temporary = `${filename}.${crypto.randomUUID()}.tmp`;
  let published = false;
  try {
    const handle = await fs.open(temporary, 'wx', 0o600);
    try {
      await handle.writeFile(JSON.stringify(value, BufferJSON.replacer));
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
  let held = true;
  const unlock = await lockfile.lock(directory, { realpath: true, stale: 30_000, update: 10_000, retries: 0,
    onCompromised: error => { held = false; throw error; } });
  const release = async () => { await unlock(); held = false; };
  release.assertHeld = () => { if (!held) throw new Error('DIRECTORY_OWNERSHIP_UNPROVEN'); };
  return release;
}

const cloneAuth = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value, BufferJSON.replacer), BufferJSON.reviver);
const cloneKeyUpdates = updates => Object.fromEntries(Object.entries(updates).map(([type, values]) =>
  [type, Object.fromEntries(Object.entries(values).map(([id, value]) => [id, value == null ? null : cloneAuth(value)]))]));
const retiredAuth = () => Object.assign(new Error('AUTH_SESSION_RETIRED'), { code: 'AUTH_SESSION_RETIRED' });
const authOwners = new WeakMap();

// One store owns every auth sequence for a line. A socket receives a disposable
// view: provider-owned objects must never alias committed storage, and a late
// callback must never write after another socket starts using these keys.
function authFacade(store, owner) {
  const creds = cloneAuth(store.data.creds);
  const credsDigest = () => crypto.createHash('sha256').update(JSON.stringify(creds, BufferJSON.replacer)).digest('hex');
  let retired = false, retiring = false, drained = false, retained = false;
  const context = new AsyncLocalStorage();
  const pending = new Set(), tokens = new Set(), attached = new WeakSet();
  const failureEvidence = [];
  let failureTail = Promise.resolve(), reportedFailure = false, failureWriting = false;
  const ownerId = crypto.randomUUID(), authGeneration = owner.generation, failedOperations = new Map();
  let evidenceRevision = 0, persistedRevision = 0, evidenceWriteFailed = false, credentialWrites = 0;
  let transportState = 'unknown', connectionGeneration = null, eventsReleased = false;
  const operationEvidence = token => ({ id: token.id, source: token.source, operation: token.method,
    started_at: token.started, sequence_at_start: token.sequence, sequence_at_end: token.endSequence ?? null,
    provider_outcome: token.providerOutcome, wrapper_outcome: token.wrapperOutcome,
    mutation_batches: token.mutations.length, credentials_changed: credsDigest() !== token.originalCreds });
  const recoverySnapshot = () => ({ version: 1, owner_id: ownerId, auth_generation: authGeneration,
    connection_generation: connectionGeneration, revision: evidenceRevision, captured_at: Date.now(),
    retired, transport_state: transportState, events_released: eventsReleased,
    pending_operations: pending.size, pending_credential_writes: credentialWrites,
    pending_journal_writes: store.pending, sequence: store.seq,
    settled: pending.size === 0 && tokens.size === 0 && credentialWrites === 0 && store.pending === 0,
    evidence_write_failed: evidenceWriteFailed, intent_ids: [...owner.intents.keys()], operations: [...failedOperations.values()],
    // Private, mode-0600 evidence only. Never returned by the operator endpoint.
    credentials: cloneAuth(creds) });
  const persistFailure = error => {
    for (const token of tokens) failedOperations.set(token.id, operationEvidence(token));
    evidenceRevision++;
    if (failureWriting) return;
    failureWriting = true;
    failureTail = failureTail.then(async () => {
      try {
        let attemptedRevision = 0;
        while (attemptedRevision !== evidenceRevision) {
          const revision = evidenceRevision;
          try {
            // Entries are immutable at admission. Coalesce bursts without
            // repeatedly cloning the entire history; include the latest final
            // operation/credential/transport evidence, even with no new failure.
            const saved = { version: 2, failures: failureEvidence.slice(), recovery: recoverySnapshot() };
            await atomicWrite(store.filename + '.auth-failure', saved);
            persistedRevision = revision;
          } catch (fenceError) {
            // A later successful snapshot must retain this durability warning.
            evidenceWriteFailed = true;
            if (error) error.authFencePersistenceFailed = true;
            fenceError.bridgePersistenceFailure = true;
            fenceError.code = 'AUTH_FAILURE_FENCE_UNWRITABLE';
            auth.onFailure?.(fenceError);
          }
          // Do not spin retrying a failed write without new evidence.
          attemptedRevision = revision;
        }
      } finally { failureWriting = false; }
    });
  };
  const evidenceChanged = () => { if (reportedFailure && !retained) persistFailure(); };
  let directSetTail = Promise.resolve(), activeArm, activeDisarm, activeArmed = false;
  let outsideProviderTransaction, inProviderTransaction;
  // Group commit. Each caller still resolves only after a durable write that
  // includes its change. Changes queued while a write is in flight share the
  // next write. Writing each queued pipeline's intent alone rewrote the whole
  // map once per job: an offline backlog of N messages cost O(N^2) fsynced
  // bytes and outlasted the drain deadline, permanently failing the line.
  const changeIntent = change => {
    const done = new Promise((resolve, reject) => owner.intentQueue.push({ change, resolve, reject }));
    if (!owner.intentFlushScheduled) {
      owner.intentFlushScheduled = true;
      owner.intentTail = owner.intentTail.then(async () => {
        owner.intentFlushScheduled = false;
        const batch = owner.intentQueue.splice(0);
        try {
          if (store.failed) throw Object.assign(new Error('STORAGE_RESTART_REQUIRED'), { bridgePersistenceFailure: true });
          const next = new Map(owner.intents);
          for (const item of batch) item.change(next);
          const filename = store.filename + '.auth-intent';
          if (next.size) await atomicWrite(filename, { version: 1, operations: [...next.values()] });
          else if (owner.intents.size) {
            await fs.unlink(filename);
            const directory = await fs.open(path.dirname(filename), 'r');
            try { await directory.sync(); } finally { await directory.close(); }
          }
          owner.intents = next;
          for (const item of batch) item.resolve();
        } catch (error) { for (const item of batch) item.reject(error); }
      });
    }
    return done;
  };
  const saveIntent = async token => {
    const evidence = cloneAuth({ id: token.id, operation: token.method, started_at: token.started,
      sequence_at_start: token.sequence, mutations: token.mutations, creds });
    try {
      await changeIntent(intents => intents.set(token.id, evidence));
      token.hasIntent = true;
    } catch (error) {
      // The provider can catch a failed buffered set(). Poison immediately;
      // outer transaction success must never erase an unresolved mutation.
      fail(error, { operation: 'intent', intent_id: token.id, mutations: token.mutations, creds: cloneAuth(creds) });
      throw error;
    }
  };
  // Resume mode: keep abandoned intents as evidence beside the journal and
  // clear the live fence, serialized with every other intent change.
  const archiveIntents = () => {
    const task = owner.intentTail.then(async () => {
      const count = owner.intents.size;
      if (!count) return 0;
      await fs.rename(store.filename + '.auth-intent', `${store.filename}.auth-intent.abandoned-${Date.now()}`);
      const directory = await fs.open(path.dirname(store.filename), 'r');
      try { await directory.sync(); } finally { await directory.close(); }
      owner.intents = new Map();
      return count;
    });
    owner.intentTail = task.catch(() => {});
    return task;
  };
  const recordMutation = async (token, updates) => {
    // An awaited child callback may share the actual provider transaction.
    // Its successful return cannot erase the outer transaction's obligation
    // to commit those buffered mutations (or fence an aborted commit).
    for (let current = token; current?.active; current = current.parent) {
      current.mutations.push(cloneKeyUpdates(updates));
      await saveIntent(current);
    }
  };
  const active = evidence => {
    if (retained) throw retiredAuth();
    if (owner.current !== auth) throw retiredAuth();
    const admitted = context.getStore();
    // A timeout deactivates tokens, but their already admitted continuations
    // still carry this context. Retain rejected late evidence before returning
    // a failure; unrelated calls on a retired facade remain ordinary rejections.
    if (retired && !admitted?.active && !admitted?.acceptedEvents && !(store.failed && admitted)) throw retiredAuth();
    if (store.failed) {
      const error = Object.assign(new Error('STORAGE_RESTART_REQUIRED'), { bridgePersistenceFailure: true });
      // A previously admitted callback can discover more server-relevant keys
      // after another callback failed. Preserve those rejected values as
      // recovery evidence without reopening the failed journal or auth owner.
      if (evidence) fail(error, evidence());
      throw error;
    }
  };
  const fail = (error, evidence) => {
    error.bridgePersistenceFailure = true;
    store.failed = true;
    if (context.getStore()) context.getStore().failureRecorded = true;
    // A known rejected mutation must not disappear on process restart. Retain
    // its private evidence and require explicit recovery before another socket
    // uses these credentials. Never automatically clear this fence.
    // Snapshot only the new evidence at admission. Cloning the entire growing
    // history on every rejection made an auth-failure burst quadratic and could
    // starve heartbeat timers for every identity in this process. Coalesce
    // writes while retaining every entry; drain still waits for the last one.
    failureEvidence.push(cloneAuth({ ...evidence, creds: evidence?.creds ?? creds }));
    persistFailure(error);
    if (!reportedFailure) { reportedFailure = true; auth.onFailure?.(error); }
  };
  const runProvider = (method, operation, args, independent = false, detached = false, source = method) => {
    active(method === 'set' ? () => ({ operation: 'rejected_keys_after_failure', mutations: [cloneKeyUpdates(args[0])] }) : undefined);
    const inherited = context.getStore();
    if (inherited?.active && !independent) {
      return (async () => {
        const mutationsBefore = inherited.mutations.length, credsBefore = credsDigest();
        try {
          if (method === 'set') await recordMutation(inherited, args[0]);
          else if (method === 'transaction' || method === 'pipeline') {
            inherited.hasTransaction = true;
            if (!inherited.hasIntent) await saveIntent(inherited);
          }
          return await operation(...args);
        } catch (error) {
          if ((inherited.mutations.length > mutationsBefore || credsDigest() !== credsBefore) && !inherited.failureRecorded) {
            fail(error, { operation: 'nested_' + method, mutations: inherited.mutations, creds: cloneAuth(creds) });
          }
          throw error;
        }
      })();
    }
    const token = { active: true, hasTransaction: method === 'transaction' || method === 'pipeline', id: crypto.randomUUID(), method, source,
      providerOutcome: 'not_started', wrapperOutcome: 'unresolved', started: Date.now(), sequence: store.seq,
      originalCreds: credsDigest(), mutations: method === 'set' ? [cloneKeyUpdates(args[0])] : [] };
    if (independent && !detached && inherited?.active && inProviderTransaction?.()) token.parent = inherited;
    let previousSet, releaseSet;
    if (method === 'set') {
      previousSet = directSetTail;
      directSetTail = new Promise(resolve => { releaseSet = resolve; });
    }
    tokens.add(token);
    const task = context.run(token, async () => {
      try {
        // Baileys' nontransactional PQueue can execute a queued callback in a
        // previous job's AsyncLocalStorage context. Keep top-level direct sets
        // out of that queue together; nested transactional sets remain inline.
        if (previousSet) await previousSet;
        // Strict mode: this fence precedes any provider work, including network
        // side effects. Resume mode: a provider pipeline writes its intent when
        // it first starts a key transaction or mutation (the nested path
        // above). A pipeline that never touches keys needs no fence, and an
        // initial sync starts thousands of them at once (14,032 on 2026-11-09).
        if (method !== 'get' && !(method === 'pipeline' && owner.resume)) await saveIntent(token);
        token.providerOutcome = 'unresolved';
        const result = await operation(...args);
        token.providerOutcome = 'completed';
        if (!token.hasIntent && token.hasTransaction && credsDigest() !== token.originalCreds) await auth.saveCreds();
        if (token.hasIntent) {
          if (token.hasTransaction && credsDigest() !== token.originalCreds) await auth.saveCreds();
          await store.flush();
          await changeIntent(intents => intents.delete(token.id));
          token.hasIntent = false;
        }
        token.wrapperOutcome = 'completed';
        return result;
      }
      catch (error) {
        if (token.providerOutcome === 'unresolved') token.providerOutcome = 'rejected';
        token.wrapperOutcome = 'rejected';
        // Work abandoned at the retirement deadline was already given up: clear
        // its intent and keep the store usable (resume mode only).
        if (token.abandoned) {
          if (token.hasIntent) await changeIntent(intents => intents.delete(token.id)).catch(() => {});
          throw error;
        }
        if (owner.resume && method === 'pipeline' && !token.hasIntent && !token.mutations.length &&
            credsDigest() === token.originalCreds) throw error;
        // A normal read/query rejection with unchanged authentication state is
        // safe to abandon. Message-send uncertainty is tracked separately by
        // the bridge; only changed or ambiguous auth state poisons this owner.
        if (token.hasIntent && !token.mutations.length && !store.failed && credsDigest() === token.originalCreds) {
          let cleared = false;
          try {
            await changeIntent(intents => intents.delete(token.id));
            token.hasIntent = false;
            cleared = true;
          } catch (cleanupError) { error = cleanupError; }
          if (cleared) throw error;
        }
        // The provider can send a stanza before committing its buffered keys.
        // A failed transaction with mutations is unsafe even if no raw set()
        // reached the journal, and even if the provider catches the rejection.
        if ((token.hasIntent || method !== 'get' || token.mutations.length) && !token.failureRecorded) {
          fail(error, { operation: method, intent_id: token.id, mutations: token.mutations });
        }
        throw error;
      } finally {
        token.active = false; token.endSequence = store.seq;
        if (reportedFailure) failedOperations.set(token.id, operationEvidence(token));
        tokens.delete(token); releaseSet?.();
      }
    });
    pending.add(task);
    const finished = () => {
      pending.delete(task);
      if (retired && !pending.size) auth.onProviderIdle?.();
      evidenceChanged();
    };
    task.then(finished, finished);
    return task;
  };
  const transition = async reset => {
    if (retained) throw retiredAuth();
    if (owner.current !== auth) throw retiredAuth();
    auth.retire();
    const generation = ++owner.generation;
    // Include provider transactions still buffering mutations and retrying a
    // commit, not only raw writes already admitted to the journal queue.
    await auth.drain();
    if (reset) await store.update(data => { data.creds = initAuthCreds(); data.keys = {}; });
    else await store.flush();
    if (owner.generation !== generation) throw retiredAuth();
    return authFacade(store, owner);
  };
  const auth = {
    store,
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          active();
          return Object.fromEntries(ids.map(id => {
            let value = cloneAuth(store.data.keys[type]?.[id]);
            if (type === 'app-state-sync-key' && value) value = proto.Message.AppStateSyncKeyData.fromObject(value);
            return [id, value];
          }));
        },
        set: async updates => {
          active(() => ({ operation: 'rejected_keys_after_failure', mutations: [cloneKeyUpdates(updates)] }));
          if (!context.getStore()?.active) return runProvider('set', auth.state.keys.set, [updates]);
          const admitted = cloneKeyUpdates(updates);
          await recordMutation(context.getStore(), admitted);
          try { return await store.update(data => {
            for (const [type, values] of Object.entries(admitted)) {
              data.keys[type] ??= {};
              for (const [id, value] of Object.entries(values)) {
                if (value === null || value === undefined) delete data.keys[type][id];
                else data.keys[type][id] = value;
              }
            }
          }); } catch (error) { fail(error, { operation: 'keys', mutations: [admitted] }); throw error; }
        },
      },
    },
    saveCreds: async () => {
      active(() => ({ operation: 'rejected_creds_after_failure', creds: cloneAuth(creds) }));
      const admitted = cloneAuth(creds);
      credentialWrites++;
      try { return await store.update(data => { data.creds = admitted; }); }
      catch (error) {
        fail(error, { operation: 'creds', creds: admitted });
        throw error;
      }
      finally { credentialWrites--; evidenceChanged(); }
    },
    trackProviderKeys: keys => {
      if (attached.has(keys)) return;
      if (keys === auth.state.keys || typeof keys.transaction !== 'function') throw new Error('AUTH_TRANSACTION_WRAPPER_REQUIRED');
      attached.add(keys);
      outsideProviderTransaction = keys.bridgeRunOutsideTransaction?.bind(keys);
      inProviderTransaction = keys.isInTransaction?.bind(keys);
      // Mutate the actual wrapper object: Baileys' socket and Signal repository
      // retain this same reference, including its AsyncLocalStorage commit path.
      for (const method of ['get', 'set', 'transaction']) {
        const operation = keys[method].bind(keys);
        keys[method] = async (...args) => runProvider(method, operation, args);
      }
    },
    trackProviderPipelines: socket => {
      // Pinned Baileys retains these same mutex objects in its receive/chats
      // closures. Their full callbacks include post-key-transaction credential
      // events and acknowledged message delivery, not only key-store commits.
      for (const name of ['messageMutex', 'notificationMutex', 'receiptMutex', 'appStatePatchMutex']) {
        const mutex = socket[name];
        if (!mutex || typeof mutex.mutex !== 'function' || attached.has(mutex)) continue;
        attached.add(mutex);
        const operation = mutex.mutex.bind(mutex);
        // Admit at enqueue (a retired or failed owner rejects immediately), but
        // fence only when the job acquires the mutex: a queued job has done no
        // provider work. Registering at enqueue made a backlog of N jobs hold N
        // intents and rewrite them on every completion (O(N^2); a 3,000-job
        // backlog took ~35 min on 2026-11-09). Jobs still queued at retirement
        // reject with AUTH_SESSION_RETIRED; they never decrypted or acknowledged
        // anything, so WhatsApp redelivers them to the next session.
        mutex.mutex = async code => {
          active();
          return operation(() => runProvider('pipeline', code, [], false, false, name));
        };
      }
    },
    trackProviderTask: operation => runProvider('pipeline', operation, [], true, false, 'bridgeTask'),
    trackProviderDetachedTask: operation => {
      if (!outsideProviderTransaction) throw new Error('AUTH_PROVIDER_DETACH_REQUIRED');
      // Detached issuance must not inherit Baileys' completed transaction ALS:
      // its later set() would otherwise report success into a dead buffer.
      return outsideProviderTransaction(() => runProvider('pipeline', operation, [], true, true, 'bridgeDetachedTask'));
    },
    trackRecoveryTransport: (closed, generation) => {
      connectionGeneration = generation;
      if (!closed || typeof closed.then !== 'function') return;
      transportState = 'open';
      Promise.resolve(closed).then(() => { transportState = 'closed'; evidenceChanged(); },
        () => { transportState = 'unconfirmed'; evidenceChanged(); });
    },
    recoveryEventsReleased: () => { eventsReleased = true; evidenceChanged(); },
    // Retention never establishes credential correctness. It only proves that
    // the failed owner has stopped and that its final uncertain evidence exists.
    // No disk reads/writes or promises may occur between this check and sealing.
    retainedIdle: () => owner.current === auth && retired && transportState === 'closed' && eventsReleased &&
      reportedFailure && store.failed && pending.size === 0 && tokens.size === 0 && credentialWrites === 0 &&
      store.pending === 0 && store.healthWrites === 0 && !store.compactionPromise && !store.activeCheckpoint &&
      !store.cleanupCheckpoint && !store.maintenanceCleanupFailed && !failureWriting && !evidenceWriteFailed &&
      evidenceRevision === persistedRevision,
    canRetain: recovery => {
      if (!auth.retainedIdle() || !recovery) return false;
      const current = recoverySnapshot();
      // captured_at is observation time, not part of the durable owner state.
      const {captured_at: _saved, ...saved} = recovery;
      const {captured_at: _now, ...observed} = current;
      return isDeepStrictEqual(saved, observed);
    },
    sealRetained: () => {
      if (!auth.retainedIdle()) throw new Error('AUTH_ISOLATION_UNPROVEN');
      retained = true;
    },
    // Permanent abandonment needs cessation, not proof that these credentials
    // are reusable. Historical intents stay unresolved and are never applied.
    quarantineIdle: () => owner.current === auth && retired && transportState === 'closed' && eventsReleased &&
      store.failed && pending.size === 0 && tokens.size === 0 && credentialWrites === 0 &&
      store.pending === 0 && store.healthWrites === 0 && !store.compactionPromise && !store.activeCheckpoint &&
      !store.cleanupCheckpoint && !failureWriting,
    sealQuarantine: () => {
      if (!auth.quarantineIdle()) throw new Error('QUARANTINE_WRITER_ACTIVE');
      retained = true;
    },
    // Observation only: no flush, marker changes, provider calls or journal replay.
    assessRecovery: async ({timeoutMs = 1000} = {}) => {
      // A closing transport or settling callback may append evidence while the
      // previous write is awaited. Observe the latest tail, never an older proof.
      let timer, observationTimedOut = false;
      const deadline = Date.now() + timeoutMs;
      try {
        await Promise.race([(async () => {
          let observed;
          do { observed = failureTail; await observed; } while (observed !== failureTail && Date.now() < deadline);
        })(), new Promise(resolve => {timer = setTimeout(() => {observationTimedOut = true; resolve();}, timeoutMs);})]);
      } finally {clearTimeout(timer);}
      const recovery = recoverySnapshot();
      const result = assessAuthRecovery({failure: reportedFailure ? {version: 2, failures: failureEvidence, recovery} : null,
        live: { evidence_persisted: persistedRevision === evidenceRevision && !evidenceWriteFailed,
          current_owner: owner.current === auth,
          credentials_match_committed: credsDigest() === crypto.createHash('sha256')
            .update(JSON.stringify(store.data.creds, BufferJSON.replacer)).digest('hex') }});
      if (observationTimedOut) {
        result.blockers.push('EVIDENCE_OBSERVATION_TIMEOUT');
        result.status = 'BLOCKED'; result.authentication_evidence_complete = false;
      }
      return result;
    },
    inProviderContext: () => owner.current === auth && Boolean(context.getStore()?.active || context.getStore()?.acceptedEvents),
    acceptBufferedEvents: callback => {
      // The real event buffer may retain acknowledged messages for 100 ms
      // after its provider pipeline returns. Retirement flushes that existing
      // buffer synchronously, including credential events, before unsubscribing.
      // This admission expires before any unrelated asynchronous continuation.
      if (owner.current !== auth) throw retiredAuth();
      const admission = { acceptedEvents: true };
      return context.run(admission, () => {
        try { return callback(); } finally { admission.acceptedEvents = false; }
      });
    },
    hasProviderWork: () => pending.size > 0,
    // Fixed booleans only: used by the session owner for administrative admission.
    managementEvidence: () => ({current_owner: owner.current === auth,
      paired: creds.registered === true && Boolean(creds.me?.id) &&
        creds.signedPreKey?.signature instanceof Uint8Array && creds.signedPreKey.signature.length >= 32 &&
        Number.isInteger(creds.registrationId) && typeof creds.advSecretKey === 'string' && Boolean(creds.advSecretKey) &&
        [creds.noiseKey, creds.signedIdentityKey, creds.signedPreKey?.keyPair].every(pair =>
          pair?.public instanceof Uint8Array && pair.public.length >= 32 &&
          pair?.private instanceof Uint8Array && pair.private.length >= 32),
      healthy: !reportedFailure && !store.failed && !evidenceWriteFailed && owner.intents.size === 0 &&
        credsDigest() === crypto.createHash('sha256').update(JSON.stringify(store.data.creds, BufferJSON.replacer)).digest('hex'),
      settled: pending.size === 0 && tokens.size === 0 && credentialWrites === 0 && store.pending === 0,
      transport_closed: transportState === 'closed', events_released: eventsReleased}),
    armSession: () => {
      active();
      activeArm ??= atomicWrite(store.filename + '.auth-active', { version: 1, started_at: Date.now(),
        sequence_at_start: store.seq, id: ownerId }).then(() => { activeArmed = true; }).catch(error => {
        fail(error, { operation: 'session_arm' }); throw error;
      });
      return activeArm;
    },
    drain: async ({ timeoutMs = owner.drainTimeoutMs } = {}) => {
      if (retained) throw new Error('AUTH_RETAINED_OFFLINE');
      let timer;
      try {
        await activeArm;
        const settleProviderTasks = async () => {
          // An admitted native callback may start a separately tracked child
          // (offline queue, token issuance, post-login key work) while retiring.
          // Keep draining until those children also finish, under one deadline.
          // Running work is always awaited: it may still emit accepted events.
          while (pending.size) await Promise.allSettled([...pending]);
        };
        let timedOut = false;
        await Promise.race([settleProviderTasks(), new Promise((resolve, reject) => {
          timer = setTimeout(() => {
            const error = Object.assign(new Error('AUTH_TRANSACTION_DRAIN_TIMEOUT'), { code: 'AUTH_TRANSACTION_DRAIN_TIMEOUT' });
            const mutations = [...tokens].flatMap(token => token.mutations);
            if (owner.resume) {
              // Give up stuck work instead of failing the line permanently: the
              // journal still holds every committed write. Its intents are kept
              // as evidence below; the next socket starts from committed state.
              for (const token of tokens) { token.active = false; token.abandoned = true; }
              timedOut = true;
              resolve();
              return;
            }
            for (const token of tokens) token.active = false;
            fail(error, { operation: 'drain_timeout', mutations });
            reject(error);
          }, timeoutMs);
        })]);
        clearTimeout(timer);
        await store.flush();
        if (owner.resume && (timedOut || owner.intents.size)) {
          const stuck = tokens.size, archived = await archiveIntents();
          owner.onRecovery?.({ event: 'auth_work_abandoned', reason: timedOut ? 'drain_timeout' : 'pending_intents',
            stuck_tasks: stuck, archived_intents: archived });
        }
        if (owner.intents.size) {
          const error = Object.assign(new Error('AUTH_PENDING_INTENTS'), { code: 'AUTH_PENDING_INTENTS' });
          fail(error, { operation: 'undrained_intents', intents: [...owner.intents.values()] });
          throw error;
        }
        if (retired && !drained && !store.closed) {
          // Admitted provider transactions can update the shared credentials
          // while their event subscription is already retired. Persist their
          // final values after the wrapper drains, before handing keys over.
          const admitted = cloneAuth(creds);
          try {
            if (JSON.stringify(admitted, BufferJSON.replacer) !== JSON.stringify(store.data.creds, BufferJSON.replacer)) {
              await store.update(data => { data.creds = admitted; });
            }
            drained = true;
          }
          catch (error) { fail(error, { operation: 'drained_creds', creds: admitted }); throw error; }
        }
        if (retired && activeArmed) {
          activeDisarm ??= (async () => {
            await fs.unlink(store.filename + '.auth-active');
            const directory = await fs.open(path.dirname(store.filename), 'r');
            try { await directory.sync(); } finally { await directory.close(); }
            activeArmed = false;
          })().catch(error => { fail(error, { operation: 'session_disarm' }); throw error; });
          await activeDisarm;
        }
      } finally { clearTimeout(timer); await failureTail; if (retired) auth.onDrained?.(); }
    },
    retire: () => {
      if (retired || retiring) return;
      retiring = true;
      try { auth.beforeRetire?.(); }
      catch (error) { fail(error, { operation: 'prepare_retirement' }); }
      finally { retired = true; retiring = false; evidenceChanged(); }
    },
    renew: () => transition(false),
    reset: () => transition(true),
  };
  owner.current = auth;
  return auth;
}

// Retiring a socket waits for its admitted provider work. In strict mode,
// missing this deadline permanently fails the line's auth store, so it must
// exceed the provider's own query timeout (20 s) plus slow-disk fsync time
// (production commits take ~200 ms each) by a wide margin.
export const DEFAULT_AUTH_DRAIN_TIMEOUT_MS = 60_000;

const markerExists = async filename => {
  try { await access(filename); return true; }
  catch (error) { if (error.code !== 'ENOENT') throw error; return false; }
};

// After an unclean exit (SIGKILL, crash, power loss) or a stuck retirement,
// a line can keep recovery markers. Every committed auth write is in the
// fsynced journal; the markers only describe work that was in progress:
//   .auth-active   a session was running
//   .auth-intent   a provider transaction was in flight (uncommitted)
//   torn tail      the last journal write did not finish (quarantined on load)
//   .auth-failure  writes were rejected; resumable only when the failure began
//                  with a drain timeout (not a disk or journal write error)
// Resume from the committed state instead of forcing a full re-pair: the
// in-flight change is discarded, and WhatsApp's retry receipts repair the few
// chats it touched. Markers are renamed with an .abandoned/.resumed suffix,
// never deleted, so the evidence stays auditable. A failure caused by a real
// write error keeps the existing fenced/retained/quarantine handling.
const RESUMABLE_FAILURE_ORIGINS = new Set(['drain_timeout']);
async function failureOrigin(filename) {
  try {
    // Read in-process only; recovery evidence is never logged.
    const evidence = JSON.parse(await fs.readFile(filename, 'utf8'));
    return evidence?.failures?.[0]?.operation ?? null;
  } catch (_) { return null; }
}
// Pre-load decision (no journal replay, no writes): may this fenced line resume?
export async function resumableAuthFence(directory) {
  const filename = path.join(directory, 'auth.json');
  if (!await markerExists(filename + '.auth-failure')) return true;
  return RESUMABLE_FAILURE_ORIGINS.has(await failureOrigin(filename + '.auth-failure'));
}
export async function resumeAfterUncleanExit(store) {
  if (store.failed) return { resumed: false, reason: 'store_failed' };
  const markers = {};
  for (const suffix of ['.auth-active', '.auth-intent', '.auth-failure']) markers[suffix] = await markerExists(store.filename + suffix);
  if (!markers['.auth-active'] && !markers['.auth-intent'] && !markers['.auth-failure']) {
    return { resumed: false, reason: 'no_marker' };
  }
  if (markers['.auth-failure'] && !RESUMABLE_FAILURE_ORIGINS.has(await failureOrigin(store.filename + '.auth-failure'))) {
    return { resumed: false, reason: 'auth_failure_present' };
  }
  const stamp = Date.now(), retained = [];
  for (const [suffix, present] of Object.entries(markers)) {
    if (!present) continue;
    const target = `${store.filename}${suffix}.${suffix === '.auth-active' ? 'resumed' : 'abandoned'}-${stamp}`;
    await fs.rename(store.filename + suffix, target);
    retained.push(path.basename(target));
  }
  const directory = await fs.open(path.dirname(store.filename), 'r');
  try { await directory.sync(); } finally { await directory.close(); }
  const reason = markers['.auth-failure'] ? 'drain_timeout_failure' : markers['.auth-intent'] ? 'in_flight_change_discarded'
    : store.incompleteTailBytes ? 'journal_tail_quarantined' : 'active_marker_only';
  return { resumed: true, reason, retained };
}

export async function assertAuthRecoveryClear(filename) {
  // Check existence without reading sensitive recovery evidence into logs.
  for (const suffix of ['.auth-failure', '.auth-intent', '.auth-active']) {
    let fenced = true;
    try { await access(filename + suffix); } catch (error) { if (error.code !== 'ENOENT') throw error; fenced = false; }
    if (fenced) throw Object.assign(new Error('AUTH_RECOVERY_REQUIRED'), { code: 'AUTH_RECOVERY_REQUIRED', bridgePersistenceFailure: true });
  }
}

export async function createAuth(directory, { store, drainTimeoutMs, resume = false, onRecovery, ...options } = {}) {
  if (!store) await assertAuthRecoveryClear(path.join(directory, 'auth.json'));
  store ??= await new JournalStore(path.join(directory, 'auth.json'), () => ({ creds: initAuthCreds(), keys: {} }), options).load();
  try { await assertAuthRecoveryClear(store.filename); } catch (error) { store.failed = true; throw error; }
  if (authOwners.has(store)) return authOwners.get(store).current.renew();
  const owner = { current: null, generation: 0, intents: new Map(), intentTail: Promise.resolve(),
    intentQueue: [], intentFlushScheduled: false, drainTimeoutMs: drainTimeoutMs ?? DEFAULT_AUTH_DRAIN_TIMEOUT_MS,
    resume, onRecovery };
  authOwners.set(store, owner);
  return authFacade(store, owner);
}
