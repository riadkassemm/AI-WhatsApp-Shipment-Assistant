import { BridgeError } from './config.js';

// Worker side of the supervisor channel (Node's fork IPC, JSON serialization).
//
// worker → supervisor  {type: 'rpc', id, method, params}
//                      {type: 'ready' | 'report', state}
// supervisor → worker  {type: 'rpc_result', id, result} or {type: 'rpc_result', id, error}
//
// A call never outlives the channel: when the supervisor goes away, every
// pending call fails with SUPERVISOR_UNAVAILABLE.
export function createSupervisorHost(channel = process, { defaultTimeoutMs = 30_000 } = {}) {
  let next = 0;
  const pending = new Map();
  const unavailable = () => new BridgeError(503, 'SUPERVISOR_UNAVAILABLE', 'The bridge supervisor is not reachable.');
  const settle = (id, outcome) => {
    const call = pending.get(id);
    if (!call) return;
    pending.delete(id);
    clearTimeout(call.timer);
    outcome(call);
  };
  channel.on('message', message => {
    if (message?.type !== 'rpc_result' || !pending.has(message.id)) return;
    settle(message.id, call => {
      if (!message.error) return call.resolve(message.result);
      const { status = 500, code = 'SUPERVISOR_ERROR', message: text = 'The bridge supervisor refused this operation.' } = message.error;
      call.reject(new BridgeError(status, code, text));
    });
  });
  channel.on('disconnect', () => {
    for (const id of [...pending.keys()]) settle(id, call => call.reject(unavailable()));
  });
  const send = message => new Promise((resolve, reject) => {
    if (!channel.connected || typeof channel.send !== 'function') return reject(unavailable());
    try { channel.send(message, error => error ? reject(unavailable()) : resolve()); }
    catch { reject(unavailable()); }
  });
  return {
    call(method, params = {}, { timeoutMs = defaultTimeoutMs } = {}) {
      const id = ++next;
      const result = new Promise((resolve, reject) => {
        const timer = timeoutMs ? setTimeout(() => settle(id, call => call.reject(new BridgeError(504, 'SUPERVISOR_TIMEOUT',
          'The bridge supervisor did not answer in time.'))), timeoutMs) : null;
        timer?.unref?.();
        pending.set(id, { resolve, reject, timer });
      });
      send({ type: 'rpc', id, method, params }).catch(error => settle(id, call => call.reject(error)));
      return result;
    },
    notify(type, state) { send({ type, state }).catch(() => {}); },
  };
}

// What the supervisor needs to know about this worker's number: the bound
// phone (duplicate-link check), the offline time (retired phones on a forced
// removal) and whether the worker is healthy (bridge readiness).
export function workerState(bridge, lineId) {
  const line = bridge.lines.get(lineId);
  const health = bridge.runtimeHealth();
  return {
    line_id: lineId,
    bound_phone: line?.store?.data?.bound_phone || null,
    recover_since: line?.store?.data?.recover_since ?? null,
    status: line?.status ?? null,
    disconnected_since: line ? (line.status === 'connected' ? null : line.disconnectedSince ?? null) : null,
    runtime: { ready: health.ready, phase: health.phase, recovery_required: health.recovery_required, recovery_mode: health.recovery_mode },
  };
}
