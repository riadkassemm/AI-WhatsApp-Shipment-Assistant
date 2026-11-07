import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { closeTransport, observeTransport, transportSnapshot } from '../src/transport-diagnostics.js';

function fixture() {
  const raw = Object.assign(new EventEmitter(), {readyState: 1, _socket: {
    remoteAddress: '2001:db8::1', remoteFamily: 'IPv6', localPort: 34000, remotePort: 443,
    bytesRead: 50, bytesWritten: 60, writableLength: 0, secret: 'private-session',
  }});
  let terminations = 0, closes = 0;
  raw.terminate = () => { terminations++; };
  const ws = {socket: raw, get isOpen() { return raw.readyState === 1; },
    get isClosed() { return raw.readyState === 3; }, get isClosing() { return raw.readyState === 2; },
    close: async () => { closes++; raw.readyState = 2; },
  };
  const closed = () => { raw.readyState = 3; raw.emit('close', 1006, Buffer.from('private-reason')); };
  return {raw, ws, closed, counts: () => ({terminations, closes})};
}

test('failed heartbeat has bounded close grace but never treats terminate as physical closure', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const {raw, ws, closed, counts} = fixture();
  let done = false; const events = [];
  const task = closeTransport(ws, {message: 'Connection was lost', output: {statusCode: 408}}, x => events.push(x))
    .then(() => { done = true; });
  await Promise.resolve();
  t.mock.timers.tick(1499); assert.equal(counts().terminations, 0);
  t.mock.timers.tick(1); assert.equal(counts().terminations, 1);
  await Promise.resolve(); assert.equal(done, false, 'replacement remains fenced until actual close');
  closed(); await task;
  assert.equal(done, true); assert.equal(raw.listenerCount('close'), 0);
  assert.equal(events.at(-1).event, 'failed_transport_terminate');
});

test('query timeouts, healthy shutdown and terminal auth failures never use accelerated termination', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  for (const error of [new Error('Shutdown'), {message: 'Timed Out', output: {statusCode: 408}},
    {message: 'Connection Failure', output: {statusCode: 401}}, {output: {statusCode: 440}}]) {
    const {ws, closed, counts} = fixture();
    const task = closeTransport(ws, error);
    await Promise.resolve(); t.mock.timers.tick(60000);
    assert.deepEqual(counts(), {terminations: 0, closes: 1});
    closed(); await task;
  }
});

test('passive transport metadata excludes errors, close reasons and session payloads, then removes listeners', () => {
  const {raw, ws, closed} = fixture(), events = [];
  observeTransport(ws, x => events.push(x));
  raw.emit('error', {code: 'ECONNRESET', message: 'private-message', key: 'private-key'});
  raw.emit('error', {code: 'private-code'});
  closed();
  assert.equal(events[0].transport.family, 'IPv6');
  assert.equal(events[1].error_code, 'ECONNRESET');
  assert.equal(events[2].error_code, 'OTHER');
  assert.equal(events[3].close_code, 1006);
  assert.equal(raw.listenerCount('error'), 0);
  assert.equal(raw.listenerCount('close'), 0);
  assert.doesNotMatch(JSON.stringify(events), /private/);
  ws.socket._socket.remoteAddress = 'not-an-ip';
  assert.equal(transportSnapshot(ws).remote_ip, null);
});

test('an already closing failed transport has one bounded grace and close rejection cannot confirm ownership release', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  const f = fixture(); f.raw.readyState = 2;
  let done = false;
  const task = closeTransport(f.ws, {message: 'Connection was lost', output: {statusCode: 408}}).then(() => { done = true; });
  t.mock.timers.tick(1500); await Promise.resolve();
  assert.deepEqual(f.counts(), {terminations: 1, closes: 0});
  assert.equal(done, false);
  f.closed(); await task;
  const rejected = fixture(); rejected.ws.close = async () => {throw new Error('private-error');};
  await assert.rejects(closeTransport(rejected.ws, new Error('Shutdown')), /TRANSPORT_CLOSE_NOT_CONFIRMED/);
  assert.equal(rejected.raw.readyState, 1, 'no forced termination of a healthy socket');
  assert.equal(rejected.raw.listenerCount('close'), 0);
});

test('passive underlying socket errors survive ws cleanup without consuming data or affecting other observers', () => {
  const {raw, ws, closed} = fixture(), events = [], provider = [];
  raw._socket = Object.assign(new EventEmitter(), raw._socket, {authorized: true});
  const tcp = raw._socket;
  tcp.on('error', () => provider.push('error'));
  tcp.on('end', () => provider.push('end'));
  tcp.on('close', () => { provider.push('close'); closed(); });
  observeTransport(ws, sample => events.push(sample));
  assert.equal(tcp.listenerCount('data'), 0, 'diagnostics never change flowing mode');
  tcp.emit('error', {code: 'ECONNRESET', message: 'private-body', secret: 'private-key'});
  tcp.emit('end'); tcp.emit('close', true);
  assert.deepEqual(events.map(x => x.event), ['transport_open', 'socket_error', 'socket_end', 'socket_closed', 'transport_closed']);
  assert.equal(events[1].error_code, 'ECONNRESET'); assert.equal(events[3].had_error, true);
  assert.equal(events[0].transport.tls_authorized, true);
  assert.deepEqual(provider, ['error', 'end', 'close']);
  assert.equal(tcp.listenerCount('error'), 1); assert.equal(tcp.listenerCount('end'), 1);
  assert.equal(tcp.listenerCount('close'), 1, 'only diagnostic listeners were removed');
  assert.doesNotMatch(JSON.stringify(events), /private/);
});

test('socket diagnostics attach after handshake, isolate unknown errors and cannot throw into transport', () => {
  const f = fixture(); f.raw.readyState = 0;
  const events = [];
  const cleanup = observeTransport(f.ws, x => events.push(x));
  f.raw._socket = Object.assign(new EventEmitter(), f.raw._socket);
  f.raw.readyState = 1; f.raw.emit('open');
  f.raw._socket.emit('error', {code: 'private-value'});
  assert.equal(events.at(-1).error_code, 'OTHER');
  cleanup(); assert.equal(f.raw._socket.listenerCount('error'), 0);
  const stop = observeTransport(f.ws, () => { throw new Error('diagnostic sink'); });
  assert.doesNotThrow(() => f.raw._socket.emit('error', {code: 'EPIPE'})); stop();
});
