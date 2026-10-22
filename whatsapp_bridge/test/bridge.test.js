import test from 'node:test';
import { Readable } from 'node:stream';
import assert from 'node:assert/strict';
import { mkdtemp, rm, stat, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';
import { proto, makeEventBuffer, DisconnectReason } from '@whiskeysockets/baileys';
import processMessage from '@whiskeysockets/baileys/lib/Utils/process-message.js';
import pino from 'pino';
import { Bridge } from '../src/bridge.js';
import { readConfig, wrapId, unwrapId, validateLineManifest } from '../src/config.js';
import { inboundEvent, outgoingEvent, mutationEvent } from '../src/events.js';
import { acquireDataDirectory, FileStore } from '../src/storage.js';
import { createServer } from '../src/server.js';
import { preserveNativeMutationMetadata } from '../src/native-events.js';

const LINE = '90001';
const PHONE = '96170123456';
const SECRET = 'a-test-secret-that-is-long-enough-123456';
const original = (id = 'ORIGINAL', fromMe = true) => ({
  key: { id, remoteJid: `${PHONE}@s.whatsapp.net`, fromMe },
  message: { conversation: 'Original body' }, messageTimestamp: Math.floor(Date.now() / 1000),
});
function mockSocket(mode = 'ack') {
  const ws = new EventEmitter();
  const socket = {
    ws, user: { id: '96170111111:1@s.whatsapp.net' }, calls: [],
    queries: [],
    ev: { process: handler => { socket.eventHandler = handler; } },
    end: () => {}, logout: async () => {},
    query: async (node, timeoutMs) => { socket.queries.push({ node, timeoutMs }); return { tag: 'iq', attrs: { type: 'result' } }; },
    relayMessage: async (jid, payload, options) => {
      socket.calls.push({ jid, payload, options });
      if (mode === 'throw') throw new Error('Relay unavailable');
      if (mode !== 'timeout') setImmediate(() => ws.emit('CB:ack,class:message', { attrs: { id: options.messageId, ...(mode === 'reject' ? { error: '403' } : {}) } }));
      return options.messageId;
    },
    sendMessage: async (jid, content, options) => {
      socket.calls.push({ jid, content, options });
      if (mode !== 'timeout') setImmediate(() => ws.emit('CB:ack,class:message', { attrs: { id: options.messageId } }));
      return { key: { remoteJid: jid, id: options.messageId, fromMe: true }, message: { conversation: content.text }, messageTimestamp: Math.floor(Date.now() / 1000) };
    },
  };
  return socket;
}
async function fixture(t, mode = 'ack', dependencies = {}) {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), 'whatsapp-bridge-test-'));
  const config = { secret: SECRET, lines: [{ line_id: LINE, expected_phone: '' }], dataDir,
    callbackUrl: 'http://127.0.0.1/callback', maxMediaBytes: 25 * 1024 * 1024, ackTimeoutMs: 30 };
  const socket = mockSocket(mode);
  const bridge = await new Bridge(config, { makeSocket: () => socket, onError: () => {}, ...dependencies }).initialize({ autoConnect: false });
  await bridge.connect(LINE);
  const line = bridge.getLine(LINE);
  line.status = 'connected';
  line.display_phone_number = '96170111111';
  await line.store.update(data => { data.messages.ORIGINAL = { message: original(), phone: PHONE, timestamp: Date.now() - 1000, text: 'Original body', notified: true }; });
  t.after(async () => { await bridge.close(); await rm(dataDir, { recursive: true, force: true }); });
  return { bridge, config, socket, line, dataDir };
}

test('configuration rejects weak secrets, nonnumeric lines, and public binds', () => {
  const valid = { WHATSAPP_LINKED_DEVICE_SECRET: SECRET, WHATSAPP_LINKED_DEVICE_LINES: JSON.stringify({ [LINE]: '' }) };
  const config = readConfig(valid);
  assert.equal(config.host, '127.0.0.1');
  assert.equal(config.port, 3001);
  assert.equal(config.lines[0].expected_phone, '');
  assert.throws(() => readConfig({ ...valid, WHATSAPP_LINKED_DEVICE_SECRET: 'short' }), /at least 32/);
  assert.throws(() => readConfig({ ...valid, WHATSAPP_LINKED_DEVICE_BIND_HOST: '0.0.0.0' }), /loopback/);
  assert.throws(() => readConfig({ ...valid, WHATSAPP_LINKED_DEVICE_LINES: '{"path../":""}' }), /numeric line IDs/);
  assert.throws(() => unwrapId(LINE, 'wamid.cloud-message'), /Only messages/);
  assert.throws(() => wrapId(LINE, 'x'.repeat(200)), /Invalid/);
  assert.deepEqual(readConfig({ WHATSAPP_LINKED_DEVICE_SECRET: SECRET }).lines, []);
  assert.throws(() => validateLineManifest({ version: 2, lines: {} }), /Invalid/);
  assert.throws(() => validateLineManifest({ version: 1, lines: { '../unsafe': '' } }), /Invalid/);
});

test('dynamic line registrations allocate IDs atomically, avoid reserved IDs, and replay across restarts without touching connected sockets', async t => {
  const { bridge, line, socket, config, dataDir } = await fixture(t);
  const currentAuth = line.auth;
  let currentSocketEnded = 0;
  socket.end = () => { currentSocketEnded++; };
  const first = { request_id: randomUUID(), expected_phone: '+961 70 123 456', reserved_line_ids: ['90002', '1234567890123456789012345678901234567890123456789012345678901234'] };
  const [registered, duplicate] = await Promise.all([bridge.addLine(first), bridge.addLine(first)]);
  assert.equal(registered.line_id, '90003');
  assert.equal(registered.created, true);
  assert.equal(duplicate.created, false);
  assert.equal(registered.line_id, duplicate.line_id);
  assert.equal(registered.status, 'disconnected');
  assert.equal(line.socket, socket);
  assert.equal(line.auth, currentAuth);
  assert.equal(line.status, 'connected');
  assert.equal(currentSocketEnded, 0);
  const [next, following] = await Promise.all([
    bridge.addLine({ request_id: randomUUID(), reserved_line_ids: ['90002'] }),
    bridge.addLine({ request_id: randomUUID(), reserved_line_ids: ['90002'] }),
  ]);
  assert.deepEqual([next.line_id, following.line_id], ['90004', '90005']);
  const manifestPath = path.join(dataDir, 'lines.json');
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  assert.equal(manifest.version, 1);
  assert.equal(manifest.lines['90003'], PHONE);
  assert.equal(manifest.requests[first.request_id].line_id, '90003');
  assert.equal(Object.keys(manifest.lines).length, 3);
  assert.equal((await stat(manifestPath)).mode & 0o777, 0o600);
  assert.equal((await stat(path.join(dataDir, '90003'))).mode & 0o777, 0o700);
  await assert.rejects(bridge.addLine({ ...first, expected_phone: '96170999999' }), error => error.code === 'REGISTRATION_CONFLICT');
  await bridge.close();
  const resumed = await new Bridge(config, { makeSocket: () => mockSocket(), onError: () => {} }).initialize({ autoConnect: false });
  t.after(() => resumed.close());
  const resumedRegistration = await resumed.addLine(first);
  assert.equal(resumedRegistration.line_id, '90003');
  assert.equal(resumedRegistration.created, false);
  assert.equal(resumed.lines.size, 4);
  assert.notEqual(resumed.getLine('90003').store, resumed.getLine(LINE).store);
});

test('failed registry persistence never advertises a new line and invalid registrations fail before allocation', async t => {
  const { bridge } = await fixture(t);
  const request = { request_id: randomUUID(), expected_phone: '' };
  const update = bridge.manifest.update.bind(bridge.manifest);
  bridge.manifest.update = async () => { throw Object.assign(new Error('Cannot persist registry'), { code: 'EIO' }); };
  await assert.rejects(bridge.addLine(request), error => error.code === 'EIO');
  assert.equal(bridge.lines.size, 1);
  assert.deepEqual(bridge.manifest.data.lines, {});
  bridge.manifest.update = update;
  assert.equal((await bridge.addLine(request)).line_id, '90002');
  await assert.rejects(bridge.addLine({ request_id: 'invalid' }), error => error.code === 'INVALID_REQUEST_ID');
  await assert.rejects(bridge.addLine({ request_id: randomUUID(), reserved_line_ids: ['../90003'] }), error => error.code === 'INVALID_RESERVED_LINES');
  await assert.rejects(bridge.addLine({ request_id: randomUUID(), expected_phone: 96170123456 }), error => error.code === 'INVALID_PHONE');
});

test('a damaged saved line cannot prevent another line from reconnecting or returning status after restart', async t => {
  const { bridge, config, dataDir } = await fixture(t);
  const damaged = await bridge.addLine({ request_id: randomUUID() });
  const damagedState = await bridge.addLine({ request_id: randomUUID() });
  await bridge.getLine(damaged.line_id).store.update(data => { data.enabled = true; });
  await bridge.getLine(LINE).store.update(data => { data.messages['PENDING-RECOVERY'] = {
    message: original('PENDING-RECOVERY', false), phone: null, pending_normalization: true,
    timestamp: Date.now() - 1000, text: 'Original body', notified: false,
  }; });
  await bridge.close();
  const { writeFile } = await import('node:fs/promises');
  await writeFile(path.join(dataDir, damaged.line_id, 'auth.json'), '{corrupt', { mode: 0o600 });
  await writeFile(path.join(dataDir, damagedState.line_id, 'state.json'), '{corrupt', { mode: 0o600 });
  let socketCount = 0;
  const resumed = await new Bridge({ ...config, lines: [{ line_id: damagedState.line_id, expected_phone: '' }, ...config.lines] },
    { makeSocket: () => { socketCount++; return mockSocket(); }, onError: () => {} }).initialize();
  t.after(() => resumed.close());
  assert.equal(socketCount, 1);
  assert.equal(resumed.getLine(LINE).status, 'connecting');
  assert.equal(resumed.getLine(damaged.line_id).status, 'error');
  assert.equal(resumed.getLine(damagedState.line_id).status, 'error');
  assert.equal(resumed.status().lines.length, 3);
  assert.equal(resumed.lines.keys().next().value, damagedState.line_id);
  resumed.getLine(LINE).status = 'connected';
  await resumed.recoverPending();
  assert.equal(resumed.getLine(LINE).store.data.messages['PENDING-RECOVERY'].pending_normalization, false);
  assert.equal(resumed.outbox.data.entries.at(-1).event.payload.entry[0].changes[0].value.messages[0].id, `ld:${LINE}:PENDING-RECOVERY`);
  assert.equal(resumed.getLine(damagedState.line_id).status, 'error');
  assert.equal(await readFile(path.join(dataDir, damagedState.line_id, 'state.json'), 'utf8'), '{corrupt');
});

test('adding the first dynamic line preserves a paired legacy fallback definition in the authoritative manifest', async t => {
  const { bridge, config, dataDir, line, socket } = await fixture(t);
  bridge.config.fallbackLineId = LINE;
  const registered = await bridge.addLine({ request_id: randomUUID() });
  assert.equal(bridge.manifest.data.lines[LINE], '');
  assert.equal(line.socket, socket);
  await bridge.close();
  const resumed = await new Bridge({ ...config, fallbackLineId: LINE }, { makeSocket: () => mockSocket(), onError: () => {} }).initialize({ autoConnect: false });
  t.after(() => resumed.close());
  assert.ok(resumed.lines.has(LINE));
  assert.ok(resumed.lines.has(registered.line_id));
});

test('simultaneous and later pairings cannot bind one WhatsApp account twice while distinct accounts stay independent', async t => {
  const { bridge, line, socket } = await fixture(t);
  const added = await bridge.addLine({ request_id: randomUUID() });
  const duplicateSocket = mockSocket();
  let duplicateEnded = 0;
  let duplicateLoggedOut = 0;
  duplicateSocket.end = () => { duplicateEnded++; };
  duplicateSocket.logout = async () => { duplicateLoggedOut++; };
  let healthyEnded = 0;
  socket.end = () => { healthyEnded++; };
  bridge.makeSocket = () => duplicateSocket;
  await bridge.connect(added.line_id);
  const second = bridge.getLine(added.line_id);
  const credentials = second.auth;
  for (const paired of [line, second]) {
    paired.auth.state.creds.registered = true;
    paired.auth.state.creds.me = { id: socket.user.id };
    await paired.auth.saveCreds();
  }
  await Promise.all([
    bridge.connectionUpdate(line, socket, { connection: 'open' }),
    bridge.connectionUpdate(second, duplicateSocket, { connection: 'open' }),
  ]);
  assert.equal(line.status, 'connected');
  assert.equal(line.store.data.bound_phone, '96170111111');
  assert.equal(second.status, 'error');
  assert.match(second.error, /already linked.*different number/);
  assert.equal(second.store.data.bound_phone, '');
  assert.equal(second.store.data.enabled, false);
  assert.equal(second.auth, credentials);
  assert.equal(bridge.lineStatus(second).is_paired, true);
  assert.equal(duplicateEnded, 1);
  assert.equal(duplicateLoggedOut, 0);
  assert.equal(healthyEnded, 0);
  // A later serial retry with the same physical account is also rejected.
  await bridge.connect(added.line_id);
  await bridge.connectionUpdate(second, duplicateSocket, { connection: 'open' });
  assert.equal(second.status, 'error');
  assert.equal(duplicateLoggedOut, 0);
  const different = await bridge.addLine({ request_id: randomUUID() });
  const differentSocket = mockSocket();
  differentSocket.user.id = '96170222222:1@s.whatsapp.net';
  bridge.makeSocket = () => differentSocket;
  await bridge.connect(different.line_id);
  await bridge.connectionUpdate(bridge.getLine(different.line_id), differentSocket, { connection: 'open' });
  assert.equal(bridge.getLine(different.line_id).status, 'connected');
  assert.equal(bridge.getLine(different.line_id).store.data.bound_phone, '96170222222');
  assert.equal(line.status, 'connected');
  assert.equal(healthyEnded, 0);
  await bridge.disconnect(added.line_id);
  assert.equal(bridge.lineStatus(second).is_paired, false);
  assert.equal(second.auth.store.data.creds.registered, false);
  assert.equal(second.auth.store.data.creds.me, undefined);
  assert.equal(bridge.lineStatus(line).is_paired, true);
  assert.equal(line.status, 'connected');
  assert.equal(healthyEnded, 0);
});

test('complete removal confirms native IQ unlink, purges only that line, and tombstones ENV routes and stale registration requests', async t => {
  const { bridge, line, socket, dataDir, config } = await fixture(t);
  line.auth.state.creds.registered = true;
  line.auth.state.creds.me = { id: socket.user.id };
  await line.auth.saveCreds();
  const request = { request_id: randomUUID() };
  const other = await bridge.addLine(request);
  const otherStore = bridge.getLine(other.line_id).store;
  const media = await bridge.uploadMedia(LINE, { content_base64: Buffer.from('private-media').toString('base64'), mime_type: 'audio/ogg' });
  await bridge.enqueue({ event_id: 'removed-callback', line_id: LINE });
  await bridge.enqueue({ event_id: 'healthy-callback', line_id: other.line_id });
  await bridge.outbox.update(data => { data.dead_letters.push({ event: { event_id: 'removed-dead-letter', line_id: LINE } }); });
  const removed = await bridge.removeLine(LINE);
  assert.deepEqual(removed, { line_id: LINE, removed: true, unlinked: true });
  assert.equal(socket.queries.length, 1);
  assert.equal(socket.queries[0].node.content[0].tag, 'remove-companion-device');
  assert.equal(socket.queries[0].node.content[0].attrs.jid, socket.user.id);
  assert.equal(socket.queries[0].node.attrs.xmlns, 'md');
  assert.equal(bridge.lines.has(LINE), false);
  assert.equal(bridge.getLine(other.line_id).store, otherStore);
  assert.deepEqual(bridge.outbox.data.entries.map(item => item.event.event_id), ['healthy-callback']);
  assert.equal(bridge.outbox.data.dead_letters.length, 0);
  await assert.rejects(stat(path.join(dataDir, LINE)), error => error.code === 'ENOENT');
  assert.ok(bridge.manifest.data.removed_line_ids.includes(LINE));
  assert.deepEqual(await bridge.removeLine(LINE), removed);
  await bridge.removeLine(other.line_id); // unpaired, no native logout needed
  assert.equal(bridge.manifest.data.requests[request.request_id], undefined);
  assert.ok(bridge.manifest.data.removed_request_ids.includes(request.request_id));
  await assert.rejects(bridge.addLine(request), error => error.code === 'REGISTRATION_REMOVED');
  const fresh = await bridge.addLine({ request_id: randomUUID() });
  assert.equal(fresh.line_id, '90003');
  await bridge.close();
  const restarted = await new Bridge(config, { makeSocket: () => mockSocket(), onError: () => {} }).initialize({ autoConnect: false });
  t.after(() => restarted.close());
  assert.equal(restarted.lines.has(LINE), false);
  assert.equal(restarted.lines.has(other.line_id), false);
  assert.ok(restarted.lines.has(fresh.line_id));
});

test('a disconnected saved session reconnects only to confirm unlink while the existing healthy account remains untouched', async t => {
  const { bridge, line, socket } = await fixture(t);
  const added = await bridge.addLine({ request_id: randomUUID() });
  const target = bridge.getLine(added.line_id);
  target.auth.state.creds.registered = true;
  target.auth.state.creds.me = { id: '96170222222:2@s.whatsapp.net' };
  await target.auth.saveCreds();
  target.status = 'error';
  const removalSocket = mockSocket();
  removalSocket.user.id = target.auth.state.creds.me.id;
  let healthyEnded = 0;
  socket.end = () => { healthyEnded++; };
  bridge.makeSocket = () => {
    setImmediate(() => removalSocket.eventHandler({ 'connection.update': { connection: 'open' } }));
    return removalSocket;
  };
  assert.equal((await bridge.removeLine(added.line_id)).unlinked, true);
  assert.equal(removalSocket.queries.length, 1);
  assert.equal(removalSocket.queries[0].node.content[0].attrs.jid, '96170222222:2@s.whatsapp.net');
  assert.equal(line.socket, socket);
  assert.equal(line.status, 'connected');
  assert.equal(healthyEnded, 0);
  assert.equal(bridge.lines.has(added.line_id), false);
});

test('an unlink timeout cannot pretend complete removal or erase saved credentials, and an explicit retry can finish', async t => {
  const { bridge, line, socket, dataDir } = await fixture(t);
  line.auth.state.creds.registered = true;
  line.auth.state.creds.me = { id: socket.user.id };
  await line.auth.saveCreds();
  const before = await readFile(path.join(dataDir, LINE, 'auth.json'), 'utf8');
  socket.query = async () => undefined;
  await assert.rejects(bridge.removeLine(LINE), error => error.code === 'UNLINK_NOT_CONFIRMED' && error.delivery_uncertain === true);
  assert.equal(bridge.lines.has(LINE), true);
  assert.equal(line.socket, socket);
  assert.equal(line.status, 'connected');
  assert.equal(line.removing, false);
  assert.equal(await readFile(path.join(dataDir, LINE, 'auth.json'), 'utf8'), before);
  assert.ok(!(bridge.manifest.data.removed_line_ids ?? []).includes(LINE));
  socket.query = async () => ({ tag: 'iq', attrs: { type: 'result' } });
  assert.equal((await bridge.removeLine(LINE)).removed, true);
});

test('server logged-out confirmation during saved-session removal permits cleanup without fabricating a socket write ACK', async t => {
  const { bridge } = await fixture(t);
  const added = await bridge.addLine({ request_id: randomUUID() });
  const target = bridge.getLine(added.line_id);
  target.auth.state.creds.registered = true;
  target.auth.state.creds.me = { id: '96170222222:2@s.whatsapp.net' };
  await target.auth.saveCreds();
  const invalidated = mockSocket();
  bridge.makeSocket = () => {
    setImmediate(() => invalidated.eventHandler({ 'connection.update': { connection: 'close',
      lastDisconnect: { error: { output: { statusCode: DisconnectReason.loggedOut } } } } }));
    return invalidated;
  };
  assert.equal((await bridge.removeLine(added.line_id)).unlinked, true);
  assert.equal(invalidated.queries.length, 0);
});

test('startup resumes confirmed removal cleanup without resurrecting an ENV or legacy fallback route', async t => {
  const { bridge, config, dataDir } = await fixture(t);
  await bridge.manifest.update(data => { data.removed_line_ids = [LINE]; });
  await bridge.enqueue({ event_id: 'obsolete-event', line_id: LINE });
  // enqueue intentionally suppresses tombstoned events; emulate a crash leaving
  // an older already-durable callback and private directory behind.
  await bridge.outbox.update(data => { data.entries.push({ event: { event_id: 'old-event', line_id: LINE }, next_try: 0 }); });
  await bridge.close();
  const restarted = await new Bridge({ ...config, fallbackLineId: LINE }, { makeSocket: () => mockSocket(), onError: () => {} }).initialize();
  t.after(() => restarted.close());
  assert.equal(restarted.lines.size, 0);
  assert.equal(restarted.outbox.data.entries.length, 0);
  await assert.rejects(stat(path.join(dataDir, LINE)), error => error.code === 'ENOENT');
  assert.equal((await restarted.removeLine(LINE)).removed, true);
});

test('cleanup failure after unlink preserves a removal tombstone for safe retries instead of reconnecting or reusing the line', async t => {
  const { bridge, dataDir } = await fixture(t);
  const request = { request_id: randomUUID() };
  const added = await bridge.addLine(request);
  const purge = bridge.purgeRemovedLine.bind(bridge);
  bridge.purgeRemovedLine = async () => { throw Object.assign(new Error('Cleanup disk failure'), { code: 'EIO' }); };
  await assert.rejects(bridge.removeLine(added.line_id), error => error.code === 'EIO');
  assert.equal(bridge.lines.has(added.line_id), false);
  assert.ok(bridge.manifest.data.removed_line_ids.includes(added.line_id));
  assert.ok((await stat(path.join(dataDir, added.line_id))).isDirectory());
  await assert.rejects(bridge.addLine(request), error => error.code === 'REGISTRATION_REMOVED');
  bridge.purgeRemovedLine = purge;
  assert.equal((await bridge.removeLine(added.line_id)).removed, true);
  await assert.rejects(stat(path.join(dataDir, added.line_id)), error => error.code === 'ENOENT');
});

test('valid line state with corrupt auth cannot be treated as unpaired or purged, while another loaded line remains intact', async t => {
  const { bridge, config, dataDir } = await fixture(t);
  const added = await bridge.addLine({ request_id: randomUUID() });
  await bridge.close();
  const { writeFile } = await import('node:fs/promises');
  const authPath = path.join(dataDir, added.line_id, 'auth.json');
  await writeFile(authPath, '{corrupt-auth', { mode: 0o600 });
  const resumed = await new Bridge(config, { makeSocket: () => mockSocket(), onError: () => {} }).initialize({ autoConnect: false });
  t.after(() => resumed.close());
  const healthy = resumed.getLine(LINE);
  const healthyState = JSON.stringify(healthy.store.data);
  assert.equal(resumed.getLine(added.line_id).status, 'error');
  assert.ok(resumed.getLine(added.line_id).store.data);
  await assert.rejects(resumed.removeLine(added.line_id), error => error.code === 'SESSION_STATE_UNKNOWN');
  assert.equal(await readFile(authPath, 'utf8'), '{corrupt-auth');
  assert.ok((await stat(path.join(dataDir, added.line_id, 'state.json'))).isFile());
  assert.equal(resumed.lines.has(added.line_id), true);
  assert.ok(Object.hasOwn(resumed.manifest.data.lines, added.line_id));
  assert.ok(!(resumed.manifest.data.removed_line_ids ?? []).includes(added.line_id));
  assert.equal(resumed.getLine(LINE), healthy);
  assert.equal(JSON.stringify(healthy.store.data), healthyState);
});

test('event normalization preserves native keys, text, replies, voice media and reactions', () => {
  const line = { line_id: LINE, display_phone_number: '96170111111' };
  const message = { ...original('VOICE', false), message: { ephemeralMessage: { message: { audioMessage: {
    mimetype: 'audio/ogg', ptt: true, fileSha256: Buffer.from([1, 2]), contextInfo: { stanzaId: 'QUOTED' },
  } } } } };
  const event = inboundEvent(line, message, PHONE, `ldmedia:${LINE}:12345678-1234-1234-1234-123456789abc`);
  const node = event.payload.entry[0].changes[0].value.messages[0];
  assert.equal(node.audio.voice, true);
  assert.equal(node.audio.mime_type, 'audio/ogg');
  assert.equal(node.context.id, `ld:${LINE}:QUOTED`);
  assert.equal(node.id, `ld:${LINE}:VOICE`);
  const reaction = inboundEvent(line, { ...original('R', false), message: { reactionMessage: { key: { id: 'ORIGINAL' }, text: '👍' } } }, PHONE);
  assert.equal(reaction.payload.entry[0].changes[0].value.messages[0].reaction.message_id, `ld:${LINE}:ORIGINAL`);
  assert.equal(inboundEvent(line, original(), PHONE), null);
});

test('messages sent from another WhatsApp device carry the peer, source and message content', () => {
  const line = { line_id: LINE, display_phone_number: '96170111111' };
  const textMessage = { ...original('PHONE-TEXT'), message: { extendedTextMessage: {
    text: 'Reply from WhatsApp', contextInfo: { stanzaId: 'CUSTOMER' },
  } } };
  const event = outgoingEvent(line, textMessage, PHONE);
  assert.equal(event.type, 'outgoing_message');
  assert.equal(event.from_me, true);
  assert.equal(event.sender_phone, PHONE);
  assert.equal(event.message_id, `ld:${LINE}:PHONE-TEXT`);
  assert.equal(event.timestamp, textMessage.messageTimestamp * 1000);
  const node = event.payload.entry[0].changes[0].value.messages[0];
  assert.equal(node.from, PHONE);
  assert.equal(node.text.body, 'Reply from WhatsApp');
  assert.equal(node.context.id, `ld:${LINE}:CUSTOMER`);
  const mediaId = `ldmedia:${LINE}:12345678-1234-1234-1234-123456789abc`;
  const voice = outgoingEvent(line, { ...original('PHONE-VOICE'), message: { audioMessage: { mimetype: 'audio/ogg', ptt: true } } }, PHONE, mediaId);
  assert.equal(voice.payload.entry[0].changes[0].value.messages[0].audio.voice, true);
  assert.equal(voice.payload.entry[0].changes[0].value.messages[0].audio.id, mediaId);
  const location = outgoingEvent(line, { ...original('PHONE-LOCATION'), message: { locationMessage: { degreesLatitude: 33.9, degreesLongitude: 35.5, name: 'Office' } } }, PHONE);
  assert.equal(location.payload.entry[0].changes[0].value.messages[0].location.name, 'Office');
  const reaction = outgoingEvent(line, { ...original('PHONE-REACTION'), message: { reactionMessage: { key: { id: 'CUSTOMER' }, text: '👍' } } }, PHONE);
  assert.equal(reaction.payload.entry[0].changes[0].value.messages[0].reaction.message_id, `ld:${LINE}:CUSTOMER`);
  const contact = outgoingEvent(line, { ...original('PHONE-CONTACT'), message: { contactMessage: { displayName: 'Example', vcard: 'private contact data' } } }, PHONE);
  assert.equal(contact.payload.entry[0].changes[0].value.messages[0].type, 'contacts');
  assert.equal(contact.payload.entry[0].changes[0].value.messages[0].contacts[0].name.formatted_name, 'Example');
  assert.equal(JSON.stringify(contact).includes('private contact data'), false);
  assert.equal(outgoingEvent(line, original('CUSTOMER', false), PHONE), null);
});

test('native edit and revoke callbacks carry original target, sender side and millisecond operation time', () => {
  const line = { line_id: LINE, display_phone_number: '96170111111' };
  const timestamp = Date.now();
  const message = { ...original('OPERATION', false), message: { protocolMessage: { type: proto.Message.ProtocolMessage.Type.MESSAGE_EDIT, key: { id: 'TARGET' }, editedMessage: { conversation: 'New text' }, timestampMs: timestamp } } };
  const event = mutationEvent(line, message, PHONE);
  assert.equal(event.message_id, `ld:${LINE}:TARGET`);
  assert.equal(event.body, 'New text');
  assert.equal(event.timestamp, timestamp);
  assert.equal(event.from_me, false);
  message.message.protocolMessage.type = proto.Message.ProtocolMessage.Type.REVOKE;
  assert.equal(mutationEvent(line, message, PHONE).type, 'message_deleted_for_everyone');
});

test('edits dispatch encrypted protocol relay, wait for real ACK and persist idempotent result', async t => {
  const { bridge, line, socket, dataDir } = await fixture(t);
  const payload = { to: PHONE, body: 'Edited body', operation_id: 'operation-edit-001' };
  const result = await bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', payload);
  assert.equal(result.confirmed, true);
  assert.equal(socket.calls.length, 1);
  assert.equal(socket.calls[0].payload.protocolMessage.type, proto.Message.ProtocolMessage.Type.MESSAGE_EDIT);
  assert.equal(socket.calls[0].payload.protocolMessage.editedMessage.conversation, payload.body);
  assert.equal(socket.calls[0].options.additionalAttributes.edit, '1');
  assert.equal(socket.calls[0].payload.protocolMessage.timestampMs, result.timestamp);
  assert.equal(line.store.data.messages.ORIGINAL.text, payload.body);
  assert.equal(bridge.outbox.data.entries[0].event.timestamp, result.timestamp);
  assert.deepEqual(await bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', payload), result);
  assert.equal(socket.calls.length, 1);
  const persisted = JSON.parse(await readFile(path.join(dataDir, LINE, 'state.json'), 'utf8'));
  assert.equal(persisted.operations[payload.operation_id].status, 'confirmed');
  assert.equal((await stat(path.join(dataDir, LINE, 'state.json'))).mode & 0o777, 0o600);
  await assert.rejects(bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', { ...payload, body: 'Different' }), error => error.code === 'OPERATION_CONFLICT');
});

test('delete for everyone relays native revoke and only then tombstones original record', async t => {
  const { bridge, socket, line } = await fixture(t);
  const result = await bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'delete', { to: PHONE, operation_id: 'operation-delete-001' });
  assert.equal(result.confirmed, true);
  assert.equal(socket.calls[0].payload.protocolMessage.key.id, 'ORIGINAL');
  assert.equal(socket.calls[0].payload.protocolMessage.type, proto.Message.ProtocolMessage.Type.REVOKE);
  assert.equal(socket.calls[0].options.additionalAttributes.edit, '7');
  assert.equal(line.store.data.messages.ORIGINAL.deleted, true);
  assert.equal(bridge.outbox.data.entries[0].event.type, 'message_deleted_for_everyone');
});

test('timeout never changes local content; retries reuse exact durable provider ID and payload', async t => {
  const { bridge, socket, line } = await fixture(t, 'timeout');
  const payload = { to: PHONE, body: 'Unknown delivery', operation_id: 'operation-timeout-001' };
  await assert.rejects(bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', payload), error => error.code === 'CONFIRMATION_UNKNOWN');
  assert.equal(line.store.data.messages.ORIGINAL.text, 'Original body');
  assert.equal(bridge.outbox.data.entries.length, 0);
  assert.equal(line.store.data.operations[payload.operation_id].status, 'uncertain');
  await assert.rejects(bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', payload));
  assert.deepEqual(socket.calls[0], socket.calls[1]);
  await line.store.update(data => { data.messages.ORIGINAL.timestamp = Date.now() - 16 * 60_000; });
  await assert.rejects(bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', payload), error => error.code === 'CONFIRMATION_UNKNOWN' && error.delivery_uncertain === true);
  assert.deepEqual(socket.calls[0], socket.calls[2]);
  line.status = 'disconnected';
  await assert.rejects(bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', payload), error => error.code === 'NOT_CONNECTED' && error.delivery_uncertain === true);
});

test('negative ACK and transport exception do not produce successful edits or callbacks', async t => {
  const { bridge, line } = await fixture(t, 'reject');
  await assert.rejects(bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', { to: PHONE, body: 'Rejected', operation_id: 'operation-rejected-001' }), error => error.code === 'WHATSAPP_REJECTED');
  assert.equal(line.store.data.messages.ORIGINAL.text, 'Original body');
  assert.equal(bridge.outbox.data.entries.length, 0);
});

test('ownership, original chat, Cloud IDs, text type, and WhatsApp time windows are enforced before relay', async t => {
  const { bridge, line, socket } = await fixture(t);
  const edit = { to: PHONE, body: 'New', operation_id: 'operation-validation-001' };
  await assert.rejects(bridge.mutate(LINE, 'wamid.cloud', 'edit', edit), error => error.code === 'UNSUPPORTED_MESSAGE');
  await assert.rejects(bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', { ...edit, to: '96170222222' }), error => error.code === 'UNKNOWN_MESSAGE');
  await line.store.update(data => { data.messages.ORIGINAL.message.key.fromMe = false; });
  await assert.rejects(bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', edit), error => error.code === 'NOT_MESSAGE_OWNER');
  await line.store.update(data => { data.messages.ORIGINAL.message.key.fromMe = true; data.messages.ORIGINAL.timestamp = Date.now() - 16 * 60_000; });
  await assert.rejects(bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', edit), error => error.code === 'TIME_WINDOW_EXPIRED');
  await line.store.update(data => { data.messages.ORIGINAL.timestamp = Date.now(); data.messages.ORIGINAL.text = null; });
  await assert.rejects(bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', edit), error => error.code === 'UNSUPPORTED_EDIT');
  assert.equal(socket.calls.length, 0);
});

test('separate edits in the same millisecond receive monotonic timestamps and both callbacks', async t => {
  const { bridge } = await fixture(t);
  const one = await bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', { to: PHONE, body: 'One', operation_id: 'operation-rapid-001' });
  const two = await bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', { to: PHONE, body: 'Two', operation_id: 'operation-rapid-002' });
  assert.ok(two.timestamp > one.timestamp);
  assert.equal(bridge.outbox.data.entries.length, 2);
});

test('a sent chat reply can be edited again after restart and then deleted for everyone', async t => {
  const { bridge, config } = await fixture(t);
  const sent = await bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'Initial reply' } });
  const messageId = sent.messages[0].id;
  const nativeId = unwrapId(LINE, messageId);
  const first = await bridge.mutate(LINE, messageId, 'edit', {
    to: PHONE, body: 'First correction', operation_id: 'operation-reply-edit-001',
  });
  assert.equal(first.confirmed, true);
  await bridge.close();

  const socket = mockSocket();
  const resumed = await new Bridge(config, { makeSocket: () => socket, onError: () => {} }).initialize({ autoConnect: false });
  t.after(() => resumed.close());
  await resumed.connect(LINE);
  resumed.getLine(LINE).status = 'connected';
  const second = await resumed.mutate(LINE, messageId, 'edit', {
    to: PHONE, body: 'Second correction', operation_id: 'operation-reply-edit-002',
  });
  assert.ok(second.timestamp > first.timestamp);
  assert.equal(resumed.getLine(LINE).store.data.messages[nativeId].text, 'Second correction');
  assert.equal(socket.calls[0].payload.protocolMessage.key.id, nativeId);
  const deletion = await resumed.mutate(LINE, messageId, 'delete', {
    to: PHONE, operation_id: 'operation-reply-delete-001',
  });
  assert.equal(deletion.confirmed, true);
  assert.equal(socket.calls[1].payload.protocolMessage.key.id, nativeId);
  assert.equal(resumed.getLine(LINE).store.data.messages[nativeId].deleted, true);
});

test('WhatsApp app replies are queued once while dashboard sends suppress even an early own echo', async t => {
  const { bridge, line, socket } = await fixture(t);
  const fromPhone = original('PHONE-REPLY');
  await bridge.receive(line, fromPhone);
  await bridge.receive(line, fromPhone);
  assert.equal(bridge.outbox.data.entries.length, 1);
  assert.equal(bridge.outbox.data.entries[0].event.type, 'outgoing_message');
  assert.equal(bridge.outbox.data.entries[0].event.message_id, `ld:${LINE}:PHONE-REPLY`);

  const sendMessage = socket.sendMessage;
  socket.sendMessage = async (jid, content, options) => {
    const marker = line.store.data.messages[options.messageId];
    assert.equal(marker.notified, true);
    assert.equal(marker.pending_send, true);
    await bridge.receive(line, { key: { id: options.messageId, remoteJid: jid, fromMe: true },
      message: { conversation: content.text }, messageTimestamp: Math.floor(Date.now() / 1000) });
    return sendMessage(jid, content, options);
  };
  const sent = await bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'Dashboard reply' }, client_message_id: 'dashboard-reply-1' });
  assert.ok(sent.messages[0].id.startsWith(`ld:${LINE}:`));
  assert.deepEqual(bridge.outbox.data.entries.map(item => item.event.type), ['outgoing_message', 'outgoing_confirmation']);
  const confirmation = bridge.outbox.data.entries[1].event;
  assert.equal(confirmation.message_id, sent.messages[0].id);
  assert.equal(confirmation.client_message_id, 'dashboard-reply-1');
  assert.equal(line.store.data.messages[unwrapId(LINE, sent.messages[0].id)].notified, true);
  assert.equal(line.store.data.messages[unwrapId(LINE, sent.messages[0].id)].confirmation_enqueued, true);
});

test('an uncertain dashboard send reconciles from a later real WhatsApp echo without a duplicate message', async t => {
  const { bridge, line } = await fixture(t, 'timeout');
  await assert.rejects(bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'Possibly sent' }, client_message_id: 'dashboard-uncertain-1' }),
    error => error.code === 'CONFIRMATION_UNKNOWN');
  const [nativeId, record] = Object.entries(line.store.data.messages).find(([, item]) => item.client_message_id === 'dashboard-uncertain-1');
  assert.equal(record.delivery_uncertain, true);
  assert.equal(bridge.lineStatus(line).uncertain_sends, 1);
  assert.equal(bridge.outbox.data.entries.length, 0);
  await bridge.receive(line, original(nativeId));
  assert.equal(bridge.outbox.data.entries.length, 1);
  assert.equal(bridge.outbox.data.entries[0].event.type, 'outgoing_confirmation');
  assert.equal(bridge.outbox.data.entries[0].event.client_message_id, 'dashboard-uncertain-1');
  assert.equal(bridge.outbox.data.entries[0].event.message_id, `ld:${LINE}:${nativeId}`);
  assert.equal(bridge.lineStatus(line).uncertain_sends, 0);
  await bridge.receive(line, original(nativeId));
  assert.equal(bridge.outbox.data.entries.length, 1);
});

test('dashboard send confirmation survives restart and retries a temporary callback failure', async t => {
  const { bridge, line, config } = await fixture(t, 'timeout');
  await assert.rejects(bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'Reconcile after restart' }, client_message_id: 'dashboard-restart-1' }));
  const nativeId = Object.keys(line.store.data.messages).find(id => line.store.data.messages[id].client_message_id === 'dashboard-restart-1');
  await bridge.receive(line, original(nativeId));
  const expected = bridge.outbox.data.entries[0].event;
  await bridge.close();

  const seen = [];
  const resumed = await new Bridge(config, { makeSocket: () => mockSocket(), onError: () => {}, fetch: async (_url, options) => {
    seen.push(JSON.parse(options.body));
    return new Response(null, { status: seen.length === 1 ? 503 : 200 });
  } }).initialize({ autoConnect: false });
  t.after(() => resumed.close());
  assert.equal(resumed.outbox.data.entries[0].event.event_id, expected.event_id);
  await resumed.deliverCallbacks();
  assert.equal(resumed.outbox.data.entries.length, 1);
  await resumed.outbox.update(data => { data.entries[0].next_try = 0; });
  await resumed.deliverCallbacks();
  assert.equal(resumed.outbox.data.entries.length, 0);
  assert.deepEqual(seen.map(item => item.event_id), [expected.event_id, expected.event_id]);
});

test('a WhatsApp app reply survives a bridge restart without duplicate callbacks', async t => {
  const { bridge, line, config } = await fixture(t);
  const fromPhone = original('PHONE-RESTART');
  await bridge.receive(line, fromPhone);
  const savedEvent = bridge.outbox.data.entries[0].event;
  await bridge.close();

  const resumed = await new Bridge(config, { makeSocket: () => mockSocket(), onError: () => {} }).initialize({ autoConnect: false });
  t.after(() => resumed.close());
  assert.equal(resumed.outbox.data.entries[0].event.event_id, savedEvent.event_id);
  await resumed.connect(LINE);
  resumed.getLine(LINE).status = 'connected';
  await resumed.receive(resumed.getLine(LINE), fromPhone);
  assert.equal(resumed.outbox.data.entries.length, 1);
});

test('customer edits and revokes synchronize while wrong-side mutations are discarded', async t => {
  const { bridge, line } = await fixture(t);
  await bridge.receive(line, original('CUSTOMER', false));
  const timestamp = Date.now() + 100;
  const mutation = { ...original('CUSTOMER-EDIT', false), message: { protocolMessage: { type: proto.Message.ProtocolMessage.Type.MESSAGE_EDIT, key: { id: 'CUSTOMER' }, editedMessage: { conversation: 'Customer correction' }, timestampMs: timestamp } } };
  await bridge.receive(line, mutation);
  assert.equal(line.store.data.messages.CUSTOMER.text, 'Customer correction');
  assert.equal(bridge.outbox.data.entries.at(-1).event.from_me, false);
  mutation.message.protocolMessage.editedMessage.conversation = 'Forged own-side edit';
  mutation.message.protocolMessage.timestampMs += 100;
  mutation.key.fromMe = true;
  await bridge.receive(line, mutation);
  assert.equal(line.store.data.messages.CUSTOMER.text, 'Customer correction');
  mutation.key.fromMe = false;
  mutation.message.protocolMessage.type = proto.Message.ProtocolMessage.Type.REVOKE;
  await bridge.receive(line, mutation);
  assert.equal(line.store.data.messages.CUSTOMER.deleted, true);
});

test('a quick WhatsApp app revoke keeps a redacted original before the deletion callback', async t => {
  const delivered = [];
  const { bridge, line } = await fixture(t, 'ack', { fetch: async (_url, options) => {
    delivered.push(JSON.parse(options.body));
    return new Response(null, { status: 200 });
  } });
  const sent = { ...original('PHONE-QUICK'), message: { conversation: 'Private text that was revoked' } };
  await bridge.receive(line, sent);
  const revoked = { ...original('PHONE-REVOKE'), message: { protocolMessage: {
    type: proto.Message.ProtocolMessage.Type.REVOKE, key: { id: 'PHONE-QUICK' }, timestampMs: Date.now() + 1,
  } } };
  await bridge.receive(line, revoked);
  assert.deepEqual(bridge.outbox.data.entries.map(item => item.event.type), ['outgoing_message', 'message_deleted_for_everyone']);
  const marker = bridge.outbox.data.entries[0].event.payload.entry[0].changes[0].value;
  assert.deepEqual(marker.messages[0], { id: `ld:${LINE}:PHONE-QUICK`, from: PHONE,
    timestamp: String(sent.messageTimestamp), type: 'unsupported' });
  assert.deepEqual(marker.contacts, []);
  assert.equal(JSON.stringify(bridge.outbox.data).includes('Private text that was revoked'), false);
  assert.equal(line.store.data.messages['PHONE-QUICK'].message.message, undefined);
  await bridge.deliverCallbacks();
  assert.deepEqual(delivered.map(event => event.type), ['outgoing_message', 'message_deleted_for_everyone']);
  assert.equal(JSON.stringify(delivered).includes('Private text that was revoked'), false);
});

test('a queued dashboard send confirmation remains ahead of a quick revoke', async t => {
  const { bridge, line } = await fixture(t);
  const sent = await bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'To be deleted' }, client_message_id: 'dashboard-delete-1' });
  const nativeId = unwrapId(LINE, sent.messages[0].id);
  await bridge.applyEvent(line, { event_id: 'quick-revoke', type: 'message_deleted_for_everyone', line_id: LINE,
    message_id: sent.messages[0].id, timestamp: Date.now() + 1, from_me: true, sender_phone: PHONE });
  assert.deepEqual(bridge.outbox.data.entries.map(item => item.event.type), ['outgoing_confirmation', 'message_deleted_for_everyone']);
  assert.equal(line.store.data.messages[nativeId].deleted, true);
});

test('a quick customer revoke replaces the queued original with a content-free tombstone', async t => {
  const { bridge, line, config } = await fixture(t);
  const originalMessage = { ...original('CUSTOMER-QUICK', false), message: { conversation: 'Private customer text' } };
  await bridge.receive(line, originalMessage);
  const revokeTime = Date.now() + 1;
  await bridge.receive(line, { ...original('CUSTOMER-REVOKE', false), message: { protocolMessage: {
    type: proto.Message.ProtocolMessage.Type.REVOKE, key: { id: 'CUSTOMER-QUICK' }, timestampMs: revokeTime,
  } } });
  assert.deepEqual(bridge.outbox.data.entries.map(item => item.event.type), ['message_tombstone', 'message_deleted_for_everyone']);
  const tombstone = bridge.outbox.data.entries[0].event;
  assert.equal(tombstone.message_id, `ld:${LINE}:CUSTOMER-QUICK`);
  assert.equal(tombstone.sender_phone, PHONE);
  assert.equal(tombstone.from_me, false);
  assert.equal(tombstone.timestamp, originalMessage.messageTimestamp * 1000);
  assert.equal(tombstone.deleted_timestamp, revokeTime);
  assert.equal(tombstone.payload, undefined);
  assert.equal(JSON.stringify(bridge.outbox.data).includes('Private customer text'), false);
  assert.equal(line.store.data.messages['CUSTOMER-QUICK'].message.message, undefined);
  await bridge.close();

  const delivered = [];
  const resumed = await new Bridge(config, { makeSocket: () => mockSocket(), onError: () => {}, fetch: async (_url, options) => {
    delivered.push(JSON.parse(options.body));
    return new Response(null, { status: 200 });
  } }).initialize({ autoConnect: false });
  t.after(() => resumed.close());
  await resumed.recoverPending();
  assert.deepEqual(resumed.outbox.data.entries.map(item => item.event.type), ['message_tombstone', 'message_deleted_for_everyone']);
  await resumed.deliverCallbacks();
  assert.deepEqual(delivered.map(event => event.type), ['message_tombstone', 'message_deleted_for_everyone']);
  assert.equal(delivered.some(event => event.type === 'message'), false);
});

test('a customer revoke arriving before its original still records a tombstone and no private content', async t => {
  const { bridge, line } = await fixture(t);
  const timestamp = Date.now() + 1;
  await bridge.receive(line, { ...original('REVOKE-FIRST', false), message: { protocolMessage: {
    type: proto.Message.ProtocolMessage.Type.REVOKE, key: { id: 'CUSTOMER-LATE' }, timestampMs: timestamp,
  } } });
  assert.deepEqual(bridge.outbox.data.entries.map(item => item.event.type), ['message_tombstone', 'message_deleted_for_everyone']);
  assert.equal(bridge.outbox.data.entries[0].event.timestamp, timestamp);
  assert.equal(bridge.outbox.data.entries[0].event.deleted_timestamp, timestamp);
  await bridge.receive(line, { ...original('CUSTOMER-LATE', false), message: { conversation: 'Too late to retain' } });
  assert.equal(JSON.stringify(line.store.data).includes('Too late to retain'), false);
  assert.equal(bridge.outbox.data.entries.length, 2);
});

test('recovery scrubs a queued original after a crash between state purge and outbox rewrite', async t => {
  const { bridge, line } = await fixture(t);
  await bridge.receive(line, { ...original('CUSTOMER-CRASH', false), message: { conversation: 'Crash boundary secret' } });
  const update = bridge.outbox.update.bind(bridge.outbox);
  let writes = 0;
  bridge.outbox.update = async action => {
    if (++writes === 2) throw new Error('Simulated outbox write failure');
    return update(action);
  };
  await assert.rejects(bridge.receive(line, { ...original('CUSTOMER-CRASH-REVOKE', false), message: { protocolMessage: {
    type: proto.Message.ProtocolMessage.Type.REVOKE, key: { id: 'CUSTOMER-CRASH' }, timestampMs: Date.now() + 1,
  } } }), /Simulated outbox write failure/);
  assert.equal(line.store.data.messages['CUSTOMER-CRASH'].deleted, true);
  assert.equal(line.store.data.messages['CUSTOMER-CRASH'].callbacks_scrubbed, false);
  assert.equal(JSON.stringify(bridge.outbox.data).includes('Crash boundary secret'), true);
  bridge.outbox.update = update;
  await bridge.recoverPending();
  assert.equal(line.store.data.messages['CUSTOMER-CRASH'].callbacks_scrubbed, true);
  assert.deepEqual(bridge.outbox.data.entries.map(item => item.event.type), ['message_tombstone', 'message_deleted_for_everyone']);
  assert.equal(JSON.stringify(bridge.outbox.data).includes('Crash boundary secret'), false);
});

test('native Baileys buffer preserves two customer edits in one second even for update-only consumers and rejects delayed edits', async t => {
  const { bridge, line } = await fixture(t);
  await line.store.update(data => { data.messages.ORIGINAL.message.key.fromMe = false; });
  const logger = pino({ level: 'silent' });
  const ev = makeEventBuffer(logger);
  const restore = preserveNativeMutationMetadata({ ev });
  t.after(() => { restore(); ev.destroy(); });
  let consumed = Promise.resolve();
  const observed = [];
  // Exercise the difficult case: only derived updates reach our consumer. Raw
  // native upserts are captured before the real Baileys buffer/processMessage.
  ev.process(events => {
    consumed = consumed.then(async () => {
      for (const update of events['messages.update'] ?? []) {
        observed.push(update.update);
        await bridge.receiveUpdate(line, update);
      }
    });
    return consumed;
  });
  const second = Math.floor(Date.now() / 1000) * 1000;
  const native = (id, body, timestamp) => ({ ...original(id, false), message: { protocolMessage: {
    type: proto.Message.ProtocolMessage.Type.MESSAGE_EDIT, key: original('ORIGINAL', false).key,
    editedMessage: { conversation: body }, timestampMs: timestamp,
  } } });
  const deliver = async message => {
    ev.buffer();
    ev.emit('messages.upsert', { messages: [message], type: 'notify' });
    await processMessage(message, { ev, creds: { me: { id: '96170111111@s.whatsapp.net' }, accountSettings: {} }, logger });
    ev.flush();
    await consumed;
  };
  await deliver(native('EDIT-ONE', 'First correction', second + 125));
  await deliver(native('EDIT-TWO', 'Second correction', second + 875));
  assert.equal(observed[0].messageTimestamp, observed[1].messageTimestamp);
  assert.equal(observed[0].bridgeMutationTimestampMs, second + 125);
  assert.equal(observed[1].bridgeMutationTimestampMs, second + 875);
  assert.deepEqual(bridge.outbox.data.entries.map(item => item.event.timestamp), [second + 125, second + 875]);
  assert.equal(line.store.data.messages.ORIGINAL.text, 'Second correction');
  await deliver(native('EDIT-ONE-DELAYED', 'First correction', second + 125));
  assert.equal(line.store.data.messages.ORIGINAL.text, 'Second correction');
  assert.equal(bridge.outbox.data.entries.length, 2);
  await deliver(native('EDIT-TWO-DUPLICATE', 'Second correction', second + 875));
  assert.equal(bridge.outbox.data.entries.length, 2);
});

test('confirmed revoke purges native content, attachment files, prior edit payloads and superseded callbacks while retaining idempotence', async t => {
  const { bridge, line } = await fixture(t);
  const media = await bridge.uploadMedia(LINE, { content_base64: Buffer.from('private-media-content').toString('base64'), mime_type: 'audio/ogg', filename: 'voice.ogg' });
  const mediaPath = bridge.mediaPath(line, media.id);
  await line.store.update(data => { data.messages.ORIGINAL.media_id = media.id; });
  const editPayload = { to: PHONE, body: 'Private corrected content', operation_id: 'operation-private-edit' };
  const edit = await bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', editPayload);
  await bridge.outbox.update(data => { data.dead_letters.push({ event: { ...data.entries[0].event }, reason: 'TEST' }); });
  const deletion = await bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'delete', { to: PHONE, operation_id: 'operation-private-delete' });
  const record = line.store.data.messages.ORIGINAL;
  assert.equal(record.deleted, true);
  assert.equal(record.message.message, undefined);
  assert.equal(record.message.key.id, 'ORIGINAL');
  assert.equal(record.text, null);
  assert.equal(record.media_id, undefined);
  assert.equal(line.store.data.media[media.id], undefined);
  await assert.rejects(stat(mediaPath), error => error.code === 'ENOENT');
  const operation = line.store.data.operations[editPayload.operation_id];
  assert.equal(operation.payload, undefined);
  assert.equal(operation.body, undefined);
  assert.equal(operation.event, undefined);
  assert.equal(operation.status, 'confirmed');
  assert.deepEqual(await bridge.mutate(LINE, `ld:${LINE}:ORIGINAL`, 'edit', editPayload), edit);
  assert.deepEqual(bridge.outbox.data.entries.map(item => item.event.type), ['message_deleted_for_everyone']);
  assert.equal(bridge.outbox.data.dead_letters.length, 0);
  assert.equal(bridge.outbox.data.entries[0].event.timestamp, deletion.timestamp);
  await bridge.recoverPending();
  assert.equal(bridge.outbox.data.entries.length, 1);
});

test('LID routing uses WhatsApp verified phone mapping and avoids mistaking LID for a phone', async t => {
  const { bridge, line, socket } = await fixture(t);
  const message = { ...original('LID-INBOUND', false), key: { id: 'LID-INBOUND', remoteJid: '123456789012345@lid', fromMe: false } };
  await bridge.receive(line, message);
  assert.equal(line.store.data.messages['LID-INBOUND'].pending_normalization, true);
  assert.equal(bridge.outbox.data.entries.length, 0);
  socket.signalRepository = { lidMapping: { getPNForLID: async () => `${PHONE}@s.whatsapp.net` } };
  await bridge.recoverPending();
  assert.equal(bridge.outbox.data.entries[0].event.sender_phone, PHONE);

  const fromPhone = { ...original('LID-OUTBOUND'), key: { id: 'LID-OUTBOUND', remoteJid: '123456789012345@lid', fromMe: true } };
  delete socket.signalRepository;
  await bridge.receive(line, fromPhone);
  assert.equal(line.store.data.messages['LID-OUTBOUND'].pending_normalization, true);
  assert.equal(line.store.data.messages['LID-OUTBOUND'].notified, false);
  socket.signalRepository = { lidMapping: { getPNForLID: async () => `${PHONE}@s.whatsapp.net` } };
  await bridge.recoverPending();
  assert.equal(bridge.outbox.data.entries.at(-1).event.type, 'outgoing_message');
  assert.equal(bridge.outbox.data.entries.at(-1).event.sender_phone, PHONE);
});

test('callbacks retry durably without blocking unrelated events, and permanent failures enter dead letters', async t => {
  const calls = [];
  const { bridge } = await fixture(t, 'ack', { fetch: async (_url, options) => {
    assert.equal(options.headers.Authorization, `Bearer ${SECRET}`);
    const event = JSON.parse(options.body);
    calls.push(event.event_id);
    const status = event.event_id === 'retry' ? 503 : event.event_id === 'bad' ? 400 : 200;
    return new Response(null, { status });
  } });
  await bridge.enqueue({ event_id: 'retry', line_id: LINE });
  await bridge.enqueue({ event_id: 'good', line_id: LINE });
  await bridge.enqueue({ event_id: 'bad', line_id: LINE });
  await bridge.deliverCallbacks();
  assert.deepEqual(calls, ['retry', 'good', 'bad']);
  assert.equal(bridge.outbox.data.entries.length, 1);
  assert.equal(bridge.outbox.data.entries[0].attempts, 1);
  assert.equal(bridge.outbox.data.dead_letters[0].reason, 'HTTP_400');
  assert.equal(bridge.status().lines[0].pending_callbacks, 1);
  assert.equal(bridge.status().lines[0].failed_callbacks, 1);
});

test('missing reaction targets stay durable across restarts without inflating pending delivery', async t => {
  let outcome = 'history';
  const seen = [];
  const fetchCallback = async (_url, options) => {
    const event = JSON.parse(options.body);
    seen.push(event.event_id);
    if (event.event_id === 'ordinary-message') return new Response(null, { status: 200 });
    if (outcome === 'network') throw new Error('Connection lost');
    return new Response(null, { status: outcome === 'applied' ? 200 : 503,
      headers: outcome === 'history' ? { 'X-WhatsApp-Callback-State': 'waiting_for_reaction_target' } : {} });
  };
  const { bridge, config } = await fixture(t, 'ack', { fetch: fetchCallback });
  const reaction = { event_id: 'waiting-reaction', line_id: LINE, type: 'outgoing_message',
    payload: { entry: [{ changes: [{ value: { messages: [{ type: 'reaction', reaction: { message_id: 'missing', emoji: '👍' } }] } }] }] } };
  await bridge.enqueue(reaction);
  await bridge.enqueue({ event_id: 'ordinary-message', line_id: LINE, type: 'message' });
  await bridge.deliverCallbacks();
  assert.deepEqual(seen, ['waiting-reaction', 'ordinary-message']);
  assert.equal(bridge.status().lines[0].pending_callbacks, 0);
  assert.equal(bridge.status().lines[0].waiting_history_reactions, 1);
  assert.deepEqual(bridge.outbox.data.entries[0].event, reaction);
  assert.equal(bridge.outbox.data.dead_letters.length, 0);
  await bridge.close();

  const resumed = await new Bridge(config, { makeSocket: () => mockSocket(), onError: () => {}, fetch: fetchCallback }).initialize({ autoConnect: false });
  t.after(() => resumed.close());
  assert.equal(resumed.status().lines[0].waiting_history_reactions, 1);
  for (const failure of ['network', 'database']) {
    outcome = failure;
    await resumed.outbox.update(data => { data.entries[0].next_try = 0; });
    await resumed.deliverCallbacks();
    assert.equal(resumed.status().lines[0].pending_callbacks, 1);
    assert.equal(resumed.status().lines[0].waiting_history_reactions, 0);
    // A subsequent confirmed history wait can restore the background state.
    outcome = 'history';
    await resumed.outbox.update(data => { data.entries[0].next_try = 0; });
    await resumed.deliverCallbacks();
    assert.equal(resumed.status().lines[0].waiting_history_reactions, 1);
  }
  outcome = 'applied'; // Original message finally synchronized (or API confirms expiry).
  await resumed.outbox.update(data => { data.entries[0].next_try = 0; });
  await resumed.deliverCallbacks();
  assert.equal(resumed.outbox.data.entries.length, 0);
  assert.equal(resumed.status().lines[0].waiting_history_reactions, 0);
  assert.equal(resumed.status().lines[0].pending_callbacks, 0);
});

test('a history-wait response cannot hide real messages or authorization failures', async t => {
  const { bridge } = await fixture(t, 'ack', { fetch: async (_url, options) => {
    const event = JSON.parse(options.body);
    return new Response(null, { status: event.event_id === 'unauthorized-reaction' ? 403 : 503,
      headers: { 'X-WhatsApp-Callback-State': 'waiting_for_reaction_target' } });
  } });
  const payload = type => ({ entry: [{ changes: [{ value: { messages: [{ type }] } }] }] });
  await bridge.enqueue({ event_id: 'text', type: 'outgoing_message', line_id: LINE, payload: payload('text') });
  await bridge.enqueue({ event_id: 'unauthorized-reaction', type: 'message_snapshot', line_id: LINE, payload: payload('reaction') });
  await bridge.deliverCallbacks();
  assert.equal(bridge.status().lines[0].pending_callbacks, 1);
  assert.equal(bridge.status().lines[0].waiting_history_reactions, 0);
  assert.equal(bridge.status().lines[0].failed_callbacks, 1);
  assert.equal(bridge.outbox.data.dead_letters[0].reason, 'HTTP_403');
});

test('HTTP routes protect QR/media with shared auth, reject invalid/oversized JSON, and serve media bytes', async t => {
  const { bridge, config } = await fixture(t);
  const server = createServer(bridge, config);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const base = `http://127.0.0.1:${server.address().port}`;
  assert.equal((await fetch(`${base}/v1/status`)).status, 401);
  const headers = { Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json' };
  const status = await fetch(`${base}/v1/status`, { headers });
  assert.equal(status.status, 200);
  assert.equal(status.headers.get('cache-control'), 'no-store');
  assert.equal((await fetch(`${base}/v1/lines`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: randomUUID() }) })).status, 401);
  const registration = { request_id: randomUUID(), expected_phone: '', reserved_line_ids: ['90002'] };
  const registered = await (await fetch(`${base}/v1/lines`, { method: 'POST', headers, body: JSON.stringify(registration) })).json();
  assert.equal(registered.line_id, '90003');
  assert.equal(registered.status, 'disconnected');
  assert.equal(registered.created, true);
  const repeated = await (await fetch(`${base}/v1/lines`, { method: 'POST', headers, body: JSON.stringify(registration) })).json();
  assert.equal(repeated.line_id, registered.line_id);
  assert.equal(repeated.created, false);
  assert.equal((await fetch(`${base}/v1/lines/${registered.line_id}`, { method: 'DELETE' })).status, 401);
  const removedLine = await fetch(`${base}/v1/lines/${registered.line_id}`, { method: 'DELETE', headers });
  assert.equal(removedLine.status, 200);
  assert.deepEqual(await removedLine.json(), { line_id: registered.line_id, removed: true, unlinked: true });
  assert.equal((await fetch(`${base}/v1/lines/${registered.line_id}`, { method: 'DELETE', headers })).status, 200);
  assert.equal((await fetch(`${base}/v1/lines/${LINE}/messages`, { method: 'POST', headers, body: '{bad' })).status, 400);
  const malformed = await fetch(`${base}/v1/lines/${LINE}/messages/%E0%A4%A/edit`, { method: 'POST', headers, body: '{}' });
  assert.equal(malformed.status, 400);
  assert.equal((await fetch(`${base}/v1/lines/${LINE}/messages`, { method: 'POST', headers, body: JSON.stringify({ extra: 'x'.repeat(66 * 1024) }) })).status, 413);
  const uploaded = await (await fetch(`${base}/v1/lines/${LINE}/media`, { method: 'POST', headers, body: JSON.stringify({ content_base64: Buffer.from('media').toString('base64'), mime_type: 'audio/ogg', filename: 'voice.ogg' }) })).json();
  const downloaded = await fetch(`${base}/v1/lines/${LINE}/media/${encodeURIComponent(uploaded.id)}`, { headers });
  assert.equal(downloaded.headers.get('content-type'), 'audio/ogg');
  assert.equal(await downloaded.text(), 'media');
  bridge.getLine(LINE).status = 'disconnected';
  const rejected = await fetch(`${base}/v1/lines/${LINE}/messages/${encodeURIComponent(`ld:${LINE}:ORIGINAL`)}/edit`, { method: 'POST', headers, body: JSON.stringify({ to: PHONE, body: 'Offline', operation_id: 'operation-offline-001' }) });
  assert.equal(rejected.status, 503);
  assert.equal((await rejected.json()).delivery_uncertain, false);
});

test('session directory process lock prevents concurrent ownership and releases cleanly', async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-lock-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const release = await acquireDataDirectory(directory);
  assert.equal((await stat(directory)).mode & 0o777, 0o700);
  await assert.rejects(acquireDataDirectory(directory), error => error.code === 'ELOCKED');
  await release();
  const nextRelease = await acquireDataDirectory(directory);
  await nextRelease();
});

test('first pairing pins the phone and later wrong-account reconnect cannot send', async t => {
  const { bridge, line, socket } = await fixture(t);
  await bridge.connectionUpdate(line, socket, { connection: 'open' });
  assert.equal(line.store.data.bound_phone, '96170111111');
  socket.user.id = '96170999999:1@s.whatsapp.net';
  await bridge.connectionUpdate(line, socket, { connection: 'open' });
  assert.equal(line.status, 'error');
  assert.equal(line.store.data.enabled, false);
  await assert.rejects(bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'Fail' } }), error => error.code === 'NOT_CONNECTED');
  await bridge.disconnect(LINE);
  assert.equal(line.store.data.bound_phone, '');
});

test('a failed scheduled reconnect retries transient network errors with backoff and resumes its saved session', async t => {
  const { bridge, line, socket } = await fixture(t);
  const resumedSocket = mockSocket();
  let attempts = 0;
  bridge.makeSocket = options => {
    assert.ok(options.auth.creds);
    if (++attempts === 1) throw Object.assign(new Error('Temporary network interruption'), { code: 'ECONNRESET' });
    return resumedSocket;
  };
  t.mock.timers.enable({ apis: ['setTimeout'] });
  await bridge.connectionUpdate(line, socket, { connection: 'close', lastDisconnect: { error: { output: { statusCode: DisconnectReason.connectionClosed } } } });
  assert.equal(line.status, 'disconnected');
  t.mock.timers.tick(1000);
  await assert.rejects(line.connectPromise, error => error.code === 'ECONNRESET');
  assert.equal(attempts, 1);
  assert.equal(line.status, 'disconnected');
  assert.ok(line.reconnectTimer);
  t.mock.timers.tick(1999);
  assert.equal(attempts, 1);
  t.mock.timers.tick(1);
  await line.connectPromise;
  assert.equal(attempts, 2);
  assert.equal(line.socket, resumedSocket);
  assert.equal(line.status, 'connecting');
});

test('scheduled reconnect halts on persistence failure and never logs out or discards saved credentials', async t => {
  const { bridge, line, socket } = await fixture(t);
  const previousAuth = line.auth;
  const update = line.store.update.bind(line.store);
  let createSocketCalls = 0;
  bridge.makeSocket = () => { createSocketCalls++; return mockSocket(); };
  t.mock.timers.enable({ apis: ['setTimeout'] });
  await bridge.connectionUpdate(line, socket, { connection: 'close' });
  line.store.update = async () => { throw Object.assign(new Error('Cannot save session'), { code: 'EIO' }); };
  t.after(() => { line.store.update = update; });
  t.mock.timers.tick(1000);
  await assert.rejects(line.connectPromise, error => error.code === 'EIO' && error.bridgePersistenceFailure === true);
  assert.equal(line.status, 'error');
  assert.match(line.error, /storage/);
  assert.equal(line.reconnectTimer, null);
  assert.equal(line.auth, previousAuth);
  assert.equal(line.store.data.enabled, true);
  t.mock.timers.tick(60_000);
  assert.equal(createSocketCalls, 0);
});

test('provider logout and bridge shutdown suppress scheduled reconnect attempts', async t => {
  const { bridge, line, socket } = await fixture(t);
  let calls = 0;
  bridge.makeSocket = () => { calls++; return mockSocket(); };
  t.mock.timers.enable({ apis: ['setTimeout'] });
  await bridge.connectionUpdate(line, socket, { connection: 'close', lastDisconnect: { error: { output: { statusCode: DisconnectReason.loggedOut } } } });
  assert.equal(line.status, 'logged_out');
  assert.equal(line.store.data.enabled, false);
  t.mock.timers.tick(60_000);
  assert.equal(calls, 0);
  await line.store.update(data => { data.enabled = true; });
  line.status = 'disconnected';
  bridge.scheduleReconnect(line);
  assert.ok(line.reconnectTimer);
  bridge.stopping = true;
  t.mock.timers.tick(1000);
  assert.equal(calls, 0);
  assert.equal(line.reconnectTimer, null);
});

test('a corrupt store fails startup instead of silently discarding captured keys', async t => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-store-test-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'state.json');
  const { writeFile } = await import('node:fs/promises');
  await writeFile(filename, '{broken', { mode: 0o600 });
  await assert.rejects(new FileStore(filename, () => ({})).load(), SyntaxError);
});

test('video messages are relayed with caption, MIME type and media reference in both directions', () => {
  const line = { line_id: LINE, display_phone_number: '96170111111' };
  const mediaId = `ldmedia:${LINE}:12345678-1234-1234-1234-123456789abc`;
  const video = { videoMessage: { mimetype: 'video/mp4', caption: 'Box arrived crushed', fileSha256: Buffer.from([7, 8]), contextInfo: { stanzaId: 'QUOTED' } } };
  const inbound = inboundEvent(line, { ...original('VIDEO', false), message: video }, PHONE, mediaId);
  const node = inbound.payload.entry[0].changes[0].value.messages[0];
  assert.equal(node.type, 'video');
  assert.deepEqual(node.video, { id: mediaId, mime_type: 'video/mp4', caption: 'Box arrived crushed', filename: undefined, sha256: '0708' });
  assert.equal(node.context.id, `ld:${LINE}:QUOTED`);
  const fromPhone = outgoingEvent(line, { ...original('PHONE-VIDEO'), message: { videoMessage: { mimetype: 'video/mp4' } } }, PHONE, mediaId);
  assert.equal(fromPhone.payload.entry[0].changes[0].value.messages[0].type, 'video', 'a video sent from the phone is not marked unsupported');
  assert.equal(fromPhone.payload.entry[0].changes[0].value.messages[0].video.id, mediaId);
});

test('received videos are captured for download and dashboard videos are sent with their caption', async t => {
  const { bridge, line, socket } = await fixture(t);
  await bridge.receive(line, { ...original('CUSTOMER-VIDEO', false), message: { videoMessage: { mimetype: 'video/mp4', caption: 'Look' } } });
  const event = bridge.outbox.data.entries.at(-1).event;
  const node = event.payload.entry[0].changes[0].value.messages[0];
  assert.equal(node.type, 'video');
  assert.equal(line.store.data.media[node.video.id].mime_type, 'video/mp4');

  const media = await bridge.uploadMedia(LINE, { content_base64: Buffer.from('mp4-bytes').toString('base64'), mime_type: 'video/mp4', filename: 'clip.mp4' });
  const sent = [];
  const sendMessage = socket.sendMessage;
  socket.sendMessage = async (jid, content, options) => { sent.push(content); return sendMessage(jid, content, options); };
  await bridge.send(LINE, { to: PHONE, type: 'video', video: { id: media.id, caption: 'Here is the return label video' }, client_message_id: 'video-send-1' });
  assert.equal(sent.length, 1);
  assert.equal(Buffer.from(sent[0].video).toString(), 'mp4-bytes');
  assert.equal(sent[0].mimetype, 'video/mp4');
  assert.equal(sent[0].caption, 'Here is the return label video');
});

test('media on an unresolvable WhatsApp host is downloaded through the default media host', async t => {
  const calls = [];
  const notFound = Object.assign(new TypeError('fetch failed'), { cause: { code: 'ENOTFOUND' } });
  const downloadMediaMessage = async (message, type, options) => {
    calls.push(options.host ?? null);
    if (options.host !== 'mmg.whatsapp.net') throw notFound;
    return Readable.from([Buffer.from('RIFF'), Buffer.from('webp-bytes')]);
  };
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = message => warnings.push(message);
  t.after(() => { console.warn = originalWarn; });
  const { bridge, line } = await fixture(t, 'ack', { downloadMediaMessage });
  const sticker = { stickerMessage: { mimetype: 'image/webp', url: 'https://a.whatsapp.net/v/t62.15575-24/sticker.enc', directPath: '/v/t62.15575-24/sticker.enc' } };
  await bridge.receive(line, { ...original('PHONE-STICKER'), message: sticker });
  const mediaId = bridge.outbox.data.entries.at(-1).event.payload.entry[0].changes[0].value.messages[0].sticker.id;

  const media = await bridge.downloadMedia(LINE, mediaId);
  assert.equal(media.content.toString(), 'RIFFwebp-bytes');
  assert.equal(media.mime_type, 'image/webp');
  assert.deepEqual(calls, [null, 'mmg.whatsapp.net']);
  assert.equal(line.store.data.media[mediaId].downloaded, true);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /host=a\.whatsapp\.net code=ENOTFOUND/);
  assert.equal(warnings[0].includes('sticker.enc'), false, 'the media path is never logged');

  // A failure already on the default host is not retried.
  calls.length = 0;
  const image = { imageMessage: { mimetype: 'image/jpeg', url: 'https://mmg.whatsapp.net/v/t62/image.enc', directPath: '/v/t62/image.enc' } };
  await bridge.receive(line, { ...original('PHONE-IMAGE'), message: image });
  const imageId = bridge.outbox.data.entries.at(-1).event.payload.entry[0].changes[0].value.messages[0].image.id;
  const failing = async (message, type, options) => { calls.push(options.host ?? null); throw notFound; };
  bridge.downloadMediaMessage = failing;
  await assert.rejects(bridge.downloadMedia(LINE, imageId), /fetch failed/);
  assert.deepEqual(calls, [null]);
});

test('number lookup reports whether a phone number has WhatsApp before a chat is started', async t => {
  const { bridge, socket } = await fixture(t);
  const asked = [];
  socket.onWhatsApp = async jid => {
    asked.push(jid);
    return jid.startsWith('96176612345@') ? [{ jid: '96176612345@s.whatsapp.net', exists: true }] : [];
  };
  assert.deepEqual(await bridge.checkNumber(LINE, { phone: '+961 76 612 345' }), { phone: '96176612345', exists: true, whatsapp_phone: '96176612345' });
  assert.deepEqual(await bridge.checkNumber(LINE, { phone: '76612345' }), { phone: '76612345', exists: false, whatsapp_phone: '76612345' });
  assert.deepEqual(asked, ['96176612345@s.whatsapp.net', '76612345@s.whatsapp.net']);
  await assert.rejects(bridge.checkNumber(LINE, { phone: 'not a number' }), error => error.code === 'INVALID_PHONE');
  assert.equal(socket.calls.length, 0, 'a lookup never sends a message');
});

test('delivered and read receipts become dashboard status events, only moving forward', async t => {
  const { bridge, line } = await fixture(t);
  const sent = await bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'Ticked reply' }, client_message_id: 'tick-1' });
  const nativeId = unwrapId(LINE, sent.messages[0].id);
  const key = { id: nativeId, remoteJid: `${PHONE}@s.whatsapp.net`, fromMe: true };
  await bridge.outbox.update(data => { data.entries = []; });
  await bridge.receiveUpdate(line, { key, update: { status: proto.WebMessageInfo.Status.DELIVERY_ACK } });
  await bridge.receiveUpdate(line, { key, update: { status: proto.WebMessageInfo.Status.DELIVERY_ACK } });
  await bridge.receiveUpdate(line, { key, update: { status: proto.WebMessageInfo.Status.READ } });
  await bridge.receiveUpdate(line, { key, update: { status: proto.WebMessageInfo.Status.DELIVERY_ACK } });
  const events = bridge.outbox.data.entries.map(item => item.event);
  assert.deepEqual(events.map(event => [event.type, event.status]), [['message_status', 'delivered'], ['message_status', 'read']]);
  assert.equal(events[0].message_id, sent.messages[0].id);
  assert.equal(events[0].sender_phone, PHONE);
  assert.equal(events[0].from_me, true);
  assert.notEqual(events[0].event_id, events[1].event_id);
});

test('a receipt confirms a send whose ACK was lost, before its status event', async t => {
  const { bridge, line } = await fixture(t, 'timeout');
  await assert.rejects(bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'Late ack' }, client_message_id: 'late-ack-1' }),
    error => error.code === 'CONFIRMATION_UNKNOWN');
  const [nativeId] = Object.entries(line.store.data.messages).find(([, item]) => item.client_message_id === 'late-ack-1');
  await bridge.receiveUpdate(line, { key: { id: nativeId, remoteJid: `${PHONE}@s.whatsapp.net`, fromMe: true },
    update: { status: proto.WebMessageInfo.Status.DELIVERY_ACK } });
  assert.deepEqual(bridge.outbox.data.entries.map(item => item.event.type), ['outgoing_confirmation', 'message_status']);
  assert.equal(bridge.outbox.data.entries[0].event.client_message_id, 'late-ack-1');
  assert.equal(bridge.lineStatus(line).uncertain_sends, 0);
});

test('receipts for bot replies and incoming messages are not forwarded', async t => {
  const { bridge, line } = await fixture(t);
  const bot = await bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'Automatic reply' } });
  await bridge.outbox.update(data => { data.entries = []; });
  await bridge.receiveUpdate(line, { key: { id: unwrapId(LINE, bot.messages[0].id), remoteJid: `${PHONE}@s.whatsapp.net`, fromMe: true },
    update: { status: proto.WebMessageInfo.Status.READ } });
  await line.store.update(data => { data.messages.INCOMING = { message: original('INCOMING', false), phone: PHONE, timestamp: Date.now(), notified: true }; });
  await bridge.receiveUpdate(line, { key: { id: 'INCOMING', remoteJid: `${PHONE}@s.whatsapp.net`, fromMe: false },
    update: { status: proto.WebMessageInfo.Status.READ } });
  assert.equal(bridge.outbox.data.entries.length, 0);
});

test('a missing WhatsApp ACK restarts the stale connection', async t => {
  const { bridge, socket } = await fixture(t, 'timeout');
  let ended = 0;
  socket.end = () => { ended++; };
  await assert.rejects(bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'No ack' }, client_message_id: 'stale-1' }),
    error => error.code === 'CONFIRMATION_UNKNOWN');
  assert.equal(ended, 1);
});

test('the ACK timeout starts after a slow relay, not before it', async t => {
  const { bridge, socket } = await fixture(t);
  const sendMessage = socket.sendMessage;
  socket.sendMessage = async (...args) => { await new Promise(resolve => setTimeout(resolve, 60)); return sendMessage(...args); };
  const sent = await bridge.send(LINE, { to: PHONE, type: 'text', text: { body: 'Slow upload' }, client_message_id: 'slow-1' });
  assert.ok(sent.messages[0].id.startsWith(`ld:${LINE}:`));
});

test('concurrent duplicate sends reuse one durable provider ID', async t => {
  const { bridge, socket } = await fixture(t);
  const payload = { to: PHONE, type: 'text', text: { body: 'one send' }, client_message_id: 'duplicate-request' };
  const [first, second] = await Promise.all([bridge.send(LINE, payload), bridge.send(LINE, payload)]);
  assert.deepEqual(first, second);
  assert.equal(socket.calls.length, 1);
  await assert.rejects(bridge.send(LINE, { ...payload, to: '96170999999' }), /another recipient/);
  assert.equal(socket.calls.length, 1);
});

test('durable uncertain reservation blocks dispatch after restart', async t => {
  const { bridge, line, socket } = await fixture(t);
  await line.store.update(data => {
    data.messages.CRASHED = { phone: PHONE, client_message_id: 'lost-worker', pending_send: true,
      message: { key: { id: 'CRASHED', remoteJid: `${PHONE}@s.whatsapp.net`, fromMe: true } } };
  });
  const payload = { to: PHONE, type: 'text', text: { body: 'do not duplicate' }, client_message_id: 'lost-worker' };
  await assert.rejects(bridge.send(LINE, payload), error => error.delivery_uncertain === true);
  assert.equal(socket.calls.length, 0);
  await line.store.update(data => { data.messages.CRASHED.ack_confirmed = true; });
  assert.deepEqual(await bridge.send(LINE, payload), { messages: [{ id: wrapId(LINE, 'CRASHED') }] });
  assert.equal(socket.calls.length, 0);
});

test('inbound contact cards become visible snapshots; internal envelopes do not', async t => {
  const { bridge, line } = await fixture(t);
  const contact = { ...original('INBOUND-CONTACT', false), message: { contactMessage: { displayName: 'Example', vcard: 'private contact data' } } };
  await bridge.receive(line, contact);
  await bridge.receive(line, contact);
  assert.equal(bridge.outbox.data.entries.length, 1);
  const event = bridge.outbox.data.entries[0].event;
  assert.equal(event.type, 'message_snapshot');
  assert.equal(event.from_me, false);
  assert.equal(event.payload.entry[0].changes[0].value.messages[0].type, 'contacts');
  assert.equal(JSON.stringify(event).includes('private contact data'), false);
  for (const kind of ['albumMessage', 'placeholderMessage', 'secretEncryptedMessage', 'senderKeyDistributionMessage']) {
    await bridge.receive(line, { ...original(kind, false), message: { messageContextInfo: {}, [kind]: {} } });
  }
  assert.equal(bridge.outbox.data.entries.length, 1);
});

test('restart recovery forwards previously omitted content once and keeps deleted content private', async t => {
  const { bridge, line, config } = await fixture(t);
  await line.store.update(data => {
    data.messages.MISSED = { message: { ...original('MISSED', false), message: { contactMessage: {} } }, phone: PHONE, notified: false };
    data.messages.REVOKED = { message: { ...original('REVOKED', false), message: { contactMessage: {} } }, phone: PHONE, notified: false, deleted: true };
  });
  await bridge.close();
  const resumed = await new Bridge(config, { makeSocket: () => mockSocket(), onError: () => {} }).initialize({ autoConnect: false });
  t.after(() => resumed.close());
  resumed.getLine(LINE).status = 'connected';
  await resumed.recoverPending();
  await resumed.recoverPending();
  assert.equal(resumed.outbox.data.entries.length, 1);
  assert.equal(resumed.outbox.data.entries[0].event.message_id, `ld:${LINE}:MISSED`);
  assert.equal(resumed.outbox.data.entries[0].event.type, 'message_snapshot');
});

test('phone placeholder recovery requires an observed matching message and preserves original routing/time', async t => {
  const { bridge, socket, line } = await fixture(t);
  const stub = { ...original('UNAVAILABLE', false), message: undefined, messageStubType: proto.WebMessageInfo.StubType.CIPHERTEXT };
  await socket.eventHandler({ 'messages.upsert': { messages: [stub], type: 'notify' } });
  await socket.eventHandler({ 'messages.update': [{ key: stub.key, update: { messageStubParameters: ['unavailable', 'request-1'] } }] });
  const recovered = original('UNAVAILABLE', false);
  for (const [message, requestId] of [
    [original('UNSOLICITED', false), 'request-1'],
    [recovered, 'wrong-request'],
    [{ ...recovered, key: { ...recovered.key, fromMe: true } }, 'request-1'],
    [{ ...recovered, key: { ...recovered.key, remoteJid: '12345@s.whatsapp.net' } }, 'request-1'],
  ]) await socket.eventHandler({ 'messages.upsert': { messages: [message], type: 'notify', requestId } });
  assert.equal(bridge.outbox.data.entries.length, 0);
  // Exercise the pinned Baileys PDO decoder, which emits requestId on recovery.
  const msg = { ...original('PDO', true), message: { protocolMessage: {
    type: proto.Message.ProtocolMessage.Type.PEER_DATA_OPERATION_REQUEST_RESPONSE_MESSAGE,
    peerDataOperationRequestResponseMessage: { stanzaId: 'request-1', peerDataOperationResult: [{
      placeholderMessageResendResponse: { webMessageInfoBytes: proto.WebMessageInfo.encode(recovered).finish() },
    }] },
  } } };
  const emitted = [];
  await processMessage(msg, { shouldProcessHistoryMsg: false, ev: { emit: (name, data) => emitted.push([name, data]) },
    creds: { me: { id: '96170111111@s.whatsapp.net' } }, logger: pino({ level: 'silent' }),
    placeholderResendCache: { get: async () => undefined, del: async () => {} } });
  const [, upsert] = emitted.find(([name]) => name === 'messages.upsert');
  assert.equal(upsert.requestId, 'request-1');
  await socket.eventHandler({ 'messages.upsert': upsert });
  await socket.eventHandler({ 'messages.upsert': upsert });
  assert.equal(bridge.outbox.data.entries.length, 1);
  assert.equal(bridge.outbox.data.entries[0].event.type, 'message');
  assert.equal(bridge.outbox.data.entries[0].event.timestamp, stub.messageTimestamp * 1000);
  assert.equal(line.store.data.messages.UNAVAILABLE.notified, true);
});

test('a blocked callback on one line does not block another line or duplicate an in-flight callback', async t => {
  let release, entered;
  const blocked = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const calls = [];
  const { bridge } = await fixture(t, 'ack', { fetch: async (_url, options) => {
    const event = JSON.parse(options.body); calls.push(event.event_id);
    if (event.event_id === 'slow') { entered(); await blocked; }
    return new Response(null, { status: 200 });
  } });
  await bridge.enqueue({ event_id: 'slow', line_id: LINE });
  const work = bridge.deliverCallbacks();
  await started;
  await bridge.enqueue({ event_id: 'other-office', line_id: '90002' });
  try {
    await bridge.deliverCallbacks();
    assert.deepEqual(calls, ['slow', 'other-office']);
    assert.equal(bridge.outbox.data.entries.length, 1);
  } finally { release(); await work; }
  assert.equal(bridge.outbox.data.entries.length, 0);
});

test('native event buffering preserves recovery provenance and rejects unsolicited recovered messages', async t => {
  const { bridge, socket, line } = await fixture(t);
  const ev = makeEventBuffer(pino({ level: 'silent' }));
  const restore = preserveNativeMutationMetadata({ ev });
  t.after(() => { restore(); ev.destroy(); });
  const batches = [];
  ev.process(events => batches.push(events));
  ev.buffer();
  const stub = { ...original('BUFFERED', false), message: undefined, messageStubType: proto.WebMessageInfo.StubType.CIPHERTEXT };
  ev.emit('messages.upsert', { messages: [stub], type: 'notify' });
  ev.emit('messages.update', [{ key: stub.key, update: { messageStubParameters: ['unavailable', 'pdo-buffered'] } }]);
  ev.emit('messages.upsert', { messages: [original('BUFFERED', false), original('UNSOLICITED', false)], type: 'notify', requestId: 'pdo-buffered' });
  ev.flush();
  assert.equal(batches[0]['messages.upsert'].requestId, undefined, 'real Baileys buffer drops the top-level request ID');
  for (const batch of batches) await socket.eventHandler(batch);
  assert.equal(bridge.outbox.data.entries.length, 1);
  assert.equal(bridge.outbox.data.entries[0].event.message_id, `ld:${LINE}:BUFFERED`);
  assert.equal(line.store.data.messages.UNSOLICITED, undefined);
});

test('shared vCards preserve names, multiple phones and folded international details', async t => {
  const { bridge, line } = await fixture(t);
  const card = { ...original('CONTACT-DETAILS', false), message: { contactsArrayMessage: { contacts: [
    { displayName: 'Fallback', vcard: 'BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Maya Example\r\nTEL;waid=96170123456:+961 70\r\n 123456\r\nTEL;TYPE=WORK:tel:+90 555 1234567\r\nEMAIL:maya@example.test\r\nORG:Example Co;Support\r\nEND:VCARD' },
    { vcard: 'BEGIN:VCARD\nFN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=D9=85=D8=A7=D9=8A=D8=A7\nTEL:+96171123456\nEND:VCARD' },
  ] } } };
  await bridge.receive(line, card);
  const event = bridge.outbox.data.entries[0].event;
  const contacts = event.payload.entry[0].changes[0].value.messages[0].contacts;
  assert.equal(contacts[0].name.formatted_name, 'Maya Example');
  assert.equal(contacts[0].phones[0].phone, '+961 70123456');
  assert.equal(contacts[0].phones[0].wa_id, '96170123456');
  assert.equal(contacts[0].phones[1].phone, '+90 555 1234567');
  assert.equal(contacts[1].name.formatted_name, 'مايا');
  assert.equal(contacts[0].emails[0].email, 'maya@example.test');
  assert.equal(contacts[0].org.company, 'Example Co');
  assert.equal(line.store.data.messages['CONTACT-DETAILS'].contacts_normalized, true);
});

test('previously notified unsupported contact markers are re-enqueued once with details', async t => {
  const { bridge, line } = await fixture(t);
  const message = { ...original('OLD-CARD', false), message: { ephemeralMessage: { message: { contactMessage: { displayName: 'Example', vcard: 'FN:Example\nTEL:+96170123456' } } } } };
  await line.store.update(data => { data.messages['OLD-CARD'] = { message, phone: PHONE, notified: true }; });
  await bridge.recoverPending();
  await bridge.recoverPending();
  assert.equal(bridge.outbox.data.entries.length, 1);
  const event = bridge.outbox.data.entries[0].event;
  assert.equal(event.payload.entry[0].changes[0].value.messages[0].type, 'contacts');
  assert.equal(event.message_id, `ld:${LINE}:OLD-CARD`);
  assert.equal(line.store.data.messages['OLD-CARD'].contacts_normalized, true);
});

test('large contact bundles stay within callback limits with a visible truncation notice', () => {
  const cards = Array.from({ length: 100 }, () => ({ displayName: '界'.repeat(512),
    vcard: Array.from({ length: 20 }, () => 'TEL:' + '1'.repeat(512)).join('\n') }));
  const event = inboundEvent({ line_id: LINE }, { ...original('BUNDLE', false), message: { contactsArrayMessage: { contacts: cards } } }, PHONE);
  assert.ok(Buffer.byteLength(JSON.stringify(event)) < 128 * 1024);
  assert.equal(event.payload.entry[0].changes[0].value.messages[0].contacts.at(-1).name.formatted_name, '[Additional contact details omitted]');
});

test('delivery lookup reads durable evidence without sending and enforces recipient scope', async t => {
  const { bridge, line, socket } = await fixture(t);
  await line.store.update(data => {
    data.messages.ORIGINAL.client_message_id = 'lookup';
    data.messages.ORIGINAL.pending_send = false;
    data.messages.ORIGINAL.delivery_uncertain = true;
  });
  const payload = { client_message_id: 'lookup', phone: PHONE };
  const before = JSON.stringify(line.store.data);
  assert.deepEqual(bridge.deliveryStatus(LINE, payload), { status: 'unconfirmed' });
  assert.deepEqual(bridge.deliveryStatus(LINE, { ...payload, client_message_id: 'missing' }), { status: 'unknown' });
  assert.throws(() => bridge.deliveryStatus(LINE, { ...payload, phone: '15555555555' }), /another recipient/);
  assert.equal(JSON.stringify(line.store.data), before);
  for (const evidence of ['ack_confirmed', 'echo_seen']) {
    await line.store.update(data => {
      data.messages.ORIGINAL.ack_confirmed = false;
      data.messages.ORIGINAL.echo_seen = false;
      data.messages.ORIGINAL[evidence] = true;
    });
    line.status = 'disconnected';
    assert.deepEqual(bridge.deliveryStatus(LINE, payload), {
      status: 'confirmed', message_id: `ld:${LINE}:ORIGINAL`, timestamp: line.store.data.messages.ORIGINAL.timestamp,
    });
  }
  assert.equal(socket.calls.length, 0);
});

test('delivery lookup HTTP route requires bridge auth and never dispatches', async t => {
  const { bridge, config, socket } = await fixture(t);
  const server = createServer(bridge, config);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const url = `http://127.0.0.1:${server.address().port}/v1/lines/${LINE}/messages/status`;
  const payload = JSON.stringify({ client_message_id: 'missing', phone: PHONE });
  assert.equal((await fetch(url, { method: 'POST', body: payload })).status, 401);
  const response = await fetch(url, { method: 'POST', headers: {
    Authorization: `Bearer ${SECRET}`, 'Content-Type': 'application/json',
  }, body: payload });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: 'unknown' });
  assert.equal(socket.calls.length, 0);
});
