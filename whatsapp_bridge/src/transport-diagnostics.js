import { isIP } from 'node:net';

const networkErrors = new Set(['ECONNRESET', 'ECONNREFUSED', 'EPIPE', 'ETIMEDOUT',
  'ENETUNREACH', 'EHOSTUNREACH', 'EAI_AGAIN', 'ENOTFOUND', 'ERR_TLS_CERT_ALTNAME_INVALID']);
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;

// Only endpoint metadata and counters. Never inspect URLs, headers, frames,
// error messages, close-reason buffers or the TLS/session object itself.
export function transportSnapshot(ws) {
  const raw = ws?.socket;
  const tcp = raw?._socket;
  const family = isIP(tcp?.remoteAddress ?? '');
  return { remote_ip: family ? tcp.remoteAddress : null,
    family: family === 4 ? 'IPv4' : family === 6 ? 'IPv6' : 'unknown',
    local_port: count(tcp?.localPort), remote_port: count(tcp?.remotePort),
    bytes_read: count(tcp?.bytesRead), bytes_written: count(tcp?.bytesWritten),
    writable_bytes: count(tcp?.writableLength), ready_state: count(raw?.readyState) };
}

export function observeTransport(ws, report) {
  const raw = ws?.socket;
  if (!raw?.on) return () => {};
  const emit = sample => { try { report({ ...sample, transport: transportSnapshot(ws) }); } catch {} };
  const onOpen = () => emit({ event: 'transport_open' });
  const onError = error => emit({ event: 'transport_error',
    error_code: networkErrors.has(error?.code) ? error.code : 'OTHER' });
  const onClose = code => {
    emit({ event: 'transport_closed', close_code: Number.isInteger(code) && code >= 1000 && code <= 4999 ? code : null });
    cleanup();
  };
  const cleanup = () => {
    raw.off('open', onOpen); raw.off('error', onError); raw.off('close', onClose);
  };
  raw.on('open', onOpen); raw.on('error', onError); raw.on('close', onClose);
  if (ws.isOpen) onOpen();
  return cleanup;
}

// The fast close is restricted to Baileys' definitive liveness failure. A
// query timeout alone, logout, retirement or normal shutdown cannot trigger it.
export function closeTransport(ws, error, report = () => {}) {
  const raw = ws?.socket;
  if (!raw || ws.isClosed) return Promise.resolve();
  const failedHeartbeat = error?.output?.statusCode === 408 && error?.message === 'Connection was lost';
  return new Promise((resolve, reject) => {
    let timer;
    const emit = event => { try { report({ event, transport: transportSnapshot(ws) }); } catch {} };
    const closed = () => { clearTimeout(timer); raw.off('close', closed); resolve(); };
    raw.once('close', closed);
    if (failedHeartbeat) {
      timer = setTimeout(() => {
        // Keep waiting for the actual close event after terminate(). The next
        // owner must never use a timeout as proof that this transport is gone.
        if (raw.readyState !== 3) {
          emit('failed_transport_terminate');
          try { raw.terminate(); } catch { /* retain the closed-transport barrier */ }
        }
      }, 1500);
      timer.unref();
    }
    emit('transport_close_started');
    if (!ws.isClosing) {
      // Invoke the existing adapter, retaining its own close cleanup. A close
      // rejection does not resolve the ownership barrier while a socket lives.
      Promise.resolve().then(() => ws.close()).catch(() => {
        if (raw.readyState === 3) closed();
        else if (!failedHeartbeat) {
          raw.off('close', closed);
          reject(new Error('TRANSPORT_CLOSE_NOT_CONFIRMED'));
        }
      });
    }
    if (raw.readyState === 3) closed();
  });
}
