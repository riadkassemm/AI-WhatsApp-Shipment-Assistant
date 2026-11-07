import { JournalStore } from './journal-store.js';
import { mkdir, readFile, open, rename, chmod, unlink, access } from 'node:fs/promises';
import { AsyncLocalStorage } from 'node:async_hooks';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import lockfile from 'proper-lockfile';
import { BufferJSON, initAuthCreds, proto } from '@whiskeysockets/baileys';

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
  return lockfile.lock(directory, { realpath: true, stale: 30_000, update: 10_000, retries: 0 });
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
  let retired = false, retiring = false, drained = false;
  const context = new AsyncLocalStorage();
  const pending = new Set(), tokens = new Set(), attached = new WeakSet();
  const failureEvidence = [];
  let failureTail = Promise.resolve(), reportedFailure = false, failureWriting = false;
  let directSetTail = Promise.resolve(), activeArm, activeDisarm, activeArmed = false;
  let outsideProviderTransaction, inProviderTransaction;
  const changeIntent = change => {
    const task = owner.intentTail.then(async () => {
      if (store.failed) throw Object.assign(new Error('STORAGE_RESTART_REQUIRED'), { bridgePersistenceFailure: true });
      const next = new Map(owner.intents);
      change(next);
      const filename = store.filename + '.auth-intent';
      if (next.size) await atomicWrite(filename, { version: 1, operations: [...next.values()] });
      else {
        await fs.unlink(filename);
        const directory = await fs.open(path.dirname(filename), 'r');
        try { await directory.sync(); } finally { await directory.close(); }
      }
      owner.intents = next;
    });
    owner.intentTail = task.catch(() => {});
    return task;
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
    if (owner.current !== auth || (retired && !context.getStore()?.active && !context.getStore()?.acceptedEvents)) throw retiredAuth();
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
    failureEvidence.push(cloneAuth(evidence));
    if (!failureWriting) {
      failureWriting = true;
      failureTail = failureTail.then(async () => {
        try {
          let written = 0;
          while (written < failureEvidence.length) {
            const failures = failureEvidence.slice();
            await atomicWrite(store.filename + '.auth-failure', { version: 1, failures });
            written = failures.length;
          }
        } catch (fenceError) {
          error.authFencePersistenceFailed = true;
          fenceError.bridgePersistenceFailure = true;
          fenceError.code = 'AUTH_FAILURE_FENCE_UNWRITABLE';
          auth.onFailure?.(fenceError);
        } finally { failureWriting = false; }
      });
    }
    if (!reportedFailure) { reportedFailure = true; auth.onFailure?.(error); }
  };
  const runProvider = (method, operation, args, independent = false, detached = false) => {
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
    const token = { active: true, hasTransaction: method === 'transaction' || method === 'pipeline', id: crypto.randomUUID(), method, started: Date.now(), sequence: store.seq,
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
        // This fence precedes any provider work, including network side effects.
        // A crash can leave a conservative recovery requirement, never a new
        // connection silently using keys from before an unfinished transaction.
        if (method !== 'get') await saveIntent(token);
        const result = await operation(...args);
        if (token.hasIntent) {
          if (token.hasTransaction && credsDigest() !== token.originalCreds) await auth.saveCreds();
          await store.flush();
          await changeIntent(intents => intents.delete(token.id));
          token.hasIntent = false;
        }
        return result;
      }
      catch (error) {
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
      } finally { token.active = false; tokens.delete(token); releaseSet?.(); }
    });
    pending.add(task);
    const finished = () => {
      pending.delete(task);
      if (retired && !pending.size) auth.onProviderIdle?.();
    };
    task.then(finished, finished);
    return task;
  };
  const transition = async reset => {
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
      try { return await store.update(data => { data.creds = admitted; }); }
      catch (error) {
        fail(error, { operation: 'creds', creds: admitted });
        throw error;
      }
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
        mutex.mutex = async (...args) => runProvider('pipeline', operation, args);
      }
    },
    trackProviderTask: operation => runProvider('pipeline', operation, [], true),
    trackProviderDetachedTask: operation => {
      if (!outsideProviderTransaction) throw new Error('AUTH_PROVIDER_DETACH_REQUIRED');
      // Detached issuance must not inherit Baileys' completed transaction ALS:
      // its later set() would otherwise report success into a dead buffer.
      return outsideProviderTransaction(() => runProvider('pipeline', operation, [], true, true));
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
    armSession: () => {
      active();
      activeArm ??= atomicWrite(store.filename + '.auth-active', { version: 1, started_at: Date.now(),
        sequence_at_start: store.seq, id: crypto.randomUUID() }).then(() => { activeArmed = true; }).catch(error => {
        fail(error, { operation: 'session_arm' }); throw error;
      });
      return activeArm;
    },
    drain: async ({ timeoutMs = 20_000 } = {}) => {
      let timer;
      try {
        await activeArm;
        const settleProviderTasks = async () => {
          // An admitted native callback may start a separately tracked child
          // (offline queue, token issuance, post-login key work) while retiring.
          // Keep draining until those children also finish, under one deadline.
          while (pending.size) await Promise.allSettled([...pending]);
        };
        await Promise.race([settleProviderTasks(), new Promise((_, reject) => {
          timer = setTimeout(() => {
            const error = Object.assign(new Error('AUTH_TRANSACTION_DRAIN_TIMEOUT'), { code: 'AUTH_TRANSACTION_DRAIN_TIMEOUT' });
            const mutations = [...tokens].flatMap(token => token.mutations);
            for (const token of tokens) token.active = false;
            fail(error, { operation: 'drain_timeout', mutations });
            reject(error);
          }, timeoutMs);
        })]);
        clearTimeout(timer);
        await store.flush();
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
      finally { retired = true; retiring = false; }
    },
    renew: () => transition(false),
    reset: () => transition(true),
  };
  owner.current = auth;
  return auth;
}

export async function assertAuthRecoveryClear(filename) {
  // Check existence without reading sensitive recovery evidence into logs.
  for (const suffix of ['.auth-failure', '.auth-intent', '.auth-active']) {
    let fenced = true;
    try { await access(filename + suffix); } catch (error) { if (error.code !== 'ENOENT') throw error; fenced = false; }
    if (fenced) throw Object.assign(new Error('AUTH_RECOVERY_REQUIRED'), { code: 'AUTH_RECOVERY_REQUIRED', bridgePersistenceFailure: true });
  }
}

export async function createAuth(directory, { store, ...options } = {}) {
  store ??= await new JournalStore(path.join(directory, 'auth.json'), () => ({ creds: initAuthCreds(), keys: {} }), options).load();
  try { await assertAuthRecoveryClear(store.filename); } catch (error) { store.failed = true; throw error; }
  if (authOwners.has(store)) return authOwners.get(store).current.renew();
  const owner = { current: null, generation: 0, intents: new Map(), intentTail: Promise.resolve() };
  authOwners.set(store, owner);
  return authFacade(store, owner);
}
