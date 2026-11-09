import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readdir, unlink } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { EventEmitter } from 'node:events';
import path from 'node:path';
import os from 'node:os';
import { Bridge } from '../src/bridge.js';
import { createServer } from '../src/server.js';
import { wrapId } from '../src/config.js';
import { readJournal } from '../src/journal-store.js';
import { proto } from '@whiskeysockets/baileys';

const LINE = '90001', PHONE = '96170123456';
const SECRET = 'private-storage-admission-test-secret';
const GiB = 1024 ** 3;
const sendPayload = key => ({ to: PHONE, type: 'text', text: { body: 'Synthetic delivery' }, client_message_id: key });
const gate = () => { let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve}; };
const lidMutation = (id, target, jid, type, fromMe = false) => ({
  key: {id, remoteJid: jid, fromMe}, messageTimestamp: Math.floor(Date.now() / 1000),
  message: {protocolMessage: {type, key: {id: target}, timestampMs: Date.now(), editedMessage: {conversation: 'Preserved queued edit'}}},
});

async function fixture(t, {prepareMedia} = {}) {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), 'bridge-pressure-'));
  let available = 20 * GiB;
  const ws = new EventEmitter();
  ws.isOpen = true;
  const socket = {
    ws, calls: [], user: { id: '96170111111:1@s.whatsapp.net' }, end: () => {},
    ev: { process: handler => { socket.eventHandler = handler; return () => {}; } },
    sendMessage: async (jid, content, options) => {
      socket.calls.push({ kind: 'send', id: options.messageId });
      setImmediate(() => ws.emit('CB:ack,class:message', { attrs: { id: options.messageId } }));
      return { key: { id: options.messageId, remoteJid: jid, fromMe: true },
        message: { conversation: content.text }, messageTimestamp: Math.floor(Date.now() / 1000) };
    },
    relayMessage: async (_jid, _payload, options) => {
      socket.calls.push({ kind: 'mutation', id: options.messageId });
      setImmediate(() => ws.emit('CB:ack,class:message', { attrs: { id: options.messageId } }));
      return options.messageId;
    },
  };
  const config = { secret: SECRET, dataDir, lines: [{ line_id: LINE, expected_phone: '' }],
    callbackUrl: 'http://127.0.0.1/unused', ackTimeoutMs: 1000, maxMediaBytes: 25 * 1024 ** 2,
    storage: { maintenance: { enabled: false } } };
  const bridge = new Bridge(config, { makeSocket: () => socket, log: () => {}, onError: () => {},
    storageFilesystem: async () => ({ bsize: 1, blocks: 100 * GiB, bavail: available, files: 1000, ffree: 900 }),
  });
  if (prepareMedia) bridge.mediaFiles.prepare = prepareMedia;
  await bridge.initialize({ autoConnect: false });
  await bridge.connect(LINE);
  const line = bridge.getLine(LINE);
  await bridge.connectionUpdate(line, socket, { connection: 'open' });
  await line.store.update(data => {
    data.messages.ORIGINAL = { message: { key: { id: 'ORIGINAL', remoteJid: `${PHONE}@s.whatsapp.net`, fromMe: true },
      message: { conversation: 'Original synthetic body' }, messageTimestamp: Math.floor(Date.now() / 1000) },
      timestamp: Date.now(), phone: PHONE, text: 'Original synthetic body', mirrored_outgoing: true };
  });
  t.after(async () => { await bridge.close(); await rm(dataDir, { recursive: true, force: true }); });
  return { bridge, line, socket, config, dataDir,
    recover: async () => { available = 20 * GiB; await bridge.storageMonitor.sample(); },
    pressure: async () => { available = 256 * 1024 ** 2; await bridge.storageMonitor.sample(); } };
}

test('failed optional media cleanup does not prevent session startup or essential persistence', async t => {
  let cleanupAttempts = 0;
  const {bridge, line, socket} = await fixture(t, {prepareMedia: async () => {
    cleanupAttempts++;
    throw Object.assign(new Error('EIO'), {code: 'EIO'});
  }});
  assert.equal(cleanupAttempts, 1);
  assert.equal(line.status, 'connected');
  assert.equal(line.socket, socket);
  line.auth.state.creds.registered = true;
  await line.auth.saveCreds();
  await line.auth.state.keys.set({session: {after_cleanup_failure: Buffer.from([4, 5])}});
  await bridge.receive(line, {key: {id: 'INCOMING-AFTER-CLEANUP-FAILURE', remoteJid: `${PHONE}@s.whatsapp.net`, fromMe: false},
    message: {conversation: 'Essential incoming evidence'}, messageTimestamp: Math.floor(Date.now() / 1000)});
  await bridge.receiveReceipt(line, line.store.data.messages.ORIGINAL.message.key, 'read');
  assert.equal(line.auth.store.data.creds.registered, true);
  assert.deepEqual(line.auth.store.data.keys.session.after_cleanup_failure, Buffer.from([4, 5]));
  const restored = await readJournal(path.join(line.directory, 'state.json'), () => assert.fail('Missing durable state'));
  assert.equal(restored.data.messages['INCOMING-AFTER-CLEANUP-FAILURE'].message.message.conversation, 'Essential incoming evidence');
  assert.equal(restored.data.messages.ORIGINAL.receipt, 'read');
  assert.ok(bridge.outbox.data.entries.length > 0);
  assert.equal(socket.calls.length, 0);
});

test('critical pressure rejects outgoing uploads before decoding while incoming and auth evidence persists', async t => {
  const { bridge, line, pressure } = await fixture(t);
  await pressure();
  const sequence = line.store.seq;
  for (let attempt = 0; attempt < 3; attempt++) {
    await assert.rejects(bridge.uploadMedia(LINE, {
      get content_base64() { assert.fail('critical-pressure upload accessed its body'); },
    }), error => error.code === 'STORAGE_PRESSURE' && error.delivery_uncertain === false);
  }
  assert.equal(line.store.seq, sequence);
  assert.deepEqual(await readdir(path.join(line.directory, 'media')), []);
  await line.auth.state.keys.set({ session: { essential: Buffer.from([8, 9]) } });
  await bridge.receive(line, { key: { id: 'INCOMING-PRESSURE', remoteJid: `${PHONE}@s.whatsapp.net`, fromMe: false },
    message: { conversation: 'Accepted incoming evidence' }, messageTimestamp: Math.floor(Date.now() / 1000) });
  await bridge.receiveReceipt(line, line.store.data.messages.ORIGINAL.message.key, 'read');
  assert.ok(line.store.data.messages['INCOMING-PRESSURE']);
  assert.equal(line.store.data.messages.ORIGINAL.receipt, 'read');
  assert.deepEqual(line.auth.store.data.keys.session.essential, Buffer.from([8, 9]));
  assert.ok(bridge.outbox.data.entries.length > 0);
});

test('queued LID edits and revokes retain verified routing after credential failure retires the socket', async t => {
  for (const type of [proto.Message.ProtocolMessage.Type.MESSAGE_EDIT, proto.Message.ProtocolMessage.Type.REVOKE]) {
    await t.test(String(type), async t => {
      const {bridge, line, socket} = await fixture(t);
      const jid = '123456789012345@lid';
      await line.store.update(data => {
        data.messages.ORIGINAL.message.key.remoteJid = jid;
        data.messages.ORIGINAL.message.key.fromMe = false;
      });
      const blocked = gate(), failed = gate();
      bridge.onError = error => failed.resolve(error);
      line.eventTail = blocked.promise;
      const queued = socket.eventHandler({'messages.upsert': {messages: [lidMutation('QUEUED-LID', 'ORIGINAL', jid, type)]}});
      const auth = line.auth, append = auth.store.append;
      auth.store.append = async () => { throw Object.assign(new Error('EIO'), {code: 'EIO'}); };
      const credentials = socket.eventHandler({'creds.update': {registered: true}});
      try {
        await failed.promise;
        assert.equal(line.socket, null);
        blocked.resolve();
        await Promise.all([queued, credentials]);
        const restored = (await readJournal(line.store.filename, () => assert.fail('Missing saved state'))).data;
        if (type === proto.Message.ProtocolMessage.Type.MESSAGE_EDIT) assert.equal(restored.messages.ORIGINAL.text, 'Preserved queued edit');
        else assert.equal(restored.messages.ORIGINAL.deleted, true);
        assert.equal(restored.pending_mutations, undefined);
        assert.ok(bridge.outbox.data.entries.some(entry => entry.event.type ===
          (type === proto.Message.ProtocolMessage.Type.MESSAGE_EDIT ? 'message_updated' : 'message_deleted_for_everyone')));
        assert.equal(socket.calls.length, 0);
      } finally {
        blocked.resolve(); await Promise.all([queued, credentials]);
        await auth.drain().catch(() => {});
        auth.store.append = append; auth.store.failed = false;
      }
    });
  }
});

test('unresolved LID mutation evidence survives restart replay until verified normalization succeeds', async t => {
  const {bridge, line, socket} = await fixture(t);
  const jid = '123456789012345@lid';
  const mutation = lidMutation('UNKNOWN-LID-REVOKE', 'MISSING-ORIGINAL', jid, proto.Message.ProtocolMessage.Type.REVOKE);
  socket.signalRepository = {lidMapping: {getPNForLID: async () => { throw Object.assign(new Error('AUTH_SESSION_RETIRED'), {code: 'AUTH_SESSION_RETIRED'}); }}};
  await bridge.receive(line, mutation);
  await bridge.receive(line, mutation);
  assert.equal(Object.keys(line.store.data.pending_mutations).length, 1, 'identical retained input is deduplicated');
  const recovered = await readJournal(line.store.filename, () => assert.fail('Missing state'));
  assert.equal(Object.values(recovered.data.pending_mutations)[0].message.key.id, mutation.key.id);
  assert.equal(bridge.outbox.data.entries.length, 0);
  socket.signalRepository.lidMapping.getPNForLID = async () => `${PHONE}@s.whatsapp.net`;
  await bridge.recoverPending();
  assert.equal(line.store.data.pending_mutations, undefined);
  assert.equal(line.store.data.messages['MISSING-ORIGINAL'].deleted, true);
  assert.equal(line.store.data.messages['MISSING-ORIGINAL'].phone, PHONE);
  assert.equal(socket.calls.length, 0);
});

test('saved target IDs cannot bind an unrelated LID or sender side to a conversation', async t => {
  const {bridge, line, socket} = await fixture(t);
  const originalJid = '111111111111111@lid', foreignJid = '222222222222222@lid';
  await line.store.update(data => { data.messages.ORIGINAL.message.key.remoteJid = originalJid; data.messages.ORIGINAL.notified = true; });
  await bridge.receive(line, lidMutation('SPOOF-LID', 'ORIGINAL', foreignJid, proto.Message.ProtocolMessage.Type.MESSAGE_EDIT, true));
  await bridge.receive(line, lidMutation('SPOOF-SIDE', 'ORIGINAL', originalJid, proto.Message.ProtocolMessage.Type.REVOKE, false));
  await bridge.receive(line, lidMutation('ORIGINAL', 'ORIGINAL', foreignJid, proto.Message.ProtocolMessage.Type.MESSAGE_EDIT, true));
  assert.equal(line.store.data.messages.ORIGINAL.text, 'Original synthetic body');
  assert.equal(line.store.data.messages.ORIGINAL.deleted, undefined);
  assert.equal(Object.keys(line.store.data.pending_mutations).length, 3);
  socket.signalRepository = {lidMapping: {getPNForLID: async jid => jid === foreignJid ? '96170999999@s.whatsapp.net' : `${PHONE}@s.whatsapp.net`}};
  await bridge.recoverPending();
  assert.equal(line.store.data.messages.ORIGINAL.text, 'Original synthetic body');
  assert.equal(line.store.data.messages.ORIGINAL.deleted, undefined);
  assert.equal(line.store.data.pending_mutations, undefined, 'verified wrong conversation/side is rejected');
  assert.equal(bridge.outbox.data.entries.length, 0);
});

test('attachment retries reuse stored bytes when pressure changes after upload', async t => {
  const { bridge, line, pressure, recover } = await fixture(t);
  const payload = { content_base64: Buffer.from('Synthetic retained attachment').toString('base64'),
    mime_type: 'application/pdf', filename: 'test.pdf' };
  const first = await bridge.uploadMedia(LINE, payload);
  const sequence = line.store.seq;
  await pressure();
  for (let attempt = 0; attempt < 3; attempt++) {
    await assert.rejects(bridge.uploadMedia(LINE, payload), { code: 'STORAGE_PRESSURE' });
  }
  await recover();
  for (let attempt = 0; attempt < 3; attempt++) assert.deepEqual(await bridge.uploadMedia(LINE, payload), first);
  assert.equal(line.store.seq, sequence, 'reusing an attachment does not create a second metadata record');
  assert.equal((await readdir(path.join(line.directory, 'media'))).length, 1);
  assert.deepEqual(Object.keys(line.store.data.media), [first.id]);
});

test('a pending send protects a shared attachment while the previous message is deleted', async t => {
  const {bridge, line, socket} = await fixture(t);
  const bytes = Buffer.from('Shared attachment retained by an admitted send');
  const media = await bridge.uploadMedia(LINE, {content_base64: bytes.toString('base64'),
    mime_type: 'application/pdf', filename: 'shared.pdf'});
  await line.store.update(data => {data.messages.ORIGINAL.media_id = media.id;});
  const entered = gate(), release = gate(), send = socket.sendMessage;
  let pendingId;
  socket.sendMessage = async (...args) => {
    pendingId = args[2].messageId;
    entered.resolve();
    await release.promise;
    return send(...args);
  };
  const sending = bridge.send(LINE, {to: PHONE, type: 'document', document: {id: media.id},
    client_message_id: 'shared-media-pending-send'});
  try {
    await entered.promise;
    assert.equal(line.store.data.messages[pendingId].pending_send, true);
    assert.equal(line.store.data.messages[pendingId].media_id, media.id);
    await line.store.update(data => {data.messages.ORIGINAL.deleted = true;});
    await bridge.purgeDeletedContent(line, wrapId(LINE, 'ORIGINAL'));
    assert.equal(line.store.data.messages.ORIGINAL.media_id, undefined);
    assert.ok(line.store.data.media[media.id]);
    assert.deepEqual((await bridge.downloadMedia(LINE, media.id)).content, bytes);
  } finally {release.resolve();}
  const result = await sending;
  assert.equal(result.messages[0].id, wrapId(LINE, pendingId));
  assert.equal(socket.calls.length, 1);
  assert.equal(line.store.data.messages[pendingId].media_id, media.id);
  assert.deepEqual((await bridge.downloadMedia(LINE, media.id)).content, bytes);
  const restored = await readJournal(path.join(line.directory, 'state.json'), () => assert.fail('Missing durable state'));
  assert.equal(restored.data.messages[pendingId].media_id, media.id);
  assert.ok(restored.data.media[media.id]);
});

test('deletion between attachment reading and send reservation prevents provider submission', async t => {
  const {bridge, line, socket} = await fixture(t);
  const media = await bridge.uploadMedia(LINE, {content_base64: Buffer.from('Removed before reservation').toString('base64'),
    mime_type: 'application/pdf', filename: 'removed.pdf'});
  await line.store.update(data => {data.messages.ORIGINAL.media_id = media.id;});
  const loaded = gate(), release = gate(), download = bridge.downloadMedia.bind(bridge);
  bridge.downloadMedia = async (...args) => {
    const result = await download(...args);
    loaded.resolve();
    await release.promise;
    return result;
  };
  const sending = bridge.send(LINE, {to: PHONE, type: 'document', document: {id: media.id},
    client_message_id: 'shared-media-deleted-before-reservation'}).then(
      () => assert.fail('Removed attachment must not be submitted'), error => error);
  try {
    await loaded.promise;
    await line.store.update(data => {data.messages.ORIGINAL.deleted = true;});
    await bridge.purgeDeletedContent(line, wrapId(LINE, 'ORIGINAL'));
    assert.equal(line.store.data.media[media.id], undefined);
  } finally {release.resolve();}
  const error = await sending;
  assert.equal(error.code, 'UNKNOWN_MEDIA');
  assert.equal(error.delivery_uncertain, false);
  assert.equal(socket.calls.length, 0);
  assert.deepEqual(Object.keys(line.store.data.messages), ['ORIGINAL']);
  assert.deepEqual(await readdir(path.join(line.directory, 'media')), []);
  const request = Object.values(line.store.data.send_requests)[0];
  assert.equal(request.native_id, null);
  assert.equal(request.retry_safe, true);
});

test('identical reupload waits for an admitted deletion and restores its bytes', async t => {
  const { bridge, line } = await fixture(t);
  const payload = { content_base64: Buffer.from('Reuploaded attachment').toString('base64'), mime_type: 'application/pdf', filename: 'same.pdf' };
  const first = await bridge.uploadMedia(LINE, payload);
  await line.store.update(data => {
    delete data.media[first.id];
    data.pending_media_deletions = [first.id];
  });
  let release;
  line.mediaTail = new Promise(resolve => { release = resolve; });
  const deletion = bridge.removePendingMedia(line);
  const upload = bridge.uploadMedia(LINE, payload);
  await bridge.storageMonitor.sampling;
  await new Promise(resolve => setImmediate(resolve));
  release();
  await deletion;
  assert.deepEqual(await upload, first);
  assert.equal((await bridge.downloadMedia(LINE, first.id)).content.toString(), 'Reuploaded attachment');
  assert.deepEqual(line.store.data.pending_media_deletions, []);
});

test('reviving a media identity cancels stale deletion and repairs a missing file', async t => {
  const { bridge, line } = await fixture(t);
  const payload = { content_base64: Buffer.from('Retained retry bytes').toString('base64'), mime_type: 'application/pdf', filename: 'same.pdf' };
  const first = await bridge.uploadMedia(LINE, payload);
  await line.store.update(data => {
    delete data.media[first.id];
    data.pending_media_deletions = [first.id];
  });
  assert.deepEqual(await bridge.uploadMedia(LINE, payload), first);
  await bridge.removePendingMedia(line);
  assert.equal((await bridge.downloadMedia(LINE, first.id)).content.toString(), 'Retained retry bytes');
  await unlink(bridge.mediaPath(line, first.id));
  assert.deepEqual(await bridge.uploadMedia(LINE, payload), first);
  assert.equal((await bridge.downloadMedia(LINE, first.id)).content.toString(), 'Retained retry bytes');
  assert.equal((await readdir(path.join(line.directory, 'media'))).length, 1);
});

test('HTTP upload pressure is checked before JSON body decoding', async t => {
  const { bridge, line, config, pressure } = await fixture(t);
  await pressure();
  const server = createServer(bridge, config);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/v1/lines/${LINE}/media`, {
    method: 'POST', headers: { authorization: `Bearer ${SECRET}`, 'content-type': 'application/json' },
    body: 'this is deliberately invalid JSON',
  });
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error.code, 'STORAGE_PRESSURE');
  assert.deepEqual(await readdir(path.join(line.directory, 'media')), []);
});

test('critical storage pressure rejects new sends and mutations before reservation while evidence still commits', async t => {
  const { bridge, line, socket, pressure } = await fixture(t);
  await pressure();
  const sequence = line.store.seq;
  await assert.rejects(bridge.send(LINE, sendPayload('pressure-new-send')),
    error => error.code === 'STORAGE_PRESSURE' && error.delivery_uncertain === false);
  await assert.rejects(bridge.mutate(LINE, wrapId(LINE, 'ORIGINAL'), 'edit', {
    to: PHONE, body: 'Blocked edit', operation_id: 'pressure-new-edit',
  }), error => error.code === 'STORAGE_PRESSURE' && error.delivery_uncertain === false);
  assert.equal(socket.calls.length, 0);
  assert.equal(line.store.seq, sequence);
  assert.deepEqual(line.store.data.send_requests, {});
  assert.deepEqual(line.store.data.operations, {});
  await socket.eventHandler({ 'creds.update': { registered: true } });
  await line.auth.state.keys.set({ session: { evidence: Buffer.from([1, 2]) } });
  await bridge.receiveReceipt(line, line.store.data.messages.ORIGINAL.message.key, 'read');
  assert.equal(line.auth.store.data.creds.registered, true);
  assert.deepEqual(line.auth.store.data.keys.session.evidence, Buffer.from([1, 2]));
  assert.equal(line.store.data.messages.ORIGINAL.receipt, 'read');
  assert.ok(bridge.outbox.data.entries.length > 0);
  assert.equal(socket.calls.length, 0);
});

test('confirmed retries return durable results under pressure and uncertain retries keep their uncertainty', async t => {
  const { bridge, line, socket, pressure } = await fixture(t);
  const payload = sendPayload('pressure-confirmed-send');
  const sent = await bridge.send(LINE, payload);
  const change = { to: PHONE, body: 'Confirmed synthetic edit', operation_id: 'pressure-confirmed-edit' };
  const edited = await bridge.mutate(LINE, wrapId(LINE, 'ORIGINAL'), 'edit', change);
  await line.ackTail;
  await Promise.allSettled(line.confirmationTasks.values());
  const uncertainPayload = sendPayload('pressure-uncertain-send');
  const uncertainKey = createHash('sha256').update(uncertainPayload.client_message_id).digest('hex');
  const operationId = 'pressure-uncertain-edit';
  const uncertainBody = 'Still uncertain';
  const fingerprint = createHash('sha256').update(JSON.stringify([LINE, 'ORIGINAL', 'edit', PHONE, uncertainBody])).digest('hex');
  await line.store.update(data => {
    data.messages.UNCERTAIN = { ...data.messages.ORIGINAL, client_message_id: uncertainPayload.client_message_id,
      pending_send: false, delivery_uncertain: true, ack_confirmed: false, echo_seen: false };
    data.send_requests[uncertainKey] = { native_id: 'UNCERTAIN', fingerprint: bridge.sendFingerprint(line, uncertainPayload) };
    data.operations[operationId] = { fingerprint, status: 'uncertain', uncertainty_seen: true, provider_id: 'SAVED-NATIVE-ID' };
  });
  await pressure();
  const calls = socket.calls.length, sequence = line.store.seq;
  assert.deepEqual(await bridge.send(LINE, payload), sent);
  assert.deepEqual(await bridge.mutate(LINE, wrapId(LINE, 'ORIGINAL'), 'edit', change), edited);
  await assert.rejects(bridge.send(LINE, uncertainPayload), error => error.code === 'DELIVERY_UNCERTAIN' && error.delivery_uncertain === true);
  await assert.rejects(bridge.mutate(LINE, wrapId(LINE, 'ORIGINAL'), 'edit', {
    to: PHONE, body: uncertainBody, operation_id: operationId,
  }), error => error.code === 'STORAGE_PRESSURE' && error.delivery_uncertain === true);
  assert.equal(socket.calls.length, calls);
  assert.equal(line.store.seq, sequence);
  assert.equal(line.store.data.operations[operationId].provider_id, 'SAVED-NATIVE-ID');
  assert.equal(line.store.data.operations[operationId].status, 'uncertain');
  assert.equal(bridge.deliveryStatus(LINE, { client_message_id: payload.client_message_id, phone: PHONE }).status, 'confirmed');
});

test('real bridge storage status stays authenticated and excludes credentials during critical pressure', async t => {
  const { bridge, line, socket, config, dataDir, pressure } = await fixture(t);
  line.auth.state.creds.synthetic_private = 'PRIVATE-AUTH-CREDENTIAL-MARKER';
  await line.auth.saveCreds();
  await pressure();
  const server = createServer(bridge, config);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => { server.close(resolve); server.closeAllConnections(); }));
  const base = `http://127.0.0.1:${server.address().port}`;
  const headers = { authorization: `Bearer ${SECRET}`, 'content-type': 'application/json' };
  assert.equal((await fetch(base + '/v1/storage')).status, 401);
  const response = await fetch(base + '/v1/storage', { headers });
  assert.equal(response.status, 200);
  const body = await response.text();
  assert.equal(JSON.parse(body).level, 'critical');
  for (const privateValue of [SECRET, dataDir, 'PRIVATE-AUTH-CREDENTIAL-MARKER', 'noiseKey', 'signedIdentityKey']) {
    assert.equal(body.includes(privateValue), false);
  }
  const blocked = await fetch(base + `/v1/lines/${LINE}/messages`, { method: 'POST', headers,
    body: JSON.stringify(sendPayload('http-pressure-send')) });
  assert.equal(blocked.status, 503);
  const error = await blocked.json();
  assert.equal(error.error.code, 'STORAGE_PRESSURE');
  assert.equal(error.delivery_uncertain, false);
  assert.equal(socket.calls.length, 0);
});
