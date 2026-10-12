import path from 'node:path';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';
import { readConfig } from './config.js';
import { installLibsignalConsoleFilter } from './safe-console.js';

process.umask(0o077);
installLibsignalConsoleFilter();
dotenv.config({ path: path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../.env'), quiet: true });
let release;
let bridge;
let server;
let stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  // Close listener first; keep auth sessions so a restart reconnects without scanning.
  if (server) { server.close(); server.closeIdleConnections(); }
  await bridge?.close();
  await release?.();
}
try {
  // Install the filter before loading the provider and its Signal dependency.
  const { acquireDataDirectory } = await import('./storage.js');
  const { Bridge } = await import('./bridge.js');
  const { createServer } = await import('./server.js');
  const config = readConfig();
  release = await acquireDataDirectory(config.dataDir);
  bridge = await new Bridge(config).initialize();
  server = createServer(bridge, config);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(config.port, config.host, resolve);
  });
  bridge.startWorkers();
  console.log(`WhatsApp linked-device bridge listening on ${config.host}:${config.port}`);
  process.once('SIGINT', () => stop().then(() => process.exit(0), () => process.exit(1)));
  process.once('SIGTERM', () => stop().then(() => process.exit(0), () => process.exit(1)));
} catch (error) {
  console.error(error.code === 'ELOCKED' ? 'Another bridge process owns this session data directory.' : error.message);
  await stop().catch(() => {});
  process.exitCode = 1;
}
