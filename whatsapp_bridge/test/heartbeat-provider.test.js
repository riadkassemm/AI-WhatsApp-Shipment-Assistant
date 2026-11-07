import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { Boom } from '@hapi/boom';

// Execute the installed, checksum-verified provider's actual keepalive function
// with a fake clock/query/transport. No socket or network client is created.
const source = await readFile(new URL('../node_modules/@whiskeysockets/baileys/lib/Socket/socket.js', import.meta.url), 'utf8');
const start = source.indexOf('    const startKeepAliveRequest =');
const end = source.indexOf('    /** i have no idea', start);
const functionSource = source.slice(start, end);
const turn = () => new Promise(resolve => setImmediate(resolve));

function fixture(query) {
  const samples = [], failures = []; let tick, now = 0;
  const context = { bridgeDiagnostic: x => samples.push(x), bridgeHeartbeatSequence: 0,
    keepAliveReq: null, keepAliveIntervalMs: 30000, lastDateRecv: new Date(0),
    performance: {now: () => now}, Date: class extends Date { static now() { return now; } },
    setInterval: fn => { tick = fn; }, ws: {isOpen: true}, query,
    generateMessageTag: () => 'synthetic-ping-id', S_WHATSAPP_NET: 's.whatsapp.net',
    logger: {warn() {}, error() {}}, Boom, DisconnectReason: {connectionLost: 408}, end: x => failures.push(x),
  };
  vm.runInNewContext(functionSource + '\nstartKeepAliveRequest();', context);
  return {samples, failures, tick: time => { now = time; tick(); }, context};
}

test('real provider query resolution counts undefined as timeout and validates explicit IQ result identity', async () => {
  for (const [response, event] of [[undefined, 'heartbeat_query_timeout'],
    [{tag: 'iq', attrs: {type: 'result', id: 'synthetic-ping-id'}}, 'heartbeat_result'],
    [{tag: 'iq', attrs: {type: 'result', id: 'wrong-id'}}, 'heartbeat_error'],
    [null, 'heartbeat_error']]) {
    let resolve;
    const f = fixture(() => new Promise(done => {resolve = done;}));
    f.tick(30000); resolve(response); await turn();
    const result = f.samples.at(-1);
    assert.equal(result.event, event);
    assert.equal(result.query_sequence, 1);
    assert.equal(Object.hasOwn(result, 'rtt_ms'), event === 'heartbeat_result');
    assert.equal(f.failures.length, 0, 'query timeout alone does not force a healthy socket closed');
  }
});

test('real provider rejected query is an error, while liveness threshold and deadline remain unchanged', async () => {
  const f = fixture(() => Promise.reject(new Error('synthetic')));
  f.tick(30000); await turn();
  assert.equal(f.samples.at(-1).event, 'heartbeat_error');
  f.tick(60000);
  assert.equal(f.failures.length, 1);
  assert.equal(f.failures[0].message, 'Connection was lost');
  assert.equal(f.failures[0].output.statusCode, 408);
  assert.equal(f.samples.at(-1).event, 'heartbeat_timeout');
});
