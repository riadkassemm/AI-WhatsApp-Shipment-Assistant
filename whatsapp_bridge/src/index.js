import path from 'node:path';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';
import { readConfig } from './config.js';
import { installLibsignalConsoleFilter } from './safe-console.js';
import { patchBaileys } from '../scripts/patch-baileys-lifecycle.js';
import { createRuntime } from './runtime-lifecycle.js';
import { readFreezeRestartMs, startFreezeWatchdog } from './freeze-watchdog.js';

process.umask(0o077);
// Kill and let systemd restart a bridge whose main thread is frozen (see freeze-watchdog.js).
startFreezeWatchdog({ thresholdMs: readFreezeRestartMs() });
installLibsignalConsoleFilter();
const runtime = createRuntime({prepare: async () => {
  dotenv.config({ path: path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../.env'), quiet: true });
  // systemd starts this file directly, so npm lifecycle hooks alone cannot
  // guarantee that every provider callback participates in session retirement.
  // Verification is read-only and precedes all provider imports and data access.
  await patchBaileys(undefined, { verifyOnly: true });
  // Install the filter before loading the provider and its Signal dependency.
  const { acquireDataDirectory } = await import('./storage.js');
  const { Bridge } = await import('./bridge.js');
  const { createServer } = await import('./server.js');
  return {config: readConfig(), acquireDataDirectory, createBridge: config => new Bridge(config), createServer};
}});
void runtime.start().catch(() => {});
void runtime.done.then(({exitCode}) => process.exit(exitCode));
