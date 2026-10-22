import http from 'node:http';
import crypto from 'node:crypto';
import { BridgeError } from './config.js';

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
  const server = http.createServer(async (request, response) => {
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    try {
      if (!authorize(request, config.secret)) throw new BridgeError(401, 'UNAUTHORIZED', 'Bridge authentication is required.');
      const pathname = new URL(request.url, 'http://localhost').pathname;
      let parts;
      try { parts = pathname.split('/').filter(Boolean).map(decodeURIComponent); }
      catch { throw new BridgeError(400, 'INVALID_PATH', 'The bridge endpoint contains invalid URL encoding.'); }
      if (request.method === 'GET' && pathname === '/v1/status') return send(response, 200, bridge.status());
      if (request.method === 'POST' && parts.length === 2 && parts[0] === 'v1' && parts[1] === 'lines') {
        return send(response, 200, await bridge.addLine(await readJson(request, 1024 * 1024)));
      }
      if (parts[0] !== 'v1' || parts[1] !== 'lines' || !/^\d{5,32}$/.test(parts[2] ?? '')) throw new BridgeError(404, 'NOT_FOUND', 'Unknown bridge endpoint.');
      const id = parts[2];
      if (request.method === 'DELETE' && parts.length === 3) return send(response, 200, await bridge.removeLine(id));
      bridge.getLine(id);
      if (request.method === 'GET' && parts.length === 5 && parts[3] === 'media') {
        const media = await bridge.downloadMedia(id, parts[4]);
        response.writeHead(200, { 'Content-Type': media.mime_type, 'Content-Length': media.content.length });
        return response.end(media.content);
      }
      if (request.method !== 'POST') throw new BridgeError(405, 'METHOD_NOT_ALLOWED', 'This endpoint requires POST.');
      const limit = parts[3] === 'media' ? Math.ceil(config.maxMediaBytes * 4 / 3) + 4096 : 64 * 1024;
      const payload = await readJson(request, limit);
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
        // Phone numbers in provider errors are masked; message content is never part of them.
        const detail = String(error?.message ?? '').replace(/\d{6,}/g, '#').slice(0, 160);
        console.warn(`Linked-device request failed: route=${request.method} ${String(request.url ?? '').split('?')[0].replace(/\/(messages|media)\/[^/]+/, '/$1/:id')} status=${status} code=${error?.code ?? error?.output?.statusCode ?? error?.name ?? 'unknown'} error=${detail}`);
      }
      const uncertainCodes = ['CONFIRMATION_UNKNOWN', 'DISCONNECTED', 'SEND_FAILED', 'INVALID_PROVIDER_RESPONSE'];
      const deliveryUncertain = error.delivery_uncertain ?? (!isKnown || uncertainCodes.includes(error.code));
      send(response, status, { delivery_uncertain: deliveryUncertain,
        error: { code: isKnown ? error.code : 'BRIDGE_ERROR', message: isKnown ? error.message : 'The bridge could not complete this operation.' } });
    }
  });
  server.requestTimeout = 60_000;
  server.headersTimeout = 10_000;
  server.keepAliveTimeout = 5000;
  return server;
}
