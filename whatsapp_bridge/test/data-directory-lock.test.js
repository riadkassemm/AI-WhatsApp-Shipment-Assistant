import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { performance } from 'node:perf_hooks';
import { acquireDataDirectory } from '../src/storage.js';

const storageUrl = new URL('../src/storage.js', import.meta.url).href;
const exists = file => stat(file).then(() => true, error => { if (error.code === 'ENOENT') return false; throw error; });

async function workspace(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'bridge-lock-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  return { root, dataDir: path.join(root, 'linked-device') };
}

// A real second process holds the lock, like a running bridge.
function holder(dataDir) {
  const child = spawn(process.execPath, ['--input-type=module', '-e', `
    import { acquireDataDirectory } from ${JSON.stringify(storageUrl)};
    await acquireDataDirectory(process.argv[1]);
    process.stdout.write('locked\\n');
    setInterval(() => {}, 1000);
  `, dataDir], { stdio: ['ignore', 'pipe', 'pipe'] });
  const locked = new Promise((resolve, reject) => {
    child.stdout.on('data', data => { if (String(data).includes('locked')) resolve(); });
    child.once('exit', code => reject(new Error(`holder exited early: ${code}`)));
  });
  const exited = new Promise(resolve => child.once('exit', (code, signal) => resolve(signal)));
  return { child, locked, exited };
}

test('a live bridge keeps the lock; after it is killed the next bridge takes it over at once', { timeout: 20_000 }, async t => {
  const { dataDir } = await workspace(t);
  const running = holder(dataDir);
  t.after(() => running.child.kill('SIGKILL'));
  await running.locked;
  await assert.rejects(acquireDataDirectory(dataDir), { code: 'ELOCKED' }, 'two bridges never share one data directory');
  running.child.kill('SIGKILL');
  assert.equal(await running.exited, 'SIGKILL');
  let reclaimed = false;
  const started = performance.now();
  const release = await acquireDataDirectory(dataDir, { onReclaimed: () => { reclaimed = true; } });
  assert.ok(performance.now() - started < 3_000, 'no 30 s stale wait after a crash');
  assert.equal(reclaimed, true);
  const record = JSON.parse(await readFile(`${dataDir}.lock-holder.json`, 'utf8'));
  assert.equal(record.pid, process.pid);
  await release();
  assert.equal(await exists(`${dataDir}.lock`), false);
  assert.equal(await exists(`${dataDir}.lock-holder.json`), false, 'a clean release removes the holder record');
});

test('a fresh lock without a holder record keeps the conservative 30 s rule', async t => {
  const { dataDir } = await workspace(t);
  await mkdir(dataDir, { recursive: true });
  await mkdir(`${dataDir}.lock`);  // e.g. left by an older bridge version
  await assert.rejects(acquireDataDirectory(dataDir), { code: 'ELOCKED' });
});

test('a recorded holder whose PID now belongs to a different process is treated as gone', async t => {
  const { dataDir } = await workspace(t);
  await mkdir(dataDir, { recursive: true });
  await mkdir(`${dataDir}.lock`);
  const boot = (await readFile('/proc/sys/kernel/random/boot_id', 'utf8').catch(() => '')).trim() || null;
  // This test process is alive, but not the process that took the lock.
  await writeFile(`${dataDir}.lock-holder.json`, JSON.stringify({ version: 1, pid: process.pid, start: 'another-process', boot_id: boot }));
  const release = await acquireDataDirectory(dataDir);
  await release();
});

test('a recorded holder that is still the same live process keeps the lock', async t => {
  const { dataDir } = await workspace(t);
  const release = await acquireDataDirectory(dataDir);
  t.after(() => release());
  await assert.rejects(acquireDataDirectory(dataDir), { code: 'ELOCKED' });
});
