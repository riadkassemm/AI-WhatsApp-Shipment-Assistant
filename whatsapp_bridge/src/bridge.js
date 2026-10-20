import { mkdir, readFile, unlink, rm } from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import makeWASocket, { Browsers, DisconnectReason, downloadMediaMessage, normalizeMessageContent, proto, generateMessageIDV2 } from '@whiskeysockets/baileys';
import pino from 'pino';
import QRCode from 'qrcode';
import { BridgeError, phoneDigits, wrapId, unwrapId, validateLineManifest, UUID_PATTERN, MANIFEST_MAX_BYTES } from './config.js';
import { FileStore, createAuth } from './storage.js';
import { AckTracker } from './ack.js';
import { inboundEvent, outgoingEvent, outgoingConfirmationEvent, messageTombstoneEvent, messageStatusEvent, mutationEvent, textBody, millis, eventId, MEDIA_TYPES, RECEIPT_STATUSES, RECEIPT_RANK } from './events.js';
import { preserveNativeMutationMetadata } from './native-events.js';

const silentLogger = pino({ level: 'silent' });
const DEFAULT_MEDIA_HOST = 'mmg.whatsapp.net';

// Host of the media URL stored in a WhatsApp message, if any.
function mediaUrlHost(message) {
  const content = normalizeMessageContent(message?.message);
  const media = content && MEDIA_TYPES.map(type => content[`${type}Message`]).find(Boolean);
  try { return media?.url ? new URL(media.url).host : null; } catch { return null; }
}
const initialLine = () => ({ bound_phone: '', messages: {}, media: {}, operations: {}, enabled: false });
// A missing ACK or a timed-out/closed query usually means the WhatsApp
// connection died without notice. Reconnecting lets the next send work.
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
    this.stopping = false;
    this.callbackRunning = false;
    this.onError = dependencies.onError ?? (() => console.error('Bridge operation failed; check the connection status.'));
  }

  async initialize({ autoConnect = true } = {}) {
    this.outbox = await new FileStore(path.join(this.config.dataDir, 'outbox.json'), () => ({ entries: [], dead_letters: [] })).load();
    this.manifest = await new FileStore(path.join(this.config.dataDir, 'lines.json'), () => ({ version: 1, lines: {}, requests: {} })).load();
    const manifest = validateLineManifest(this.manifest.data);
    // Confirmed removal tombstones also form a durable cleanup work queue.
    // Repeating the precise numeric directory/outbox purge is safe after a crash.
    for (const id of manifest.removed_line_ids) {
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
    for (const definition of definitions.values()) {
      const line = this.lineContext(definition);
      this.lines.set(line.line_id, line);
      try {
        await this.prepareLine(line);
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
        if (autoConnect && line.store.data.enabled) await this.connect(line.line_id);
      } catch (error) {
        this.onError(error);
        line.ack?.close();
        line.socket?.end(new Error('Failed to initialize this linked-device line'));
        line.socket = null;
        if (!error.bridgePersistenceFailure && line.store?.data?.enabled && transientConnectionError(error)) {
          line.status = 'disconnected';
          this.scheduleReconnect(line);
        } else {
          line.status = 'error';
          line.error = 'Could not safely load or connect this WhatsApp line. Check its session storage and configuration.';
        }
      }
    }
    return this;
  }
  lineContext(definition) {
    return { ...definition, directory: path.join(this.config.dataDir, definition.line_id),
      display_phone_number: definition.expected_phone, status: 'disconnected', socket: null, ack: null,
      eventTail: Promise.resolve(), sendTail: Promise.resolve(), reconnectTimer: null };
  }
  async prepareLine(line) {
    try {
      await mkdir(path.join(line.directory, 'media'), { recursive: true, mode: 0o700 });
      line.store = await new FileStore(path.join(line.directory, 'state.json'), initialLine).load();
      // Retained disconnected/error sessions must remain identifiable as paired
      // after restart so the manager can explicitly unlink their credentials.
      line.auth = await createAuth(line.directory);
      line.display_phone_number = line.store.data.bound_phone || line.expected_phone;
    } catch (error) { error.bridgePersistenceFailure = true; throw error; }
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
      await this.prepareLine(line);
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
      try {
        // Finish an already dispatched mutation; prevent subsequent sends and
        // callbacks while this particular number is being unlinked.
        await line.sendTail;
        if (line.connectPromise) await line.connectPromise;
        if (this.lineStatus(line).is_paired && !line.removalRemoteConfirmed) await this.confirmDeviceUnlink(line);
        this.stopRemovedSocket(line);
        await line.eventTail;
        await line.auth?.store.flush();
        await line.store.flush();
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
  stopRemovedSocket(line) {
    const socket = line.socket;
    line.socket = null;
    line.ack?.close();
    socket?.end(new Error('Linked-device number removal'));
    clearTimeout(line.reconnectTimer);
    line.reconnectTimer = null;
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
    return { line_id: line.line_id, display_phone_number: line.display_phone_number, status: line.status,
      is_paired: Boolean(creds?.registered || creds?.me?.id),
      pending_callbacks: this.outbox.data.entries.filter(item => item.event.line_id === line.line_id).length,
      failed_callbacks: (this.outbox.data.dead_letters ?? []).filter(item => item.event.line_id === line.line_id).length,
      uncertain_sends: Object.values(line.store?.data?.messages ?? {}).filter(item => item.delivery_uncertain && !item.confirmation_enqueued).length,
      ...(line.qr ? { qr: line.qr, qr_data_url: line.qr_data_url } : {}), ...(line.error ? { error: line.error } : {}) };
  }
  status() { return { lines: [...this.lines.values()].map(line => this.lineStatus(line)) }; }

  async connect(id) {
    const line = this.getLine(id);
    if (line.removing) throw new BridgeError(503, 'LINE_REMOVING', 'This WhatsApp number is being removed.');
    if (line.socket && ['connected', 'connecting', 'qr'].includes(line.status)) return this.lineStatus(line);
    if (line.connectPromise) return line.connectPromise;
    line.connectPromise = this.openSocket(line).finally(() => { line.connectPromise = null; });
    return line.connectPromise;
  }

  async openSocket(line) {
    if (this.stopping) throw new BridgeError(503, 'STOPPING', 'The bridge is shutting down.');
    line.ack?.close();
    line.socket?.end(new Error('Replacing linked-device session'));
    line.socket = null;
    clearTimeout(line.reconnectTimer);
    line.reconnectTimer = null;
    try {
      if (!line.store?.data) await this.prepareLine(line);
      await line.store.update(data => { data.enabled = true; });
      line.auth = await createAuth(line.directory);
    } catch (error) {
      // Session persistence failures must never enter network retry loops.
      error.bridgePersistenceFailure = true;
      throw error;
    }
    line.status = 'connecting';
    delete line.error;
    delete line.qr;
    delete line.qr_data_url;
    const socket = this.makeSocket({ auth: line.auth.state, logger: silentLogger, browser: Browsers.ubuntu('Support Dashboard'),
      printQRInTerminal: false, emitOwnEvents: false, enableRecentMessageCache: false,
      syncFullHistory: false, shouldSyncHistoryMessage: () => false,
      markOnlineOnConnect: false, connectTimeoutMs: 25_000, defaultQueryTimeoutMs: 20_000,
      shouldIgnoreJid: jid => /@(g\.us|broadcast|newsletter)$/.test(jid),
      getMessage: async key => line.store.data.messages[key.id]?.message?.message,
    });
    line.socket = socket;
    // Real sockets expose the same emitter used by their native receive path.
    // Mock sockets may supply only ev.process; keep that seam for transport tests.
    if (typeof socket.ev.emit === 'function') line.restoreNativeEvents = preserveNativeMutationMetadata(socket);
    line.ack?.close();
    line.ack = new AckTracker(socket, this.config.ackTimeoutMs);
    socket.ev.process(events => {
      line.eventTail = line.eventTail.then(async () => {
        if (line.socket !== socket) return;
        if (events['creds.update']) {
          Object.assign(line.auth.state.creds, events['creds.update']);
          await line.auth.saveCreds();
        }
        if (events['connection.update']) await this.connectionUpdate(line, socket, events['connection.update']);
        if (line.removing) return;
        const upsert = events['messages.upsert'];
        // Drop placeholder resend payloads. They may not represent authenticated live input.
        if (upsert && !upsert.requestId) for (const message of upsert.messages ?? []) await this.receive(line, message);
        for (const update of events['messages.update'] ?? []) await this.receiveUpdate(line, update);
      }).catch(error => {
        line.status = 'error';
        line.error = 'Could not safely save or synchronize the WhatsApp session. Reconnect after checking bridge storage.';
        line.ack?.close();
        socket.end(new Error('Linked-device event persistence failed'));
        if (line.socket === socket) line.socket = null;
        this.onError(error);
      });
      return line.eventTail;
    });
    return this.lineStatus(line);
  }

  async connectionUpdate(line, socket, update) {
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
      line.qr_data_url = await QRCode.toDataURL(update.qr, { errorCorrectionLevel: 'M', margin: 3, width: 320 });
      line.status = 'qr';
    }
    if (update.connection === 'open') {
      const ownPhone = phoneDigits(socket.user?.id?.split('@')[0]?.split(':')[0] ?? line.auth.state.creds.me?.id?.split('@')[0]?.split(':')[0]);
      const expected = line.expected_phone || line.store.data.bound_phone;
      if (expected && ownPhone !== expected) {
        line.status = 'error';
        line.error = 'The linked phone does not match this configured line. Disconnect it and pair the expected phone.';
        await line.store.update(data => { data.enabled = false; });
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
      line.display_phone_number = ownPhone;
      line.status = 'connected';
      line.reconnectAttempts = 0;
      delete line.error;
      delete line.qr;
      delete line.qr_data_url;
    }
    if (update.connection === 'close' && line.socket === socket) {
      line.ack.close();
      line.socket = null;
      delete line.qr;
      delete line.qr_data_url;
      const reason = update.lastDisconnect?.error?.output?.statusCode;
      if (reason === DisconnectReason.loggedOut) {
        line.status = 'logged_out';
        line.error = 'This linked device was logged out. Connect and scan a new QR code.';
        await line.store.update(data => { data.enabled = false; });
        await line.auth.reset();
      } else {
        line.status = 'disconnected';
        this.scheduleReconnect(line, reason === DisconnectReason.restartRequired);
      }
    }
  }
  scheduleReconnect(line, restartRequired = false) {
    if (this.stopping || line.removing || !line.store?.data?.enabled || line.status === 'error' || line.reconnectTimer) return;
    const delay = restartRequired ? 250 : Math.min(60_000, 1000 * 2 ** Math.min(line.reconnectAttempts ?? 0, 6));
    line.reconnectAttempts = (line.reconnectAttempts ?? 0) + 1;
    line.reconnectTimer = setTimeout(() => {
      line.reconnectTimer = null;
      if (this.stopping || line.removing || !line.store.data.enabled || line.status === 'error') return;
      this.connect(line.line_id).catch(error => {
        this.onError(error);
        if (!this.stopping && line.store.data.enabled && line.status !== 'error' &&
            !error.bridgePersistenceFailure && transientConnectionError(error)) {
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
    const line = this.getLine(id);
    if (line.removing) throw new BridgeError(503, 'LINE_REMOVING', 'This WhatsApp number is being removed.');
    if (!line.store?.data) throw new BridgeError(503, 'LINE_STORAGE_UNAVAILABLE', 'Check this line session storage before disconnecting.');
    clearTimeout(line.reconnectTimer);
    await line.store.update(data => { data.enabled = false; });
    const socket = line.socket;
    line.socket = null;
    line.ack?.close();
    if (socket) {
      try { await socket.logout(); }
      catch { socket.end(new Error('Explicit linked-device disconnect')); }
    }
    await line.eventTail;
    line.auth ??= await createAuth(line.directory);
    await line.auth.reset();
    await line.store.update(data => { data.bound_phone = ''; });
    line.display_phone_number = line.expected_phone;
    line.status = 'logged_out';
    delete line.error;
    delete line.qr;
    delete line.qr_data_url;
    return this.lineStatus(line);
  }

  // Asks WhatsApp whether a number has an account, so staff cannot start a chat
  // with a number that is missing its country code or is not on WhatsApp.
  async checkNumber(id, payload) {
    const line = this.getLine(id);
    const socket = this.connected(line);
    const phone = phoneDigits(payload?.phone);
    const lookup = socket.onWhatsApp(`${phone}@s.whatsapp.net`);
    let timer;
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new BridgeError(504, 'LOOKUP_TIMEOUT', 'WhatsApp did not answer the number lookup in time.')), 10_000); });
    let results;
    try { results = await Promise.race([lookup, timeout]); } finally { clearTimeout(timer); }
    const match = (results ?? []).find(item => item?.exists);
    const jidPhone = match ? String(match.jid ?? '').split('@')[0].split(':')[0] : '';
    return { phone, exists: Boolean(match), whatsapp_phone: /^\d{5,15}$/.test(jidPhone) ? jidPhone : phone };
  }
  connected(line) {
    if (line.removing) throw new BridgeError(503, 'LINE_REMOVING', 'This WhatsApp number is being removed.');
    if (line.status !== 'connected' || !line.socket) throw new BridgeError(503, 'NOT_CONNECTED', 'Connect this WhatsApp line before sending or changing messages.');
    return line.socket;
  }
  async counterpart(line, key) {
    for (const jid of [key.remoteJidAlt, key.remoteJid]) {
      if (jid?.endsWith('@s.whatsapp.net')) return phoneDigits(jid.split('@')[0].split(':')[0]);
    }
    if (key.remoteJid?.endsWith('@lid')) {
      const phoneJid = await line.socket?.signalRepository?.lidMapping?.getPNForLID(key.remoteJid);
      if (phoneJid?.endsWith('@s.whatsapp.net')) return phoneDigits(phoneJid.split('@')[0].split(':')[0]);
    }
    return null;
  }
  async enqueue(event) {
    if (this.lines.get(event.line_id)?.removing || (this.manifest.data.removed_line_ids ?? []).includes(event.line_id)) return;
    await this.outbox.update(data => {
      if (!data.entries.some(entry => entry.event.event_id === event.event_id)) data.entries.push({ event, attempts: 0, next_try: 0, created_at: Date.now() });
    });
  }
  // Delivered/read receipts become dashboard ticks. Only messages the
  // dashboard knows about are forwarded (its own sends and phone-app messages),
  // never the bot's automatic replies.
  async receiveReceipt(line, key, status) {
    const record = line.store.data.messages[key.id];
    if (!record?.message?.key?.fromMe || !record.phone || record.deleted) return;
    if (!record.client_message_id && !record.mirrored_outgoing) return;
    if ((RECEIPT_RANK[record.receipt] ?? 0) >= RECEIPT_RANK[status]) return;
    if (record.client_message_id && !record.confirmation_enqueued && !record.pending_send) {
      // A receipt proves WhatsApp accepted a send whose ACK was lost.
      await line.store.update(data => { data.messages[key.id].ack_confirmed = true; });
      await this.enqueueOwnConfirmation(line, key.id);
    }
    await this.enqueue(messageStatusEvent(line, key.id, record.phone, status, Date.now()));
    await line.store.update(data => { if (data.messages[key.id]) data.messages[key.id].receipt = status; });
  }
  async enqueueOwnConfirmation(line, nativeId) {
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
    await line.store.update(data => {
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
    });
    const targetsMessage = event => event.message_id === wrapped ||
      event.payload?.entry?.some(entry => entry.changes?.some(change => change.value?.messages?.some(message => message.id === wrapped)));
    const deletedRecord = line.store.data.messages[nativeId];
    const deletedTimestamp = deletedRecord?.last_mutation_timestamp ?? Date.now();
    await this.outbox.update(data => {
      const recoveredTombstones = [];
      const keep = (item, wasDeadLetter = false) => {
        if (!targetsMessage(item.event)) return true;
        if (['message_deleted_for_everyone', 'outgoing_confirmation', 'message_tombstone'].includes(item.event.type)) return true;
        if (item.event.type === 'message' && item.event.from_me === false) {
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
          // The backend needs the original provider ID before it can apply the
          // revoke. Keep only a content-free marker, ahead of the delete event.
          for (const entry of item.event.payload?.entry ?? []) for (const change of entry.changes ?? []) {
            const value = change.value;
            if (!value) continue;
            value.contacts = [];
            value.messages = (value.messages ?? []).map(message => message.id === wrapped ?
              { id: message.id, from: message.from, timestamp: message.timestamp, type: 'unsupported' } : message);
          }
          item.next_try = 0;
          return true;
        }
        return false;
      };
      data.entries = data.entries.filter(item => keep(item, false));
      data.dead_letters = (data.dead_letters ?? []).filter(item => keep(item, true));
      for (const item of recoveredTombstones.reverse()) {
        if (!data.entries.some(entry => entry.event.event_id === item.event.event_id)) data.entries.unshift(item);
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
    line.restoreNativeEvents?.purgeTarget?.(nativeId);
    await this.removePendingMedia(line);
    await line.store.update(data => {
      if (data.messages[nativeId]?.deleted) data.messages[nativeId].callbacks_scrubbed = true;
    });
  }
  async removePendingMedia(line) {
    for (const mediaId of line.store.data.pending_media_deletions ?? []) {
      try { await unlink(this.mediaPath(line, mediaId)); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
      await line.store.update(data => { data.pending_media_deletions = data.pending_media_deletions.filter(id => id !== mediaId); });
    }
  }
  async receive(line, message) {
    const key = message.key;
    if (!key?.id || !key.remoteJid || /@(g\.us|broadcast|newsletter)$/.test(key.remoteJid)) return;
    wrapId(line.line_id, key.id);
    const existing = line.store.data.messages[key.id];
    const phone = existing?.phone ?? await this.counterpart(line, key);
    const mutation = mutationEvent(line, message, phone);
    if (mutation) {
      const ownOperation = Object.values(line.store.data.operations).find(operation => operation.provider_id === key.id);
      // A relayed operation is confirmed only by its ACK, never by a local echo.
      if (ownOperation) return;
      if (phone) await this.applyEvent(line, mutation);
      return;
    }
    const content = normalizeMessageContent(message.message);
    if (!content || content.protocolMessage) return;
    if (existing?.notified) {
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
    const event = phone ? (key.fromMe ? outgoingEvent(line, message, phone, mediaId) : inboundEvent(line, message, phone, mediaId)) : null;
    if (event) await this.enqueue(event);
    await line.store.update(data => {
      data.messages[key.id] = { ...(data.messages[key.id] ?? {}), message, phone, media_id: mediaId,
        timestamp: existing?.timestamp ?? millis(message.messageTimestamp), text: textBody(content), notified: Boolean(event),
        pending_normalization: !phone, ...(key.fromMe && event ? { mirrored_outgoing: true } : {}) };
    });
  }
  async receiveUpdate(line, { key, update }) {
    if (!key?.id || !update) return;
    if (RECEIPT_STATUSES[update.status]) await this.receiveReceipt(line, key, RECEIPT_STATUSES[update.status]);
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
    if (!payload || typeof payload.content_base64 !== 'string' || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(payload.content_base64)) throw new BridgeError(400, 'INVALID_MEDIA', 'Provide valid base64 media content.');
    const bytes = Buffer.from(payload.content_base64, 'base64');
    if (!bytes.length || bytes.length > this.config.maxMediaBytes) throw new BridgeError(413, 'MEDIA_TOO_LARGE', 'Media must be nonempty and no larger than 25 MiB.');
    const mime = String(payload.mime_type ?? '').split(';')[0].trim().toLowerCase();
    if (!/^[a-z0-9][a-z0-9.+-]*\/[a-z0-9][a-z0-9.+-]*$/.test(mime)) throw new BridgeError(400, 'INVALID_MEDIA', 'Provide a valid media MIME type.');
    const mediaId = `ldmedia:${id}:${crypto.randomUUID()}`;
    const filename = this.mediaPath(line, mediaId);
    await this.writeBytes(filename, bytes);
    await line.store.update(data => { data.media[mediaId] = { mime_type: mime, filename: path.basename(String(payload.filename ?? 'attachment')).slice(0, 255), downloaded: true }; });
    return { id: mediaId };
  }
  mediaPath(line, id) {
    const match = /^ldmedia:(\d{5,32}):([0-9a-f-]{36})$/.exec(id);
    if (!match || match[1] !== line.line_id) throw new BridgeError(404, 'UNKNOWN_MEDIA', 'Media does not belong to this linked-device line.');
    return path.join(line.directory, 'media', `${match[2]}.bin`);
  }
  async writeBytes(filename, bytes) {
    const { open, rename } = await import('node:fs/promises');
    const temporary = `${filename}.${crypto.randomUUID()}.tmp`;
    const handle = await open(temporary, 'wx', 0o600);
    try { await handle.writeFile(bytes); await handle.sync(); } finally { await handle.close(); }
    await rename(temporary, filename);
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
          throw new BridgeError(413, 'MEDIA_TOO_LARGE', 'Incoming media exceeds the 25 MiB bridge limit.');
        }
        chunks.push(chunk);
      }
      const bytes = Buffer.concat(chunks);
      await this.writeBytes(filename, bytes);
      await line.store.update(data => { data.media[mediaId].downloaded = true; });
    }
    return { content: await readFile(filename), mime_type: media.mime_type };
  }

  async send(id, payload) {
    const line = this.getLine(id);
    return this.serialSend(line, async () => {
      const socket = this.connected(line);
      const phone = phoneDigits(payload?.to);
      const jid = `${phone}@s.whatsapp.net`;
      const clientMessageId = payload?.client_message_id;
      if (clientMessageId !== undefined &&
          (typeof clientMessageId !== 'string' || !clientMessageId.trim() || [...clientMessageId].length > 191 ||
           /[\u0000-\u001f\u007f]/u.test(clientMessageId))) {
        throw new BridgeError(400, 'INVALID_CLIENT_MESSAGE_ID', 'The client message ID must contain 1–191 printable characters.');
      }
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
        if (payload.type === 'audio') content.ptt = Boolean(mediaNode.voice);
        if (payload.type === 'document') content.fileName = mediaNode.filename || 'attachment';
        if (['image', 'document', 'video'].includes(payload.type) && mediaNode.caption) content.caption = String(mediaNode.caption).slice(0, 1024);
      } else throw new BridgeError(400, 'UNSUPPORTED_TYPE', 'This message type is not supported by the linked-device bridge.');
      const options = {};
      if (payload.context?.message_id) options.quoted = this.target(line, payload.context.message_id, phone, false).message;
      const nativeId = generateMessageIDV2(socket.user?.id);
      const waiter = line.ack.expect(nativeId, { deferred: true });
      try {
        // Reserve the native ID durably before the socket can emit a fromMe
        // upsert. A fast echo or crash must not mirror this dashboard send as
        // a second, unattributed WhatsApp-app message in the ticket.
        await line.store.update(data => { data.messages[nativeId] = {
          message: { key: { id: nativeId, remoteJid: jid, fromMe: true } },
          phone, timestamp: Date.now(), notified: true, pending_send: true,
          ...(clientMessageId ? { client_message_id: clientMessageId } : {}),
        }; });
        const message = await socket.sendMessage(jid, content, { ...options, messageId: nativeId });
        waiter.startTimer();
        if (!message?.key?.id || message.key.id !== nativeId) throw new BridgeError(502, 'INVALID_PROVIDER_RESPONSE', 'WhatsApp did not return the expected message ID.');
        // Keep retry keys even if ACK is lost; no success is returned until confirmed.
        await line.store.update(data => { data.messages[nativeId] = { message, phone, timestamp: millis(message.messageTimestamp), text: textBody(message.message), notified: true, pending_send: true,
          ...(clientMessageId ? { client_message_id: clientMessageId } : {}),
          echo_seen: Boolean(data.messages[nativeId]?.echo_seen), confirmation_enqueued: Boolean(data.messages[nativeId]?.confirmation_enqueued),
          ...(MEDIA_TYPES.includes(payload.type) ? { media_id: payload[payload.type].id } : {}) }; });
        await waiter.promise;
        await line.store.update(data => { data.messages[nativeId].pending_send = false; data.messages[nativeId].ack_confirmed = true; });
        await this.enqueueOwnConfirmation(line, nativeId);
        return { messages: [{ id: wrapId(id, nativeId) }] };
      } catch (error) {
        waiter.cancel();
        if (staleConnectionError(error)) this.restartStaleSocket(line, socket);
        // A send failure may follow provider delivery. Retain the native ID
        // and source correlation, then await a real WhatsApp echo or callback.
        if (line.store.data.messages[nativeId]) {
          try {
            await line.store.update(data => { data.messages[nativeId].pending_send = false; data.messages[nativeId].delivery_uncertain = true; });
            await this.enqueueOwnConfirmation(line, nativeId);
          } catch (persistError) {
            persistError.delivery_uncertain = true;
            throw persistError;
          }
        }
        throw error;
      }
    });
  }

  restartStaleSocket(line, socket) {
    if (line.socket !== socket || line.removing || this.stopping) return;
    console.warn(`Linked-device connection looks stale; reconnecting: line=${line.line_id}`);
    socket.end(new Error('WhatsApp connection stopped responding'));
  }
  serialSend(line, action) {
    const task = line.sendTail.then(action);
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
      if (!operation) {
        const timestamp = Math.max(Date.now(), (target.last_mutation_timestamp ?? 0) + 1);
        const protocolMessage = { key: target.message.key,
          type: kind === 'edit' ? proto.Message.ProtocolMessage.Type.MESSAGE_EDIT : proto.Message.ProtocolMessage.Type.REVOKE,
          timestampMs: timestamp, ...(kind === 'edit' ? { editedMessage: { conversation: body } } : {}) };
        operation = { fingerprint, target_id: nativeId, kind, phone, body, provider_id: generateMessageIDV2(socket.user?.id), status: 'pending', timestamp, payload: { protocolMessage } };
        await line.store.update(data => { data.operations[operationId] = operation; });
      } else await line.store.update(data => { data.operations[operationId].status = 'pending'; });
      const waiter = line.ack.expect(operation.provider_id);
      try {
        const providerId = await socket.relayMessage(target.message.key.remoteJid, operation.payload,
          { messageId: operation.provider_id, additionalAttributes: { edit: kind === 'edit' ? '1' : '7' } });
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
        waiter.cancel();
        if (line.store.data.operations[operationId]?.status !== 'confirmed') await line.store.update(data => {
          const current = data.operations[operationId];
          current.status = error.code === 'WHATSAPP_REJECTED' && !current.uncertainty_seen ? 'rejected' : 'uncertain';
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
    this.workerTimer = setInterval(() => this.deliverCallbacks().catch(this.onError), 1000);
    this.workerTimer.unref();
    this.pendingTimer = setInterval(() => this.recoverPending().catch(this.onError), 15_000);
    this.pendingTimer.unref();
    this.recoverPending().catch(this.onError);
    this.deliverCallbacks().catch(this.onError);
  }
  async recoverPending() {
    for (const line of this.lines.values()) {
      if (!line.store?.data || line.status === 'error' || line.removing) continue;
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
        if (line.status === 'connected') for (const record of Object.values(line.store.data.messages)) if (record.pending_normalization) await this.receive(line, record.message);
      } catch (error) {
        // A failed line's recovery must not block other independent numbers.
        this.onError(error);
      }
    }
  }
  async deliverCallbacks() {
    if (this.callbackRunning || this.stopping) return;
    this.callbackRunning = true;
    try {
      for (let count = 0; count < 100 && !this.stopping; count++) {
        const entry = this.outbox.data.entries.find(item => item.next_try <= Date.now() && !this.lines.get(item.event.line_id)?.removing &&
          !(this.manifest.data.removed_line_ids ?? []).includes(item.event.line_id));
        if (!entry) return;
        try {
          const response = await this.fetch(this.config.callbackUrl, { method: 'POST', redirect: 'error',
            headers: { Authorization: `Bearer ${this.config.secret}`, 'Content-Type': 'application/json' },
            body: JSON.stringify(entry.event), signal: AbortSignal.timeout(5000) });
          await response.body?.cancel();
          if (!response.ok) {
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
            if (current) { current.attempts++; current.next_try = Date.now() + Math.min(60_000, 1000 * 2 ** Math.min(current.attempts, 6)); }
          });
          continue;
        }
      }
    } finally { this.callbackRunning = false; }
  }
  async deadLetter(entry, reason) {
    await this.outbox.update(data => {
      data.dead_letters ??= [];
      data.dead_letters.push({ ...entry, failed_at: Date.now(), reason });
      data.entries = data.entries.filter(item => item.event.event_id !== entry.event.event_id);
    });
  }
  async close() {
    this.stopping = true;
    clearInterval(this.workerTimer);
    clearInterval(this.pendingTimer);
    await this.registrationTail;
    for (const line of this.lines.values()) {
      clearTimeout(line.reconnectTimer);
      line.ack?.close();
      line.socket?.end(new Error('Bridge shutdown'));
      await line.sendTail;
      await line.eventTail;
      await line.auth?.store.flush();
      await line.store?.flush();
    }
    await this.outbox.flush();
    await this.manifest?.flush();
  }
}
