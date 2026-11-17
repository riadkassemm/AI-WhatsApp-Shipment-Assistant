import path from 'node:path';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';
import { readLineWorkerConfig } from './config.js';
import { installLibsignalConsoleFilter } from './safe-console.js';
import { patchBaileys } from '../scripts/patch-baileys-lifecycle.js';
import { createRuntime } from './runtime-lifecycle.js';
import { readFreezeRestartMs, startFreezeWatchdog } from './freeze-watchdog.js';
import { createSupervisorHost, workerState } from './worker-ipc.js';

// One number's bridge process in per-line mode, started only by supervisor.js.
// It runs the ordinary Bridge restricted to that number and serves the
// ordinary HTTP API on a private unix socket that only the supervisor uses.
// A freeze, crash or SIGKILL here costs this number a restart; the other
// numbers keep running.
process.umask(0o077);
startFreezeWatchdog({ thresholdMs: readFreezeRestartMs() });
installLibsignalConsoleFilter();
const host = createSupervisorHost(process);
const REPORT_MS = 10_000;
let reportTimer;
const runtime = createRuntime({prepare: async () => {
  dotenv.config({ path: path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../.env'), quiet: true });
  await patchBaileys(undefined, { verifyOnly: true });
  const { acquireDataDirectory } = await import('./storage.js');
  const { Bridge } = await import('./bridge.js');
  const { createServer } = await import('./server.js');
  const { setCheckpointGate } = await import('./journal-maintenance.js');
  const config = readLineWorkerConfig();
  // At most one checkpoint across all numbers' processes, as in one process.
  setCheckpointGate(async action => {
    await host.call('checkpointAcquire', {}, { timeoutMs: 0 });
    try { return await action(); }
    finally { host.call('checkpointRelease').catch(() => {}); }
  });
  return {
    config,
    // The supervisor holds the data directory; this process owns its number.
    acquireDataDirectory: (_directory, options) => acquireDataDirectory(config.worker.lockPath, { ...options,
      onReclaimed: () => console.error(JSON.stringify({component: 'whatsapp_bridge', event: 'line_lock_reclaimed', line_id: config.worker.lineId})) }),
    createBridge: workerConfig => new Bridge(workerConfig, { host }),
    createServer,
    onReady: bridge => {
      host.notify('ready', workerState(bridge, config.worker.lineId));
      reportTimer = setInterval(() => host.notify('report', workerState(bridge, config.worker.lineId)), REPORT_MS);
      reportTimer.unref();
    },
  };
}});
// Without its supervisor no request can reach this worker: stop cleanly.
process.on('disconnect', () => { void runtime.stop(); });
void runtime.start().catch(() => {});
void runtime.done.then(({exitCode}) => { clearInterval(reportTimer); process.exit(exitCode); });
