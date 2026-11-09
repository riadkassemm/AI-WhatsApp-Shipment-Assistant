import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm, readFile, writeFile} from 'node:fs/promises';
import {Readable} from 'node:stream';
import os from 'node:os';
import path from 'node:path';
import {EventEmitter} from 'node:events';
import {Bridge} from '../src/bridge.js';
import {JournalStore} from '../src/journal-store.js';

const gate = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const healthyStorage = async () => ({bsize: 4096, blocks: 10_000_000, bavail: 5_000_000, files: 1_000_000, ffree: 900_000});
const socket = () => ({ws: new EventEmitter(), ev: {process: () => () => {}}, end() {}});

async function setup(t, count = 2, Type = Bridge) {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), 'bridge-initialization-'));
  const config = {dataDir, lines: Array.from({length: count}, (_, index) => ({line_id: String(90001 + index), expected_phone: ''})),
    storage: {maintenance: {enabled: false}}, ackTimeoutMs: 25, maxMediaBytes: 1024 * 1024};
  const errors = [], sockets = [];
  const dependencies = {log() {}, onError: error => errors.push(error), storageFilesystem: healthyStorage,
    makeSocket: () => { const created = socket(); sockets.push(created); return created; }};
  const seed = await new Bridge(config, dependencies).initialize({autoConnect: false});
  for (const line of seed.lines.values()) await line.store.update(data => { data.enabled = true; });
  await seed.close();
  const bridge = new Type(config, dependencies);
  t.after(async () => {
    try { if (!bridge.closed) await bridge.close(); }
    finally { await rm(dataDir, {recursive: true, force: true}); }
  });
  return {bridge, sockets, errors, config};
}

test('initialization prepares every configured store before opening any provider socket', async t => {
  const prepared = [];
  class OrderedBridge extends Bridge {
    async prepareLine(line) { await super.prepareLine(line); prepared.push(line.line_id); }
    async connect(id) {
      assert.equal(prepared.length, 8);
      assert.equal(this.ownedStores.size, 17);
      return super.connect(id);
    }
  }
  const {bridge, sockets} = await setup(t, 8, OrderedBridge);
  const initialization = bridge.initialize();
  assert.equal(bridge.initialize(), initialization);
  await initialization;
  assert.equal(sockets.length, 8);
  await bridge.close();
  for (const line of bridge.lines.values()) {
    await assert.rejects(readFile(line.auth.store.filename + '.auth-active'), {code: 'ENOENT'});
  }
});

test('shutdown during a later store load owns the unassigned store and prevents every socket', async t => {
  const entered = gate(), release = gate();
  let loadingStore;
  class LoadingBridge extends Bridge {
    async loadStore(filename, initial, kind, line) {
      const store = await super.loadStore(filename, initial, kind, line);
      if (line?.line_id === '90002' && kind === 'state') {
        loadingStore = store; entered.resolve(); await release.promise;
      }
      return store;
    }
  }
  const {bridge, sockets} = await setup(t, 2, LoadingBridge);
  const initializing = bridge.initialize();
  const cancelled = assert.rejects(initializing, {code: 'STOPPING'});
  await entered.promise;
  let closed = false;
  const closing = bridge.close().then(() => { closed = true; });
  assert.equal(bridge.stopping, true);
  assert.equal(bridge.initializationController.signal.aborted, true);
  assert.equal(bridge.getLine('90002').store, undefined);
  assert.equal(loadingStore.maintenanceStopped, true);
  await Promise.resolve();
  assert.equal(closed, false);
  release.resolve();
  await Promise.all([cancelled, closing]);
  assert.equal(sockets.length, 0);
  assert.equal(bridge.storageMonitor.closed, true);
  assert.equal(loadingStore.closed, true);
  assert.ok([...bridge.ownedStores].every(store => store.closed && store.maintenanceStopped));
  await assert.rejects(bridge.connect('90001'), {code: 'STOPPING'});
  bridge.startWorkers();
  assert.equal(bridge.workerTimer, undefined);
});

test('an escaping initialization failure retains every partial owner for explicit cleanup', async t => {
  class RejectingBridge extends Bridge {
    async loadFile(filename, initial) {
      const store = await super.loadFile(filename, initial);
      if (path.basename(filename) === 'retired-phones.json') throw new Error('SYNTHETIC_INITIALIZATION_FAILURE');
      return store;
    }
  }
  const {bridge, sockets} = await setup(t, 2, RejectingBridge);
  await assert.rejects(bridge.initialize(), /SYNTHETIC_INITIALIZATION_FAILURE/);
  assert.equal(bridge.ownedFiles.size, 2);
  assert.equal(bridge.retiredPhones, undefined);
  const files = [...bridge.ownedFiles];
  let flushed = 0;
  for (const file of files) { const flush = file.flush.bind(file); file.flush = async () => { await flush(); flushed++; }; }
  await bridge.close();
  assert.equal(flushed, 2);
  assert.equal(sockets.length, 0);
  assert.ok([...bridge.ownedStores].every(store => store.closed));
});

test('shutdown waits for a facade created by an already admitted auth renewal', async t => {
  const {bridge, sockets} = await setup(t);
  await bridge.initialize({autoConnect: false});
  const line = bridge.getLine('90001'), entered = gate(), release = gate();
  const renew = line.auth.renew.bind(line.auth);
  let renewed;
  line.auth.renew = async () => {
    renewed = await renew(); entered.resolve(); await release.promise; return renewed;
  };
  const connecting = bridge.connect(line.line_id);
  await entered.promise;
  const closing = bridge.close();
  release.resolve();
  await Promise.all([connecting, closing]);
  assert.equal(sockets.length, 0);
  await assert.rejects(renewed.state.keys.set({session: {late: Buffer.from([1])}}), {code: 'AUTH_SESSION_RETIRED'});
  assert.equal(renewed.store.closed, true);
  await assert.rejects(readFile(renewed.store.filename + '.auth-active'), {code: 'ENOENT'});
});

test('repeated close calls preserve a cleanup failure instead of reporting later success', async t => {
  const {bridge} = await setup(t);
  await bridge.initialize({autoConnect: false});
  const partial = new JournalStore(path.join(bridge.config.dataDir, 'unassigned.json'), () => ({}));
  bridge.ownedStores.add(partial);
  partial.failed = true;
  const closing = bridge.close();
  assert.equal(bridge.close(), closing);
  await assert.rejects(closing, /BRIDGE_SHUTDOWN_FAILED/);
  assert.equal(partial.closed, true);
  assert.equal(bridge.close(), closing);
  await assert.rejects(bridge.close(), /BRIDGE_SHUTDOWN_FAILED/);
});

test('shutdown waits for an accepted recovery job before closing its evidence stores', async t => {
  const {bridge} = await setup(t);
  await bridge.initialize({autoConnect: false});
  const line = bridge.getLine('90001'), entered = gate(), release = gate();
  bridge.recoverPendingOnce = async () => {
    entered.resolve(); await release.promise;
    await line.store.update(data => { data.recovery_test_evidence = 'committed'; });
  };
  const recovering = bridge.recoverPending();
  await entered.promise;
  const closing = bridge.close();
  assert.equal(line.store.closed, false);
  release.resolve();
  await Promise.all([recovering, closing]);
  assert.equal(line.store.data.recovery_test_evidence, 'committed');
  assert.equal(line.store.closed, true);
});

test('closed registration owners do not accumulate in the lifecycle ownership registry', async t => {
  const {bridge} = await setup(t);
  await bridge.initialize({autoConnect: false});
  const count = bridge.ownedStores.size;
  for (let attempt = 0; attempt < 8; attempt++) {
    const store = await bridge.loadStore(path.join(bridge.config.dataDir, `temporary-${attempt}.json`), () => ({}), 'outbox');
    assert.equal(bridge.ownedStores.size, count + 1);
    await bridge.closeStore(store);
    assert.equal(bridge.ownedStores.size, count);
  }
  await bridge.close();
  assert.equal(bridge.ownedStores.size, 0);
});

test('repeated failed loads retain one journal owner and require recovery before retry', async t => {
  const {bridge} = await setup(t);
  await bridge.initialize({autoConnect: false});
  const filename = path.join(bridge.config.dataDir, 'failed-load.json');
  await writeFile(filename, 'invalid synthetic storage', {mode: 0o600});
  const count = bridge.ownedStores.size;
  await assert.rejects(bridge.loadStore(filename, () => ({}), 'outbox'));
  assert.equal(bridge.ownedStores.size, count + 1);
  const failed = [...bridge.ownedStores].find(store => store.filename === filename);
  assert.equal(failed.failed, true);
  for (let attempt = 0; attempt < 8; attempt++) {
    await assert.rejects(bridge.loadStore(filename, () => ({}), 'outbox'), {code: 'STORAGE_RESTART_REQUIRED'});
    assert.equal(bridge.ownedStores.size, count + 1);
  }
  await assert.rejects(bridge.close(), /BRIDGE_SHUTDOWN_FAILED/);
  assert.equal(failed.closed, true);
});

test('disconnect requests arriving after shutdown cannot unlink or reset credentials', async t => {
  const {bridge} = await setup(t);
  await bridge.initialize({autoConnect: false});
  const line = bridge.getLine('90001');
  const identity = Buffer.from(line.auth.store.data.creds.signedIdentityKey.public);
  await bridge.close();
  await assert.rejects(bridge.disconnect(line.line_id), {code: 'STOPPING'});
  assert.equal(line.store.data.enabled, true);
  assert.deepEqual(line.auth.store.data.creds.signedIdentityKey.public, identity);
});

test('shutdown retains stores through an already admitted explicit disconnect', async t => {
  const {bridge} = await setup(t);
  await bridge.initialize({autoConnect: false});
  await bridge.connect('90001');
  const line = bridge.getLine('90001'), entered = gate(), release = gate();
  line.socket.logout = async () => {entered.resolve(); await release.promise;};
  const disconnecting = bridge.disconnect(line.line_id);
  await entered.promise;
  const closing = bridge.close();
  assert.equal(line.store.closed, false);
  assert.equal(line.auth.store.closed, false);
  release.resolve();
  await Promise.all([disconnecting, closing]);
  assert.equal(line.store.data.enabled, false);
  assert.equal(line.store.data.bound_phone, '');
  assert.equal(line.store.closed, true);
  await assert.rejects(line.auth.state.keys.set({session: {late: Buffer.from([1])}}), {code: 'AUTH_SESSION_RETIRED'});
});

async function mediaFixture(t) {
  const {bridge} = await setup(t);
  await bridge.initialize({autoConnect: false});
  await bridge.connect('90001');
  const line = bridge.getLine('90001');
  line.socket.ws.isOpen = true; line.session.ready = true; line.status = 'connected';
  const mediaId = 'ldmedia:90001:11111111-1111-1111-1111-111111111111';
  await line.store.update(data => {data.media[mediaId] = {mime_type: 'application/octet-stream', downloaded: false,
    message: {message: {documentMessage: {url: 'https://a.whatsapp.net/synthetic'}}}};});
  return {bridge, line, mediaId};
}

test('a download completing after shutdown cannot publish media or mark it downloaded', async t => {
  const {bridge, line, mediaId} = await mediaFixture(t);
  const entered = gate(), release = gate();
  bridge.downloadMediaMessage = async () => {entered.resolve(); await release.promise; return Readable.from([Buffer.from('synthetic')]);};
  const downloading = bridge.downloadMedia(line.line_id, mediaId);
  const rejected = assert.rejects(downloading, {code: 'STOPPING'});
  await entered.promise;
  await bridge.close();
  release.resolve(); await rejected;
  assert.equal(line.store.data.media[mediaId].downloaded, false);
  await assert.rejects(readFile(bridge.mediaPath(line, mediaId)), {code: 'ENOENT'});
});

test('shutdown drains media publication that began before retirement', async t => {
  const {bridge, line, mediaId} = await mediaFixture(t);
  bridge.downloadMediaMessage = async () => Readable.from([Buffer.from('synthetic')]);
  const entered = gate(), release = gate(), writeBytes = bridge.writeBytes.bind(bridge);
  bridge.writeBytes = async (...args) => {entered.resolve(); await release.promise; return writeBytes(...args);};
  const downloading = bridge.downloadMedia(line.line_id, mediaId);
  await entered.promise;
  const closing = bridge.close();
  assert.equal(line.store.closed, false);
  release.resolve();
  const [media] = await Promise.all([downloading, closing]);
  assert.equal(media.content.toString(), 'synthetic');
  assert.equal(line.store.data.media[mediaId].downloaded, true);
  assert.equal(line.store.closed, true);
});

test('shutdown prevents a failed optional media download from starting its fallback request', async t => {
  const {bridge, line, mediaId} = await mediaFixture(t);
  const entered = gate(), release = gate();
  let attempts = 0;
  bridge.downloadMediaMessage = async () => {attempts++; entered.resolve(); await release.promise; throw new Error('SYNTHETIC_NETWORK_FAILURE');};
  const downloading = bridge.downloadMedia(line.line_id, mediaId);
  const rejected = assert.rejects(downloading, {code: 'STOPPING'});
  await entered.promise; await bridge.close(); release.resolve(); await rejected;
  assert.equal(attempts, 1);
  assert.equal(line.store.data.media[mediaId].downloaded, false);
});
