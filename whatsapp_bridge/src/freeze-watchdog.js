import { Worker } from 'node:worker_threads';

// A frozen main thread (for example the 2026-11-09 storage retry loop at
// 100% CPU) cannot run any timer, signal handler or HTTP request, so it can
// never notice its own freeze. A separate thread watches a heartbeat the main
// thread writes every second. When the heartbeat stops for longer than the
// threshold, the watchdog logs one line and kills the process; systemd
// (Restart=always) starts a fresh bridge, and numbers resume their saved
// sessions (unclean-exit resume). The clock is process.hrtime: monotonic and
// shared by all threads, so wall-clock corrections cannot look like a freeze.
const WORKER_SOURCE = `
const { workerData } = require('node:worker_threads');
const fs = require('node:fs');
const { beat, thresholdMs, checkMs } = workerData;
const timer = setInterval(() => {
  const blockedMs = Number(process.hrtime.bigint() - Atomics.load(beat, 0)) / 1e6;
  if (blockedMs <= thresholdMs) return;
  clearInterval(timer);
  try {
    fs.writeSync(2, JSON.stringify({component: 'whatsapp_bridge', event: 'bridge_frozen_restart',
      blocked_ms: Math.round(blockedMs), threshold_ms: thresholdMs}) + '\\n');
  } catch (_) { /* Logging must never prevent recovery. */ }
  process.kill(process.pid, 'SIGKILL');
}, checkMs);
`;

export const DEFAULT_FREEZE_RESTART_MS = 60_000;

export function readFreezeRestartMs(env = process.env) {
  const raw = env.WHATSAPP_LINKED_DEVICE_FREEZE_RESTART_MS;
  if (raw === undefined || raw === '') return DEFAULT_FREEZE_RESTART_MS;
  const value = Number(raw);
  if (value === 0) return 0;
  if (!Number.isSafeInteger(value) || value < 20_000) {
    throw new Error('WHATSAPP_LINKED_DEVICE_FREEZE_RESTART_MS must be 0 (disabled) or an integer of at least 20000.');
  }
  return value;
}

export function startFreezeWatchdog({ thresholdMs = DEFAULT_FREEZE_RESTART_MS, checkMs = 5_000, beatMs = 1_000 } = {}) {
  if (!thresholdMs) return () => {};
  const beat = new BigInt64Array(new SharedArrayBuffer(8));
  const stamp = () => { Atomics.store(beat, 0, process.hrtime.bigint()); };
  stamp();
  const heartbeat = setInterval(stamp, beatMs);
  heartbeat.unref();
  // execArgv: [] keeps the CommonJS worker independent of how the parent was
  // started (an inherited --input-type=module would break its require()).
  const worker = new Worker(WORKER_SOURCE, { eval: true, execArgv: [], workerData: { beat, thresholdMs, checkMs } });
  worker.unref();
  return () => { clearInterval(heartbeat); return worker.terminate(); };
}
