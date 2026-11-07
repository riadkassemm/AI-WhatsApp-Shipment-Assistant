import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import makeWASocket, { NACK_REASONS, proto } from '@whiskeysockets/baileys';
import { WebSocketClient } from '@whiskeysockets/baileys/lib/Socket/Client/websocket.js';
import { Bridge } from '../src/bridge.js';
import { readJournal } from '../src/journal-store.js';
import pino from 'pino';

const gate = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const LINE = '90001', PHONE = '96170123456';
const healthyStorage = async () => ({ bsize: 4096, blocks: 10_000_000, bavail: 5_000_000, files: 1_000_000, ffree: 900_000 });

async function nativeFixture(t) {
  const wireStarted = gate(), wireRelease = gate();
  const diagnostics = [];
  const logger = pino({ level: 'debug', serializers: { error: pino.stdSerializers.err, ackErr: pino.stdSerializers.err } }, { write: chunk => {
    const entry = JSON.parse(chunk);
    if (entry.level >= 40) diagnostics.push({ message: entry.msg, error: entry.err?.message ?? entry.error?.message });
  } });
  let sends = 0, sendFailure;
  // Use the installed provider's real listeners, query bookkeeping, event
  // buffer and auth transaction wrapper. Replace only its physical transport;
  // no DNS request, WhatsApp connection or customer message is possible.
  t.mock.method(WebSocketClient.prototype, 'connect', function () {
    const transport = new EventEmitter();
    transport.readyState = 1;
    transport.close = () => { transport.readyState = 3; transport.emit('close'); };
    transport.send = (_bytes, callback) => {
      sends++;
      wireStarted.resolve();
      wireRelease.promise.then(() => {
        callback?.(sendFailure ?? null);
        if (sendFailure) return;
        for (const event of this.eventNames().filter(name => String(name).startsWith('TAG:'))) {
          this.emit(event, { tag: 'iq', attrs: { id: String(event).slice(4), type: 'result' },
            content: [{ tag: 'count', attrs: { value: '10000' } }, { tag: 'digest', attrs: {} }] });
        }
      });
    };
    this.socket = transport;
  });
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-native-lifecycle-'));
  const bridge = await new Bridge({ dataDir: directory, secret: 'isolated-provider-lifecycle-test-secret',
    lines: [{ line_id: LINE, expected_phone: '' }], callbackUrl: 'http://127.0.0.1/callback',
    ackTimeoutMs: 100, maxMediaBytes: 1024 }, {
    makeSocket: options => {
      options.auth.creds.me ??= { id: '96170111111:1@s.whatsapp.net' };
      return makeWASocket({ ...options, fireInitQueries: false, logger });
    }, storageFilesystem: healthyStorage, onError: error => diagnostics.push({ bridge_error: error.message }), log: () => {},
  }).initialize({ autoConnect: false });
  t.after(async () => {
    wireRelease.resolve();
    await new Promise(resolve => setImmediate(resolve));
    await bridge.close();
    await rm(directory, { recursive: true, force: true });
  });
  await bridge.connect(LINE);
  // Settle Baileys' initial nextTick buffer admission before simulating a
  // running connection failure; otherwise the fixture buffers after teardown.
  await new Promise(resolve => setImmediate(resolve));
  const line = bridge.getLine(LINE), socket = line.socket;
  assert.equal(socket.bridgeTaskVersion, 1, 'tests require the checksum-verified installed provider patch');
  return { bridge, line, socket, wireStarted, wireRelease, sends: () => sends,
    rejectSends: error => { sendFailure = error; }, diagnostics };
}

test('the installed CB:success callback drains post-query credentials before replacement', async t => {
  const { bridge, line, socket, wireStarted, wireRelease, sends } = await nativeFixture(t);
  const auth = line.auth;
  const callback = socket.ws.listeners('CB:success')[0];
  const login = callback({ attrs: { lid: '' } });
  assert.equal(typeof login?.then, 'function');
  await wireStarted.promise;
  let replaced = false;
  const renewal = bridge.openSocket(line).then(() => { replaced = true; });
  try {
    assert.equal(auth.hasProviderWork(), true);
    assert.equal(replaced, false);
    wireRelease.resolve(); await login; await renewal;
    assert.equal(auth.store.data.creds.me.lid, '');
    assert.equal(line.auth.state.creds.me.lid, '');
    assert.ok(sends() >= 3, 'actual count, passive-IQ and digest callbacks ran');
    await assert.rejects(readFile(auth.store.filename + '.auth-intent'), { code: 'ENOENT' });
  } finally { wireRelease.resolve(); await Promise.allSettled([login, renewal]); }
});

test('the installed bad-ACK callback retains delayed rejection evidence through shutdown', async t => {
  const { bridge, line, socket, wireStarted, wireRelease } = await nativeFixture(t);
  await line.store.update(data => {
    data.messages.DELAYED_BAD_ACK = { message: { key: { id: 'DELAYED_BAD_ACK', fromMe: true, remoteJid: PHONE + '@s.whatsapp.net' },
      message: { conversation: 'Synthetic outgoing message' }, messageTimestamp: Math.floor(Date.now() / 1000) },
      phone: PHONE, timestamp: Date.now(), mirrored_outgoing: true };
  });
  const callback = socket.ws.listeners('CB:ack,class:message')[0];
  const rejected = callback({ attrs: { id: 'DELAYED_BAD_ACK', from: PHONE + '@s.whatsapp.net',
    error: String(NACK_REASONS.SenderReachoutTimelocked) } });
  assert.equal(typeof rejected?.then, 'function', 'native listener must return its complete bad-ACK task');
  await wireStarted.promise;
  let closed = false;
  const closing = bridge.close().then(() => { closed = true; });
  try {
    assert.equal(closed, false);
    wireRelease.resolve(); await rejected; await closing;
    const saved = (await readJournal(line.store.filename, () => ({}))).data;
    const evidence = Object.values(saved.provider_rejections);
    assert.equal(evidence.length, 1);
    assert.equal(evidence[0].key.id, 'DELAYED_BAD_ACK');
    assert.equal(evidence[0].parameters[0], String(NACK_REASONS.SenderReachoutTimelocked));
    assert.equal(saved.messages.DELAYED_BAD_ACK.receipt, undefined, 'negative evidence does not fabricate a positive receipt');
    await assert.rejects(readFile(line.auth.store.filename + '.auth-active'), { code: 'ENOENT' });
  } finally { wireRelease.resolve(); await Promise.allSettled([rejected, closing]); }
});

test('actual offline receive queue preserves decoded messages when receipt writes fail during retirement', async t => {
  const { bridge, line, socket, wireStarted, wireRelease, rejectSends, sends, diagnostics } = await nativeFixture(t);
  // A known mapping makes the first physical write the delivery receipt.
  // Otherwise the decoder first asks for a PN/LID mapping; failing that query
  // would test rejection before the message has been decoded or acknowledged.
  await socket.signalRepository.lidMapping.storeLIDPNMappings([{ pn: PHONE + '@s.whatsapp.net', lid: '123456789012@lid' }]);
  await line.auth.drain();
  assert.equal(sends(), 0);
  const callback = socket.ws.listeners('CB:message')[0];
  const receive = id => callback({ tag: 'message', attrs: { id, from: PHONE + '@s.whatsapp.net',
    type: 'text', t: String(Math.floor(Date.now() / 1000)), offline: '1' }, content: [
    { tag: 'plaintext', attrs: {}, content: Buffer.from(proto.Message.encode({ conversation: 'Accepted ' + id }).finish()) },
  ] });
  const first = receive('NATIVE_OFFLINE_ONE'), second = receive('NATIVE_OFFLINE_TWO');
  await wireStarted.promise;
  let closed = false;
  const closing = bridge.close().then(() => { closed = true; });
  try {
    assert.equal(closed, false);
    // A transport write may have reached WhatsApp before its completion reports
    // EPIPE. The already decoded payload cannot depend on server redelivery.
    rejectSends(Object.assign(new Error('Synthetic receipt completion failure'), { code: 'EPIPE' }));
    wireRelease.resolve();
    await Promise.all([first, second, closing]);
    const saved = (await readJournal(line.store.filename, () => ({}))).data;
    if (!saved.messages.NATIVE_OFFLINE_ONE || !saved.messages.NATIVE_OFFLINE_TWO) t.diagnostic(JSON.stringify(diagnostics));
    assert.equal(saved.messages.NATIVE_OFFLINE_ONE.text, 'Accepted NATIVE_OFFLINE_ONE');
    assert.equal(saved.messages.NATIVE_OFFLINE_TWO.text, 'Accepted NATIVE_OFFLINE_TWO');
    const callbacks = (await readJournal(bridge.outbox.filename, () => ({}))).data.entries;
    assert.equal(callbacks.filter(item => item.event.type === 'message').length, 2);
    assert.ok(sends() >= 2);
    await assert.rejects(readFile(line.auth.store.filename + '.auth-active'), { code: 'ENOENT' });
  } finally { wireRelease.resolve(); await Promise.allSettled([first, second, closing]); }
});

test('actual unavailable and decrypt-failure stubs remain recoverable when their ACK completion fails', async t => {
  for (const mode of ['unavailable', 'decrypt_failure']) {
    const { bridge, line, socket, wireStarted, wireRelease, rejectSends, sends, diagnostics } = await nativeFixture(t);
    const id = 'PENDING_STUB_' + mode;
    await socket.signalRepository.lidMapping.storeLIDPNMappings([{ pn: PHONE + '@s.whatsapp.net', lid: '123456789012@lid' }]);
    await line.auth.drain();
    // Simulate a previously requested phone recovery. This bypasses another
    // recovery query so the injected write failure is exactly the stub ACK.
    await socket.placeholderResendCache.set(id, true);
    assert.equal(sends(), 0);
    const content = mode === 'unavailable' ? [{ tag: 'unavailable', attrs: {} }] :
      [{ tag: 'enc', attrs: { type: 'msg' }, content: Buffer.from([1]) }];
    const processing = socket.ws.listeners('CB:message')[0]({ tag: 'message', attrs: { id,
      from: PHONE + '@s.whatsapp.net', type: 'text', t: String(Math.floor(Date.now() / 1000)), offline: '1' }, content });
    await wireStarted.promise;
    const closing = bridge.close();
    try {
      rejectSends(Object.assign(new Error('Synthetic stub ACK completion failure'), { code: 'EPIPE' }));
      wireRelease.resolve(); await Promise.all([processing, closing]);
      const saved = (await readJournal(line.store.filename, () => ({}))).data;
      if (!saved.messages[id]) t.diagnostic(JSON.stringify(diagnostics));
      assert.equal(saved.messages[id].pending_placeholder, true);
      assert.equal(saved.messages[id].notified, false);
      assert.equal(saved.messages[id].message.messageStubType, proto.WebMessageInfo.StubType.CIPHERTEXT);
      assert.equal(saved.messages[id].message.key.remoteJid, PHONE + '@s.whatsapp.net');
      assert.equal((await readJournal(bridge.outbox.filename, () => ({}))).data.entries.length, 0);
      await assert.rejects(readFile(line.auth.store.filename + '.auth-active'), { code: 'ENOENT' });
    } finally { wireRelease.resolve(); await Promise.allSettled([processing, closing]); }
  }
});

test('native provider failure blocks immediately and replacement waits for physical closure', async t => {
  const {bridge, line, socket, wireRelease} = await nativeFixture(t);
  wireRelease.resolve();
  line.session.ready = true; line.status = 'connected';
  const raw = socket.ws.socket;
  const closeStarted = gate(), releaseClose = gate();
  raw.close = () => {
    raw.readyState = 2; closeStarted.resolve();
    releaseClose.promise.then(() => { raw.readyState = 3; raw.emit('close', 1006); });
  };
  // Invoke a real native provider end path, not the bridge's public end wrapper.
  socket.ws.emit('CB:failure', {attrs: {reason: '408'}});
  assert.equal(line.session.ready, false);
  assert.equal(line.status, 'disconnected');
  assert.equal(line.diagnostics.disconnects.temporary_transport, 1);
  await closeStarted.promise;
  let replaced = false;
  const renewal = bridge.connect(LINE).then(() => {replaced = true;});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(replaced, false);
  assert.equal(line.socket, null);
  releaseClose.resolve(); await renewal;
  assert.notEqual(line.socket, socket);
  assert.equal(line.diagnostics.generation, 2);
  assert.equal(line.diagnostics.recovery_started_at != null, true, 'first failure survives transport replacement');
});

test('native auth and conflict failures block retry before delayed close and never accelerate termination', async t => {
  for (const [reason, category] of [['401', 'logged_out'], ['403', 'authentication_failure'], ['440', 'session_conflict']]) {
    const {bridge, line, socket, wireRelease} = await nativeFixture(t);
    wireRelease.resolve();
    const raw = socket.ws.socket;
    const closeStarted = gate(), releaseClose = gate(); let terminated = false;
    raw.terminate = () => { terminated = true; };
    raw.close = () => {
      raw.readyState = 2; closeStarted.resolve();
      releaseClose.promise.then(() => { raw.readyState = 3; raw.emit('close', 1006); });
    };
    socket.ws.emit('CB:failure', {attrs: {reason}});
    assert.equal(line.retryBlocked, true);
    assert.equal(line.recoveryCategory, category);
    bridge.scheduleReconnect(line);
    assert.equal(Boolean(line.reconnectTimer), false);
    await closeStarted.promise;
    assert.equal(terminated, false);
    releaseClose.resolve(); await socket.bridgeTransportClosed;
    await new Promise(resolve => setImmediate(resolve));
    await line.eventTail;
    assert.equal(Boolean(line.reconnectTimer), false);
  }
});

test('installed waitForMessage resolves undefined on timeout and releases its response listeners', async t => {
  const {socket} = await nativeFixture(t);
  t.mock.timers.enable({apis: ['setTimeout']});
  try {
    const beforeClose = socket.ws.listenerCount('close');
    const waiting = socket.waitForMessage('synthetic-heartbeat-timeout', 20000);
    assert.equal(socket.ws.listenerCount('TAG:synthetic-heartbeat-timeout'), 1);
    t.mock.timers.tick(20000);
    assert.equal(await waiting, undefined);
    assert.equal(socket.ws.listenerCount('TAG:synthetic-heartbeat-timeout'), 0);
    assert.equal(socket.ws.listenerCount('close'), beforeClose);
  } finally { t.mock.timers.reset(); }
});
