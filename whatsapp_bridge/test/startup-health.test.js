import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm, readFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Bridge} from '../src/bridge.js';
import {createRuntime} from '../src/runtime-lifecycle.js';
import {createServer} from '../src/server.js';
import {acquireDataDirectory} from '../src/storage.js';
import {readJournal} from '../src/journal-store.js';

const gate = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
const healthy = () => ({bsize: 4096, blocks: 10_000_000, bavail: 5_000_000, files: 1_000_000, ffree: 900_000});
const secret = 'synthetic-health-secret-that-never-leaves-loopback';
const configuration = dataDir => ({dataDir, lines: [], host: '127.0.0.1', port: 0, secret,
  storage: {maintenance: {enabled: false}}});

test('HTTP liveness precedes slow replay while readiness and all operations remain gated', async t => {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), 'bridge-startup-health-'));
  const entered = gate(), release = gate();
  let server;
  class SlowBridge extends Bridge {
    async loadStore(...args) {entered.resolve(); await release.promise; return super.loadStore(...args);}
  }
  const runtime = createRuntime({logger: {info() {}, error() {}}, prepare: async () => ({
    config: configuration(dataDir), acquireDataDirectory,
    createBridge: config => new SlowBridge(config, {storageFilesystem: async () => healthy(), log() {},
      makeSocket() {throw new Error('UNEXPECTED_PROVIDER_CONNECTION');}}),
    createServer: (bridge, config) => server = createServer(bridge, config),
  })});
  t.after(async () => {release.resolve(); await runtime.stop(); await rm(dataDir, {recursive: true, force: true});});
  const starting = runtime.start();
  await entered.promise;
  const endpoint = `http://127.0.0.1:${server.address().port}`;
  const headers = {authorization: `Bearer ${secret}`, 'content-type': 'application/json'};
  assert.equal((await fetch(endpoint + '/v1/health/live')).status, 401);
  const live = await fetch(endpoint + '/v1/health/live', {headers});
  assert.equal(live.status, 200);
  assert.equal((await live.json()).phase, 'storage_replay');
  const readiness = await fetch(endpoint + '/v1/health/ready', {headers});
  assert.equal(readiness.status, 503);
  assert.equal((await readiness.json()).ready, false);
  for (const [method, route] of [['GET', '/v1/status'], ['POST', '/v1/lines/90001/messages'], ['DELETE', '/v1/lines/90001']]) {
    const response = await fetch(endpoint + route, {method, headers, ...(method === 'POST' ? {body: '{}'} : {})});
    assert.equal(response.status, 503);
    const result = await response.json();
    assert.equal(result.error.code, 'BRIDGE_INITIALIZING');
    assert.equal(result.delivery_uncertain, false);
  }
  release.resolve(); await starting;
  const ready = await fetch(endpoint + '/v1/health/ready', {headers});
  assert.equal(ready.status, 200);
  assert.equal((await ready.json()).phase, 'ready');
  assert.equal((await runtime.stop()).exitCode, 0);
});

test('capacity-only pressure recovers automatically but a failed shared journal stays fenced', async t => {
  const dataDir = await mkdtemp(path.join(os.tmpdir(), 'bridge-storage-health-'));
  let disk = healthy();
  const bridge = await new Bridge(configuration(dataDir), {log() {}, storageFilesystem: async () => disk,
    makeSocket() {throw new Error('UNEXPECTED_PROVIDER_CONNECTION');}}).initialize();
  t.after(async () => {await bridge.close().catch(() => {}); await rm(dataDir, {recursive: true, force: true});});
  assert.equal(bridge.runtimeHealth().ready, true);
  disk = {...healthy(), bavail: 1};
  await bridge.storageMonitor.sample();
  assert.equal(bridge.runtimeHealth().recovery_mode, 'automatic_after_capacity_recovers');
  assert.equal(bridge.runtimeHealth().ready, false);
  disk = healthy();
  await bridge.storageMonitor.sample();
  assert.equal(bridge.runtimeHealth().ready, true);
  await bridge.outbox.update(data => {data.entries.push({event: {event_id: 'already-durable', line_id: '90001'}});});
  const journalLimit = bridge.storageMonitor.config.journalCriticalBytes;
  bridge.storageMonitor.config.journalCriticalBytes = 1;
  assert.equal(bridge.runtimeHealth().recovery_mode, 'operator_attention', 'journal growth is not solely a free-capacity problem');
  bridge.storageMonitor.config.journalCriticalBytes = journalLimit;
  const before = await readFile(bridge.outbox.filename + '.journal');
  bridge.outbox.append = async () => {throw Object.assign(new Error('private filesystem detail'), {code: 'ENOSPC'});};
  await assert.rejects(bridge.outbox.update(data => {data.entries.push({event: {event_id: 'not-committed'}});}), {code: 'ENOSPC'});
  await bridge.storageMonitor.sample();
  assert.equal(bridge.runtimeHealth().recovery_mode, 'review_required');
  assert.deepEqual(bridge.runtimeHealth().recovery_required, [{line_id: null, kind: 'outbox', code: 'STORAGE_RECOVERY_REQUIRED'}]);
  await assert.rejects(bridge.outbox.update(data => {data.entries = [];}), /STORAGE_RESTART_REQUIRED/);
  assert.deepEqual(await readFile(bridge.outbox.filename + '.journal'), before);
  const restored = await readJournal(bridge.outbox.filename, () => ({}));
  assert.deepEqual(restored.data.entries.map(entry => entry.event.event_id), ['already-durable']);
  assert.equal(bridge.runtimeHealth().ready, false, 'free disk must not erase ambiguous-write recovery fencing');
});
