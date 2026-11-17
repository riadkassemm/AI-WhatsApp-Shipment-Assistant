// A stand-in for line-worker.js: same IPC protocol and unix socket, no Baileys.
// Behaviour per number comes from FAKE_WORKER_CONFIG (JSON keyed by line ID).
import http from 'node:http';
import { mkdir, unlink, writeFile, readFile } from 'node:fs/promises';
import path from 'node:path';
import { workerSocketPath } from '../src/config.js';
import { createSupervisorHost } from '../src/worker-ipc.js';

const lineId = process.env.WHATSAPP_LINKED_DEVICE_WORKER_LINE_ID;
const dataDir = process.env.WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY;
const settings = JSON.parse(process.env.FAKE_WORKER_CONFIG || '{}')[lineId] ?? {};
const host = createSupervisorHost(process);
const directory = path.join(dataDir, lineId);

// Crash-loop on demand, counted on disk so a test can stop the loop.
const failures = path.join(dataDir, `fake-fail-${lineId}`);
let remaining = 0;
try { remaining = Number(await readFile(failures, 'utf8')); } catch { /* none */ }
if (settings.failStart || remaining > 0) {
  if (remaining > 0) await writeFile(failures, String(remaining - 1));
  process.exit(3);
}
await mkdir(directory, { recursive: true, mode: 0o700 });
await writeFile(path.join(directory, 'state.json'), '{}');

const state = () => ({ line_id: lineId, bound_phone: settings.boundPhone ?? null, status: 'connected', disconnected_since: null,
  recover_since: null, runtime: { ready: true, phase: 'ready', recovery_required: [], recovery_mode: 'none' } });
const body = request => new Promise(resolve => {
  const chunks = [];
  request.on('data', chunk => chunks.push(chunk));
  request.on('end', () => { try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')); } catch { resolve({}); } });
});
const send = (response, status, payload) => {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(payload));
};
const server = http.createServer(async (request, response) => {
  if (request.headers.authorization !== `Bearer ${process.env.WHATSAPP_LINKED_DEVICE_SECRET}`) return send(response, 401, { error: { code: 'UNAUTHORIZED' } });
  const url = new URL(request.url, 'http://localhost');
  const payload = await body(request);
  if (request.method === 'GET' && url.pathname === '/v1/status') {
    return send(response, 200, {
      lines: [{ line_id: lineId, display_phone_number: settings.boundPhone ?? '', status: 'connected', is_paired: true, pid: process.pid }],
      storage: { level: 'warning', checked_at: Date.now(), disk: { available_bytes: 1 }, stores: [{ line_id: lineId, kind: 'state' }],
        issues: [{ code: 'DISK_SPACE_LOW', level: 'warning' }, { code: 'JOURNAL_LIMIT', level: 'warning', line_id: lineId, kind: 'state' }],
        pending_optional_write_bytes: 1 },
      runtime: { live: true, ready: true, phase: 'ready', recovery_required: [], recovery_mode: 'none' },
      event_loop: { sampled_at: Date.now() - Number(settings.loopAgeMs ?? 0), p99_ms: Number(settings.p99 ?? 1), max_ms: 2, lag_spikes_total: 1 },
    });
  }
  if (request.method === 'DELETE' && url.pathname === `/v1/lines/${lineId}`) {
    await host.call('tombstone', { line_id: lineId });
    return send(response, 200, { line_id: lineId, removed: true, unlinked: true });
  }
  if (request.method === 'POST' && url.pathname === `/v1/lines/${lineId}/messages`) {
    return send(response, 200, { message_id: `ld:${lineId}:${payload.client_message_id ?? 'x'}`, pid: process.pid });
  }
  if (request.method === 'POST' && url.pathname === `/v1/lines/${lineId}/crash`) process.exit(1);
  if (request.method === 'POST' && url.pathname === `/v1/lines/${lineId}/media`) {
    setTimeout(() => send(response, 200, { media_id: 'm1' }), Number(payload.slow_ms ?? 0));
    return;
  }
  if (request.method === 'POST' && url.pathname === `/v1/lines/${lineId}/rpc`) {
    try { return send(response, 200, { result: await host.call(payload.method, payload.params ?? {}, { timeoutMs: 0 }) }); }
    catch (error) { return send(response, error.status ?? 500, { error: { code: error.code, message: error.message } }); }
  }
  send(response, 404, { error: { code: 'NOT_FOUND' } });
});
const socketPath = workerSocketPath(dataDir, lineId);
await mkdir(path.dirname(socketPath), { recursive: true, mode: 0o700 });
await unlink(socketPath).catch(() => {});
server.listen(socketPath, () => {
  if (!settings.neverReady) host.notify('ready', state());
});
const stop = () => server.close(() => process.exit(0));
process.on('SIGTERM', () => { if (!settings.ignoreSigterm) stop(); });
process.on('disconnect', stop);
