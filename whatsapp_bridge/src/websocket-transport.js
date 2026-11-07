import { Agent } from 'node:https';

export function readWebSocketIpFamily(value) {
  // 0/unset retains the provider and Node's existing address selection.
  if (value === undefined || value === '' || value === '0') return 0;
  if (value === '4') return 4;
  throw new Error('WHATSAPP_WS_IP_FAMILY must be 0 (system default) or 4 (IPv4 only).');
}

export function createWebSocketAgent(family = 0) {
  if (family === 0) return undefined;
  if (family !== 4) throw new Error('Unsupported WhatsApp WebSocket IP family.');
  // Baileys passes agent only to its WebSocket client (media uses fetchAgent).
  // Restrict DNS/TCP selection, retaining the URL hostname for SNI and Node's
  // normal certificate/hostname checks. No global DNS, agent or TLS changes.
  // No IPv6 fallback, including on reconnect. Upgraded sockets are independent
  // and leave the HTTP agent's pool; closing one does not retire another line.
  return new Agent({family: 4, autoSelectFamily: false, rejectUnauthorized: true, keepAlive: false});
}
