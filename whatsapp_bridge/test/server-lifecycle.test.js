import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {once} from 'node:events';
import {createServer} from '../src/server.js';
import {BridgeError} from '../src/config.js';

const gate = () => { let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve}; };
const secret = 'isolated-http-lifecycle-secret';

test('HTTP shutdown drains accepted work after its requesting client disconnects', async t => {
  const entered = gate(), release = gate(), finished = [];
  const bridge = {getLine() {}, async downloadMedia() {
    entered.resolve(); await release.promise; finished.push('media');
    return {mime_type: 'application/octet-stream', content: Buffer.from('synthetic')};
  }};
  const server = createServer(bridge, {secret});
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(async () => { release.resolve(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await server.drainRequests(); });
  const request = http.get({host: '127.0.0.1', port: server.address().port,
    path: '/v1/lines/90001/media/synthetic', headers: {authorization: `Bearer ${secret}`}});
  request.on('error', () => {});
  await entered.promise;
  const clientClosed = new Promise(resolve => request.once('close', resolve));
  request.destroy(); await clientClosed;
  await new Promise(resolve => server.close(resolve));
  let drained = false;
  const draining = server.drainRequests().then(() => {drained = true;});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(drained, false, 'connection closure must not relinquish asynchronous handler ownership');
  assert.deepEqual(finished, []);
  release.resolve(); await draining;
  assert.deepEqual(finished, ['media']);
  await server.drainRequests();
});

test('handled HTTP business errors do not make a completed request drain fail', async t => {
  const server = createServer({getLine() {throw new BridgeError(404, 'UNKNOWN_LINE', 'Synthetic line is absent.');}}, {secret});
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(async () => {server.closeAllConnections(); await new Promise(resolve => server.close(resolve));});
  const response = await fetch(`http://127.0.0.1:${server.address().port}/v1/lines/90001/media/synthetic`,
    {headers: {authorization: `Bearer ${secret}`}});
  assert.equal(response.status, 404);
  assert.equal((await response.json()).error.code, 'UNKNOWN_LINE');
  await server.drainRequests();
});

test('unexpected escaping handler failures remain visible to every shutdown drain', async t => {
  t.mock.method(console, 'error', () => {});
  const server = createServer({}, {secret});
  let destroyed = false;
  server.emit('request', {}, {
    setHeader() {throw new Error('SYNTHETIC_RESPONSE_FAILURE');},
    destroy() {destroyed = true;},
  });
  await assert.rejects(server.drainRequests(), /HTTP_REQUEST_DRAIN_FAILED/);
  assert.equal(destroyed, true);
  await assert.rejects(server.drainRequests(), /HTTP_REQUEST_DRAIN_FAILED/);
});

test('concurrent media admission rejects before buffering and releases the slot after completion', async t => {
  const entered = gate(), release = gate();
  let uploads = 0;
  const server = createServer({getLine() {}, async uploadMedia() {
    uploads++; entered.resolve(); await release.promise; return {id: 'synthetic-upload'};
  }}, {secret, maxMediaBytes: 192 * 1024 * 1024});
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(async () => {release.resolve(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await server.drainRequests();});
  const endpoint = `http://127.0.0.1:${server.address().port}/v1/lines/90001/media`;
  const headers = {authorization: `Bearer ${secret}`, 'content-type': 'application/json'};
  const first = fetch(endpoint, {method: 'POST', headers, body: '{}'});
  await entered.promise;
  const rejected = await fetch(endpoint.replace('90001', '90002'), {method: 'POST', headers, body: '{}'});
  assert.equal(rejected.status, 503);
  const result = await rejected.json();
  assert.equal(result.error.code, 'MEDIA_UPLOAD_BUSY');
  assert.equal(result.delivery_uncertain, false);
  assert.equal(uploads, 1, 'another office cannot allocate a second whole-media request');
  release.resolve();
  assert.equal((await first).status, 200);
  assert.equal((await fetch(endpoint, {method: 'POST', headers, body: '{}'})).status, 200);
  assert.equal(uploads, 2);
});
