import test from 'node:test';
import assert from 'node:assert/strict';
import https from 'node:https';
import dns from 'node:dns';
import net from 'node:net';
import tls from 'node:tls';
import { once } from 'node:events';
import { execFileSync } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { WebSocketServer } from 'ws';
import { WebSocketClient } from '@whiskeysockets/baileys/lib/Socket/Client/websocket.js';
import { readConfig } from '../src/config.js';
import { createWebSocketAgent } from '../src/websocket-transport.js';
import { observeTransport } from '../src/transport-diagnostics.js';

// Local TLS/WebSocket doubles only; no WhatsApp protocol, credentials or DNS.
async function endpoint(t, host = '127.0.0.1') {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-ws-ip-family-'));
  t.after(() => rm(directory, {recursive: true, force: true}));
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
    '-subj', '/CN=bridge.mock.test', '-addext', 'subjectAltName=DNS:bridge.mock.test',
    '-keyout', path.join(directory, 'key.pem'), '-out', path.join(directory, 'cert.pem')], {stdio: 'ignore'});
  const cert = await readFile(path.join(directory, 'cert.pem'));
  const server = https.createServer({key: await readFile(path.join(directory, 'key.pem')), cert});
  server.on('tlsClientError', () => {});
  const sni = [], peers = new Set();
  server.on('secureConnection', socket => { sni.push(socket.servername); });
  const wss = new WebSocketServer({server});
  wss.on('connection', socket => {
    peers.add(socket); socket.on('close', () => peers.delete(socket));
    socket.on('message', value => socket.send(value));
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen({host, port: 0, ipv6Only: true}, resolve);
  });
  t.after(async () => {
    for (const peer of peers) peer.terminate();
    await new Promise(resolve => wss.close(resolve));
    await new Promise(resolve => server.close(resolve));
  });
  return {cert, sni, peers, port: server.address().port};
}

function agentFor(t, server, {trusted = true} = {}) {
  const agent = createWebSocketAgent(4);
  t.after(() => agent.destroy());
  if (trusted) agent.options.ca = server.cert; // Private, ephemeral mock CA only.
  const lookups = [];
  agent.options.lookup = (hostname, options, done) => {
    lookups.push({hostname, family: options.family, all: options.all});
    assert.equal(options.family, 4);
    assert.notEqual(options.all, true, 'no automatic dual-stack fallback');
    done(null, '127.0.0.1', 4);
  };
  return {agent, lookups};
}

function connect(t, port, agent, hostname = 'bridge.mock.test') {
  const client = new WebSocketClient(new URL(`wss://${hostname}:${port}`), {connectTimeoutMs: 2000,
    ...(agent ? {agent} : {})});
  const opened = once(client, 'open');
  client.connect();
  t.after(async () => {
    const raw = client.socket;
    if (raw && raw.readyState !== 3) {
      const closed = new Promise(resolve => raw.once('close', resolve));
      raw.terminate(); await closed;
    }
  });
  return {client, opened};
}

test('family configuration is explicit, defaults are unchanged, and invalid values fail startup', () => {
  const env = {WHATSAPP_LINKED_DEVICE_SECRET: 'isolated-transport-test-secret-123456'};
  for (const value of [undefined, '', '0']) {
    assert.equal(readConfig({...env, WHATSAPP_WS_IP_FAMILY: value}).wsIpFamily, 0);
  }
  assert.equal(readConfig({...env, WHATSAPP_WS_IP_FAMILY: '4'}).wsIpFamily, 4);
  for (const value of ['6', 'auto', '4.0', '04', ' 4 ', 'IPv4']) {
    assert.throws(() => readConfig({...env, WHATSAPP_WS_IP_FAMILY: value}), /WHATSAPP_WS_IP_FAMILY/);
  }
  const globalAgent = https.globalAgent, lookup = dns.lookup, order = dns.getDefaultResultOrder();
  const auto = net.getDefaultAutoSelectFamily();
  assert.equal(createWebSocketAgent(), undefined);
  assert.equal(createWebSocketAgent(0), undefined);
  const agent = createWebSocketAgent(4);
  assert.equal(agent.options.family, 4);
  assert.equal(agent.options.autoSelectFamily, false);
  assert.equal(agent.options.rejectUnauthorized, true);
  assert.equal(agent.options.checkServerIdentity, undefined, 'retain Node hostname verification');
  assert.equal(agent.options.servername, undefined, 'derive SNI from the URL');
  agent.destroy();
  assert.equal(https.globalAgent, globalAgent); assert.equal(dns.lookup, lookup);
  assert.equal(dns.getDefaultResultOrder(), order); assert.equal(net.getDefaultAutoSelectFamily(), auto);
  assert.throws(() => createWebSocketAgent(6), /Unsupported/);
});

test('the real provider WebSocket adapter uses IPv4 for eight independent sockets and replacement', {timeout: 15000}, async t => {
  const server = await endpoint(t), {agent, lookups} = agentFor(t, server);
  const samples = [], connections = Array.from({length: 8}, () => connect(t, server.port, agent));
  for (const {client} of connections) observeTransport(client, sample => samples.push(sample));
  await Promise.all(connections.map(x => x.opened));
  const ports = new Set();
  for (const {client} of connections) {
    const tcp = client.socket._socket;
    assert.equal(tcp.remoteFamily, 'IPv4'); assert.equal(tcp.authorized, true);
    ports.add(tcp.localPort);
  }
  assert.equal(ports.size, 8); assert.equal(server.peers.size, 8);
  assert.deepEqual(server.sni, Array(8).fill('bridge.mock.test'));
  assert.ok(samples.filter(x => x.event === 'transport_open').every(x => x.transport.family === 'IPv4' && x.transport.tls_authorized));
  await Promise.all(connections.map(async ({client}, i) => {
    const response = once(client, 'message'); client.send(Buffer.from(`mock-${i}`));
    assert.equal(String((await response)[0]), `mock-${i}`);
  }));
  await connections[0].client.close();
  assert.ok(connections.slice(1).every(({client}) => client.isOpen), 'one closure does not close other identities');
  const replacement = connect(t, server.port, agent); await replacement.opened;
  assert.equal(replacement.client.socket._socket.remoteFamily, 'IPv4');
  assert.equal(replacement.client.socket._socket.authorized, true);
  assert.equal(lookups.length, 9); assert.ok(lookups.every(x => x.family === 4 && x.hostname === 'bridge.mock.test'));
});

test('IPv4-only transport rejects untrusted certificates and hostname mismatch', {timeout: 15000}, async t => {
  const server = await endpoint(t);
  const untrusted = agentFor(t, server, {trusted: false});
  const first = connect(t, server.port, untrusted.agent);
  await assert.rejects(first.opened, {code: 'DEPTH_ZERO_SELF_SIGNED_CERT'});
  const trusted = agentFor(t, server);
  const mismatch = connect(t, server.port, trusted.agent, 'wrong.mock.test');
  await assert.rejects(mismatch.opened, {code: 'ERR_TLS_CERT_ALTNAME_INVALID'});
  assert.equal(server.peers.size, 0);
});

test('IPv4 unavailability is visible and never falls back to a reachable IPv6 mock', {timeout: 15000}, async t => {
  const server = await endpoint(t, '::1'), {agent, lookups} = agentFor(t, server);
  const samples = [], {client, opened} = connect(t, server.port, agent);
  observeTransport(client, sample => samples.push(sample));
  await assert.rejects(opened, {code: 'ECONNREFUSED'});
  assert.deepEqual(lookups.map(x => x.family), [4]);
  assert.ok(samples.some(x => x.event === 'transport_error' && x.error_code === 'ECONNREFUSED'));
  assert.equal(server.peers.size, 0, 'the live IPv6 listener was not used as fallback');
});

test('default mode preserves the existing unconfigured TLS path and can connect over IPv6', {timeout: 15000}, async t => {
  const server = await endpoint(t, '::1'), previous = https.globalAgent;
  // ws uses its own tls.connect when no agent is supplied. Give that unchanged
  // path an isolated CA and DNS result; never resolve the test name externally.
  const originalConnect = tls.connect;
  t.mock.method(tls, 'connect', (options, ...args) => {
    assert.equal(options.family, undefined);
    assert.equal(options.autoSelectFamily, undefined);
    return originalConnect({...options, ca: server.cert,
      lookup: (hostname, lookupOptions, done) => {
        assert.equal(hostname, 'bridge.mock.test');
        if (lookupOptions.all) done(null, [{address: '::1', family: 6}]);
        else done(null, '::1', 6);
      }}, ...args);
  });
  const {client, opened} = connect(t, server.port, createWebSocketAgent(0));
  await opened;
  assert.equal(https.globalAgent, previous);
  assert.equal(client.socket._socket.remoteFamily, 'IPv6');
  assert.equal(client.socket._socket.authorized, true);
  assert.deepEqual(server.sni, ['bridge.mock.test']);
});
