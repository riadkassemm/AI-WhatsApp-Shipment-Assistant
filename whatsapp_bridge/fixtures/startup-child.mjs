// Forked by startup-lifecycle.test.js. All credentials, messages and sockets are
// synthetic; this executable never constructs a network provider. The one HTTP
// fixture binds an ephemeral loopback port for an incomplete-request test.
import { EventEmitter } from 'node:events';
import { mkdir, writeFile, stat } from 'node:fs/promises';
import { createServer as createHttpServer } from 'node:http';
import path from 'node:path';
import { BufferJSON, initAuthCreds, proto, addTransactionCapability } from '@whiskeysockets/baileys';
import pino from 'pino';
import { Bridge } from '../src/bridge.js';
import { acquireDataDirectory } from '../src/storage.js';
import { createRuntime } from '../src/runtime-lifecycle.js';

const [directory, scenario] = process.argv.slice(2);
if (!directory || !scenario || !process.send) throw new Error('ISOLATED_CHILD_ARGUMENTS_REQUIRED');
process.umask(0o077);
const FIRST = '90001', SECOND = '90002', PHONE = '96170123456';
const gates = new Map();
const counts = { acquired: 0, released: 0, bridges: 0, sockets: 0, ended: 0,
  closeCalls: 0, closed: 0, servers: 0, listeners: 0, workers: 0,
  keyTransactions: 0, keyCommits: 0, keyFailures: 0, keyAppendAttempts: 0 };
const send = (type, details = {}) => process.send?.({ type, ...details });
const wait = name => {
  if (!gates.has(name)) {
    let release;
    const promise = new Promise(resolve => { release = resolve; });
    gates.set(name, { promise, release });
  }
  return gates.get(name).promise;
};
process.on('message', message => {
  if (message?.release) gates.get(message.release)?.release();
});
const exists = filename => stat(filename).then(() => true, error => {
  if (error.code === 'ENOENT') return false;
  throw error;
});
const original = (id, fromMe) => ({ key: { id, fromMe, remoteJid: PHONE + '@s.whatsapp.net' },
  message: { conversation: 'Synthetic ' + id }, messageTimestamp: Math.floor(Date.now() / 1000) });
for (const id of [FIRST, SECOND]) {
  await mkdir(path.join(directory, id), { recursive: true, mode: 0o700 });
  const creds = initAuthCreds();
  creds.registered = true;
  creds.me = { id: '96170111111:1@s.whatsapp.net' };
  await writeFile(path.join(directory, id, 'auth.json'), JSON.stringify({ creds, keys: {} }, BufferJSON.replacer), { mode: 0o600 });
  await writeFile(path.join(directory, id, 'state.json'), JSON.stringify({ enabled: true, bound_phone: '',
    media: {}, operations: {}, send_requests: {}, messages: { ORIGINAL: {
      message: original('ORIGINAL', true), phone: PHONE, timestamp: Date.now(), notified: true, mirrored_outgoing: true,
    } } }), { mode: 0o600 });
}
const config = { dataDir: directory, host: '127.0.0.1', port: 0,
  secret: 'isolated-startup-lifecycle-test-secret', callbackUrl: 'http://127.0.0.1/never-called',
  lines: [FIRST, SECOND].map(line_id => ({ line_id, expected_phone: '' })),
  ackTimeoutMs: 100, maxMediaBytes: 1024, storage: { maintenance: { enabled: false } } };
const healthyStorage = async () => ({ bsize: 4096, blocks: 10_000_000,
  bavail: 5_000_000, files: 1_000_000, ffree: 900_000 });
let bridge;
function mockSocket(options) {
  counts.sockets++;
  const socket = { ws: new EventEmitter(), user: { id: '96170111111:1@s.whatsapp.net' },
    ev: { process(handler) { socket.eventHandler = handler; return () => {}; } },
    end() { counts.ended++; }, logout: async () => {},
    query: async () => { throw new Error('UNEXPECTED_PROVIDER_QUERY'); },
    sendMessage: async () => { throw new Error('UNEXPECTED_PROVIDER_SEND'); },
    relayMessage: async () => { throw new Error('UNEXPECTED_PROVIDER_SEND'); } };
  socket.ws.isOpen = true;
  if (['accepted', 'credential_failure', 'key_failure'].includes(scenario)) {
    // Exercise the installed provider's buffering/retry implementation. Bridge
    // installs authentication ownership around this actual transaction wrapper.
    socket.authState = { creds: options.auth.creds, keys: addTransactionCapability(options.auth.keys,
      pino({ level: 'silent' }), { maxCommitRetries: 3, delayBetweenTriesMs: 1 }) };
  }
  return socket;
}
function createBridge() {
  counts.bridges++;
  bridge = new Bridge(config, { makeSocket: mockSocket, storageFilesystem: healthyStorage,
    log: () => {}, onError: error => send('bridge_error', { code: error.code ?? error.message }) });
  const close = bridge.close.bind(bridge);
  let observedClose;
  bridge.close = () => {
    const closing = close();
    if (!observedClose) {
      observedClose = closing;
      counts.closeCalls++;
      send('shutdown_started', { counts: { ...counts } });
      closing.then(() => { counts.closed++; }, () => {});
    } else if (closing !== observedClose) throw new Error('SHUTDOWN_PROMISE_CHANGED');
    return closing;
  };
  bridge.startWorkers = () => { counts.workers++; };
  if (['replay', 'deadline'].includes(scenario)) {
    const load = bridge.loadStore.bind(bridge);
    bridge.loadStore = async (...args) => {
      const store = await load(...args);
      if (args[0] === path.join(directory, SECOND, 'state.json')) {
        const blocked = wait('replay');
        send('replay_blocked', { counts: { ...counts } });
        await blocked;
      }
      return store;
    };
  }
  if (['accepted', 'credential_failure', 'key_failure'].includes(scenario)) {
    const connect = bridge.connect.bind(bridge);
    bridge.connect = async id => {
      if (id === SECOND) {
        const blocked = wait('connect');
        send('connect_blocked', { counts: { ...counts } });
        await blocked;
      }
      const result = await connect(id);
      if (id === FIRST) {
        const line = bridge.getLine(FIRST), socket = line.socket;
        await bridge.connectionUpdate(line, socket, { connection: 'open' });
        line.eventTail = wait('events');
        const append = line.auth.store.append;
        line.auth.store.append = async (...args) => {
          const blocked = wait('credentials');
          send('credentials_blocked');
          await blocked;
          const record = JSON.parse(JSON.parse(args[0]).record);
          const keyWrite = record.ops.some(operation => operation.path[0] === 'keys');
          if (keyWrite) counts.keyAppendAttempts++;
          if (scenario === 'credential_failure') throw Object.assign(new Error('Synthetic credential write failure'), { code: 'EIO' });
          if (scenario === 'key_failure' && keyWrite) throw Object.assign(new Error('Synthetic key commit failure'), { code: 'EIO' });
          return append(...args);
        };
        void socket.eventHandler({ 'messages.upsert': { messages: [original('ACCEPTED_INCOMING', false)] } });
        void socket.eventHandler({ 'messages.update': [{ key: original('ORIGINAL', true).key,
          update: { status: proto.WebMessageInfo.Status.READ } }] });
        void socket.eventHandler({ 'creds.update': { nextPreKeyId: 500, myAppStateKeyId: 'accepted-before-stop' } });
        const keys = socket.authState.keys;
        counts.keyTransactions++;
        keys.transaction(async () => {
          await keys.set({ session: { startupRetained: Buffer.from([7, 5, 1]) } });
          const blocked = wait('keys');
          send('keys_buffered', { persisted: Boolean(line.auth.store.data.keys.session?.startupRetained) });
          await blocked;
        }, 'startup-peer').then(() => { counts.keyCommits++; send('keys_committed'); },
          error => { counts.keyFailures++; send('keys_failed', { code: error.code ?? error.message }); });
        send('events_accepted');
      }
      return result;
    };
  }
  if (scenario === 'initialize_failure') {
    bridge.storageMonitor.start = async () => {
      const blocked = wait('failure');
      send('initialization_will_fail', { counts: { ...counts } });
      await blocked;
      throw new Error('SYNTHETIC_INITIALIZATION_FAILURE');
    };
  }
  if (scenario === 'cleanup_failure') {
    const load = bridge.loadStore.bind(bridge);
    bridge.loadStore = async (...args) => {
      const store = await load(...args);
      if (args[2] === 'outbox') {
        const closeStore = store.close.bind(store);
        store.close = async () => { await closeStore(); throw new Error('SYNTHETIC_CLOSE_FAILURE'); };
      }
      return store;
    };
  }
  return bridge;
}
const logger = {
  info: (...args) => send('log', { message: args.map(value => typeof value === 'string' ? value : JSON.stringify(value)).join(' ') }),
  error: (...args) => send('log', { message: args.map(value => typeof value === 'string' ? value : JSON.stringify(value)).join(' ') }),
  log: (...args) => send('log', { message: args.map(value => typeof value === 'string' ? value : JSON.stringify(value)).join(' ') }),
};
const runtime = createRuntime({ logger, shutdownTimeoutMs: scenario === 'deadline' ? 250 : 10_000,
  prepare: async () => {
    if (scenario === 'prepare') {
      const blocked = wait('prepare'); send('prepare_blocked'); await blocked;
    }
    return { config, createBridge,
    acquireDataDirectory: async target => {
      if (scenario === 'lock_before') {
        const blocked = wait('lock'); send('lock_blocked', { counts: { ...counts } }); await blocked;
      }
      const release = await acquireDataDirectory(target);
      counts.acquired++;
      if (scenario === 'lock_after') {
        const blocked = wait('lock'); send('lock_blocked', { counts: { ...counts } }); await blocked;
      }
      return async () => { counts.released++; await release(); };
    },
    createServer: () => {
      counts.servers++;
      if (scenario === 'http_body') {
        const server = createHttpServer(request => {
          request.setTimeout(30_000);
          request.on('error', () => {});
          request.once('data', () => {
            const line = bridge.getLine(FIRST);
            line.eventTail = wait('http_events');
            void line.socket.eventHandler({ 'messages.upsert': { messages: [original('HTTP_SHUTDOWN_INCOMING', false)] } });
            send('body_pending', { requestComplete: request.complete });
          });
        });
        server.once('listening', () => { counts.listeners++; send('http_listening', { port: server.address().port }); });
        const closeAll = server.closeAllConnections.bind(server);
        server.closeAllConnections = () => {
          send('http_forced_close', { bridgeClosed: bridge.closed, closed: counts.closed });
          closeAll();
        };
        return server;
      }
      const server = new EventEmitter();
      let closed = false;
      server.listen = () => {
        counts.listeners++;
        if (scenario === 'listen') {
          const blocked = wait('listen'); send('listen_blocked');
          blocked.then(() => { if (!closed) server.emit('listening'); });
        } else queueMicrotask(() => { if (!closed) server.emit('listening'); });
        return server;
      };
      server.close = callback => {
        closed = true;
        queueMicrotask(() => { server.emit('close'); callback?.(); });
        return server;
      };
      server.closeIdleConnections = () => {};
      if (scenario === 'server_error') wait('server_error').then(() => {
        const line = bridge.getLine(FIRST);
        line.eventTail = wait('server_error_events');
        void line.socket.eventHandler({ 'messages.upsert': { messages: [original('SERVER_ERROR_INCOMING', false)] } });
        send('server_error_accepted');
        server.emit('error', Object.assign(new Error('Synthetic listener failure'), { code: 'SYNTHETIC_LISTENER_FAILURE' }));
      });
      return server;
    },
  }; },
});
// A second signal is delivered only after this notification, so the assertion
// does not depend on OS signal coalescing or scheduler timing.
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => send('signal_seen', { signal }));
runtime.done.then(async result => {
  const summary = { type: 'done', exitCode: result.exitCode, counts,
    lockExists: await exists(directory + '.lock'),
    fences: await Promise.all([FIRST, SECOND].map(id => exists(path.join(directory, id, 'auth.json.auth-active')))),
    error: result.error?.message };
  process.send(summary, () => process.exit(result.exitCode));
});
runtime.start().then(() => send('ready', { counts: { ...counts } }), error => send('startup_rejected', { code: error.code ?? error.message }));
