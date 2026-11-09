import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFreezeRestartMs, DEFAULT_FREEZE_RESTART_MS } from '../src/freeze-watchdog.js';

const moduleUrl = new URL('../src/freeze-watchdog.js', import.meta.url).href;
function child(body) {
  const script = `import { startFreezeWatchdog } from ${JSON.stringify(moduleUrl)};
startFreezeWatchdog({ thresholdMs: 400, checkMs: 100, beatMs: 50 });
${body}`;
  const process_ = spawn(process.execPath, ['--input-type=module', '-e', script], { stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  process_.stderr.on('data', data => { stderr += data; });
  return new Promise(resolve => process_.once('exit', (code, signal) => resolve({ code, signal, stderr })));
}

test('a frozen main thread is killed by the watchdog so systemd can restart the bridge', { timeout: 15_000 }, async () => {
  const started = Date.now();
  // Model the 2026-11-09 freeze: the main thread never yields again.
  const result = await child(`setTimeout(() => { const end = Date.now() + 10_000; while (Date.now() < end) {} }, 200);`);
  assert.equal(result.signal, 'SIGKILL');
  assert.ok(Date.now() - started < 8_000, 'killed well before the freeze would have ended');
  const event = JSON.parse(result.stderr.trim().split('\n').at(-1));
  assert.equal(event.event, 'bridge_frozen_restart');
  assert.ok(event.blocked_ms > 400);
});

test('a healthy, busy-but-responsive main thread is never killed and exits normally', { timeout: 15_000 }, async () => {
  // Short CPU bursts (like an initial sync) still yield to the heartbeat.
  const result = await child(`let bursts = 0; const tick = () => { const end = Date.now() + 100; while (Date.now() < end) {}
if (++bursts < 15) setTimeout(tick, 10); }; tick();`);
  assert.equal(result.signal, null);
  assert.equal(result.code, 0);
  assert.equal(result.stderr, '');
});

test('freeze restart threshold defaults to 60 s, can be disabled, and refuses tiny values', () => {
  assert.equal(readFreezeRestartMs({}), DEFAULT_FREEZE_RESTART_MS);
  assert.equal(DEFAULT_FREEZE_RESTART_MS, 60_000);
  assert.equal(readFreezeRestartMs({ WHATSAPP_LINKED_DEVICE_FREEZE_RESTART_MS: '0' }), 0);
  assert.equal(readFreezeRestartMs({ WHATSAPP_LINKED_DEVICE_FREEZE_RESTART_MS: '90000' }), 90_000);
  assert.throws(() => readFreezeRestartMs({ WHATSAPP_LINKED_DEVICE_FREEZE_RESTART_MS: '5000' }), /at least 20000/);
});
