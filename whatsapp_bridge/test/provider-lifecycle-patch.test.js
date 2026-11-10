import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { patchBaileys, patchManifest, transformProviderSource } from '../scripts/patch-baileys-lifecycle.js';

const execute = promisify(execFile);
const hash = text => crypto.createHash('sha256').update(text).digest('hex');
const installedRoot = fileURLToPath(new URL('../node_modules/@whiskeysockets/baileys/', import.meta.url));
const names = Object.keys(patchManifest);
const gate = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'baileys-lifecycle-patch-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  await fs.writeFile(path.join(root, 'package.json'), JSON.stringify({ version: '7.0.0-rc14', type: 'module' }));
  const original = new Map();
  for (const name of names) {
    let source = await fs.readFile(path.join(installedRoot, name), 'utf8');
    if (hash(source) !== patchManifest[name].sha256) source = transformProviderSource(name, source, { reverse: true });
    assert.equal(hash(source), patchManifest[name].sha256, 'test fixtures come from the exact pinned public dependency');
    original.set(name, source);
    await fs.mkdir(path.dirname(path.join(root, name)), { recursive: true });
    await fs.writeFile(path.join(root, name), source);
  }
  return { root, original };
}

test('provider lifecycle patch is checksum verified, reversible, idempotent and syntactically valid', async t => {
  const { root, original } = await fixture(t);
  await assert.rejects(patchBaileys(root, { verifyOnly: true }), /BAILEYS_LIFECYCLE_PATCH_REQUIRED/);
  assert.deepEqual(await patchBaileys(root), { version: 1, files: names.length, patched: names.length });
  const patched = new Map();
  for (const name of names) {
    const source = await fs.readFile(path.join(root, name), 'utf8');
    patched.set(name, source);
    assert.equal(transformProviderSource(name, source, { reverse: true }), original.get(name));
    await execute(process.execPath, ['--check', path.join(root, name)]);
  }
  assert.equal((await patchBaileys(root)).patched, 0);
  assert.equal((await patchBaileys(root, { verifyOnly: true })).patched, 0);
  for (const name of names) assert.equal(await fs.readFile(path.join(root, name), 'utf8'), patched.get(name));
});

test('unknown provider source or version fails before modifying any file', async t => {
  const { root, original } = await fixture(t);
  const finalName = names.at(-1);
  await fs.appendFile(path.join(root, finalName), '\n// unexpected upstream change\n');
  await assert.rejects(patchBaileys(root), /BAILEYS_PATCH_SOURCE_MISMATCH/);
  for (const name of names.slice(0, -1)) assert.equal(await fs.readFile(path.join(root, name), 'utf8'), original.get(name));
  await fs.writeFile(path.join(root, finalName), original.get(finalName));
  await fs.writeFile(path.join(root, 'package.json'), '{"version":"7.0.0-rc15"}');
  await assert.rejects(patchBaileys(root), /BAILEYS_PATCH_VERSION_MISMATCH/);
  for (const name of names) assert.equal(await fs.readFile(path.join(root, name), 'utf8'), original.get(name));
});

test('verification rejects tampered and partially installed patches, and interrupted installation resumes safely', async t => {
  const { root, original } = await fixture(t);
  await fs.writeFile(path.join(root, names[0]), transformProviderSource(names[0], original.get(names[0])));
  await assert.rejects(patchBaileys(root, { verifyOnly: true }), /BAILEYS_LIFECYCLE_PATCH_REQUIRED/);
  assert.equal((await patchBaileys(root)).patched, names.length - 1);
  const target = path.join(root, names[1]);
  const patched = await fs.readFile(target, 'utf8');
  await fs.writeFile(target, patched.replace('fireInitQueries', 'unexpectedInitQueries'));
  await assert.rejects(patchBaileys(root, { verifyOnly: true }), /BAILEYS_PATCH_SOURCE_MISMATCH/);
  await assert.rejects(patchBaileys(root), /BAILEYS_PATCH_SOURCE_MISMATCH/);
  assert.equal(await fs.readFile(target, 'utf8'), patched.replace('fireInitQueries', 'unexpectedInitQueries'));
});

test('actual source rename failure leaves only complete original or patched files and no temporary files', async t => {
  const { root, original } = await fixture(t);
  const rename = fs.rename;
  let injected = false;
  fs.rename = async (from, to) => {
    if (to === path.join(root, names[1]) && !injected) {
      injected = true;
      throw Object.assign(new Error('Injected source install failure'), { code: 'EIO' });
    }
    return rename(from, to);
  };
  try { await assert.rejects(patchBaileys(root), { code: 'EIO' }); }
  finally { fs.rename = rename; }
  for (const name of names) {
    const source = await fs.readFile(path.join(root, name), 'utf8');
    assert.ok(source === original.get(name) || source === transformProviderSource(name, original.get(name)));
    assert.equal((await fs.readdir(path.dirname(path.join(root, name)))).some(entry => entry.endsWith('.tmp')), false);
  }
  await assert.rejects(patchBaileys(root, { verifyOnly: true }), /BAILEYS_LIFECYCLE_PATCH_REQUIRED/);
  assert.equal((await patchBaileys(root)).patched, names.length - 1);
  await patchBaileys(root, { verifyOnly: true });
});

test('patch refuses symlinked shared dependency roots and source files', async t => {
  const { root, original } = await fixture(t);
  const linked = root + '-linked';
  await fs.symlink(root, linked, 'dir');
  t.after(() => fs.unlink(linked));
  await assert.rejects(patchBaileys(linked), /BAILEYS_PATCH_ROOT_SYMLINK/);
  const target = path.join(root, names[0]);
  const saved = target + '.original';
  await fs.rename(target, saved);
  await fs.symlink(saved, target);
  await assert.rejects(patchBaileys(root), /BAILEYS_PATCH_FILE_SYMLINK/);
  assert.equal(await fs.readFile(saved, 'utf8'), original.get(names[0]));
});

test('patched real offline processor exposes the entire accepted queue and restarts without a completion race', async t => {
  const { root } = await fixture(t);
  await patchBaileys(root);
  const { makeOfflineNodeProcessor } = await import(pathToFileURL(path.join(root, 'lib/Utils/offline-node-processor.js')));
  const release = gate(), entered = gate(), handled = [], failures = [];
  const processor = makeOfflineNodeProcessor(new Map([['message', async node => {
    handled.push(node.id);
    if (node.id === 1) { entered.resolve(); await release.promise; }
  }]]), { isWsOpen: () => true, onUnexpectedError: error => failures.push(error),
    yieldToEventLoop: () => new Promise(resolve => setImmediate(resolve)) }, 1);
  const first = processor.enqueue('message', { id: 1 });
  await entered.promise;
  const second = processor.enqueue('message', { id: 2 });
  assert.equal(first, second, 'every queued caller waits for the shared accepted pump');
  let finished = false;
  first.then(() => { finished = true; });
  assert.equal(finished, false);
  release.resolve();
  await first;
  assert.deepEqual(handled, [1, 2]);
  // Starting the next pump directly from completion must not be overwritten by
  // an old asynchronous finally callback resetting its ownership flag.
  await first.then(async () => {
    const third = processor.enqueue('message', { id: 3 });
    const fourth = processor.enqueue('message', { id: 4 });
    assert.equal(third, fourth);
    await fourth;
  });
  assert.deepEqual(handled, [1, 2, 3, 4]);
  assert.deepEqual(failures, []);
});

test('patched offline processor recovers its ownership after a processing-yield failure', async t => {
  const { root } = await fixture(t);
  await patchBaileys(root);
  const { makeOfflineNodeProcessor } = await import(pathToFileURL(path.join(root, 'lib/Utils/offline-node-processor.js')));
  let failYield = true;
  const handled = [], errors = [];
  const processor = makeOfflineNodeProcessor(new Map([['message', async node => { handled.push(node.id); }]]), {
    isWsOpen: () => true, onUnexpectedError: error => errors.push(error),
    yieldToEventLoop: async () => { if (failYield) { failYield = false; throw new Error('synthetic yield failure'); } },
  }, 1);
  await processor.enqueue('message', { id: 1 });
  await new Promise(resolve => setImmediate(resolve));
  await processor.enqueue('message', { id: 2 });
  assert.deepEqual(handled, [1, 2]);
  assert.equal(errors.length, 1);
});

// rc14 query() resolves undefined after its timeout. Run the provider's own
// refreshMediaConn source (original and patched) against that behaviour.
async function mediaConnection(source, answers) {
  const { Boom } = await import('@hapi/boom');
  const baileys = await import('@whiskeysockets/baileys');
  const start = source.indexOf('    const refreshMediaConn = async');
  const end = source.indexOf('        return mediaConn;\n    };', start) + '        return mediaConn;\n    };'.length;
  const query = async () => answers.shift();
  return new Function('query', 'Boom', 'getBinaryNodeChild', 'getBinaryNodeChildren', 'S_WHATSAPP_NET', 'logger',
    `let mediaConn; let mediaHost; ${source.slice(start, end)}; return refreshMediaConn;`)(
    query, Boom, baileys.getBinaryNodeChild, baileys.getBinaryNodeChildren, baileys.S_WHATSAPP_NET, { debug() {} });
}

test('a timed-out media connection lookup no longer breaks every later upload on that connection', async t => {
  const { original } = await fixture(t);
  const name = 'lib/Socket/messages-send.js';
  const answer = { tag: 'iq', attrs: {}, content: [{ tag: 'media_conn', attrs: { auth: 'synthetic', ttl: '300' },
    content: [{ tag: 'host', attrs: { hostname: 'mmg.whatsapp.net', maxContentLengthBytes: '1' } }] }] };
  const unpatched = await mediaConnection(original.get(name), [undefined, answer]);
  await assert.rejects(unpatched(), TypeError);
  await assert.rejects(unpatched(), TypeError, 'the original keeps returning the cached failure');
  const patched = await mediaConnection(transformProviderSource(name, original.get(name)), [undefined, answer]);
  await assert.rejects(patched(), error => error.output?.statusCode === 408, 'an empty answer is a timeout');
  assert.equal((await patched()).auth, 'synthetic', 'the next upload fetches a fresh media connection');
});

test('the bridge relay hook runs immediately before the message stanza is written', async t => {
  const { original } = await fixture(t);
  const patched = transformProviderSource('lib/Socket/messages-send.js', original.get('lib/Socket/messages-send.js'));
  const hook = '            await config.bridgeBeforeRelay?.(msgId, message);\n            await sendNode(stanza);';
  assert.equal(patched.split(hook).length - 1, 1);
  const relay = patched.slice(patched.indexOf('    const relayMessage = async'), patched.indexOf(hook));
  assert.equal(relay.includes('await sendNode(stanza);\n                return;'), true, 'only the ignored newsletter path writes elsewhere');
});
