import path from 'node:path';
import { fileURLToPath } from 'node:url';

export class BridgeError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}
export const MANIFEST_MAX_BYTES = 128 * 1024;
export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function validateLineManifest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || (value.version !== undefined && value.version !== 1) ||
      !value.lines || typeof value.lines !== 'object' || Array.isArray(value.lines)) throw new Error('Invalid linked-device line manifest.');
  if (Buffer.byteLength(JSON.stringify(value), 'utf8') > MANIFEST_MAX_BYTES) throw new Error('Linked-device line manifest exceeds 128 KiB.');
  const lines = Object.fromEntries(Object.entries(value.lines).map(([id, phone]) => {
    if (!/^\d{5,32}$/.test(id) || typeof phone !== 'string') throw new Error('Invalid linked-device line manifest.');
    return [id, phoneDigits(phone, true)];
  }));
  const removed = value.removed_line_ids ?? [];
  const removedRequests = value.removed_request_ids ?? [];
  if (!Array.isArray(removed) || removed.some(id => typeof id !== 'string' || !/^\d{5,32}$/.test(id)) ||
      !Array.isArray(removedRequests) || removedRequests.some(id => typeof id !== 'string' || !UUID_PATTERN.test(id))) throw new Error('Invalid linked-device removal ledger.');
  if (removed.some(id => Object.hasOwn(lines, id))) throw new Error('A removed linked-device line cannot remain active in the manifest.');
  const rawRequests = value.requests ?? {};
  if (!rawRequests || typeof rawRequests !== 'object' || Array.isArray(rawRequests)) throw new Error('Invalid linked-device registration ledger.');
  const requests = Object.fromEntries(Object.entries(rawRequests).map(([id, item]) => {
    if (!UUID_PATTERN.test(id) || !item || typeof item !== 'object' || Array.isArray(item) ||
        typeof item.line_id !== 'string' || typeof item.expected_phone !== 'string' || !Object.hasOwn(lines, item.line_id)) throw new Error('Invalid linked-device registration ledger.');
    const phone = phoneDigits(item.expected_phone, true);
    if (lines[item.line_id] !== phone) throw new Error('Conflicting linked-device registration ledger.');
    return [id.toLowerCase(), { line_id: item.line_id, expected_phone: phone }];
  }));
  if (removedRequests.some(id => Object.hasOwn(requests, id.toLowerCase()))) throw new Error('A removed registration request cannot remain active.');
  return { version: 1, lines, requests, removed_line_ids: [...new Set(removed)], removed_request_ids: [...new Set(removedRequests.map(id => id.toLowerCase()))] };
}

export function phoneDigits(value, allowEmpty = false) {
  const raw = String(value ?? '').trim();
  if (!raw && allowEmpty) return '';
  if (!/^\+?[0-9 ()-]+$/.test(raw)) throw new BridgeError(400, 'INVALID_PHONE', 'A valid international phone number is required.');
  const phone = raw.replace(/\D/g, '');
  if (!/^\d{5,15}$/.test(phone)) throw new BridgeError(400, 'INVALID_PHONE', 'A valid international phone number is required.');
  return phone;
}

export function readConfig(env = process.env) {
  const secret = env.WHATSAPP_LINKED_DEVICE_SECRET ?? '';
  if (secret.length < 32) throw new Error('WHATSAPP_LINKED_DEVICE_SECRET must contain at least 32 characters.');
  let mapping;
  try {
    mapping = env.WHATSAPP_LINKED_DEVICE_LINES ? JSON.parse(env.WHATSAPP_LINKED_DEVICE_LINES) : (env.WHATSAPP_PHONE_NUMBER_ID ? { [env.WHATSAPP_PHONE_NUMBER_ID]: '' } : {});
  } catch { throw new Error('WHATSAPP_LINKED_DEVICE_LINES must be a JSON object.'); }
  if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)) throw new Error('WHATSAPP_LINKED_DEVICE_LINES must be a JSON object.');
  let fallbackLineId;
  if ((!env.WHATSAPP_LINKED_DEVICE_LINES || !Object.keys(mapping).length) && env.WHATSAPP_PHONE_NUMBER_ID) {
    fallbackLineId = env.WHATSAPP_PHONE_NUMBER_ID;
    mapping = { [fallbackLineId]: '' };
  }
  const lines = Object.entries(mapping).map(([id, phone]) => {
    if (!/^\d{5,32}$/.test(id) || typeof phone !== 'string') throw new Error('Linked-device lines require numeric line IDs (5–32 digits) and phone strings.');
    return { line_id: id, expected_phone: phoneDigits(phone, true) };
  });
  const endpoint = new URL(env.WHATSAPP_LINKED_DEVICE_URL ?? 'http://127.0.0.1:3001');
  if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.pathname !== '/') throw new Error('WHATSAPP_LINKED_DEVICE_URL must be a bare HTTP(S) origin.');
  const host = env.WHATSAPP_LINKED_DEVICE_BIND_HOST ?? '127.0.0.1';
  if (!['127.0.0.1', '::1', 'localhost'].includes(host)) throw new Error('The bridge must bind to a loopback address; use a secured reverse proxy for remote access.');
  const callbackUrl = new URL(env.WHATSAPP_LINKED_DEVICE_CALLBACK_URL ?? 'http://127.0.0.1:8000/api/whatsapp/linked-device/events');
  if (!['http:', 'https:'].includes(callbackUrl.protocol) || callbackUrl.username || callbackUrl.password) throw new Error('The callback URL must be HTTP(S) without embedded credentials.');
  return {
    secret, lines, fallbackLineId, host, port: Number(endpoint.port || (endpoint.protocol === 'https:' ? 443 : 80)), callbackUrl: callbackUrl.href,
    dataDir: path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..', env.WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY ?? 'whatsapp_bridge/data'),
    maxMediaBytes: 25 * 1024 * 1024, ackTimeoutMs: 20_000,
  };
}

export function wrapId(lineId, nativeId) {
  const wrapped = `ld:${lineId}:${nativeId}`;
  if (!/^\d{5,32}$/.test(lineId) || !/^[A-Za-z0-9_.-]+$/.test(nativeId ?? '') || wrapped.length > 191) throw new BridgeError(400, 'INVALID_MESSAGE_ID', 'Invalid linked-device message ID.');
  return wrapped;
}

export function unwrapId(lineId, wrapped) {
  const prefix = `ld:${lineId}:`;
  if (typeof wrapped !== 'string' || !wrapped.startsWith(prefix)) throw new BridgeError(409, 'UNSUPPORTED_MESSAGE', 'Only messages captured by this linked-device line can be changed.');
  const nativeId = wrapped.slice(prefix.length);
  wrapId(lineId, nativeId);
  return nativeId;
}
