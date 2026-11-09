import http from 'node:http';
import crypto from 'node:crypto';
import { BridgeError } from './config.js';
import { safeStorageCode } from './journal-maintenance.js';

function authorize(request, secret) {
  const expected = Buffer.from(`Bearer ${secret}`);
  const supplied = Buffer.from(request.headers.authorization ?? '');
  return supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected);
}
async function readJson(request, limit) {
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers['content-type'] ?? '')) throw new BridgeError(415, 'JSON_REQUIRED', 'Send an application/json request.');
  const length = Number(request.headers['content-length'] ?? 0);
  if (length > limit) throw new BridgeError(413, 'REQUEST_TOO_LARGE', 'Request body exceeds the bridge limit.');
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new BridgeError(413, 'REQUEST_TOO_LARGE', 'Request body exceeds the bridge limit.');
    chunks.push(chunk);
  }
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid JSON object');
    return value;
  } catch { throw new BridgeError(400, 'INVALID_JSON', 'Send a valid JSON object.'); }
}

export function createServer(bridge, config) {
  const send = (response, status, payload) => {
    response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
    response.end(JSON.stringify(payload));
  };
  const activeRequests = new Set();
  let activeMediaUploads = 0;
  let requestFailed = false, requestFailure;
  const handleRequest = async (request, response) => {
    let ownsMediaSlot = false;
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    try {
      if (!authorize(request, config.secret)) throw new BridgeError(401, 'UNAUTHORIZED', 'Bridge authentication is required.');
      const pathname = new URL(request.url, 'http://localhost').pathname;
      if (request.method === 'GET' && ['/v1/health/live', '/v1/health/ready'].includes(pathname)) {
        const health = bridge.runtimeHealth();
        return send(response, (pathname.endsWith('/live') ? health.live : health.ready) ? 200 : 503, health);
      }
      if (bridge.stopping || bridge.initialized === false) {
        const error = new BridgeError(503, bridge.stopping ? 'STOPPING' : 'BRIDGE_INITIALIZING',
          bridge.stopping ? 'The bridge is shutting down.' : 'The bridge is loading saved sessions. Replies will wait until it is ready.');
        error.delivery_uncertain = false;
        throw error;
      }
      let parts;
      try { parts = pathname.split('/').filter(Boolean).map(decodeURIComponent); }
      catch { throw new BridgeError(400, 'INVALID_PATH', 'The bridge endpoint contains invalid URL encoding.'); }
      if (request.method === 'GET' && pathname === '/v1/status') return send(response, 200, bridge.status());
      if (request.method === 'POST' && pathname === '/v1/shutdown-preflight') {
        // Explicit operator action: seals only proven failed owners in memory.
        // No signals, healthy socket closure, credential writes or fence clearing.
        try { await bridge.prepareShutdown(); }
        catch (error) { return send(response, 503, {shutdown_safe: false, code: safeStorageCode(error), runtime: bridge.runtimeHealth()}); }
        return send(response, 200, {shutdown_safe: true, runtime: bridge.runtimeHealth()});
      }
      if (request.method === 'GET' && pathname === '/v1/storage') {
        if (!bridge.storageMonitor) throw new BridgeError(503, 'STORAGE_MONITOR_UNAVAILABLE', 'Storage monitoring is not initialized.');
        return send(response, 200, bridge.storageMonitor.status());
      }
      if (request.method === 'POST' && parts.length === 2 && parts[0] === 'v1' && parts[1] === 'lines') {
        return send(response, 200, await bridge.addLine(await readJson(request, 1024 * 1024)));
      }
      if (parts[0] !== 'v1' || parts[1] !== 'lines' || !/^\d{5,32}$/.test(parts[2] ?? '')) throw new BridgeError(404, 'NOT_FOUND', 'Unknown bridge endpoint.');
      const id = parts[2];
      if (request.method === 'POST' && parts.length === 4 && parts[3] === 'decommission') {
        const payload = await readJson(request, 1024);
        if (payload.confirm_line_id !== id || typeof payload.actor !== 'string' || !payload.actor.trim() || payload.actor.length > 128) throw new BridgeError(400, 'DECOMMISSION_CONFIRMATION_REQUIRED', 'Confirm the selected identity and administrator.');
        return send(response, 200, await bridge.decommissionLine(id, {actor: payload.actor}));
      }
      if (request.method === 'DELETE' && parts.length === 3) return send(response, 200, await bridge.removeLine(id));
      bridge.getLine(id);
      if (parts[3] === 'management-operations') {
        const line = bridge.getLine(id);
        if (request.method === 'POST' && parts.length === 4) return send(response, 202,
          await bridge.management.submit(line, await readJson(request, 2048)));
        if (request.method === 'GET' && parts.length === 5) return send(response, 200, bridge.management.get(line, parts[4]));
      }
      if (request.method === 'GET' && parts.length === 4 && parts[3] === 'auth-recovery') {
        return send(response, 200, await bridge.assessAuthenticationRecovery(id));
      }
      if (request.method === 'GET' && parts.length === 5 && parts[3] === 'media') {
        const media = await bridge.downloadMedia(id, parts[4]);
        response.writeHead(200, { 'Content-Type': media.mime_type, 'Content-Length': media.content.length });
        return response.end(media.content);
      }
      if (request.method !== 'POST') throw new BridgeError(405, 'METHOD_NOT_ALLOWED', 'This endpoint requires POST.');
      if (parts.length === 4 && parts[3] === 'media') {
        // Reserve before reading the 256 MiB base64 body. Bound whole-request
        // allocations across offices; rejected uploads have no provider effect.
        if (activeMediaUploads >= (config.maxConcurrentMediaUploads ?? 1)) {
          throw new BridgeError(503, 'MEDIA_UPLOAD_BUSY', 'An attachment upload is in progress. This reply will wait and retry.');
        }
        activeMediaUploads++; ownsMediaSlot = true;
        // Reject critical pressure before buffering/decoding the HTTP body.
        await bridge.storageMonitor?.sample();
        bridge.storageMonitor?.assertCanSubmit(id);
      }
      const limit = parts[3] === 'media' ? Math.ceil(config.maxMediaBytes * 4 / 3) + 4096 : 64 * 1024;
      const payload = await readJson(request, limit);
      if (parts.length === 5 && parts[3] === 'messages' && parts[4] === 'read') return send(response, 200, await bridge.markRead(id, payload));
      if (parts.length === 5 && parts[3] === 'messages' && parts[4] === 'status') return send(response, 200, bridge.deliveryStatus(id, payload));
      if (parts.length === 4 && parts[3] === 'connect') return send(response, 200, await bridge.connect(id));
      if (parts.length === 4 && parts[3] === 'disconnect') return send(response, 200, await bridge.disconnect(id));
      if (parts.length === 4 && parts[3] === 'media') return send(response, 200, await bridge.uploadMedia(id, payload));
      if (parts.length === 4 && parts[3] === 'messages') return send(response, 200, await bridge.send(id, payload));
      if (parts.length === 5 && parts[3] === 'contacts' && parts[4] === 'check') return send(response, 200, await bridge.checkNumber(id, payload));
      if (parts.length === 6 && parts[3] === 'messages' && ['edit', 'delete'].includes(parts[5])) return send(response, 200, await bridge.mutate(id, parts[4], parts[5], payload));
      throw new BridgeError(404, 'NOT_FOUND', 'Unknown bridge endpoint.');
    } catch (error) {
      if (response.headersSent) return response.destroy();
      const isKnown = error instanceof BridgeError;
      const status = isKnown ? error.status : 500;
      if (status >= 500) {
        // Do not stringify provider errors: they may contain bodies, JIDs or keys.
        const providerStatus = error?.output?.statusCode;
        console.warn(JSON.stringify({ component: 'whatsapp_bridge', event: 'request_failed',
          route: `${request.method} ${String(request.url ?? '').split('?')[0].replace(/\/(messages|media)\/[^/]+/, '/$1/:id')}`,
          http_status: status, error_code: isKnown ? error.code : 'BRIDGE_ERROR',
          cause_code: safeStorageCode(error), persistence_failure: error?.bridgePersistenceFailure === true,
          provider_status: Number.isInteger(providerStatus) ? providerStatus : null }));
      }
      const uncertainCodes = ['CONFIRMATION_UNKNOWN', 'DISCONNECTED', 'SEND_FAILED', 'INVALID_PROVIDER_RESPONSE'];
      const deliveryUncertain = error.delivery_uncertain ?? (!isKnown || uncertainCodes.includes(error.code));
      send(response, status, { delivery_uncertain: deliveryUncertain,
        error: { code: isKnown ? error.code : 'BRIDGE_ERROR', message: isKnown ? error.message : 'The bridge could not complete this operation.' } });
    } finally {
      if (ownsMediaSlot) activeMediaUploads--;
    }
  };
  const server = http.createServer((request, response) => {
    // Closing a client socket does not cancel its already admitted media or
    // persistence work. Keep ownership until the full handler settles, even
    // when Node's server.close callback has already reported no connections.
    const task = handleRequest(request, response);
    activeRequests.add(task);
    task.then(() => activeRequests.delete(task), error => {
      activeRequests.delete(task);
      if (!requestFailed) {requestFailed = true; requestFailure = error;}
      try { response.destroy(); } catch (_) { /* Preserve the handler failure. */ }
      try { console.error(JSON.stringify({component: 'whatsapp_bridge', event: 'request_handler_failed'})); }
      catch (_) { /* Shutdown still observes the saved failure. */ }
    });
  });
  server.drainRequests = async () => {
    // The caller first closes connection admission. The loop also covers
    // handlers delivered while the final existing connections were closing.
    while (activeRequests.size) await Promise.allSettled([...activeRequests]);
    if (requestFailed) throw new Error('HTTP_REQUEST_DRAIN_FAILED', {cause: requestFailure});
  };
  server.requestTimeout = 60_000;
  server.headersTimeout = 10_000;
  server.keepAliveTimeout = 5000;
  return server;
}
