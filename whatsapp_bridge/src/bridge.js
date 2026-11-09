import { mkdir, readFile, readdir, rename, unlink, rm, stat, open as openFile } from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import makeWASocket, { Browsers, DisconnectReason, downloadMediaMessage, normalizeMessageContent, proto, generateMessageIDV2, initAuthCreds, DEFAULT_CONNECTION_CONFIG } from '@whiskeysockets/baileys';
import pino from 'pino';
import QRCode from 'qrcode';
import { BridgeError, phoneDigits, wrapId, unwrapId, validateLineManifest, UUID_PATTERN, MANIFEST_MAX_BYTES } from './config.js';
import { FileStore, createAuth, resumeAfterUncleanExit, authFailureDiskCode } from './storage.js';
import { JournalStore, encode, readJournal } from './journal-store.js';
import { safeStorageCode, errorLabel } from './journal-maintenance.js';
import { StorageMonitor } from './storage-monitor.js';
import { MediaFiles } from './media-files.js';
import { AckTracker } from './ack.js';
import { inboundEvent, outgoingEvent, outgoingConfirmationEvent, messageTombstoneEvent, messageStatusEvent, mutationEvent, textBody, millis, eventId, disconnectReason, MEDIA_TYPES, RECEIPT_STATUSES, RECEIPT_RANK } from './events.js';
import { preserveNativeMutationMetadata } from './native-events.js';
import { classifyDisconnect, reconnectDelay, newConnectionDiagnostics, observeHeartbeat, EventLoopDiagnostics } from './connection-diagnostics.js';

const silentLogger = pino({ level: 'silent' });
const DEFAULT_MEDIA_HOST = 'mmg.whatsapp.net';

function isReactionCallback(event) {
  if (!['outgoing_message', 'message_snapshot'].includes(event.type)) return false;
  const messages = (event.payload?.entry ?? []).flatMap(entry =>
    (entry.changes ?? []).flatMap(change => change.value?.messages ?? []));
  return messages.length === 1 && messages[0].type === 'reaction';
}

// A photo/video album arrives as an envelope plus one message per item. The
// envelope is never forwarded (events.js), so a reaction to it has no target
// in support and the API would wait out its retry window for nothing. Deliver
// such a reaction against the album's first item instead. Outbox entries keep
// the original event; this mapping is recomputed on every delivery attempt.
function albumReactionCallback(event, messages) {
  if (!['message', 'outgoing_message'].includes(event.type) || !messages) return event;
  const entries = event.payload?.entry;
  const changes = entries?.length === 1 ? entries[0]?.changes : null;
  const nodes = changes?.length === 1 ? changes[0]?.value?.messages : null;
  if (nodes?.length !== 1 || nodes[0]?.type !== 'reaction' || typeof nodes[0].reaction?.message_id !== 'string') return event;
  let albumId;
  try { albumId = unwrapId(event.line_id, nodes[0].reaction.message_id); } catch { return event; }
  if (!messages[albumId]?.message?.message?.albumMessage) return event;
  const items = Object.entries(messages).filter(([, record]) => {
    const content = record?.message?.message;
    return content?.messageContextInfo?.messageAssociation?.parentMessageKey?.id === albumId &&
      MEDIA_TYPES.some(type => content[`${type}Message`]);
  }).sort(([leftId, left], [rightId, right]) =>
    (left.timestamp ?? 0) - (right.timestamp ?? 0) || (leftId < rightId ? -1 : leftId > rightId ? 1 : 0));
  // Items normally follow the envelope within seconds; until one is stored,
  // keep the original target and the existing history-wait behavior.
  if (!items.length) return event;
  const mapped = structuredClone(event);
  mapped.payload.entry[0].changes[0].value.messages[0].reaction.message_id = wrapId(event.line_id, items[0][0]);
  return mapped;
}

// Host of the media URL stored in a WhatsApp message, if any.
function mediaUrlHost(message) {
  const content = normalizeMessageContent(message?.message);
  const media = content && MEDIA_TYPES.map(type => content[`${type}Message`]).find(Boolean);
  try { return media?.url ? new URL(media.url).host : null; } catch { return null; }
}
const initialLine = () => ({ bound_phone: '', messages: {}, media: {}, operations: {}, enabled: false });
// After a logout or a re-link, messages sent while the line was offline are
// recovered from WhatsApp's recent history once it pairs again.
const RECOVERY_MARGIN_MS = 60_000;
const RECOVERY_WINDOW_MS = 15 * 60_000;
const RETIRED_PHONE_MS = 7 * 24 * 60 * 60_000;
const RECOVERY_SYNC_TYPES = [proto.HistorySync.HistorySyncType.INITIAL_BOOTSTRAP, proto.HistorySync.HistorySyncType.RECENT];
// Recovery hints must never make a connection or removal fail.
const knownPhone = value => { try { return phoneDigits(value, true); } catch { return ''; } };
// A missing ACK or a timed-out/closed query usually means the WhatsApp
// connection died without notice. Reconnecting lets the next send work.
const DEAD_TRANSPORT_REASONS = new Set(['stale_transport', 'connection_closed', 'transport_closed']);
const staleConnectionError = error => error?.code === 'CONFIRMATION_UNKNOWN' ||
  [408, 428].includes(error?.output?.statusCode) || /timed out|connection closed/i.test(String(error?.message ?? ''));
const transientConnectionError = error => ['ECONNRESET', 'ECONNREFUSED', 'ETIMEDOUT', 'ENETUNREACH', 'EHOSTUNREACH', 'EAI_AGAIN', 'ENOTFOUND']
  .includes(error?.code) || [408, 428, 500, 502, 503, 504].includes(error?.output?.statusCode);

export class Bridge {
  constructor(config, dependencies = {}) {
    this.config = config;
    this.makeSocket = dependencies.makeSocket ?? makeWASocket;
    this.fetch = dependencies.fetch ?? fetch;
    this.downloadMediaMessage = dependencies.downloadMediaMessage ?? downloadMediaMessage;
    this.lines = new Map();
    this.registrationTail = Promise.resolve();
    this.phoneReservations = new Map();
    this.mediaFiles = new MediaFiles();
    this.stopping = false;
    this.initialized = false;
    this.startupPhase = 'created';
    this.startedAt = Date.now();
    this.initializationController = new AbortController();
    // Own a store before load() yields: a failed or cancelled replay must not
    // leave an untracked maintenance owner behind the directory lock.
    this.ownedStores = new Set();
    this.ownedFiles = new Set();
    this.log = dependencies.log ?? (entry => console.info(JSON.stringify(entry)));
    this.storageMonitor = new StorageMonitor(config.dataDir, {config: config.storage?.monitoring,
      log: this.log, filesystem: dependencies.storageFilesystem});
    this.callbackTasks = new Map();
    this.callbackWakeups = new Set();
    this.reconnectSlots = new Map();
    this.random = dependencies.random ?? Math.random;
    this.eventLoop = new EventLoopDiagnostics();
    this.onError = dependencies.onError ?? (error => this.log({component: 'whatsapp_bridge',
      event: 'operation_failed', code: errorLabel(error),
      persistence_failure: error?.bridgePersistenceFailure === true}));
  }

  assertRunning() {
    if (this.stopping) throw new BridgeError(503, 'STOPPING', 'The bridge is shutting down.');
  }
  initialize(options = {}) {
    this.initializePromise ??= this.initializeOnce(options);
    return this.initializePromise;
  }
  async loadFile(filename, initial) {
    this.assertRunning();
    const store = new FileStore(filename, initial);
    this.ownedFiles.add(store);
    await store.load();
    this.assertRunning();
    return store;
  }
  async initializeOnce({ autoConnect = true } = {}) {
    this.assertRunning();
    this.startupPhase = 'storage_replay';
    this.outbox = await this.loadStore(path.join(this.config.dataDir, 'outbox.json'), () => ({ entries: [], dead_letters: [] }), 'outbox');
    this.manifest = await this.loadFile(path.join(this.config.dataDir, 'lines.json'), () => ({ version: 1, lines: {}, requests: {} }));
    // Phones of removed lines and when they went offline, so linking one again recovers the gap.
    this.retiredPhones = await this.loadFile(path.join(this.config.dataDir, 'retired-phones.json'), () => ({ version: 1, phones: {} }));
    const manifest = validateLineManifest(this.manifest.data);
    // Confirmed removal tombstones also form a durable cleanup work queue.
    // Repeating the precise numeric directory/outbox purge is safe after a crash.
    for (const id of manifest.removed_line_ids) {
      this.assertRunning();
      try { await this.purgeRemovedLine(id); }
      catch (error) { this.onError(error); }
    }
    const definitions = new Map(this.config.lines
      .filter(line => !manifest.removed_line_ids.includes(line.line_id) &&
        !(line.line_id === this.config.fallbackLineId && (Object.keys(manifest.lines).length || manifest.removed_line_ids.length)))
      .map(line => [line.line_id, line]));
    for (const [id, expected] of Object.entries(manifest.lines)) {
      if (definitions.has(id) && definitions.get(id).expected_phone !== expected) throw new Error('The linked-device manifest conflicts with a configured line.');
      definitions.set(id, { line_id: id, expected_phone: expected });
    }
    const prepared = [];
    const initializationFailed = (line, error) => {
      this.assertRunning();
      this.onError(error);
      line.session?.dispose();
      line.ack?.close();
      line.socket?.end(new Error('Failed to initialize this linked-device line'));
      line.socket = null;
      const policy = classifyDisconnect(error);
      line.recoveryCategory = policy.category;
      line.retryBlocked = !policy.retry;
      if (policy.retry && line.store?.data?.enabled && transientConnectionError(error)) {
        line.status = 'disconnected';
        this.scheduleReconnect(line);
      } else {
        line.status = 'error';
        line.error = 'Could not safely load or connect this WhatsApp line. Check its session storage and configuration.';
      }
    };
    // Finish replay for every line before any provider can acknowledge new
    // traffic. Shutdown during a slow later replay then has no live sockets.
    for (const definition of definitions.values()) {
      this.assertRunning();
      const line = this.lineContext(definition);
      this.lines.set(line.line_id, line);
      try {
        await this.prepareLine(line);
        this.assertRunning();
        // No send call from the previous process can still be running. Preserve
        // its native ID and wait for a real provider echo to resolve uncertainty.
        if (Object.values(line.store.data.messages).some(record => record.pending_send)) {
          await line.store.update(data => {
            for (const record of Object.values(data.messages)) if (record.pending_send) {
              record.pending_send = false;
              record.delivery_uncertain = true;
            }
          });
        }
        this.assertRunning();
        prepared.push(line);
      } catch (error) {
        initializationFailed(line, error);
      }
    }
    this.startupPhase = 'connecting';
    for (const line of prepared) {
      this.assertRunning();
      if (autoConnect && line.store.data.enabled) {
        try { await this.connect(line.line_id); }
        catch (error) { initializationFailed(line, error); }
      }
    }
    this.assertRunning();
    await this.storageMonitor.start();
    this.assertRunning();
    this.initialized = true;
    this.startupPhase = 'ready';
    return this;
  }
  async loadStore(filename, initial, kind, line = null) {
    this.assertRunning();
    // A failed load can retain a large partially decoded state before its line
    // field is assigned. Repeated retries must neither create a second journal
    // owner nor accumulate copies of that state. Successful cleanup releases
    // ownership; unresolved failures require a controlled recovery/restart.
    if ([...this.ownedStores].some(store => store.filename === filename)) {
      throw Object.assign(new Error('STORAGE_RESTART_REQUIRED'), {code: 'STORAGE_RESTART_REQUIRED', bridgePersistenceFailure: true});
    }
    const store = new JournalStore(filename, initial, {maintenance: this.config.storage?.maintenance,
      diagnostics: event => {
        if (event.event === 'storage_commit' && event.persist_ms < 100 && event.queue_wait_ms < 100) return;
        this.log({component: 'whatsapp_bridge', kind, ...(line ? {line_id: line.line_id} : {}), ...event});
      }});
    this.ownedStores.add(store);
    // A failed load must remain visible to the monitor even without a socket.
    this.storageMonitor.register(store, {lineId: line?.line_id ?? null, kind});
    await store.load({signal: this.initializationController.signal});
    this.assertRunning();
    return store;
  }
  async loadAuth(line) {
    const store = await this.loadStore(path.join(line.directory, 'auth.json'), () => ({creds: initAuthCreds(), keys: {}}), 'auth', line);
    // The directory lock guarantees no other process owns this line, and
    // ownedStores guarantees this process loads it once. A marker found here
    // was therefore left by a process that exited without retiring the session.
    if (this.config.auth?.resumeAfterUncleanExit !== false) {
      const outcome = await resumeAfterUncleanExit(store);
      if (outcome.reason !== 'no_marker') {
        this.log({component: 'whatsapp_bridge', event: outcome.resumed ? 'auth_session_resumed_after_unclean_exit' : 'auth_session_fenced_after_unclean_exit',
          line_id: line.line_id, reason: outcome.reason});
      }
    }
    const resume = this.config.auth?.resumeAfterUncleanExit !== false;
    const auth = await createAuth(line.directory, {store, drainTimeoutMs: this.config.auth?.drainTimeoutMs, resume,
      onRecovery: event => this.log({component: 'whatsapp_bridge', line_id: line.line_id, ...event})});
    if (this.stopping) { auth.retire(); await auth.drain(); this.assertRunning(); }
    return auth;
  }
  async closeStore(store) {
    if (!store) return;
    await store.close();
    // Registrations/removals can repeat without restarting the process. Keep
    // failed cleanup owners, but release successfully closed historical data.
    this.ownedStores.delete(store);
  }
  lineContext(definition) {
    return { ...definition, directory: path.join(this.config.dataDir, definition.line_id),
      display_phone_number: definition.expected_phone, status: 'disconnected', socket: null, ack: null,
      generation: 0, session: null, ackTail: Promise.resolve(), confirmationTasks: new Map(),
      eventTail: Promise.resolve(), sendTail: Promise.resolve(), sendPending: 0, sendTasks: new Map(), readTail: Promise.resolve(), reconnectTimer: null,
      disconnectedSince: Date.now(), recoveryCategory: null, retryBlocked: false,
      diagnostics: newConnectionDiagnostics() };
  }
  async prepareLine(line) {
    try {
      this.assertRunning();
      await mkdir(path.join(line.directory, 'media'), { recursive: true, mode: 0o700 });
      this.assertRunning();
      // Optional media cleanup must not prevent intact credentials or incoming
      // evidence from loading. The writer retains failed cleanup obligations
      // and refuses further media allocation until they can be synchronized.
      await this.mediaFiles.prepare(path.join(line.directory, 'media')).catch(this.onError);
      this.assertRunning();
      line.store ??= await this.loadStore(path.join(line.directory, 'state.json'), initialLine, 'state', line);
      this.assertRunning();
      // Rebuild only missing index entries at startup, also after a rollback
      // export was used by an older bridge. Never infer a legacy payload hash.
      const requests = new Map();
      for (const [nativeId, record] of Object.entries(line.store.data.messages)) if (record.client_message_id) {
        const key = crypto.createHash('sha256').update(record.client_message_id).digest('hex');
        const previous = requests.get(key) ?? line.store.data.send_requests?.[key];
        if (previous?.native_id && previous.native_id !== nativeId) throw new Error('DUPLICATE_DURABLE_SEND_KEY');
        requests.set(key, {native_id: nativeId, fingerprint: record.request_fingerprint ?? null});
      }
      if (!line.store.data.send_requests) await line.store.update(data => {data.send_requests = {};});
      const entries = [...requests].filter(([key, value]) => {
        const old = line.store.data.send_requests[key];
        return !old || old.native_id !== value.native_id || old.fingerprint !== value.fingerprint;
      });
      for (let offset = 0; offset < entries.length; offset += 256) {
        this.assertRunning();
        await line.store.update(data => {
          for (const [key, value] of entries.slice(offset, offset + 256)) data.send_requests[key] = value;
        });
      }
      // Retained disconnected/error sessions must remain identifiable as paired
      // after restart so the manager can explicitly unlink their credentials.
      line.auth ??= await this.loadAuth(line);
      this.assertRunning();
      line.display_phone_number = line.store.data.bound_phone || line.expected_phone;
    } catch (error) { if (!this.stopping) error.bridgePersistenceFailure = true; throw error; }
  }
  async addLine(payload) {
    if (!payload || typeof payload.request_id !== 'string' || !UUID_PATTERN.test(payload.request_id)) throw new BridgeError(400, 'INVALID_REQUEST_ID', 'A UUID registration request ID is required.');
    const requestId = payload.request_id.toLowerCase();
    if (payload.expected_phone !== undefined && typeof payload.expected_phone !== 'string') throw new BridgeError(400, 'INVALID_PHONE', 'Expected phone must be an international phone string or empty.');
    const phone = phoneDigits(payload.expected_phone ?? '', true);
    const reserved = payload.reserved_line_ids ?? [];
    if (!Array.isArray(reserved) || reserved.length > 10000 || reserved.some(id => typeof id !== 'string' || !/^\d{5,64}$/.test(id))) throw new BridgeError(400, 'INVALID_RESERVED_LINES', 'Reserved line IDs must be an array of at most 10000 numeric IDs.');
    const task = this.registrationTail.then(async () => {
      if (this.stopping) throw new BridgeError(503, 'STOPPING', 'The bridge is shutting down.');
      const manifest = validateLineManifest(this.manifest.data);
      const prior = manifest.requests[requestId];
      if (manifest.removed_request_ids.includes(requestId)) throw new BridgeError(409, 'REGISTRATION_REMOVED', 'This registration request belongs to a removed number. Start a new registration request.');
      if (prior) {
        if (prior.expected_phone !== phone) throw new BridgeError(409, 'REGISTRATION_CONFLICT', 'This registration request was already used for a different expected phone.');
        return { ...this.lineStatus(this.getLine(prior.line_id)), created: false };
      }
      const occupied = new Set([...this.lines.keys(), ...Object.keys(manifest.lines), ...manifest.removed_line_ids, ...reserved]);
      let numericId = 90001;
      while (occupied.has(String(numericId))) numericId++;
      const id = String(numericId);
      const line = this.lineContext({ line_id: id, expected_phone: phone });
      // Prepare private state first. Failed storage writes never advertise a line.
      try {
        await this.prepareLine(line);
        this.assertRunning();
        await this.manifest.update(data => {
          data.version = 1;
          data.requests ??= {};
          // Keep an existing legacy fallback paired line routable when its first
          // additional line makes Python switch from fallback to manifest routing.
          const fallback = !manifest.removed_line_ids.includes(this.config.fallbackLineId) && this.lines.get(this.config.fallbackLineId);
          if (fallback) data.lines[fallback.line_id] = fallback.expected_phone;
          data.lines[id] = phone;
          data.requests[requestId] = { line_id: id, expected_phone: phone };
          if (Buffer.byteLength(JSON.stringify(data), 'utf8') > MANIFEST_MAX_BYTES) throw new BridgeError(413, 'MANIFEST_TOO_LARGE', 'The linked-device line manifest exceeds its size limit.');
          validateLineManifest(data);
        });
      } catch (error) {
        // A retry can reuse this ID only after its unpublished owners and
        // workers finish. Retain every prepared file, including credentials.
        line.auth?.retire();
        await Promise.allSettled([this.closeStore(line.auth?.store), this.closeStore(line.store)]);
        this.storageMonitor.unregister(id);
        throw error;
      }
      this.lines.set(id, line);
      return { ...this.lineStatus(line), created: true };
    });
    this.registrationTail = task.catch(() => {});
    return task;
  }
  async removeLine(id) {
    if (typeof id !== 'string' || !/^\d{5,32}$/.test(id)) throw new BridgeError(400, 'INVALID_LINE_ID', 'A numeric linked-device line ID is required.');
    const task = this.registrationTail.then(async () => {
      const manifest = validateLineManifest(this.manifest.data);
      if (manifest.removed_line_ids.includes(id)) {
        await this.purgeRemovedLine(id);
        return { line_id: id, removed: true, unlinked: true };
      }
      const line = this.getLine(id);
      if (!line.store?.data) throw new BridgeError(503, 'LINE_STORAGE_UNAVAILABLE', 'Check this number session storage before removing it.');
      const creds = line.auth?.store?.data?.creds;
      const keyBytes = value => value instanceof Uint8Array && value.length >= 32;
      const keyPair = value => keyBytes(value?.public) && keyBytes(value?.private);
      if (!line.auth?.state?.creds || typeof creds?.registered !== 'boolean' ||
          !keyPair(creds.noiseKey) || !keyPair(creds.signedIdentityKey) || !keyPair(creds.signedPreKey?.keyPair) ||
          !keyBytes(creds.signedPreKey?.signature) || !Number.isInteger(creds.registrationId) ||
          typeof creds.advSecretKey !== 'string' || !creds.advSecretKey) {
        throw new BridgeError(503, 'SESSION_STATE_UNKNOWN', 'The saved session could not be safely loaded. Its paired state is unknown; credentials were retained.');
      }
      if (this.stopping) throw new BridgeError(503, 'STOPPING', 'The bridge is shutting down.');
      const previousEnabled = line.store.data.enabled;
      const previousStatus = line.status;
      const previousSocket = line.socket;
      line.removing = true;
      clearTimeout(line.reconnectTimer);
      line.reconnectTimer = null;
      this.reconnectSlots.delete(line.line_id);
      try {
        // Finish an already dispatched mutation; prevent subsequent sends and
        // callbacks while this particular number is being unlinked.
        await line.sendTail;
        await line.readTail;
        await line.mediaTail;
        if (line.connectPromise) await line.connectPromise;
        if (this.lineStatus(line).is_paired && !line.removalRemoteConfirmed) await this.confirmDeviceUnlink(line);
        this.stopRemovedSocket(line);
        await line.eventTail;
        await line.ackTail;
        await Promise.allSettled(line.confirmationTasks.values());
        await line.auth?.drain();
        await line.store.flush();
        await this.retirePhone(line);
        // Only a remote-confirmed removal (or an unpaired line) enters the
        // durable tombstones. They suppress ENV/fallback routes on every restart.
        await this.manifest.update(data => {
          data.removed_line_ids ??= [];
          data.removed_request_ids ??= [];
          if (!data.removed_line_ids.includes(id)) data.removed_line_ids.push(id);
          delete data.lines[id];
          for (const [requestId, request] of Object.entries(data.requests ?? {})) {
            if (request.line_id === id) {
              delete data.requests[requestId];
              if (!data.removed_request_ids.includes(requestId)) data.removed_request_ids.push(requestId);
            }
          }
          validateLineManifest(data);
        });
        await this.closeStore(line.auth?.store);
        await this.closeStore(line.store);
        this.storageMonitor.unregister(id);
        this.lines.delete(id);
        await this.purgeRemovedLine(id);
        line.restoreNativeEvents?.();
        line.auth = null;
        line.store = null;
        return { line_id: id, removed: true, unlinked: true };
      } catch (error) {
        if ((this.manifest.data.removed_line_ids ?? []).includes(id)) this.lines.delete(id);
        else {
          if (line.socket !== previousSocket || previousStatus !== 'connected' || line.removalRemoteConfirmed) this.stopRemovedSocket(line);
          await line.eventTail;
          await line.store.update(data => { data.enabled = previousEnabled; });
          line.status = line.socket && previousStatus === 'connected' ? 'connected' : 'error';
          if (line.status === 'error') line.error = 'This number could not be fully removed. Its saved session is retained; retry removal.';
        }
        throw error;
      } finally {
        line.removing = false;
        line.removalWaiter = null;
      }
    });
    this.registrationTail = task.catch(() => {});
    return task;
  }
  // ---- Admin recovery actions (dashboard). None deletes data: files move to
  // linked-device-archive/<timestamp>-<action>-<line>/ beside the data dir.
  archiveDirectory(action, id) {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    return path.join(path.dirname(this.config.dataDir), 'linked-device-archive', `${stamp}-${action}-${id}`);
  }
  async syncDirectory(directory) {
    const handle = await openFile(directory, 'r');
    try { await handle.sync(); } finally { await handle.close(); }
  }
  // Stop the transport and close this line's auth journal, including a
  // failed or fenced one that never produced an auth owner.
  async releaseAuth(line) {
    this.stopRemovedSocket(line);
    const auth = line.auth;
    line.auth = null;
    if (auth) await auth.drain({timeoutMs: 15_000}).catch(() => {});
    const filename = path.join(line.directory, 'auth.json');
    for (const store of [...this.ownedStores]) {
      if (store.filename !== filename) continue;
      // close() fences writes before flushing; a failed store refuses the flush.
      try { await store.close(); } catch (error) { if (!store.failed) throw error; }
      this.ownedStores.delete(store);
    }
  }
  recoveryAction(id, action) {
    if (typeof id !== 'string' || !/^\d{5,32}$/.test(id)) throw new BridgeError(400, 'INVALID_LINE_ID', 'A numeric linked-device line ID is required.');
    this.assertRunning();
    const task = this.registrationTail.then(async () => {
      this.assertRunning();
      return action();
    });
    this.registrationTail = task.catch(() => {});
    return task;
  }
  authStoreOf(line) {
    const filename = path.join(line.directory, 'auth.json');
    return line.auth?.store ?? [...this.ownedStores].find(store => store.filename === filename) ?? null;
  }
  sessionBlocked(line) {
    return this.authStoreOf(line)?.failed === true;
  }
  savedLogin(line) {
    const creds = this.authStoreOf(line)?.data?.creds;
    return Boolean(creds?.registered || creds?.me?.id);
  }
  sessionBlockedError() {
    return new BridgeError(409, 'SESSION_BLOCKED', "This number's saved session is blocked. Use Resume session (no phone needed) or Reset session (pair with the phone).");
  }
  // Drop and re-open only this number's connection.
  async reconnectLine(id) {
    const line = this.getLine(id);
    if (line.removing || line.resetting) throw new BridgeError(503, 'LINE_BUSY', 'This number is being changed. Try again shortly.');
    if (this.sessionBlocked(line)) throw this.sessionBlockedError();
    if (line.socket && line.session && !line.session.closed) this.restartStaleSocket(line, line.socket);
    line.retryBlocked = false;
    return this.connect(id);
  }
  // For a blocked number: continue from its last saved WhatsApp session, no
  // phone needed. Safeguards: never on a connected number; only when the
  // session is actually blocked; never after a recorded disk or journal
  // failure; only when the saved session reads cleanly and contains a login.
  // The markers move to linked-device-archive/<time>-resume-<line>/; the
  // committed keys stay. Only this number reconnects.
  resumeSession(id) {
    return this.recoveryAction(id, async () => {
      const line = this.getLine(id);
      if (line.removing || line.resetting) throw new BridgeError(503, 'LINE_BUSY', 'This number is being changed. Try again shortly.');
      if (line.status === 'connected') throw new BridgeError(409, 'LINE_CONNECTED', 'This number is connected. There is nothing to resume.');
      const filename = path.join(line.directory, 'auth.json');
      const markerNames = (await readdir(line.directory)).filter(name =>
        /^auth\.json\.auth-(active|intent|failure)$/.test(name) || /^auth\.json\.auth-failure\.[0-9a-f-]+\.tmp$/.test(name));
      if (!this.sessionBlocked(line) && !markerNames.some(name => /^auth\.json\.auth-(intent|failure)$/.test(name))) {
        throw new BridgeError(409, 'SESSION_NOT_BLOCKED', "This number's session is not blocked. Use Reconnect instead.");
      }
      const diskCode = await authFailureDiskCode(line.directory);
      if (diskCode) {
        throw new BridgeError(409, 'STORAGE_FAILURE', `This session stopped on a disk error (${diskCode}). Ask a developer to check the server disk, then use Reset session.`);
      }
      let saved;
      try { saved = await readJournal(filename, () => ({ creds: null, keys: {} }), { repairTail: false }); }
      catch (_) { throw new BridgeError(409, 'SESSION_DAMAGED', 'The saved session cannot be read. Use Reset session and pair with the phone.'); }
      if (!(saved.data?.creds?.registered || saved.data?.creds?.me?.id)) {
        throw new BridgeError(409, 'NO_SAVED_LOGIN', 'There is no saved WhatsApp login to resume. Use Reset session and pair with the phone.');
      }
      line.resetting = true;
      let archive;
      try {
        await line.connectPromise?.catch(() => {});
        await this.releaseAuth(line);
        archive = this.archiveDirectory('resume', id);
        await mkdir(archive, {recursive: true, mode: 0o700});
        for (const name of (await readdir(line.directory)).filter(item =>
          /^auth\.json\.auth-(active|intent|failure)$/.test(item) || /^auth\.json\.auth-failure\.[0-9a-f-]+\.tmp$/.test(item))) {
          await rename(path.join(line.directory, name), path.join(archive, name));
        }
        await this.syncDirectory(line.directory);
        await this.syncDirectory(archive);
        line.auth = await this.loadAuth(line);
        Object.assign(line, {status: 'disconnected', retryBlocked: false, recoveryCategory: null});
        delete line.error;
      } finally { line.resetting = false; }
      this.log({component: 'whatsapp_bridge', event: 'auth_session_resumed_by_admin', line_id: id,
        archived_markers: markerNames.length, archive: path.basename(archive)});
      return this.connect(id);
    });
  }
  // For a number that cannot connect: archive its session keys and start an
  // unpaired session on the same line. state.json (messages, bound phone) is
  // kept, so it can only be re-paired with the same phone and keeps its history.
  resetSession(id) {
    return this.recoveryAction(id, async () => {
      const line = this.getLine(id);
      if (line.removing || line.resetting) throw new BridgeError(503, 'LINE_BUSY', 'This number is being changed. Try again shortly.');
      if (line.status === 'connected') throw new BridgeError(409, 'LINE_CONNECTED', 'This number is connected. Use Reconnect instead; reset is only for a number that cannot connect.');
      line.resetting = true;
      try {
        await line.connectPromise?.catch(() => {});
        await this.releaseAuth(line);
        const archive = this.archiveDirectory('reset', id);
        await mkdir(archive, {recursive: true, mode: 0o700});
        for (const name of await readdir(line.directory)) {
          if (name.startsWith('auth.json')) await rename(path.join(line.directory, name), path.join(archive, name));
        }
        await this.syncDirectory(line.directory);
        await this.syncDirectory(archive);
        line.auth = await this.loadAuth(line);
        Object.assign(line, {status: 'disconnected', retryBlocked: true, recoveryCategory: null});
        delete line.error;
        this.log({component: 'whatsapp_bridge', event: 'auth_session_reset', line_id: id, archive: path.basename(archive)});
        return this.lineStatus(line);
      } finally { line.resetting = false; }
    });
  }
  // For a number that cannot be unlinked (no usable session): archive the
  // whole folder and tombstone it. The phone still lists the old device; the
  // dashboard tells the admin to log it out there.
  forceRemoveLine(id) {
    return this.recoveryAction(id, async () => {
      if (validateLineManifest(this.manifest.data).removed_line_ids.includes(id)) return { line_id: id, removed: true, forced: true };
      const line = this.getLine(id);
      if (line.removing || line.resetting) throw new BridgeError(503, 'LINE_BUSY', 'This number is being changed. Try again shortly.');
      line.removing = true;
      try {
        await Promise.allSettled([line.sendTail, line.readTail, line.mediaTail, line.connectPromise]);
        await this.releaseAuth(line);
        await Promise.allSettled([line.eventTail, line.ackTail, ...line.confirmationTasks.values()]);
        if (line.store?.data) {
          await this.retirePhone(line).catch(this.onError);
          await this.closeStore(line.store).catch(error => { if (!line.store.failed) throw error; });
        }
        const archive = this.archiveDirectory('remove', id);
        await mkdir(path.dirname(archive), {recursive: true, mode: 0o700});
        await rename(line.directory, archive);
        await this.syncDirectory(this.config.dataDir);
        await this.manifest.update(data => {
          data.removed_line_ids ??= [];
          data.removed_request_ids ??= [];
          if (!data.removed_line_ids.includes(id)) data.removed_line_ids.push(id);
          delete data.lines[id];
          for (const [requestId, request] of Object.entries(data.requests ?? {})) {
            if (request.line_id === id) {
              delete data.requests[requestId];
              if (!data.removed_request_ids.includes(requestId)) data.removed_request_ids.push(requestId);
            }
          }
          validateLineManifest(data);
        });
        await this.outbox.update(data => {
          data.entries = data.entries.filter(item => item.event.line_id !== id);
          data.dead_letters = (data.dead_letters ?? []).filter(item => item.event.line_id !== id);
        });
        this.storageMonitor.unregister(id);
        this.lines.delete(id);
        line.restoreNativeEvents?.();
        line.store = null;
        this.log({component: 'whatsapp_bridge', event: 'line_force_removed', line_id: id, archive: path.basename(archive)});
        return { line_id: id, removed: true, forced: true };
      } catch (error) {
        line.removing = false;
        throw error;
      }
    });
  }
  stopRemovedSocket(line) {
    const socket = line.socket;
    line.auth?.retire();
    line.session?.dispose();
    line.socket = null;
    line.ack?.close();
    socket?.end(new Error('Linked-device number removal'));
    clearTimeout(line.reconnectTimer);
    line.reconnectTimer = null;
    this.reconnectSlots.delete(line.line_id);
    delete line.qr;
    delete line.qr_data_url;
  }
  async confirmDeviceUnlink(line) {
    let socket = line.status === 'connected' ? line.socket : null;
    try {
      if (!socket) {
        let timer;
        const opened = new Promise((resolve, reject) => {
          line.removalWaiter = { resolve, reject };
          timer = setTimeout(() => reject(new BridgeError(504, 'UNLINK_NOT_CONFIRMED', 'The saved WhatsApp session did not reconnect in time. Retry removal.')), this.config.removalConnectTimeoutMs ?? 25_000);
        });
        opened.catch(() => {});
        try {
          if (!line.socket) await this.openSocket(line);
          socket = await opened;
        } finally { clearTimeout(timer); line.removalWaiter = null; }
      }
      if (line.removalRemoteConfirmed) return;
      const jid = line.auth?.state.creds.me?.id;
      if (!socket || typeof socket.query !== 'function' || typeof jid !== 'string' || !jid) throw new Error('Saved session cannot issue unlink query');
      // Same native operation as Baileys.logout, using query instead of sendNode
      // so success means an explicit provider IQ result, not just a socket write.
      const result = await socket.query({ tag: 'iq', attrs: { to: 's.whatsapp.net', type: 'set', xmlns: 'md' },
        content: [{ tag: 'remove-companion-device', attrs: { jid, reason: 'user_initiated' } }] }, this.config.removalQueryTimeoutMs ?? 15_000);
      if (result?.tag !== 'iq' || result.attrs?.type !== 'result') throw new Error('No explicit unlink acknowledgment');
      line.removalRemoteConfirmed = true;
    } catch {
      // A real server logged-out response proves the saved companion is already
      // invalid, including when removal closes the socket before its IQ reply.
      if (line.removalRemoteConfirmed) return;
      const error = new BridgeError(502, 'UNLINK_NOT_CONFIRMED', 'WhatsApp did not confirm device unlinking. The saved session was retained for retry.');
      error.delivery_uncertain = true;
      throw error;
    }
  }
  async purgeRemovedLine(id) {
    if (!/^\d{5,32}$/.test(id)) throw new Error('Invalid removal cleanup line ID.');
    await this.outbox.update(data => {
      data.entries = data.entries.filter(item => item.event.line_id !== id);
      data.dead_letters = (data.dead_letters ?? []).filter(item => item.event.line_id !== id);
    });
    await rm(path.join(this.config.dataDir, id), { recursive: true, force: true });
  }

  getLine(id) {
    const line = this.lines.get(id);
    if (!line) throw new BridgeError(404, 'UNKNOWN_LINE', 'This linked-device line is not configured.');
    return line;
  }

  lineStatus(line) {
    const creds = line.auth?.store?.data?.creds;
    const pending = this.outbox.data.entries.filter(item => item.event.line_id === line.line_id && !item.waiting_for_history);
    const oldest = pending.reduce((at, item) => Math.min(at, item.created_at ?? Date.now()), Date.now());
    return { line_id: line.line_id, display_phone_number: line.display_phone_number, status: line.status,
      is_paired: Boolean(creds?.registered || creds?.me?.id),
      recovery_category: line.recoveryCategory,
      // A blocked session cannot reconnect or pair until it is resumed or reset.
      session_blocked: this.sessionBlocked(line),
      saved_login: this.savedLogin(line),
      retry_scheduled: Boolean(line.reconnectTimer),
      diagnostics: { ...line.diagnostics,
        last_frame_age_ms: line.diagnostics.last_frame_at ? Math.max(0, Date.now() - line.diagnostics.last_frame_at) : null },
      oldest_callback_age_seconds: pending.length ? Math.max(0, Date.now() - oldest) / 1000 : 0,
      pending_callbacks: pending.length,
      waiting_history_reactions: this.outbox.data.entries.filter(item => item.event.line_id === line.line_id && item.waiting_for_history).length,
      failed_callbacks: (this.outbox.data.dead_letters ?? []).filter(item => item.event.line_id === line.line_id).length,
      uncertain_sends: Object.values(line.store?.data?.messages ?? {}).filter(item => item.delivery_uncertain && !item.confirmation_enqueued).length,
      disconnected_since: line.status === 'connected' ? null : line.disconnectedSince ?? null,
      logout_reason: line.status === 'logged_out' ? line.store?.data?.logout_reason ?? null : null,
      logged_out_at: line.status === 'logged_out' ? line.store?.data?.logged_out_at ?? null : null,
      recovering_history: line.status === 'connected' && this.recovering(line),
      ...(line.qr ? { qr: line.qr, qr_data_url: line.qr_data_url } : {}), ...(line.error ? { error: line.error } : {}) };
  }
  recovering(line) {
    const data = line.store?.data;
    return Boolean(data?.recover_since) && !(data.recover_until && Date.now() > data.recover_until);
  }
  runtimeHealth() {
    const storage = this.storageMonitor.status();
    const recoveryRequired = storage.stores.filter(store => store.failed)
      .map(store => ({line_id: store.line_id, kind: store.kind, code: 'STORAGE_RECOVERY_REQUIRED'}));
    const criticalIssues = storage.issues.filter(issue => issue.level === 'critical');
    const capacityOnly = criticalIssues.length > 0 && criticalIssues.every(issue =>
      ['DISK_SPACE_LOW', 'INODES_LOW'].includes(issue.code));
    const phase = this.stopping ? 'stopping' : !this.initialized ? this.startupPhase :
      storage.level === 'critical' ? 'storage_degraded' : 'ready';
    return {live: !this.stopping, ready: phase === 'ready', phase,
      startup_elapsed_ms: this.initialized ? null : Date.now() - this.startedAt,
      recovery_required: recoveryRequired,
      recovery_mode: recoveryRequired.length ? 'review_required' :
        capacityOnly ? 'automatic_after_capacity_recovers' : criticalIssues.length ? 'operator_attention' : 'none',
      connected_lines: [...this.lines.values()].filter(line => line.status === 'connected').length,
      configured_lines: this.lines.size};
  }
  status() { return { lines: [...this.lines.values()].map(line => this.lineStatus(line)),
    storage: this.storageMonitor.status(), runtime: this.runtimeHealth(), event_loop: this.eventLoop.snapshot() }; }

  async connect(id) {
    this.assertRunning();
    const line = this.getLine(id);
    if (line.removing) throw new BridgeError(503, 'LINE_REMOVING', 'This WhatsApp number is being removed.');
    if (this.sessionBlocked(line)) throw this.sessionBlockedError();
    if (line.socket && !line.session?.closed && ['connected', 'connecting', 'qr'].includes(line.status)) return this.lineStatus(line);
    if (line.connectPromise) return line.connectPromise;
    line.connectPromise = this.openSocket(line).finally(() => { line.connectPromise = null; });
    return line.connectPromise;
  }

  async openSocket(line) {
    if (this.stopping) throw new BridgeError(503, 'STOPPING', 'The bridge is shutting down.');
    const generation = ++line.generation;
    line.retryBlocked = false;
    line.recoveryCategory = null;
    line.auth?.retire();
    line.session?.dispose();
    line.ack?.close();
    line.socket?.end(new Error('Replacing linked-device session'));
    line.socket = null;
    clearTimeout(line.reconnectTimer);
    line.reconnectTimer = null;
    this.reconnectSlots.delete(line.line_id);
    try {
      if (!line.store?.data) await this.prepareLine(line);
      this.assertRunning();
      await line.store.update(data => { data.enabled = true; });
      this.assertRunning();
      const renewal = line.transportLost ? { drainTimeoutMs: this.config.auth?.reconnectDrainTimeoutMs ?? 15_000 } : undefined;
      line.auth = line.auth ? await line.auth.renew(renewal) : await this.loadAuth(line);
      line.transportLost = false;
      if (this.stopping || generation !== line.generation) {
        line.auth.retire(); await line.auth.drain(); return this.lineStatus(line);
      }
      // Retired provider pipelines can emit their already accepted messages
      // while auth drains. Persist that final event tail before a new socket.
      await line.eventTail;
      this.assertRunning();
      await this.adoptRetiredPhone(line);
    } catch (error) {
      // Session persistence failures must never enter network retry loops.
      if (!this.stopping && error.code !== 'AUTH_SESSION_RETIRED') error.bridgePersistenceFailure = true;
      throw error;
    }
    const auth = line.auth;
    if (this.stopping || generation !== line.generation) { auth.retire(); return this.lineStatus(line); }
    // Standalone provider credential events can follow a server-side change
    // outside any Signal transaction. Keep a durable active-session fence until
    // retirement drains all accepted work; an unclean restart needs review.
    await auth.armSession();
    if (this.stopping || generation !== line.generation) { auth.retire(); await auth.drain(); return this.lineStatus(line); }
    line.status = 'connecting';
    this.trace(line, 'connection_transition', { state: 'connecting' });
    delete line.error;
    delete line.qr;
    delete line.qr_data_url;
    let socket;
    try { socket = this.makeSocket({ auth: auth.state, logger: silentLogger, browser: Browsers.ubuntu('Support Dashboard'),
      bridgeConnectionDiagnostics: sample => {
        if (generation !== line.generation || this.stopping) return;
        if (observeHeartbeat(line.diagnostics, sample)) {
          this.trace(line, sample.event, { frame_age_ms: line.diagnostics.heartbeat_frame_age_ms,
            timer_delay_ms: line.diagnostics.heartbeat_timer_delay_ms,
            rtt_ms: line.diagnostics.heartbeat_rtt_ms, event_loop: this.eventLoop.snapshot() });
        }
      },
      bridgeTask: work => (...args) => {
        // The checksum-verified pinned provider exposes complete asynchronous
        // callback boundaries, including jobs it starts without awaiting them.
        let task;
        try { task = auth.trackProviderTask(() => work(...args)); }
        catch (error) { task = Promise.reject(error); }
        task.catch(error => { if (error.code !== 'AUTH_SESSION_RETIRED') this.onError(error); });
        return task;
      },
      bridgeDetachedTask: work => (...args) => {
        let task;
        try { task = auth.trackProviderDetachedTask(() => work(...args)); }
        catch (error) { task = Promise.reject(error); }
        task.catch(error => { if (error.code !== 'AUTH_SESSION_RETIRED') this.onError(error); });
        return task;
      },
      makeSignalRepository: (state, ...args) => {
        // Install ownership before Signal operations or socket event handlers
        // can start using the provider's buffered transaction wrapper.
        auth.trackProviderKeys(state.keys);
        return DEFAULT_CONNECTION_CONFIG.makeSignalRepository(state, ...args);
      },
      printQRInTerminal: false, emitOwnEvents: false, enableRecentMessageCache: false,
      // Only the recent history of a line recovering from an outage, never a full sync.
      syncFullHistory: false, shouldSyncHistoryMessage: notification => this.recovering(line) && RECOVERY_SYNC_TYPES.includes(notification?.syncType),
      markOnlineOnConnect: false, connectTimeoutMs: 25_000, defaultQueryTimeoutMs: 20_000,
      shouldIgnoreJid: jid => /@(g\.us|broadcast|newsletter)$/.test(jid),
      getMessage: async key => line.store.data.messages[key.id]?.message?.message,
    });
      if (socket.authState?.keys) auth.trackProviderKeys(socket.authState.keys);
      auth.trackProviderPipelines(socket);
      auth.beforeRetire = socket.bridgePrepareRetirement?.bind(socket);
    } catch (error) { auth.retire(); throw error; }
    line.socket = socket;
    line.diagnostics.supported = socket.bridgeConnectionDiagnosticsVersion === 1;
    const session = { generation, ready: false, closed: false };
    line.session = session;
    const onClose = () => {
      if (line.socket !== socket) return;
      this.blockConnection(line, socket, 'transport_closed');
      this.scheduleReconnect(line);
    };
    socket.ws.on('close', onClose);
    let flushingBuffer = false, finishingRetirement = false, requestedEnd, endRequested = false, nativeEndStarted = false;
    let destroyRequested = false, nativeDestroyed = false;
    const nativeEnd = socket.end.bind(socket);
    const nativeDestroy = socket.ev.destroy?.bind(socket.ev);
    const flushAcceptedBuffer = () => {
      if (flushingBuffer || session.subscriptionReleased || typeof socket.ev.flush !== 'function') return;
      flushingBuffer = true;
      try { auth.acceptBufferedEvents(() => socket.ev.flush()); }
      finally { flushingBuffer = false; }
    };
    const releaseSubscription = () => {
      if (session.subscriptionReleased) return;
      flushAcceptedBuffer();
      session.subscriptionReleased = true;
      session.restoreNativeEvents?.(); session.unsubscribe?.();
    };
    const finishRetirement = (force = false) => {
      if (finishingRetirement) return;
      finishingRetirement = true;
      try {
        flushAcceptedBuffer();
        if (auth.hasProviderWork() && !force) return;
        // Controlled retirement must keep the transport available until
        // accepted receive callbacks finish their receipt/ack/upsert sequence.
        if (endRequested && !nativeEndStarted) {
          nativeEndStarted = true;
          Promise.resolve(nativeEnd(requestedEnd)).catch(this.onError);
        }
        releaseSubscription();
        if (destroyRequested && !nativeDestroyed) { nativeDestroyed = true; nativeDestroy?.(); }
      } finally { finishingRetirement = false; }
    };
    session.dispose = () => {
      if (session.disposed) return;
      session.disposed = true;
      session.closed = true; session.ready = false;
      auth.retire();
      socket.ws.off('close', onClose);
      if (session.keepLateAcks) session.ack?.interrupt();
      else session.ack?.close();
      finishRetirement();
    };
    socket.end = reason => {
      // A timed-out outgoing send can still receive authoritative late ACKs.
      // Keep that evidence listener until explicit replacement/shutdown closes
      // it; accepting evidence never re-enables submissions on this socket.
      if (!session.disposed && !this.stopping) session.keepLateAcks = true;
      if (!endRequested) { endRequested = true; requestedEnd = reason; }
      session.dispose();
      finishRetirement();
    };
    if (nativeDestroy) socket.ev.destroy = () => {
      // Baileys' internal end() calls destroy() directly. Retain its buffer
      // and listeners while already admitted native callbacks are still live.
      destroyRequested = true;
      session.dispose();
      finishRetirement();
    };
    auth.onDrained = () => finishRetirement(true);
    auth.onProviderIdle = finishRetirement;
    // Real sockets expose the same emitter used by their native receive path.
    // Mock sockets may supply only ev.process; keep that seam for transport tests.
    if (typeof socket.ev.emit === 'function') {
      line.restoreNativeEvents = session.restoreNativeEvents = preserveNativeMutationMetadata(socket);
      const nativeEmit = socket.ev.emit;
      // The provider's delayed flush can outlive the task context that queued
      // its messages. Fence new emissions at admission, so every later native
      // flush still contains only events accepted before/during the drain.
      session.bufferAdmissionFenced = true;
      socket.ev.emit = function (...args) {
        if (session.disposed && !auth.inProviderContext()) return false;
        return nativeEmit.apply(this, args);
      };
    }
    line.ack?.close();
    line.ack = session.ack = new AckTracker(socket, this.config.ackTimeoutMs, {
      onTimeout: id => this.trace(line, 'ack_timeout', { generation, native_id: id }),
      onConfirmation: id => {
        if (line.socket !== socket || line.generation !== generation || this.stopping) return;
        line.ackTail = line.ackTail.then(async () => {
          const record = line.store?.data?.messages[id];
          if (!record?.client_message_id || record.ack_confirmed) return;
          await line.store.update(data => { if (data.messages[id]) data.messages[id].ack_confirmed = true; });
          this.trace(line, 'ack_confirmed', { generation, native_id: id });
          if (!line.store.data.messages[id]?.pending_send) await this.enqueueOwnConfirmation(line, id);
        }).catch(this.onError);
      },
    });
    const eventFailed = error => {
      if (line.socket !== socket || line.generation !== generation) { this.onError(error); return; }
      this.observeDisconnect(line, socket, {lastDisconnect: {error: {bridgePersistenceFailure: true}}});
      this.blockConnection(line, socket, 'persistence_failed');
      line.status = 'error';
      line.recoveryCategory = 'storage_failure';
      line.diagnostics.last_category = 'storage_failure';
      line.retryBlocked = true;
      line.error = 'Could not safely save or synchronize the WhatsApp session. Reconnect after checking bridge storage.';
      clearTimeout(line.reconnectTimer);
      line.reconnectTimer = null;
      this.reconnectSlots.delete(line.line_id);
      session.dispose();
      line.ack?.close();
      socket.end(new Error('Linked-device event persistence failed'));
      if (line.socket === socket) line.socket = null;
      this.onError(error);
    };
    auth.onFailure = eventFailed;
    session.unsubscribe = socket.ev.process(events => {
      // Identity is checked at admission, never after another event has had a
      // chance to retire the socket. WhatsApp may already have acknowledged an
      // incoming message; accepted payloads must survive failure/replacement.
      const acceptedPipeline = auth.inProviderContext();
      const acceptedBuffer = session.disposed && session.bufferAdmissionFenced && !session.subscriptionReleased;
      if (!acceptedPipeline && !acceptedBuffer &&
          (line.socket !== socket || line.generation !== generation || session.disposed)) return Promise.resolve();
      // Fence immediately, before persistence or another queued event can yield.
      if (events['connection.update']?.connection === 'close') {
        // Classify before queued persistence yields: a raw socket-close timer
        // must not reconnect a revoked/conflicting session in the meantime.
        this.observeDisconnect(line, socket, events['connection.update']);
        this.blockConnection(line, socket, 'connection_closed');
      }
      let credentialsSaved;
      if (events['creds.update']) {
        // Baileys may already have rotated a server-side pre-key. Admit this
        // write now, before slow message/history processing can be overtaken by
        // reconnect. The next auth facade drains this same durable store queue.
        try {
          Object.assign(auth.state.creds, events['creds.update']);
          credentialsSaved = Promise.resolve(auth.saveCreds());
        } catch (error) { credentialsSaved = Promise.reject(error); }
        // Observe immediately, but do not reject this batch before preserving
        // its incoming messages and delivery evidence below.
        credentialsSaved = credentialsSaved.catch(error => {
          if (!error.bridgePersistenceFailure) eventFailed(error);
        });
      }
      line.eventTail = line.eventTail.then(async () => {
        if (credentialsSaved) await credentialsSaved;
        let connectionError;
        if (events['connection.update']) {
          try { await this.connectionUpdate(line, socket, events['connection.update']); }
          catch (error) { connectionError = error; }
        }
        if (line.removing) return;
        const upsert = events['messages.upsert'];
        if (upsert) for (const message of upsert.messages ?? []) {
          const requestId = message.bridgeRecoveryRequestId ?? upsert.requestId;
          if (requestId) {
            // Accept phone recovery only for an observed unavailable message,
            // preserving its authenticated routing metadata. Unsolicited PDOs
            // must not create customer messages or spoof another conversation.
            const pending = line.store.data.messages[message.key?.id] ?? message.bridgeRecoveryOriginal;
            if (!pending?.pending_placeholder || pending.message.key.remoteJid !== message.key?.remoteJid ||
                Boolean(pending.message.key.fromMe) !== Boolean(message.key?.fromMe) ||
                (pending.placeholder_request_id && pending.placeholder_request_id !== requestId)) continue;
            await this.receive(line, { ...pending.message, message: message.message });
          } else await this.receive(line, message);
        }
        for (const update of events['messages.update'] ?? []) await this.receiveUpdate(line, update);
        const history = events['messaging-history.set'];
        if (history && this.recovering(line)) await this.recoverHistory(line, history.messages ?? []);
        if (connectionError) throw connectionError;
      }).catch(eventFailed);
      return line.eventTail;
    });
    return this.lineStatus(line);
  }

  observeDisconnect(line, socket, update) {
    if (line.socket !== socket || line.session?.generation !== line.generation || line.session.disconnectRecorded) return;
    line.session.disconnectRecorded = true;
    const policy = classifyDisconnect(update.lastDisconnect?.error);
    line.recoveryCategory = policy.category;
    line.retryBlocked = !policy.retry;
    line.diagnostics.last_category = policy.category;
    line.diagnostics.disconnects[policy.category] = (line.diagnostics.disconnects[policy.category] ?? 0) + 1;
    if (!policy.retry) {
      clearTimeout(line.reconnectTimer);
      line.reconnectTimer = null;
      this.reconnectSlots.delete(line.line_id);
    }
  }
  async connectionUpdate(line, socket, update) {
    if (line.socket !== socket || line.session?.generation !== line.generation) return;
    const session = line.session;
    if (update.connection === 'close') {
      this.observeDisconnect(line, socket, update);
      this.blockConnection(line, socket, 'connection_closed');
      const code = update.lastDisconnect?.error?.output?.statusCode;
      this.trace(line, 'connection_closed', { provider_status: Number.isInteger(code) ? code : null,
        provider_reason: disconnectReason(update.lastDisconnect?.error) });
    }
    if (update.connection !== 'close' && session.closed) return;
    if (line.removing) {
      if (update.qr) line.removalWaiter?.reject(new BridgeError(409, 'UNLINK_NOT_CONFIRMED', 'The saved session cannot reconnect for unlinking. Its credentials have been retained.'));
      if (update.connection === 'open') { line.status = 'connected'; line.removalWaiter?.resolve(socket); }
      if (update.connection === 'close' && line.socket === socket) {
        line.ack?.close();
        line.socket = null;
        const reason = update.lastDisconnect?.error?.output?.statusCode;
        if (reason === DisconnectReason.loggedOut) {
          line.removalRemoteConfirmed = true;
          line.removalWaiter?.resolve(null);
        } else line.removalWaiter?.reject(new BridgeError(503, 'UNLINK_NOT_CONFIRMED', 'The saved WhatsApp session could not reconnect for unlinking. Retry removal.'));
      }
      return;
    }
    if (update.qr) {
      line.qr = update.qr;
      const qrUrl = await QRCode.toDataURL(update.qr, { errorCorrectionLevel: 'M', margin: 3, width: 320 });
      if (line.session !== session || session.closed) return;
      line.qr_data_url = qrUrl;
      line.status = 'qr';
    }
    if (update.connection === 'open') {
      const ownPhone = phoneDigits(socket.user?.id?.split('@')[0]?.split(':')[0] ?? line.auth.state.creds.me?.id?.split('@')[0]?.split(':')[0]);
      const expected = line.expected_phone || line.store.data.bound_phone;
      if (expected && ownPhone !== expected) {
        line.status = 'error';
        line.error = 'The linked phone does not match this configured line. Disconnect it and pair the expected phone.';
        await line.store.update(data => { data.enabled = false; });
        if (line.session !== session) return;
        session.dispose();
        line.ack.close();
        socket.end(new Error('Unexpected linked-device phone'));
        line.socket = null;
        return;
      }
      const existingOwner = [...this.lines.values()].find(other => other.line_id !== line.line_id && other.store?.data?.bound_phone === ownPhone);
      const reservedOwner = this.phoneReservations.get(ownPhone);
      if (existingOwner || (reservedOwner && reservedOwner !== line.line_id)) {
        line.status = 'error';
        line.error = 'This WhatsApp number is already linked to another line. Unlink this device and pair a different number.';
        await line.store.update(data => { data.enabled = false; });
        if (line.session !== session) return;
        session.dispose();
        line.ack.close();
        line.socket = null;
        delete line.qr;
        delete line.qr_data_url;
        socket.end(new Error('WhatsApp number is already linked to another line'));
        return;
      }
      // Reserve before awaiting disk writes, so simultaneous pairings cannot
      // both bind one physical account and produce duplicate inbound threads.
      this.phoneReservations.set(ownPhone, line.line_id);
      try { await line.store.update(data => { data.bound_phone = ownPhone; }); }
      finally { if (this.phoneReservations.get(ownPhone) === line.line_id) this.phoneReservations.delete(ownPhone); }
      if (line.session !== session || session.closed || socket.ws.isOpen !== true || this.stopping) return;
      line.display_phone_number = ownPhone;
      session.ready = true;
      line.status = 'connected';
      line.disconnectedSince = null;
      line.recoveryCategory = null;
      line.retryBlocked = false;
      if (line.diagnostics.recovery_started_at != null) {
        const seconds = Math.max(0, Date.now() - line.diagnostics.recovery_started_at) / 1000;
        line.diagnostics.recovery_count++;
        line.diagnostics.recovery_seconds_sum += seconds;
        line.diagnostics.recovery_seconds_last = seconds;
        delete line.diagnostics.recovery_started_at;
        this.trace(line, 'recovery_completed', { duration_ms: seconds * 1000 });
      }
      this.trace(line, 'connection_transition', { state: 'ready' });
      if (line.store.data.logged_out_at) await line.store.update(data => { delete data.logged_out_at; });
      if (line.store.data.recover_since && !line.store.data.recover_until) {
        // WhatsApp sends recent history right after pairing; accept it for a bounded time.
        await line.store.update(data => { data.recover_until = Date.now() + RECOVERY_WINDOW_MS; delete data.logout_reason; });
        this.trace(line, 'history_recovery_started', { since: line.store.data.recover_since });
      }
      session.readyAt = Date.now();
      delete line.error;
      delete line.qr;
      delete line.qr_data_url;
    }
    if (update.connection === 'close' && line.socket === socket) {
      session.dispose();
      line.ack.close();
      line.socket = null;
      delete line.qr;
      delete line.qr_data_url;
      const reason = update.lastDisconnect?.error?.output?.statusCode;
      if (reason === DisconnectReason.loggedOut) {
        clearTimeout(line.reconnectTimer);
        line.reconnectTimer = null;
        this.reconnectSlots.delete(line.line_id);
        line.status = 'logged_out';
        line.error = 'This linked device was logged out. Connect and scan a new QR code.';
        const auth = line.auth;
        const reason = disconnectReason(update.lastDisconnect?.error);
        const offlineSince = line.disconnectedSince ?? Date.now();
        await line.store.update(data => {
          data.enabled = false;
          data.logout_reason = reason;
          data.logged_out_at = Date.now();
          // Re-pairing recovers what customers sent from here on. Keep the earliest point.
          data.recover_since = Math.min(data.recover_since ?? Infinity, offlineSince - RECOVERY_MARGIN_MS);
          delete data.recover_until;
        });
        if (line.session !== session) return;
        line.auth = await auth.reset();
      } else if (line.retryBlocked) {
        line.status = 'error';
        line.error = line.recoveryCategory === 'session_conflict'
          ? 'Another session replaced this connection. Administrator review is required; saved credentials are retained.'
          : 'WhatsApp rejected this session or protocol. Administrator review is required; saved credentials are retained.';
      } else {
        line.status = 'disconnected';
        this.scheduleReconnect(line, reason === DisconnectReason.restartRequired);
      }
    }
  }
  scheduleReconnect(line, restartRequired = false) {
    if (this.stopping || line.removing || line.retryBlocked || !line.store?.data?.enabled || line.status !== 'disconnected' || line.reconnectTimer) return;
    const now = Date.now();
    let at = now + reconnectDelay(line.reconnectAttempts ?? 0, restartRequired, this.random);
    // Space nearby attempts without allowing a line's long backoff to hold
    // healthy identities behind it. Reservations are process-local, not owners.
    for (const reserved of [...this.reconnectSlots.values()].sort((a, b) => a - b)) {
      if (Math.abs(at - reserved) < 250) at = reserved + 250;
    }
    this.reconnectSlots.set(line.line_id, at);
    const delay = at - now;
    line.reconnectAttempts = (line.reconnectAttempts ?? 0) + 1;
    const generation = line.generation;
    this.trace(line, 'recovery_scheduled', { delay_ms: delay, attempt: line.reconnectAttempts });
    line.reconnectTimer = setTimeout(() => {
      line.reconnectTimer = null;
      this.reconnectSlots.delete(line.line_id);
      if (generation !== line.generation || this.stopping || line.removing || !line.store.data.enabled || line.status !== 'disconnected') return;
      line.diagnostics.reconnect_attempts++;
      this.connect(line.line_id).catch(error => {
        this.onError(error);
        const policy = classifyDisconnect(error);
        line.recoveryCategory = policy.category;
        line.retryBlocked = !policy.retry;
        if (!this.stopping && line.store.data.enabled && line.status !== 'error' &&
            policy.retry && transientConnectionError(error)) {
          line.status = 'disconnected';
          this.scheduleReconnect(line);
        } else if (!this.stopping && line.store.data.enabled && line.status !== 'error') {
          line.status = 'error';
          line.error = error.bridgePersistenceFailure
            ? 'Could not safely load or save the WhatsApp session. Check bridge storage before reconnecting.'
            : 'Could not initialize the WhatsApp connection. Check bridge configuration before reconnecting.';
        }
      });
    }, delay);
    line.reconnectTimer.unref();
  }

  async disconnect(id) {
    this.assertRunning();
    // Unlinking can outlive its HTTP connection. Share registration/removal
    // ownership so shutdown retains its stores until an admitted unlink ends.
    const task = this.registrationTail.then(async () => {
      this.assertRunning();
      const line = this.getLine(id);
      if (line.removing) throw new BridgeError(503, 'LINE_REMOVING', 'This WhatsApp number is being removed.');
      if (!line.store?.data) throw new BridgeError(503, 'LINE_STORAGE_UNAVAILABLE', 'Check this line session storage before disconnecting.');
      clearTimeout(line.reconnectTimer);
      line.reconnectTimer = null;
      this.reconnectSlots.delete(line.line_id);
      line.generation++;
      line.auth?.retire();
      line.session?.dispose();
      await line.store.update(data => { data.enabled = false; });
      const socket = line.socket;
      line.socket = null;
      line.ack?.close();
      if (socket) {
        try { await socket.logout(); }
        catch { socket.end(new Error('Explicit linked-device disconnect')); }
      }
      await line.eventTail;
      line.auth ??= await this.loadAuth(line);
      line.auth = await line.auth.reset();
      await line.store.update(data => { data.bound_phone = ''; });
      line.display_phone_number = line.expected_phone;
      line.status = 'logged_out';
      delete line.error;
      delete line.qr;
      delete line.qr_data_url;
      return this.lineStatus(line);
    });
    this.registrationTail = task.catch(() => {});
    return task;
  }

  // Baileys filters out negative contacts. An empty/absent response therefore
  // cannot distinguish an unregistered number from an incomplete lookup.
  async checkNumber(id, payload) {
    const line = this.getLine(id);
    const socket = this.connected(line);
    const phone = phoneDigits(payload?.phone);
    const lookup = socket.onWhatsApp(`${phone}@s.whatsapp.net`);
    let timer;
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new BridgeError(504, 'LOOKUP_TIMEOUT', 'WhatsApp did not answer the number lookup in time.')), 10_000); });
    let results;
    try { results = await Promise.race([lookup, timeout]); } finally { clearTimeout(timer); }
    const match = (Array.isArray(results) ? results : []).find(item => item?.exists === true);
    const jid = String(match?.jid ?? '');
    // Numeric LIDs are private account identifiers, not dialable phone numbers.
    const jidPhone = jid.endsWith('@s.whatsapp.net') ? jid.split('@')[0].split(':')[0] : '';
    return { phone, exists: match ? true : null, whatsapp_phone: /^\d{5,15}$/.test(jidPhone) ? jidPhone : phone };
  }
  trace(line, event, fields = {}) {
    // Callers supply only fixed labels, generations, durations and opaque IDs.
    this.log({ component: 'whatsapp_bridge', event, line_id: line.line_id, generation: line.generation, ...fields });
  }
  blockConnection(line, socket, reason) {
    if (line.socket !== socket || !line.session || line.session.closed) return;
    line.disconnectedSince ??= Date.now();
    line.diagnostics.recovery_started_at ??= Date.now();
    line.diagnostics.disconnects_total = (line.diagnostics.disconnects_total ?? 0) + 1;
    if (line.session.readyAt && Date.now() - line.session.readyAt >= 60_000) line.reconnectAttempts = 0;
    line.session.closed = true;
    line.session.ready = false;
    // Work still running on a dead transport cannot finish normally; the next
    // reconnect waits a short deadline for it instead of the full one.
    if (DEAD_TRANSPORT_REASONS.has(reason)) line.transportLost = true;
    line.ack?.interrupt();
    if (!line.removing && line.status !== 'error') line.status = 'disconnected';
    this.trace(line, 'connection_transition', { state: 'blocked', reason });
  }
  connected(line, expectedSocket = null) {
    if (line.removing) throw new BridgeError(503, 'LINE_REMOVING', 'This WhatsApp number is being removed.');
    const socket = line.socket;
    if (socket && line.session?.ready && socket.ws.isOpen !== true) this.restartStaleSocket(line, socket);
    if (this.stopping || line.status !== 'connected' || !socket ||
        !line.session?.ready || line.session.closed || line.session.generation !== line.generation ||
        socket.ws.isOpen !== true || (expectedSocket && expectedSocket !== socket)) {
      this.trace(line, 'send_blocked', { reason: 'transport_not_ready' });
      const error = new BridgeError(503, 'NOT_CONNECTED', 'This WhatsApp line is reconnecting. Try again once connected.');
      error.delivery_uncertain = false;
      throw error;
    }
    return socket;
  }
  // Text sends and edit/delete relays are normally acknowledged within ~2 s;
  // silence for 30 s means the transport is dead, so reconnect early instead
  // of waiting the 120 s that a media upload to WhatsApp may legitimately need.
  submissionTimeout(media = false) {
    const general = this.config.submissionTimeoutMs ?? 120_000;
    return media ? general : Math.min(general, this.config.textSubmissionTimeoutMs ?? 30_000);
  }
  async awaitSubmission(task, waiter, { media = false } = {}) {
    let timer;
    // ACKs may precede sendMessage's return; only rejection interrupts that call.
    const interrupted = waiter.promise.then(() => new Promise(() => {}));
    try {
      return await Promise.race([task, interrupted, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new BridgeError(504, 'CONFIRMATION_UNKNOWN', 'Message submission did not finish in time.')),
          this.submissionTimeout(media));
      })]);
    } finally { clearTimeout(timer); }
  }
  async counterpart(line, key) {
    for (const jid of [key.remoteJidAlt, key.remoteJid]) {
      if (jid?.endsWith('@s.whatsapp.net')) return phoneDigits(jid.split('@')[0].split(':')[0]);
    }
    if (key.remoteJid?.endsWith('@lid')) {
      try {
        const phoneJid = await line.socket?.signalRepository?.lidMapping?.getPNForLID(key.remoteJid);
        if (phoneJid?.endsWith('@s.whatsapp.net')) return phoneDigits(phoneJid.split('@')[0].split(':')[0]);
      } catch (error) {
        // Retirement or failed auth storage must not discard an accepted LID
        // message. receive() retains its native payload for later normalization.
        this.onError(error);
      }
    }
    return null;
  }
  async enqueue(event) {
    if (this.lines.get(event.line_id)?.removing || (this.manifest.data.removed_line_ids ?? []).includes(event.line_id)) return;
    await this.outbox.update(data => {
      if (!data.entries.some(entry => entry.event.event_id === event.event_id)) data.entries.push({ event, attempts: 0, next_try: 0, created_at: Date.now() });
    });
    this.wakeCallbacks(event.line_id);
  }
  // Delivered/read receipts become dashboard ticks. Only messages the
  // dashboard knows about are forwarded (its own sends and phone-app messages),
  // never the bot's automatic replies.
  async receiveReceipt(line, key, status, timestamp = Date.now()) {
    const record = line.store.data.messages[key.id];
    if (key.fromMe === false) return; // Own-device reads are not recipient receipts.
    if (!record?.message?.key?.fromMe || !record.phone || record.deleted) return;
    if (!record.client_message_id && !record.mirrored_outgoing) return;
    if ((RECEIPT_RANK[record.receipt] ?? 0) >= RECEIPT_RANK[status]) return;
    if (record.client_message_id && !record.confirmation_enqueued) {
      // A receipt proves WhatsApp accepted a send whose ACK was lost.
      await line.store.update(data => { data.messages[key.id].ack_confirmed = true; });
      await this.enqueueOwnConfirmation(line, key.id);
    }
    await this.enqueue(messageStatusEvent(line, key.id, record.phone, status, timestamp));
    await line.store.update(data => { if (data.messages[key.id] && (RECEIPT_RANK[data.messages[key.id].receipt] ?? 0) < RECEIPT_RANK[status]) data.messages[key.id].receipt = status; });
  }
  async markRead(id, payload) {
    const line = this.getLine(id);
    const task = (line.readTail ?? Promise.resolve()).then(async () => {
      const socket = this.connected(line);
      const phone = phoneDigits(payload?.phone);
      const record = this.target(line, payload?.message_id, phone, false);
      const key = record.message.key;
      if (key.fromMe) throw new BridgeError(400, 'NOT_INCOMING', 'Only incoming messages can be marked read.');
      const timestamp = Number(record.message.messageTimestamp?.toNumber?.() ?? record.message.messageTimestamp);
      if (!Number.isFinite(timestamp) || timestamp <= 0) throw new BridgeError(409, 'MISSING_READ_BOUNDARY', 'The original message timestamp is unavailable.');
      await socket.readMessages([key]);
      // Remember exactly which same-second messages were read. Never substitute
      // Date.now() or a current chat snapshot for the original message range.
      await line.store.update(data => { data.messages[key.id].dashboard_read = true; });
      const newer = Object.values(line.store.data.messages).some(other => {
        if (other.phone !== phone || other.message?.key?.fromMe || other.message?.key?.id === key.id || other.deleted) return false;
        const time = Number(other.message?.messageTimestamp?.toNumber?.() ?? other.message?.messageTimestamp);
        return !Number.isFinite(time) || time > timestamp || (time === timestamp && !other.dashboard_read);
      });
      if (!newer) {
        // readMessages informs the customer; this app-state patch clears the
        // unread counter on the account's other linked devices. The explicit
        // key/time range is retained even if an incoming message races the ACK.
        await socket.chatModify({ markRead: true, lastMessages: [{key: {...key}, messageTimestamp: timestamp}] }, key.remoteJid);
      }
      return { success: true, chat_state_synced: !newer };
    });
    line.readTail = task.catch(() => {});
    return task;
  }
  deliveryStatus(id, payload) {
    const line = this.getLine(id);
    const clientId = payload?.client_message_id;
    const phone = payload?.phone;
    if (typeof clientId !== 'string' || !clientId || clientId.length > 191 ||
        typeof phone !== 'string' || !/^\d{6,20}$/.test(phone)) {
      throw new BridgeError(400, 'INVALID_DELIVERY_LOOKUP', 'A client message ID and recipient are required.');
    }
    const request = line.store.data.send_requests[crypto.createHash('sha256').update(clientId).digest('hex')];
    if (!request?.native_id) return { status: 'unknown' };
    const nativeId = request.native_id, record = line.store.data.messages[nativeId];
    if (record.phone !== phone) throw new BridgeError(409, 'CLIENT_ID_CONFLICT', 'The client ID belongs to another recipient.');
    // Read durable evidence only, even while disconnected. No socket send,
    // resend, queue mutation, or receipt upgrade occurs in this endpoint.
    if (!record.ack_confirmed && !record.echo_seen) return { status: 'unconfirmed' };
    return { status: 'confirmed', message_id: wrapId(id, nativeId), timestamp: record.timestamp };
  }

  async enqueueOwnConfirmation(line, nativeId) {
    if (line.confirmationTasks.has(nativeId)) return line.confirmationTasks.get(nativeId);
    const task = this.enqueueOwnConfirmationOnce(line, nativeId).finally(() => line.confirmationTasks.delete(nativeId));
    line.confirmationTasks.set(nativeId, task);
    return task;
  }
  async enqueueOwnConfirmationOnce(line, nativeId) {
    const record = line.store.data.messages[nativeId];
    if (!record?.client_message_id || record.confirmation_enqueued || (!record.ack_confirmed && !record.echo_seen)) return;
    const timestamp = millis(record.message?.messageTimestamp ?? record.timestamp);
    await this.enqueue(outgoingConfirmationEvent(line, nativeId, record.phone, record.client_message_id, timestamp));
    await line.store.update(data => {
      if (data.messages[nativeId]) data.messages[nativeId].confirmation_enqueued = true;
    });
  }
  async applyEvent(line, event) {
    const id = unwrapId(line.line_id, event.message_id);
    const record = line.store.data.messages[id];
    if (record && (Boolean(record.message.key.fromMe) !== event.from_me || (record.phone && record.phone !== event.sender_phone))) return;
    if (record?.deleted) {
      if (event.type === 'message_deleted_for_everyone') await this.purgeDeletedContent(line, event.message_id);
      return;
    }
    if (record?.last_event_id === event.event_id) return;
    if (record?.last_mutation_timestamp && record.last_mutation_timestamp > event.timestamp) return;
    await this.enqueue(event);
    await line.store.update(data => {
      const current = data.messages[id] ?? (event.type === 'message_deleted_for_everyone' ?
        (data.messages[id] = { message: { key: { id, remoteJid: `${event.sender_phone}@s.whatsapp.net`, fromMe: event.from_me } },
          phone: event.sender_phone, timestamp: event.timestamp, notified: true }) : null);
      if (!current) return;
      current.last_event_id = event.event_id;
      current.last_mutation_timestamp = event.timestamp;
      if (event.type === 'message_deleted_for_everyone') { current.deleted = true; current.text = null; current.callbacks_scrubbed = false; }
      else { current.text = event.body; current.message.message = { conversation: event.body }; }
    });
    if (event.type === 'message_deleted_for_everyone') await this.purgeDeletedContent(line, event.message_id);
  }
  async purgeDeletedContent(line, wrapped) {
    const nativeId = unwrapId(line.line_id, wrapped);
    await this.mediaStorage(line, () => line.store.update(data => {
      const record = data.messages[nativeId];
      const mediaIds = Object.entries(data.media)
        .filter(([id, media]) => id === record?.media_id || media.message?.key?.id === nativeId).map(([id]) => id);
      if (record) {
        // Keep just the original routing key and times needed for deduplication.
        record.message = { key: record.message.key, messageTimestamp: record.message.messageTimestamp };
        record.text = null;
        delete record.media_id;
      }
      data.pending_media_deletions ??= [];
      for (const id of mediaIds) {
        const usedElsewhere = Object.entries(data.messages).some(([key, item]) => key !== nativeId && !item.deleted && item.media_id === id);
        if (!usedElsewhere) {
          delete data.media[id];
          if (!data.pending_media_deletions.includes(id)) data.pending_media_deletions.push(id);
        }
      }
      for (const operation of Object.values(data.operations)) {
        if (operation.target_id !== nativeId || operation.kind !== 'edit') continue;
        delete operation.body;
        delete operation.payload;
        delete operation.event;
        operation.event_enqueued = true;
      }
    }));
    const targetsMessage = event => event.message_id === wrapped ||
      event.payload?.entry?.some(entry => entry.changes?.some(change => change.value?.messages?.some(message => message.id === wrapped)));
    const deletedRecord = line.store.data.messages[nativeId];
    const deletedTimestamp = deletedRecord?.last_mutation_timestamp ?? Date.now();
    await this.outbox.update(data => {
      const recoveredTombstones = [];
      const keep = (item, wasDeadLetter = false) => {
        if (!targetsMessage(item.event)) return true;
        if (item.event.type === 'message_deleted_for_everyone' && !item.event.operation_id && deletedRecord?.message?.key?.fromMe === true && !deletedRecord.client_message_id) {
          // Phone-originated originals may never have reached the app. Deliver
          // the deletion and its original routing time together, rather than
          // retrying a mutation of a database row that cannot appear.
          item.event = messageTombstoneEvent(line, nativeId, deletedRecord.phone,
            deletedRecord.timestamp, deletedTimestamp, true, Boolean(deletedRecord.message.messageTimestamp));
          item.next_try = 0;
          if (wasDeadLetter) {
            recoveredTombstones.push({ event: item.event, attempts: 0, next_try: 0, created_at: Date.now() });
            return false;
          }
          return true;
        }
        if (['message_deleted_for_everyone', 'outgoing_confirmation', 'message_tombstone'].includes(item.event.type)) return true;
        if (['message', 'message_snapshot'].includes(item.event.type) && item.event.from_me === false) {
          // Never route the revoked original through the normal customer/AI
          // webhook. A separate content-free callback creates its database
          // placeholder before the following delete event is delivered.
          item.event = messageTombstoneEvent(line, nativeId, item.event.sender_phone, item.event.timestamp, deletedTimestamp);
          item.next_try = 0;
          if (wasDeadLetter) {
            recoveredTombstones.push({ event: item.event, attempts: 0, next_try: 0, created_at: Date.now() });
            return false;
          }
          return true;
        }
        if (item.event.type === 'outgoing_message') {
          item.event = messageTombstoneEvent(line, nativeId, deletedRecord.phone,
            item.event.timestamp, deletedTimestamp, true);
          item.next_try = 0;
          if (wasDeadLetter) {
            recoveredTombstones.push({ event: item.event, attempts: 0, next_try: 0, created_at: Date.now() });
            return false;
          }
          return true;
        }
        return false;
      };
      const seen = new Set();
      data.entries = data.entries.filter(item => {
        if (!keep(item, false) || seen.has(item.event.event_id)) return false;
        seen.add(item.event.event_id);
        return true;
      });
      data.dead_letters = (data.dead_letters ?? []).filter(item => keep(item, true));
      for (const item of recoveredTombstones.reverse()) {
        if (!data.entries.some(entry => entry.event.event_id === item.event.event_id)) data.entries.unshift(item);
      }
      if (deletedRecord?.pending_original_tombstone) {
        const event = messageTombstoneEvent(line, nativeId, deletedRecord.phone,
          deletedRecord.timestamp, deletedTimestamp, true);
        if (!data.entries.some(item => item.event.event_id === event.event_id)) {
          data.entries.push({ event, attempts: 0, next_try: 0, created_at: Date.now() });
        }
      }
      // A revoke can arrive before the original upsert, or after its callback
      // was delivered. The backend accepts this idempotent content-free marker
      // before applying the revoke, so neither ordering loses the placeholder.
      if (deletedRecord?.message?.key?.fromMe === false &&
          !data.entries.some(item => item.event.type === 'message_tombstone' && item.event.message_id === wrapped)) {
        const deleteIndex = data.entries.findIndex(item => item.event.type === 'message_deleted_for_everyone' && item.event.message_id === wrapped);
        if (deleteIndex >= 0) data.entries.splice(deleteIndex, 0, {
          event: messageTombstoneEvent(line, nativeId, deletedRecord.phone, deletedRecord.timestamp, deletedTimestamp),
          attempts: 0, next_try: 0, created_at: Date.now(),
        });
      }
    });
    this.wakeCallbacks(line.line_id);
    line.restoreNativeEvents?.purgeTarget?.(nativeId);
    await this.removePendingMedia(line);
    await line.store.update(data => {
      if (data.messages[nativeId]?.deleted) Object.assign(data.messages[nativeId], { callbacks_scrubbed: true, tombstone_version: 2, pending_original_tombstone: false });
    });
  }
  mediaStorage(line, action) {
    const task = (line.mediaTail ?? Promise.resolve()).then(action);
    line.mediaTail = task.catch(() => {});
    return task;
  }
  async removePendingMedia(line) {
    return this.mediaStorage(line, async () => {
      for (const mediaId of line.store.data.pending_media_deletions ?? []) {
        try { await unlink(this.mediaPath(line, mediaId)); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
        await line.store.update(data => { data.pending_media_deletions = data.pending_media_deletions.filter(id => id !== mediaId); });
      }
    });
  }
  async retirePhone(line) {
    const phone = knownPhone(line.store.data.bound_phone || line.display_phone_number);
    if (!phone) return;
    const offlineSince = line.store.data.recover_since ?? (line.status === 'connected' ? Date.now() : line.disconnectedSince ?? Date.now());
    await this.retiredPhones.update(data => {
      const cutoff = Date.now() - RETIRED_PHONE_MS;
      for (const [known, entry] of Object.entries(data.phones)) if (!(entry?.removed_at > cutoff)) delete data.phones[known];
      data.phones[phone] = { offline_since: Math.min(data.phones[phone]?.offline_since ?? Infinity, offlineSince), removed_at: Date.now() };
    });
  }
  async adoptRetiredPhone(line) {
    // The second socket of a fresh pairing already knows its phone, before
    // WhatsApp sends history. A line that was paired before is not a re-link.
    const own = knownPhone(line.auth.state.creds.me?.id?.split('@')[0]?.split(':')[0]);
    const entry = own && !line.store.data.bound_phone && !line.store.data.recover_since ? this.retiredPhones?.data.phones[own] : null;
    if (!entry || !(entry.removed_at > Date.now() - RETIRED_PHONE_MS)) return;
    await line.store.update(data => { data.recover_since = entry.offline_since - RECOVERY_MARGIN_MS; delete data.recover_until; });
    await this.retiredPhones.update(data => { delete data.phones[own]; });
  }
  async recoverHistory(line, messages) {
    const since = line.store.data.recover_since;
    let recovered = 0;
    const ordered = [...messages].sort((a, b) => millis(a.messageTimestamp) - millis(b.messageTimestamp));
    for (const message of ordered) {
      const sentAt = millis(message.messageTimestamp);
      if (!sentAt || sentAt < since || sentAt > Date.now() + RECOVERY_MARGIN_MS) continue;
      if (line.store.data.messages[message.key?.id]?.notified) continue;
      await this.receive(line, message, { recovered: true });
      if (line.store.data.messages[message.key?.id]?.notified) recovered++;
    }
    if (recovered) this.trace(line, 'history_recovered', { messages: recovered });
  }

  async receive(line, message, { recovered = false } = {}) {
    const key = message.key;
    if (!key?.id || !key.remoteJid || /@(g\.us|broadcast|newsletter)$/.test(key.remoteJid)) return;
    wrapId(line.line_id, key.id);
    const existing = line.store.data.messages[key.id];
    const existingRouteMatches = existing && Boolean(existing.message?.key?.fromMe) === Boolean(key.fromMe) &&
      [existing.message?.key?.remoteJid, existing.message?.key?.remoteJidAlt].includes(key.remoteJid);
    let phone = existingRouteMatches ? existing.phone : undefined;
    if (!phone) {
      try { phone = await this.counterpart(line, key); }
      catch (error) {
        // A queued LID payload can outlive its auth owner. Preserve it for
        // verified normalization instead of losing it when key lookup fails.
        if (!key.remoteJid.endsWith('@lid')) throw error;
        this.onError(error);
      }
    }
    const mutation = mutationEvent(line, message, phone);
    if (mutation) {
      const ownOperation = Object.values(line.store.data.operations).find(operation => operation.provider_id === key.id);
      // A relayed operation is confirmed only by its ACK, never by a local echo.
      if (ownOperation) return;
      const target = line.store.data.messages[unwrapId(line.line_id, mutation.message_id)];
      // A target ID alone is not a conversation binding. The exact saved LID
      // and sender side must agree before reusing its already verified phone.
      if (!phone && target?.phone && Boolean(target.message?.key?.fromMe) === Boolean(key.fromMe) &&
          [target.message?.key?.remoteJid, target.message?.key?.remoteJidAlt].includes(key.remoteJid)) phone = target.phone;
      const pendingId = crypto.createHash('sha256').update(encode(message)).digest('hex');
      if (!phone) {
        if (!line.store.data.pending_mutations?.[pendingId]) await line.store.update(data => {
          data.pending_mutations ??= {};
          data.pending_mutations[pendingId] = {message};
        });
        return;
      }
      await this.applyEvent(line, {...mutation, sender_phone: phone});
      // Only release retained input after the durable event/tombstone work
      // finishes (or its verified routing/side is rejected by applyEvent).
      if (line.store.data.pending_mutations?.[pendingId]) await line.store.update(data => {
        delete data.pending_mutations[pendingId];
        if (!Object.keys(data.pending_mutations).length) delete data.pending_mutations;
      });
      return;
    }
    if (existing?.deleted) {
      // Never resurrect content, including late contact cards. If a revoke
      // preceded its original, recover the real send time without guessing a
      // conversation from the deletion time.
      if (!existing.message.messageTimestamp && message.messageTimestamp && phone === existing.phone && Boolean(key.fromMe) === Boolean(existing.message.key.fromMe)) {
        await line.store.update(data => {
          data.messages[key.id].message.messageTimestamp = message.messageTimestamp;
          data.messages[key.id].timestamp = millis(message.messageTimestamp);
          if (key.fromMe && !existing.client_message_id) {
            data.messages[key.id].pending_original_tombstone = true;
            data.messages[key.id].callbacks_scrubbed = false;
          }
        });
      }
      await this.purgeDeletedContent(line, wrapId(line.line_id, key.id));
      return;
    }
    const content = normalizeMessageContent(message.message);
    if (!content || content.placeholderMessage) {
      if ((content?.placeholderMessage || message.messageStubType === proto.WebMessageInfo.StubType.CIPHERTEXT) && !existing?.notified) {
        await line.store.update(data => {
          data.messages[key.id] = { ...existing, message, phone, notified: false,
            pending_placeholder: true, placeholder_request_id: message.messageStubParameters?.[1] ?? existing?.placeholder_request_id,
            timestamp: millis(message.messageTimestamp) };
        });
      }
      return;
    }
    if (content.protocolMessage) return;
    const isContact = Boolean(content.contactMessage || content.contactsArrayMessage);
    if (existing?.notified && !(isContact && !existing.contacts_normalized)) {
      // A real fromMe upsert can confirm a dashboard send whose ACK or HTTP
      // response was lost. Keep its original client ID and reconcile the
      // existing database row instead of mirroring it as a second message.
      if (key.fromMe && existing.client_message_id && !existing.confirmation_enqueued &&
          (existing.pending_send || existing.delivery_uncertain) && phone === existing.phone) {
        await line.store.update(data => {
          const current = data.messages[key.id];
          current.message = message;
          current.timestamp = millis(message.messageTimestamp);
          current.text = textBody(content);
          current.echo_seen = true;
        });
        await this.enqueueOwnConfirmation(line, key.id);
      }
      return;
    }
    const mediaType = MEDIA_TYPES.find(type => content[`${type}Message`]);
    let mediaId = existing?.media_id;
    if (mediaType && !mediaId) {
      mediaId = `ldmedia:${line.line_id}:${crypto.randomUUID()}`;
      await line.store.update(data => { data.media[mediaId] = { message, mime_type: content[`${mediaType}Message`].mimetype ?? 'application/octet-stream', downloaded: false }; });
    }
    const event = phone ? (key.fromMe ? outgoingEvent(line, message, phone, mediaId) : inboundEvent(line, message, phone, mediaId, { recovered })) : null;
    if (event) await this.enqueue(event);
    await line.store.update(data => {
      data.messages[key.id] = { ...(data.messages[key.id] ?? {}), message, phone, media_id: mediaId,
        timestamp: existing?.timestamp ?? millis(message.messageTimestamp), text: textBody(content), notified: Boolean(event),
        pending_normalization: !phone, pending_placeholder: false, contacts_normalized: isContact && Boolean(event),
        ...(key.fromMe && event ? { mirrored_outgoing: true } : {}) };
    });
  }
  async receiveUpdate(line, { key, update }) {
    if (!key?.id || !update) return;
    if (update.status === proto.WebMessageInfo.Status.ERROR) {
      // Negative provider evidence can arrive after a send is already marked
      // uncertain, or while an account-restriction query delays the native
      // callback. Retain it independently of positive receipt/UI transitions;
      // this evidence never authorizes a retry or a new provider message ID.
      const evidence = { key, status: update.status, parameters: update.messageStubParameters ?? [] };
      const id = crypto.createHash('sha256').update(encode(evidence)).digest('hex');
      await line.store.update(data => {
        data.provider_rejections ??= {};
        data.provider_rejections[id] ??= { ...evidence, observed_at: Date.now() };
      });
    }
    if (update.messageStubParameters?.[1] && line.store.data.messages[key.id]?.pending_placeholder) {
      await line.store.update(data => { data.messages[key.id].placeholder_request_id = update.messageStubParameters[1]; });
    }
    if (RECEIPT_STATUSES[update.status]) await this.receiveReceipt(line, key, RECEIPT_STATUSES[update.status], millis(update.messageTimestamp));
    // messages.delete is a local chat deletion event and is intentionally never relayed.
    const deleted = update.messageStubType === proto.WebMessageInfo.StubType.REVOKE;
    const edited = Boolean(update.message?.editedMessage);
    if (!deleted && !edited) return;
    const record = line.store.data.messages[key.id];
    const phone = record?.phone ?? await this.counterpart(line, key);
    if (!phone) return;
    const sourceId = update.bridgeMutationSourceId ?? update.key?.id;
    const ownOperation = Object.values(line.store.data.operations).find(operation => operation.provider_id === sourceId || (operation.target_id === key.id && operation.status === 'pending'));
    if (ownOperation) return;
    if (deleted && record?.deleted) return;
    const body = edited ? textBody(update.message) : undefined;
    if (edited && body === null) return;
    const timestamp = update.bridgeMutationTimestampMs ? millis(update.bridgeMutationTimestampMs) : millis(update.messageTimestamp);
    if (edited && record?.text === body && record?.last_mutation_timestamp === timestamp) return;
    const type = deleted ? 'message_deleted_for_everyone' : 'message_updated';
    await this.applyEvent(line, { event_id: eventId(type, line.line_id, key.id, timestamp, body), type, line_id: line.line_id,
      display_phone_number: line.display_phone_number, message_id: wrapId(line.line_id, key.id), body, timestamp,
      from_me: Boolean(key.fromMe), sender_phone: phone });
  }

  async uploadMedia(id, payload) {
    const line = this.getLine(id);
    if (line.removing) throw new BridgeError(503, 'LINE_REMOVING', 'This WhatsApp number is being removed.');
    if (!line.store?.data) throw new BridgeError(503, 'LINE_STORAGE_UNAVAILABLE', 'Check this line session storage before uploading media.');
    // Outgoing uploads are optional new work, unlike incoming messages and
    // delivery evidence. Probe before decoding the potentially large body.
    await this.storageMonitor.sample();
    this.storageMonitor.assertCanSubmit(id);
    const encoded = payload?.content_base64;
    if (typeof encoded !== 'string' || encoded.length % 4 !== 0) throw new BridgeError(400, 'INVALID_MEDIA', 'Provide valid base64 media content.');
    // Bound allocation first. Repeated regex groups overflow V8's stack on
    // normal multi-megabyte attachments; Buffer's decoder alone is permissive.
    const uploadLimit = this.config.maxUploadBytes ?? this.config.maxMediaBytes;
    if (encoded.length > Math.ceil(uploadLimit / 3) * 4) throw new BridgeError(413, 'MEDIA_TOO_LARGE', 'Media exceeds the upload limit.');
    const releaseCapacity = this.storageMonitor.reserveOptionalWrite(id, encoded.length / 4 * 3);
    try {
      const bytes = Buffer.from(encoded, 'base64');
      if (bytes.toString('base64') !== encoded) throw new BridgeError(400, 'INVALID_MEDIA', 'Provide valid base64 media content.');
      if (!bytes.length || bytes.length > uploadLimit) throw new BridgeError(413, 'MEDIA_TOO_LARGE', `Media must be nonempty and no larger than ${uploadLimit / (1024 * 1024)} MiB.`);
      const rawMime = String(payload.mime_type ?? '').trim().toLowerCase();
      const baseMime = rawMime.split(';')[0].trim();
      const mime = baseMime === 'audio/ogg' && /codecs\s*=\s*opus/.test(rawMime) ? 'audio/ogg; codecs=opus' : baseMime;
      if (!/^[a-z0-9][a-z0-9.+-]*\/[a-z0-9][a-z0-9.+-]*$/.test(baseMime)) throw new BridgeError(400, 'INVALID_MEDIA', 'Provide a valid media MIME type.');
      const displayFilename = path.basename(String(payload.filename ?? 'attachment')).slice(0, 255);
      const contentHash = crypto.createHash('sha256').update(bytes).digest('hex');
      // A send can be rejected after its upload completed. Identical retries must
      // reuse those bytes instead of creating another otherwise orphaned file.
      const digest = crypto.createHash('sha256').update(JSON.stringify(['outgoing-media-v1', contentHash, mime, displayFilename])).digest('hex');
      const stableId = `${digest.slice(0, 8)}-${digest.slice(8, 12)}-${digest.slice(12, 16)}-${digest.slice(16, 20)}-${digest.slice(20, 32)}`;
      const mediaId = `ldmedia:${id}:${stableId}`;
      return await this.mediaStorage(line, async () => {
        if (line.removing || this.stopping) throw new BridgeError(503, 'STOPPING', 'Media storage is shutting down.');
        const filename = this.mediaPath(line, mediaId);
        const existing = line.store.data.media[mediaId];
        if (existing) {
          if (existing.content_hash !== contentHash || existing.mime_type !== mime || existing.filename !== displayFilename) {
            throw new BridgeError(409, 'MEDIA_ID_CONFLICT', 'The attachment storage identity conflicts with existing media.');
          }
          if (existing.downloaded && !(line.store.data.pending_media_deletions ?? []).includes(mediaId)) {
            const saved = await stat(filename).catch(error => { if (error.code !== 'ENOENT') throw error; return null; });
            if (saved?.isFile() && saved.size === bytes.length) return { id: mediaId };
          }
        }
        this.storageMonitor.assertCanSubmit(id);
        await this.writeBytes(filename, bytes);
        await line.store.update(data => {
          data.media[mediaId] = { content_hash: contentHash, mime_type: mime, filename: displayFilename, downloaded: true };
          if (data.pending_media_deletions) data.pending_media_deletions = data.pending_media_deletions.filter(value => value !== mediaId);
        });
        return { id: mediaId };
      });
    } finally { releaseCapacity(); }
  }
  mediaPath(line, id) {
    const match = /^ldmedia:(\d{5,32}):([0-9a-f-]{36})$/.exec(id);
    if (!match || match[1] !== line.line_id) throw new BridgeError(404, 'UNKNOWN_MEDIA', 'Media does not belong to this linked-device line.');
    return path.join(line.directory, 'media', `${match[2]}.bin`);
  }
  async writeBytes(filename, bytes) {
    return this.mediaFiles.write(filename, bytes);
  }
  async downloadMedia(id, mediaId) {
    const line = this.getLine(id);
    if (!line.store?.data) throw new BridgeError(503, 'LINE_STORAGE_UNAVAILABLE', 'Check this line session storage before retrieving media.');
    const filename = this.mediaPath(line, mediaId);
    const media = line.store.data.media[mediaId];
    if (!media) throw new BridgeError(404, 'UNKNOWN_MEDIA', 'Media was not captured by this linked-device line.');
    if (!media.downloaded) {
      const socket = this.connected(line);
      const download = options => this.downloadMediaMessage(media.message, 'stream', { ...options, options: { signal: AbortSignal.timeout(30_000) } }, { logger: silentLogger, reuploadRequest: socket.updateMediaMessage });
      let stream;
      try {
        stream = await download({});
      } catch (error) {
        this.assertRunning();
        // Some messages (seen with stickers) carry a URL on a host that does not
        // resolve, such as a.whatsapp.net. Baileys keeps that host when it builds
        // the URL from directPath, so the download can never succeed. Retry once
        // through WhatsApp's default media host.
        const host = mediaUrlHost(media.message);
        console.warn(`Linked-device media download failed: line=${line.line_id} host=${host ?? 'none'} code=${error?.cause?.code ?? error?.code ?? error?.name ?? 'unknown'}`);
        if (!host || host === DEFAULT_MEDIA_HOST) throw error;
        stream = await download({ host: DEFAULT_MEDIA_HOST });
      }
      const chunks = [];
      let size = 0;
      for await (const chunk of stream) {
        size += chunk.length;
        if (size > this.config.maxMediaBytes) {
          stream.destroy();
          throw new BridgeError(413, 'MEDIA_TOO_LARGE', `Incoming media exceeds the ${this.config.maxMediaBytes / (1024 * 1024)} MiB bridge limit.`);
        }
        chunks.push(chunk);
      }
      const bytes = Buffer.concat(chunks);
      // Network downloads are optional work and may finish after shutdown.
      // Only publication enters the media queue: already admitted publication
      // drains with close(), while late downloads allocate no storage.
      await this.mediaStorage(line, async () => {
        this.assertRunning();
        if (!line.store.data.media[mediaId]) throw new BridgeError(404, 'UNKNOWN_MEDIA', 'Media was removed while its download was in progress.');
        await this.writeBytes(filename, bytes);
        await line.store.update(data => { data.media[mediaId].downloaded = true; });
      });
    }
    return { content: await readFile(filename), mime_type: media.mime_type };
  }

  sendFingerprint(line, payload) {
    const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object'
      ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
    const normalized = {...payload, to: phoneDigits(payload?.to)};
    delete normalized.client_message_id;
    if (MEDIA_TYPES.includes(payload.type)) {
      const media = line.store.data.media[payload[payload.type]?.id];
      // Uploaded IDs are not logical content: the API can upload identical bytes
      // again after a definitely-not-submitted failure.
      if (media?.content_hash) normalized[payload.type] = {...payload[payload.type], id: media.content_hash, mime_type: media.mime_type};
    }
    return crypto.createHash('sha256').update(JSON.stringify(canonical(normalized))).digest('hex');
  }
  queueOwnConfirmation(line, nativeId) {
    // The acknowledged record is already fsynced. Recovery scans this durable
    // obligation after a crash; unrelated callback backlog cannot delay HTTP.
    this.enqueueOwnConfirmation(line, nativeId).catch(error => this.onError(error));
  }
  async send(id, payload) {
    const line = this.getLine(id);
    const clientId = payload?.client_message_id;
    if (!clientId || typeof clientId !== 'string') return this.dispatchSend(id, payload);
    const key = crypto.createHash('sha256').update(clientId).digest('hex');
    const fingerprint = this.sendFingerprint(line, payload);
    const durable = line.store.data.messages[line.store.data.send_requests[key]?.native_id];
    if (durable?.ack_confirmed || durable?.echo_seen) return this.existingSend(line, payload, key, fingerprint);
    const inFlight = line.sendTasks.get(key);
    if (inFlight) {
      if (inFlight.fingerprint !== fingerprint) throw new BridgeError(409, 'CLIENT_ID_CONFLICT', 'This send key belongs to another payload.');
      return inFlight.task;
    }
    // Known durable results bypass unrelated sends queued on this line.
    const known = this.existingSend(line, payload, key, fingerprint);
    if (known) return known;
    const task = this.dispatchSend(id, payload).finally(() => line.sendTasks.delete(key));
    line.sendTasks.set(key, {fingerprint, task});
    return task;
  }
  existingSend(line, payload, key, fingerprint) {
    const previous = line.store.data.send_requests[key];
    if (!previous) return null;
    if (previous.native_id && line.store.data.messages[previous.native_id]?.phone !== phoneDigits(payload.to)) throw new BridgeError(409, 'CLIENT_ID_CONFLICT', 'The client ID belongs to another recipient.');
    if (previous.fingerprint !== fingerprint) throw new BridgeError(409, 'CLIENT_ID_CONFLICT', 'This send key belongs to another payload or cannot be verified. Check the original send.');
    if (previous.retry_safe && previous.native_id === null) return null;
    const record = line.store.data.messages[previous.native_id];
    if (record.ack_confirmed || record.echo_seen) return {messages: [{id: wrapId(line.line_id, previous.native_id)}]};
    const error = new BridgeError(409, 'DELIVERY_UNCERTAIN', 'The original delivery is unconfirmed; it will not be sent again.');
    error.delivery_uncertain = true;
    throw error;
  }
  async dispatchSend(id, payload) {
    const line = this.getLine(id);
    return this.serialSend(line, async () => {
      const phone = phoneDigits(payload?.to);
      const jid = `${phone}@s.whatsapp.net`;
      const clientMessageId = payload?.client_message_id;
      if (clientMessageId !== undefined &&
          (typeof clientMessageId !== 'string' || !clientMessageId.trim() || [...clientMessageId].length > 191 ||
           /[\u0000-\u001f\u007f]/u.test(clientMessageId))) {
        throw new BridgeError(400, 'INVALID_CLIENT_MESSAGE_ID', 'The client message ID must contain 1–191 printable characters.');
      }
      // The line send queue serializes this lookup with durable reservation.
      // A retry never invents a second native ID, even after a process restart.
      if (clientMessageId) {
        const key = crypto.createHash('sha256').update(clientMessageId).digest('hex');
        const known = this.existingSend(line, payload, key, this.sendFingerprint(line, payload));
        if (known) return known;
      }
      const fingerprint = this.sendFingerprint(line, payload);
      const socket = this.connected(line);
      let content;
      if (payload.type === 'text') {
        if (typeof payload.text?.body !== 'string' || !payload.text.body.trim() || payload.text.body.length > 4096) throw new BridgeError(400, 'INVALID_TEXT', 'Text must contain 1–4096 characters.');
        content = { text: payload.text.body, linkPreview: null };
      } else if (payload.type === 'location') {
        const location = payload.location;
        if (!Number.isFinite(location?.latitude) || !Number.isFinite(location?.longitude) || Math.abs(location.latitude) > 90 || Math.abs(location.longitude) > 180) throw new BridgeError(400, 'INVALID_LOCATION', 'Provide valid location coordinates.');
        content = { location: { degreesLatitude: location.latitude, degreesLongitude: location.longitude, name: location.name, address: location.address } };
      } else if (payload.type === 'reaction') {
        const target = this.target(line, payload.reaction?.message_id, phone, false);
        const emoji = payload.reaction?.emoji;
        if (typeof emoji !== 'string' || emoji.length > 32) throw new BridgeError(400, 'INVALID_REACTION', 'Provide a reaction emoji or an empty string.');
        content = { react: { key: target.message.key, text: emoji } };
      } else if (MEDIA_TYPES.includes(payload.type)) {
        const mediaNode = payload[payload.type];
        const media = await this.downloadMedia(id, mediaNode?.id);
        content = { [payload.type]: media.content, mimetype: media.mime_type };
        if (payload.type === 'audio') {
          content.ptt = Boolean(mediaNode.voice);
          if (content.ptt) {
            // Browser recordings are normalized to Ogg/Opus by the API. Never
            // relay an arbitrary codec as PTT, including old uploaded assets.
            const opusHeader = media.content.subarray(0, 128).indexOf(Buffer.from('OpusHead'));
            if (!media.content.subarray(0, 4).equals(Buffer.from('OggS')) ||
                opusHeader < 0 || media.content[opusHeader + 9] !== 1) {
              throw new BridgeError(400, 'INVALID_VOICE_CODEC', 'Record the voice note again; WhatsApp voice notes require mono Ogg/Opus.');
            }
            content.mimetype = 'audio/ogg; codecs=opus';
          }
        }
        if (payload.type === 'document') content.fileName = mediaNode.filename || 'attachment';
        if (['image', 'document', 'video'].includes(payload.type) && mediaNode.caption) content.caption = String(mediaNode.caption).slice(0, 1024);
      } else throw new BridgeError(400, 'UNSUPPORTED_TYPE', 'This message type is not supported by the linked-device bridge.');
      const options = {};
      if (payload.context?.message_id) options.quoted = this.target(line, payload.context.message_id, phone, false).message;
      this.connected(line, socket);
      this.storageMonitor.assertCanSubmit(id);
      const nativeId = generateMessageIDV2(socket.user?.id);
      const waiter = line.ack.expect(nativeId, { deferred: true });
      let submitted = false;
      try {
        // Reserve the native ID durably before the socket can emit a fromMe
        // upsert. A fast echo or crash must not mirror this dashboard send as
        // a second, unattributed WhatsApp-app message in the ticket.
        await this.mediaStorage(line, () => line.store.update(data => {
          const mediaId = MEDIA_TYPES.includes(payload.type) ? payload[payload.type].id : undefined;
          // Publication of this durable reference must serialize with deletion
          // of another message sharing the same deduplicated attachment.
          if (mediaId && (!data.media[mediaId] || data.pending_media_deletions?.includes(mediaId))) {
            throw new BridgeError(404, 'UNKNOWN_MEDIA', 'The attachment was removed before this send was reserved.');
          }
          if (clientMessageId) data.send_requests[crypto.createHash('sha256').update(clientMessageId).digest('hex')] = {native_id: nativeId, fingerprint};
          data.messages[nativeId] = {
          request_fingerprint: fingerprint, message: { key: { id: nativeId, remoteJid: jid, fromMe: true } },
          phone, timestamp: Date.now(), notified: true, pending_send: true,
          ...(mediaId ? { media_id: mediaId } : {}),
          ...(clientMessageId ? { client_message_id: clientMessageId } : {}),
        }; }));
        this.connected(line, socket);
        const sendStarted = Date.now();
        this.trace(line, 'send_attempt', { native_id: nativeId, content_type: payload.type });
        submitted = true;
        const message = await this.awaitSubmission(socket.sendMessage(jid, content, { ...options, messageId: nativeId }), waiter,
          { media: MEDIA_TYPES.includes(payload.type) });
        waiter.startTimer();
        if (!message?.key?.id || message.key.id !== nativeId) throw new BridgeError(502, 'INVALID_PROVIDER_RESPONSE', 'WhatsApp did not return the expected message ID.');
        // Keep retry keys even if ACK is lost; no success is returned until confirmed.
        await line.store.update(data => { data.messages[nativeId] = { request_fingerprint: fingerprint, message, phone, timestamp: millis(message.messageTimestamp), text: textBody(message.message), notified: true, pending_send: true,
          ...(clientMessageId ? { client_message_id: clientMessageId } : {}),
          ack_confirmed: Boolean(data.messages[nativeId]?.ack_confirmed), receipt: data.messages[nativeId]?.receipt, echo_seen: Boolean(data.messages[nativeId]?.echo_seen), confirmation_enqueued: Boolean(data.messages[nativeId]?.confirmation_enqueued),
          ...(MEDIA_TYPES.includes(payload.type) ? { media_id: payload[payload.type].id } : {}) }; });
        await waiter.promise;
        const acknowledgedAt = waiter.confirmedAt ?? Date.now();
        await line.store.update(data => { data.messages[nativeId].pending_send = false; data.messages[nativeId].ack_confirmed = true; });
        this.trace(line, 'send_result', {send_to_ack_ms: acknowledgedAt-sendStarted, ack_to_response_ms: Date.now()-acknowledgedAt});
        this.queueOwnConfirmation(line, nativeId);
        return { messages: [{ id: wrapId(id, nativeId) }] };
      } catch (error) {
        waiter.cancel();
        if (!submitted) {
          try {
            await line.store.update(data => { delete data.messages[nativeId]; if (clientMessageId) data.send_requests[crypto.createHash('sha256').update(clientMessageId).digest('hex')] = {native_id: null, fingerprint, retry_safe: true}; });
          } catch (persistError) { error = persistError; }
          error.delivery_uncertain = false;
          this.trace(line, 'send_not_submitted', { native_id: nativeId });
          throw error;
        }
        if (line.store.data.messages[nativeId]?.ack_confirmed || line.store.data.messages[nativeId]?.echo_seen) {
          await line.store.update(data => { data.messages[nativeId].pending_send = false; });
          this.queueOwnConfirmation(line, nativeId);
          return { messages: [{ id: wrapId(id, nativeId) }] };
        }
        if (error.code === 'WHATSAPP_REJECTED' && !line.store.data.messages[nativeId]?.receipt) {
          await line.store.update(data => { delete data.messages[nativeId]; if (clientMessageId) data.send_requests[crypto.createHash('sha256').update(clientMessageId).digest('hex')] = {native_id: null, fingerprint, retry_safe: true}; });
          error.delivery_uncertain = false;
          throw error;
        }
        if (staleConnectionError(error)) this.restartStaleSocket(line, socket);
        // A send failure may follow provider delivery. Retain the native ID
        // and source correlation, then await a real WhatsApp echo or callback.
        if (line.store.data.messages[nativeId]) {
          try {
            await line.store.update(data => { data.messages[nativeId].pending_send = false; data.messages[nativeId].delivery_uncertain = !data.messages[nativeId].ack_confirmed && !data.messages[nativeId].echo_seen; });
            this.queueOwnConfirmation(line, nativeId);
          } catch (persistError) {
            persistError.delivery_uncertain = true;
            throw persistError;
          }
        }
        error.delivery_uncertain = true;
        this.trace(line, 'send_uncertain', { native_id: nativeId, reason: staleConnectionError(error) ? 'transport_or_ack_timeout' : 'submission_interrupted' });
        throw error;
      }
    });
  }

  restartStaleSocket(line, socket) {
    if (line.socket !== socket || line.removing || this.stopping) return;
    this.blockConnection(line, socket, 'stale_transport');
    socket.end(new Error('WhatsApp connection stopped responding'));
    this.scheduleReconnect(line);
  }
  serialSend(line, action) {
    if (line.sendPending >= 64) throw new BridgeError(503, 'SEND_QUEUE_FULL', 'This connection is busy. The request was not submitted.');
    line.sendPending++;
    const task = line.sendTail.then(action).finally(() => { line.sendPending--; });
    line.sendTail = task.catch(() => {});
    return task;
  }
  target(line, wrapped, phone, requireOwn = true) {
    const nativeId = unwrapId(line.line_id, wrapped);
    const record = line.store.data.messages[nativeId];
    if (!record || record.phone !== phone) throw new BridgeError(409, 'UNKNOWN_MESSAGE', 'The original message key for this chat is unavailable.');
    if (requireOwn && !record.message.key.fromMe) throw new BridgeError(403, 'NOT_MESSAGE_OWNER', 'Only messages sent by this WhatsApp account can be changed.');
    if (record.deleted) throw new BridgeError(409, 'MESSAGE_DELETED', 'This message has already been deleted for everyone.');
    return record;
  }

  async mutate(id, wrapped, kind, payload) {
    const line = this.getLine(id);
    if (!line.store?.data) throw new BridgeError(503, 'LINE_STORAGE_UNAVAILABLE', 'Check this line session storage before changing messages.');
    return this.serialSend(line, async () => {
      const operationId = payload?.operation_id;
      if (typeof operationId !== 'string' || !/^[A-Za-z0-9_.-]{8,128}$/.test(operationId)) throw new BridgeError(400, 'INVALID_OPERATION', 'A unique operation ID of 8–128 characters is required.');
      const phone = phoneDigits(payload.to);
      const nativeId = unwrapId(id, wrapped);
      const body = kind === 'edit' ? payload.body : undefined;
      if (kind === 'edit' && (typeof body !== 'string' || !body.trim() || body.length > 4096)) throw new BridgeError(400, 'INVALID_TEXT', 'Edited text must contain 1–4096 characters.');
      const fingerprint = crypto.createHash('sha256').update(JSON.stringify([id, nativeId, kind, phone, body])).digest('hex');
      let operation = line.store.data.operations[operationId];
      if (operation && operation.fingerprint !== fingerprint) throw new BridgeError(409, 'OPERATION_CONFLICT', 'This operation ID was already used for a different change.');
      if (operation?.status === 'confirmed') return operation.result;
      const target = this.target(line, wrapped, phone);
      const age = Date.now() - target.timestamp;
      const window = kind === 'edit' ? 15 * 60_000 : 48 * 60 * 60_000;
      // Reconcile an already relayed operation with its original wire ID/payload,
      // including when the eligibility window expires while its ACK is uncertain.
      if (!operation && (age < -60_000 || age > window)) throw new BridgeError(409, 'TIME_WINDOW_EXPIRED', kind === 'edit' ? 'WhatsApp messages can be edited within 15 minutes of sending.' : 'The deletion window has expired for this WhatsApp message.');
      if (kind === 'edit' && target.text === null) throw new BridgeError(409, 'UNSUPPORTED_EDIT', 'Only text messages can be edited.');
      const socket = this.connected(line);
      this.storageMonitor.assertCanSubmit(id);
      if (!operation) {
        const timestamp = Math.max(Date.now(), (target.last_mutation_timestamp ?? 0) + 1);
        const protocolMessage = { key: target.message.key,
          type: kind === 'edit' ? proto.Message.ProtocolMessage.Type.MESSAGE_EDIT : proto.Message.ProtocolMessage.Type.REVOKE,
          timestampMs: timestamp, ...(kind === 'edit' ? { editedMessage: { conversation: body } } : {}) };
        operation = { fingerprint, target_id: nativeId, kind, phone, body, provider_id: generateMessageIDV2(socket.user?.id), status: 'pending', timestamp, payload: { protocolMessage } };
        await line.store.update(data => { data.operations[operationId] = operation; });
      } else await line.store.update(data => { data.operations[operationId].status = 'pending'; });
      let waiter;
      let submitted = false;
      try {
        this.connected(line, socket);
        waiter = line.ack.expect(operation.provider_id, { deferred: true });
        this.trace(line, 'send_attempt', { native_id: operation.provider_id, content_type: kind });
        submitted = true;
        const providerId = await this.awaitSubmission(socket.relayMessage(target.message.key.remoteJid, operation.payload,
          { messageId: operation.provider_id, additionalAttributes: { edit: kind === 'edit' ? '1' : '7' } }), waiter);
        waiter.startTimer();
        if (providerId !== operation.provider_id) throw new BridgeError(502, 'INVALID_PROVIDER_RESPONSE', 'WhatsApp did not relay the expected operation.');
        await waiter.promise;
        const timestamp = operation.timestamp;
        const result = { confirmed: true, id: wrapped, message_id: operation.provider_id, operation_id: operationId, timestamp };
        const type = kind === 'edit' ? 'message_updated' : 'message_deleted_for_everyone';
        const event = { event_id: eventId(type, id, nativeId, timestamp, body), type, line_id: id,
          display_phone_number: line.display_phone_number, message_id: wrapped, ...(kind === 'edit' ? { body } : {}),
          timestamp, from_me: true, sender_phone: phone, operation_id: operationId };
        // Store an ACK durably before publishing it. Repeated HTTP requests now return this result.
        await line.store.update(data => { data.operations[operationId].status = 'confirmed'; data.operations[operationId].result = result; data.operations[operationId].event = event; });
        await this.applyEvent(line, event);
        await line.store.update(data => { data.operations[operationId].event_enqueued = true; });
        return result;
      } catch (error) {
        waiter?.cancel();
        if (submitted && staleConnectionError(error)) this.restartStaleSocket(line, socket);
        if (line.store.data.operations[operationId]?.status !== 'confirmed') await line.store.update(data => {
          const current = data.operations[operationId];
          current.status = !submitted && !current.uncertainty_seen ? 'not_submitted'
            : error.code === 'WHATSAPP_REJECTED' && !current.uncertainty_seen ? 'rejected' : 'uncertain';
          if (current.status === 'uncertain') current.uncertainty_seen = true;
        });
        throw error;
      }
    }).catch(error => {
      const operation = line.store.data.operations[payload?.operation_id];
      if (operation && ['pending', 'uncertain', 'confirmed'].includes(operation.status)) error.delivery_uncertain = true;
      throw error;
    });
  }

  startWorkers() {
    if (this.stopping || this.workerTimer || this.pendingTimer) return;
    this.eventLoop.start();
    this.workerTimer = setInterval(() => this.deliverCallbacks().catch(this.onError), 1000);
    this.workerTimer.unref();
    this.pendingTimer = setInterval(() => this.recoverPending().catch(this.onError), 15_000);
    this.pendingTimer.unref();
    this.recoverPending().catch(this.onError);
    this.deliverCallbacks().catch(this.onError);
  }
  async recoverPending() {
    // A history pass may outlast the timer interval on a busy line. Do not
    // multiply disk writes or replay the same work with overlapping passes.
    if (this.recoveryTask) return this.recoveryTask;
    if (this.stopping) return;
    this.recoveryTask = this.recoverPendingOnce().finally(() => { this.recoveryTask = null; });
    return this.recoveryTask;
  }
  async recoverPendingOnce() {
    // Reconcile retained revokes before slower history normalization on any
    // line. Do not rewrite every historical deleted record during an upgrade.
    const retained = [...this.outbox.data.entries, ...(this.outbox.data.dead_letters ?? [])];
    for (const { event } of retained) {
      if (event.type !== 'message_deleted_for_everyone' || event.operation_id) continue;
      const line = this.lines.get(event.line_id);
      if (!line?.store?.data || line.status === 'error' || line.removing) continue;
      try {
        const record = line.store.data.messages[unwrapId(line.line_id, event.message_id)];
        if (record?.deleted && record.message?.key?.fromMe === true && !record.client_message_id && record.tombstone_version !== 2) {
          await this.purgeDeletedContent(line, event.message_id);
        }
      } catch (error) { this.onError(error); }
    }
    for (const line of this.lines.values()) {
      if (!line.store?.data || line.status === 'error' || line.removing) continue;
      if (line.store.data.recover_until && Date.now() > line.store.data.recover_until) {
        try {
          await line.store.update(data => { delete data.recover_since; delete data.recover_until; });
          this.trace(line, 'history_recovery_finished', {});
        } catch (error) { this.onError(error); }
      }
      try {
        // Recover a crash between the durable tombstone and content/media purge.
        for (const [nativeId, record] of Object.entries(line.store.data.messages)) {
          if (record.deleted && (record.callbacks_scrubbed === false || record.message?.message || record.media_id ||
              Object.values(line.store.data.operations).some(operation => operation.target_id === nativeId && operation.kind === 'edit' && (operation.payload || operation.body || operation.event)))) {
            await this.purgeDeletedContent(line, wrapId(line.line_id, nativeId));
          }
        }
        await this.removePendingMedia(line);
        for (const operation of Object.values(line.store.data.operations)) {
          if (operation.status === 'confirmed' && operation.event && !operation.event_enqueued) {
            await this.applyEvent(line, operation.event);
            await line.store.update(data => {
              const current = Object.values(data.operations).find(item => item.provider_id === operation.provider_id);
              current.event_enqueued = true;
            });
          }
        }
        for (const [nativeId, record] of Object.entries(line.store.data.messages)) {
          if (record.client_message_id && !record.confirmation_enqueued && (record.ack_confirmed || record.echo_seen)) {
            await this.enqueueOwnConfirmation(line, nativeId);
          }
        }
        if (line.status === 'connected') for (const pending of Object.values(line.store.data.pending_mutations ?? {})) {
          await this.receive(line, pending.message);
        }
        if (line.status === 'connected') for (const record of Object.values(line.store.data.messages)) {
          // Retry unsupported content retained by older builds as well as LID
          // resolution. Never replay notified messages or incomplete placeholders.
          const content = normalizeMessageContent(record.message?.message);
          if ((!record.notified || (!record.contacts_normalized &&
                (content?.contactMessage || content?.contactsArrayMessage))) &&
              !record.deleted && !record.pending_placeholder &&
              (record.pending_normalization || (record.message?.key?.fromMe ? outgoingEvent : inboundEvent)(line, record.message, record.phone, record.media_id))) {
            await this.receive(line, record.message);
          }
        }
      } catch (error) {
        // A failed line's recovery must not block other independent numbers.
        this.onError(error);
      }
    }
  }
  wakeCallbacks(id) {
    if (this.stopping || !this.workerTimer) return;
    this.callbackWakeups.add(id);
    if (this.callbackWakeHandle) return;
    this.callbackWakeHandle = setImmediate(() => {
      this.callbackWakeHandle = null;
      const ids = [...this.callbackWakeups];
      this.callbackWakeups.clear();
      for (const lineId of ids) {
        if (this.callbackTasks.has(lineId)) this.callbackWakeups.add(lineId);
        else this.startLineCallbacks(lineId).catch(this.onError);
      }
    });
  }
  startLineCallbacks(id) {
    if (this.callbackTasks.has(id)) return this.callbackTasks.get(id);
    const task = Promise.resolve().then(() => this.deliverLineCallbacks(id)).finally(() => {
      this.callbackTasks.delete(id);
      // An enqueue can race the worker's final empty check. Its durable wakeup
      // is consumed only after this owner releases the per-line slot.
      if (this.callbackWakeups.delete(id)) this.wakeCallbacks(id);
    });
    this.callbackTasks.set(id, task);
    return task;
  }
  async deliverCallbacks() {
    if (this.stopping) return;
    const tasks = [];
    for (const id of new Set(this.outbox.data.entries.map(item => item.event.line_id))) {
      if (this.callbackTasks.has(id)) continue;
      tasks.push(this.startLineCallbacks(id));
    }
    const results = await Promise.allSettled(tasks);
    for (const result of results) if (result.status === 'rejected') throw result.reason;
  }
  async deliverLineCallbacks(id) {
    // One in-flight callback per line. A slow media/customer callback on one
    // number must not delay receipt of messages on every other office number.
    for (let count = 0; count < 100 && !this.stopping; count++) {
      const entry = this.outbox.data.entries.find(item => item.event.line_id === id && item.next_try <= Date.now() && !this.lines.get(item.event.line_id)?.removing &&
        !(this.manifest.data.removed_line_ids ?? []).includes(item.event.line_id));
      if (!entry) return;
      let waitingForHistory = false;
      try {
        const callback = albumReactionCallback(entry.event, this.lines.get(id)?.store?.data?.messages);
        const response = await this.fetch(this.config.callbackUrl, { method: 'POST', redirect: 'error',
          headers: { Authorization: `Bearer ${this.config.secret}`, 'Content-Type': 'application/json' },
          body: JSON.stringify(callback), signal: AbortSignal.timeout(5000) });
        await response.body?.cancel();
        if (!response.ok) {
          waitingForHistory = response.status === 503 && isReactionCallback(entry.event) &&
            response.headers?.get('X-WhatsApp-Callback-State') === 'waiting_for_reaction_target';
          if (response.status >= 400 && response.status < 500 && response.status !== 429) {
            await this.deadLetter(entry, `HTTP_${response.status}`);
            continue;
          }
          throw new Error('Callback rejected');
        }
        await this.outbox.update(data => { data.entries = data.entries.filter(item => item.event.event_id !== entry.event.event_id); });
      } catch {
        if (Date.now() - (entry.created_at ?? 0) > 7 * 24 * 60 * 60_000) {
          await this.deadLetter(entry, 'RETRY_PERIOD_EXPIRED');
          continue;
        }
        await this.outbox.update(data => {
          const current = data.entries.find(item => item.event.event_id === entry.event.event_id);
          if (current) {
            // Keep the full event durable and retry it as before. Only a
            // validated missing reaction target is background history work;
            // another failure makes it an ordinary pending update again.
            current.waiting_for_history = waitingForHistory;
            current.attempts++;
            current.next_try = Date.now() + Math.min(60_000, 1000 * 2 ** Math.min(current.attempts, 6));
          }
        });
        continue;
      }
    }
  }
  async deadLetter(entry, reason) {
    await this.outbox.update(data => {
      data.dead_letters ??= [];
      data.dead_letters.push({ ...entry, failed_at: Date.now(), reason });
      data.entries = data.entries.filter(item => item.event.event_id !== entry.event.event_id);
    });
  }
  close() {
    // Set admission fences synchronously, including while initialize() is
    // suspended in a store replay or socket/auth preparation.
    this.stopping = true;
    this.initializationController.abort();
    this.closePromise ??= this.closeOnce().finally(() => { this.closed = true; });
    return this.closePromise;
  }
  async closeOnce() {
    this.stopping = true;
    const failures = [];
    const settled = async tasks => {
      for (const result of await Promise.allSettled(tasks)) if (result.status === 'rejected') failures.push(result.reason);
    };
    clearInterval(this.workerTimer);
    clearInterval(this.pendingTimer);
    clearImmediate(this.callbackWakeHandle);
    this.callbackWakeups.clear();
    this.reconnectSlots.clear();
    this.eventLoop.close();
    const authDrains = new Map(), maintenance = new Map(), stoppedLines = new Set();
    const begin = operation => {
      try {
        const task = Promise.resolve(operation());
        task.catch(() => {});
        return task;
      } catch (error) { failures.push(error); return Promise.resolve(); }
    };
    const stopStore = store => {
      if (store && !maintenance.has(store)) maintenance.set(store, begin(() => store.stopMaintenance()));
    };
    const stopLine = line => {
      clearTimeout(line.reconnectTimer);
      line.reconnectTimer = null;
      this.reconnectSlots.delete(line.line_id);
      if (!stoppedLines.has(line)) { stoppedLines.add(line); line.generation++; }
      if (line.auth && !authDrains.has(line.auth)) {
        begin(() => line.auth.retire());
        begin(() => line.session?.dispose());
        begin(() => line.ack?.close());
        begin(() => line.socket?.end(new Error('Bridge shutdown')));
        authDrains.set(line.auth, begin(() => line.auth.drain()));
      }
      stopStore(line.auth?.store); stopStore(line.store);
    };
    // Start socket retirement, all auth drains, checkpoint cancellation and
    // monitoring shutdown before awaiting any phase. Their deadlines overlap
    // within systemd's 35-second budget; essential journal writes remain open.
    for (const line of this.lines.values()) stopLine(line);
    for (const store of this.ownedStores) stopStore(store);
    stopStore(this.outbox);
    await settled([this.storageMonitor.close(), ...maintenance.values(),
      // initialize() may still be assigning a newly loaded facade to its line.
      // Its owner must settle before the final drain/close and lock release.
      this.initializePromise?.catch(() => {}), this.registrationTail,
      this.recoveryTask, ...this.callbackTasks.values()]);
    // A registration admitted before stopping can have finished during the
    // drain. Its new stores have no socket but still need orderly closure.
    for (const line of this.lines.values()) stopLine(line);
    for (const store of this.ownedStores) stopStore(store);
    await settled([...maintenance.values()]);
    await Promise.all([...this.lines.values()].map(async line => {
      try {
        await line.connectPromise?.catch(() => {});
        // A renewal admitted before shutdown may have produced a new facade.
        stopLine(line);
        await line.sendTail;
        await line.readTail;
        await line.mediaTail;
        // Auth failure must not skip already accepted message/delivery work.
        await settled([...authDrains.values()]);
        await line.eventTail;
        await line.ackTail;
        await Promise.allSettled(line.confirmationTasks.values());
      } catch (error) { failures.push(error); }
    }));
    await settled([...authDrains.values()]);
    // Include failed/partially loaded stores, even when line.store or line.auth
    // could never be assigned. Only settled cleanup permits lock release.
    const stores = new Set([...this.ownedStores, this.outbox,
      ...[...this.lines.values()].flatMap(line => [line.auth?.store, line.store])]);
    await settled([...stores].filter(Boolean).map(store => begin(() => this.closeStore(store))));
    const files = new Set([...this.ownedFiles, this.manifest, this.retiredPhones]);
    await settled([...files].filter(Boolean).map(store => begin(() => store.flush())));
    if (failures.length) throw new AggregateError(failures, 'BRIDGE_SHUTDOWN_FAILED');
  }
}
