import test from 'node:test';
import assert from 'node:assert/strict';
import { fork } from 'node:child_process';
import { connect } from 'node:net';
import { mkdtemp, rm, stat, readFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { acquireDataDirectory } from '../src/storage.js';
import { readJournal } from '../src/journal-store.js';
import { BufferJSON } from '@whiskeysockets/baileys';

const fixture = new URL('../fixtures/startup-child.mjs', import.meta.url);
async function childFixture(t, scenario) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'bridge-process-startup-'));
  const child = fork(fixture, [directory, scenario], { stdio: ['ignore', 'pipe', 'pipe', 'ipc'] });
  const messages = [], waiting = [];
  let stdout = '', stderr = '', exited = false;
  child.stdout.on('data', data => { stdout += data; });
  child.stderr.on('data', data => { stderr += data; });
  child.on('message', message => {
    messages.push(message);
    for (const watcher of [...waiting]) if (watcher.predicate(message)) watcher.resolve(message);
  });
  const exit = new Promise(resolve => child.once('exit', (code, signal) => {
    exited = true;
    resolve({ code, signal });
    for (const watcher of [...waiting]) watcher.reject(new Error(`Child exited while awaiting IPC: ${code}/${signal}; ${stderr}; ${stdout}`));
  }));
  t.after(async () => {
    if (!exited) { child.kill('SIGKILL'); await exit; }
    await rm(directory, { recursive: true, force: true });
    await rm(directory + '.lock', { recursive: true, force: true });
  });
  const receive = (type, predicate = () => true) => {
    const match = message => message.type === type && predicate(message);
    const existing = messages.find(match);
    if (existing) return Promise.resolve(existing);
    if (exited) return Promise.reject(new Error(`Child already exited: ${stderr}`));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(reject, new Error(`Timed out awaiting ${type}; IPC=${JSON.stringify(messages)}; ${stderr}`)), 15_000);
      const watcher = { predicate: match, resolve: message => finish(resolve, message), reject: error => finish(reject, error) };
      const finish = (action, value) => { clearTimeout(timer); waiting.splice(waiting.indexOf(watcher), 1); action(value); };
      waiting.push(watcher);
    });
  };
  return { child, directory, receive, messages, exit, release: name => child.send({ release: name }),
    signal: signal => assert.equal(child.kill(signal), true), isAlive: () => !exited };
}
async function assertLocked(directory) {
  await assert.rejects(acquireDataDirectory(directory), error => error.code === 'ELOCKED');
}
async function assertReleased(directory) {
  const release = await acquireDataDirectory(directory);
  await release();
}
function assertStoppedBeforeServing(done, healthListener = true) {
  assert.equal(done.counts.workers, 0);
  assert.equal(done.counts.servers, Number(healthListener));
  assert.equal(done.counts.listeners, Number(healthListener));
}

for (const signal of ['SIGTERM', 'SIGINT']) {
  test(`${signal} during dependency preparation cannot acquire storage or create a bridge`, { timeout: 20_000 }, async t => {
    const child = await childFixture(t, 'prepare');
    await child.receive('prepare_blocked');
    child.signal(signal);
    await child.receive('signal_seen');
    child.release('prepare');
    const done = await child.receive('done');
    assert.deepEqual(await child.exit, { code: 0, signal: null });
    assert.equal(done.counts.acquired, 0);
    assert.equal(done.counts.bridges, 0);
    assertStoppedBeforeServing(done, false);
  });
  for (const scenario of ['lock_before', 'lock_after']) {
    test(`${signal} during ${scenario} retains late lock ownership and never initializes`, { timeout: 20_000 }, async t => {
      const child = await childFixture(t, scenario);
      const blocked = await child.receive('lock_blocked');
      assert.equal(blocked.counts.bridges, 0);
      if (scenario === 'lock_after') await assertLocked(child.directory);
      child.signal(signal);
      await child.receive('signal_seen');
      assert.equal(child.isAlive(), true, 'the lock operation still belongs to shutdown');
      child.release('lock');
      const done = await child.receive('done');
      assert.deepEqual(await child.exit, { code: 0, signal: null });
      assert.equal(done.counts.acquired, 1);
      assert.equal(done.counts.released, 1);
      assert.equal(done.counts.bridges, 0);
      assertStoppedBeforeServing(done, false);
      await assertReleased(child.directory);
    });
  }
  test(`${signal} during later-line replay stops before any socket and drains owned stores`, { timeout: 20_000 }, async t => {
    const child = await childFixture(t, 'replay');
    const blocked = await child.receive('replay_blocked');
    assert.equal(blocked.counts.sockets, 0, 'all stores are prepared before accepting provider traffic');
    child.signal(signal);
    await child.receive('shutdown_started');
    await assertLocked(child.directory);
    child.release('replay');
    const done = await child.receive('done');
    assert.deepEqual(await child.exit, { code: 0, signal: null });
    assert.equal(done.counts.closed, 1);
    assert.equal(done.counts.sockets, 0);
    assert.deepEqual(done.fences, [false, false]);
    assertStoppedBeforeServing(done);
    await assertReleased(child.directory);
  });
  test(`${signal} drains accepted incoming, receipt, credentials and real buffered Baileys keys during partial startup`, { timeout: 20_000 }, async t => {
    const child = await childFixture(t, 'accepted');
    await Promise.all([child.receive('connect_blocked'), child.receive('credentials_blocked'), child.receive('events_accepted')]);
    assert.equal((await child.receive('keys_buffered')).persisted, false, 'real provider transaction has not committed its buffered key');
    child.signal(signal);
    await child.receive('shutdown_started');
    await assertLocked(child.directory);
    child.release('connect');
    child.release('events');
    child.release('credentials');
    child.release('keys');
    const done = await child.receive('done');
    assert.deepEqual(await child.exit, { code: 0, signal: null });
    assert.equal(done.counts.sockets, 1, 'shutdown prevents the next line creating a socket');
    assert.equal(done.counts.ended, 1);
    assert.deepEqual(done.fences, [false, false]);
    assertStoppedBeforeServing(done);
    const state = (await readJournal(path.join(child.directory, '90001/state.json'), () => ({}))).data;
    const auth = (await readJournal(path.join(child.directory, '90001/auth.json'), () => ({}))).data;
    const outbox = (await readJournal(path.join(child.directory, 'outbox.json'), () => ({}))).data;
    assert.equal(state.messages.ACCEPTED_INCOMING.text, 'Synthetic ACCEPTED_INCOMING');
    assert.equal(state.messages.ORIGINAL.receipt, 'read');
    assert.equal(auth.creds.nextPreKeyId, 500);
    assert.equal(auth.creds.myAppStateKeyId, 'accepted-before-stop');
    assert.deepEqual(auth.keys.session.startupRetained, Buffer.from([7, 5, 1]));
    assert.equal(done.counts.keyTransactions, 1);
    assert.equal(done.counts.keyCommits, 1);
    assert.equal(done.counts.keyFailures, 0);
    assert.equal(done.counts.keyAppendAttempts, 1);
    await assert.rejects(stat(path.join(child.directory, '90001/auth.json.auth-intent')), { code: 'ENOENT' });
    await assert.rejects(stat(path.join(child.directory, '90001/auth.json.auth-failure')), { code: 'ENOENT' });
    assert.equal(outbox.entries.filter(entry => entry.event.type === 'message').length, 1);
    assert.equal(outbox.entries.filter(entry => entry.event.type === 'message_status' && entry.event.status === 'read').length, 1);
    await assertReleased(child.directory);
  });
}

test('SIGTERM while listen is pending closes the listener without starting background workers', { timeout: 20_000 }, async t => {
  const child = await childFixture(t, 'listen');
  await child.receive('listen_blocked');
  child.signal('SIGTERM');
  const done = await child.receive('done');
  assert.deepEqual(await child.exit, { code: 0, signal: null });
  assert.equal(done.counts.workers, 0);
  assert.equal(done.counts.servers, 1);
  assert.equal(done.counts.listeners, 1);
  assert.equal(done.counts.ended, 0, 'provider sockets do not start before the health listener binds');
  assert.equal(done.counts.released, 1);
  assert.deepEqual(done.fences, [false, false]);
});

test('SIGTERM drains accepted messages before closing an incomplete HTTP request within the stop budget', { timeout: 20_000 }, async t => {
  const child = await childFixture(t, 'http_body');
  const listening = await child.receive('http_listening');
  await child.receive('ready');
  const client = connect({ host: '127.0.0.1', port: listening.port });
  t.after(() => client.destroy());
  client.on('error', () => {});
  await new Promise((resolve, reject) => { client.once('connect', resolve); client.once('error', reject); });
  client.write('POST /pending HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100000\r\nConnection: keep-alive\r\n\r\nx');
  assert.equal((await child.receive('body_pending')).requestComplete, false);
  const stoppingAt = Date.now();
  child.signal('SIGTERM');
  await child.receive('shutdown_started');
  await assertLocked(child.directory);
  assert.equal(client.destroyed, false, 'HTTP is not destroyed before accepted bridge events drain');
  child.release('http_events');
  const forced = await child.receive('http_forced_close');
  assert.equal(forced.bridgeClosed, true);
  const done = await child.receive('done');
  assert.deepEqual(await child.exit, { code: 0, signal: null });
  assert.ok(Date.now() - stoppingAt < 5000, 'an incomplete HTTP body cannot consume the 35-second stop budget');
  assert.equal(done.counts.released, 1);
  assert.deepEqual(done.fences, [false, false]);
  const state = (await readJournal(path.join(child.directory, '90001/state.json'), () => ({}))).data;
  assert.equal(state.messages.HTTP_SHUTDOWN_INCOMING.text, 'Synthetic HTTP_SHUTDOWN_INCOMING');
});

test('a server error after startup drains accepted events and exits unsuccessfully without abrupt termination', { timeout: 20_000 }, async t => {
  const child = await childFixture(t, 'server_error');
  await child.receive('ready');
  child.release('server_error');
  await Promise.all([child.receive('server_error_accepted'), child.receive('shutdown_started')]);
  await assertLocked(child.directory);
  assert.equal(child.isAlive(), true);
  child.release('server_error_events');
  const done = await child.receive('done');
  assert.deepEqual(await child.exit, { code: 1, signal: null });
  assert.equal(done.counts.closed, 1);
  assert.equal(done.counts.released, 1);
  assert.deepEqual(done.fences, [false, false]);
  const state = (await readJournal(path.join(child.directory, '90001/state.json'), () => ({}))).data;
  assert.equal(state.messages.SERVER_ERROR_INCOMING.text, 'Synthetic SERVER_ERROR_INCOMING');
});

test('initialization rejection cleans partially initialized sockets and releases the lock only after cleanup', { timeout: 20_000 }, async t => {
  const child = await childFixture(t, 'initialize_failure');
  const blocked = await child.receive('initialization_will_fail');
  assert.equal(blocked.counts.sockets, 2);
  await assertLocked(child.directory);
  child.release('failure');
  const done = await child.receive('done');
  assert.deepEqual(await child.exit, { code: 1, signal: null });
  assert.equal(done.counts.closed, 1);
  assert.equal(done.counts.ended, 2);
  assert.equal(done.counts.released, 1);
  assert.deepEqual(done.fences, [false, false]);
  assertStoppedBeforeServing(done);
  await assertReleased(child.directory);
});

test('repeated SIGTERM and SIGINT share one startup cancellation and one release', { timeout: 20_000 }, async t => {
  const child = await childFixture(t, 'replay');
  await child.receive('replay_blocked');
  child.signal('SIGTERM');
  await child.receive('signal_seen', message => message.signal === 'SIGTERM');
  child.signal('SIGINT');
  await child.receive('signal_seen', message => message.signal === 'SIGINT');
  await assertLocked(child.directory);
  child.release('replay');
  const done = await child.receive('done');
  assert.deepEqual(await child.exit, { code: 0, signal: null });
  assert.equal(done.counts.closeCalls, 1);
  assert.equal(done.counts.released, 1);
  assertStoppedBeforeServing(done);
});

test('cleanup failure exits unsuccessfully without explicitly releasing storage ownership', { timeout: 20_000 }, async t => {
  const child = await childFixture(t, 'cleanup_failure');
  await child.receive('ready');
  child.signal('SIGTERM');
  const done = await child.receive('done');
  assert.deepEqual(await child.exit, { code: 1, signal: null });
  assert.equal(done.counts.released, 0);
  assert.equal(done.lockExists, true, 'the coordinator did not advertise safe ownership transfer');
});

for (const scenario of ['credential_failure', 'key_failure']) test(`${scenario} during startup cancellation retains buffered key, message and receipt evidence`, { timeout: 20_000 }, async t => {
  const child = await childFixture(t, scenario);
  await Promise.all([child.receive('connect_blocked'), child.receive('credentials_blocked'), child.receive('events_accepted')]);
  assert.equal((await child.receive('keys_buffered')).persisted, false);
  child.signal('SIGTERM');
  await child.receive('shutdown_started');
  await assertLocked(child.directory);
  child.release('connect');
  child.release('events');
  child.release('credentials');
  child.release('keys');
  const done = await child.receive('done');
  assert.deepEqual(await child.exit, { code: 1, signal: null });
  assert.equal(done.counts.released, 0);
  assert.equal(done.lockExists, true);
  assert.equal(done.counts.sockets, 1);
  assert.equal(done.fences[0], true);
  assert.equal(done.counts.keyTransactions, 1);
  assert.equal(done.counts.keyFailures, 1);
  assert.equal(done.counts.keyCommits, 0);
  assert.equal((await stat(path.join(child.directory, '90001/auth.json.auth-failure'))).isFile(), true);
  const evidence = JSON.parse(await readFile(path.join(child.directory, '90001/auth.json.auth-failure'), 'utf8'), BufferJSON.reviver);
  const retained = evidence.failures.flatMap(failure => failure.mutations ?? []).map(mutation => mutation.session?.startupRetained).filter(Boolean);
  assert.ok(retained.some(value => Buffer.from(value).equals(Buffer.from([7, 5, 1]))), 'rejected provider keys remain in durable recovery evidence');
  const auth = (await readJournal(path.join(child.directory, '90001/auth.json'), () => ({}))).data;
  assert.equal(auth.keys.session?.startupRetained, undefined, 'a failed transaction must not publish its buffered key');
  if (scenario === 'key_failure') {
    assert.equal(done.counts.keyAppendAttempts, 1, 'provider retries cannot bypass the failed authentication owner');
    assert.equal(auth.creds.nextPreKeyId, 500, 'the earlier successful credential update remains committed');
  }
  const state = (await readJournal(path.join(child.directory, '90001/state.json'), () => ({}))).data;
  const outbox = (await readJournal(path.join(child.directory, 'outbox.json'), () => ({}))).data;
  assert.equal(state.messages.ACCEPTED_INCOMING.text, 'Synthetic ACCEPTED_INCOMING');
  assert.equal(state.messages.ORIGINAL.receipt, 'read');
  assert.equal(outbox.entries.filter(entry => entry.event.type === 'message').length, 1);
  assert.equal(outbox.entries.filter(entry => entry.event.type === 'message_status' && entry.event.status === 'read').length, 1);
  assertStoppedBeforeServing(done);
});

test('shutdown deadline reports failure but keeps the lock while startup cleanup is still in flight', { timeout: 20_000 }, async t => {
  const child = await childFixture(t, 'deadline');
  await child.receive('replay_blocked');
  child.signal('SIGTERM');
  await child.receive('log', message => /shutdown_deadline_exceeded/i.test(message.message));
  assert.equal(child.isAlive(), true, 'a deadline cannot falsely finish cleanup');
  await assertLocked(child.directory);
  child.release('replay');
  const done = await child.receive('done');
  assert.deepEqual(await child.exit, { code: 1, signal: null });
  assert.equal(done.counts.sockets, 0);
  assertStoppedBeforeServing(done);
});
