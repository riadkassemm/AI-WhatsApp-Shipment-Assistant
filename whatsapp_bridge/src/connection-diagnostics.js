import { monitorEventLoopDelay, performance } from 'node:perf_hooks';

// Fixed labels only. Never retain provider errors, stanzas, addresses or keys.
export function classifyDisconnect(error) {
  if (error?.bridgePersistenceFailure) return { category: 'storage_failure', retry: false };
  const code = error?.output?.statusCode;
  if (code === 401) return { category: 'logged_out', retry: false };
  if (code === 403) return { category: 'authentication_failure', retry: false };
  if (code === 440) return { category: 'session_conflict', retry: false };
  if ([411, 500].includes(code)) return { category: 'protocol_failure', retry: false };
  if (code === 515) return { category: 'restart_required', retry: true };
  return { category: 'temporary_transport', retry: true };
}

export function reconnectDelay(attempt, restartRequired = false, random = Math.random) {
  const ceiling = Math.min(60000, (restartRequired ? 1000 : 2000) * 2 ** Math.min(attempt, 6));
  // Equal jitter preserves a quiet period, even with an unlucky random draw.
  return Math.floor(ceiling / 2 + ceiling / 2 * Math.max(0, Math.min(1, random())));
}

export function newConnectionDiagnostics() {
  return { supported: false, version: 2, disconnects: {}, disconnects_total: 0, reconnect_attempts: 0,
    recovery_count: 0, recovery_seconds_sum: 0, recovery_seconds_last: 0,
    heartbeat_requests: 0, heartbeat_successes: 0, heartbeat_query_timeouts: 0,
    heartbeat_failures: 0, heartbeat_timeouts: 0, raw_frames: 0, decoded_frames: 0,
    last_category: null, ...generationDiagnostics() };
}

function generationDiagnostics() {
  return { last_frame_at: null, last_raw_frame_at: null, heartbeat_observed_at: null,
    heartbeat_request_at: null, heartbeat_success_at: null, heartbeat_query_completed_at: null,
    heartbeat_rtt_ms: null, heartbeat_query_elapsed_ms: null, heartbeat_query_sequence: 0,
    heartbeat_outcome: 'unknown', heartbeat_frame_age_ms: 0, heartbeat_timer_delay_ms: 0,
    transport: null, transport_error_code: null, transport_close_code: null,
    socket_error_code: null, socket_end_at: null, socket_closed_at: null, socket_close_had_error: null };
}

export function resetConnectionDiagnostics(diagnostics, generation) {
  Object.assign(diagnostics, generationDiagnostics(), { generation, generation_started_at: Date.now() });
}

export function beginInterruption(diagnostics, now = Date.now(), monotonic = performance.now()) {
  diagnostics.recovery_started_at ??= now;
  diagnostics.recovery_started_monotonic ??= monotonic;
}

export function finishInterruption(diagnostics, monotonic = performance.now()) {
  if (diagnostics.recovery_started_monotonic == null) return null;
  const seconds = Math.max(0, monotonic - diagnostics.recovery_started_monotonic) / 1000;
  diagnostics.recovery_count++;
  diagnostics.recovery_seconds_sum += seconds;
  diagnostics.recovery_seconds_last = seconds;
  delete diagnostics.recovery_started_at;
  delete diagnostics.recovery_started_monotonic;
  return seconds;
}

export function observeHeartbeat(diagnostics, sample, now = Date.now()) {
  if (sample.event === 'raw_frame') {
    diagnostics.last_raw_frame_at = now; diagnostics.raw_frames++; return false;
  }
  if (sample.event === 'decoded_frame') {
    diagnostics.last_frame_at = now; diagnostics.decoded_frames++; return false;
  }
  if (!['heartbeat_tick', 'heartbeat_request', 'heartbeat_result', 'heartbeat_query_timeout',
    'heartbeat_error', 'heartbeat_timeout'].includes(sample.event)) return false;
  const bounded = value => Number.isFinite(value) ? Math.max(0, Math.min(value, 86400000)) : null;
  const queryEvent = ['heartbeat_request', 'heartbeat_result', 'heartbeat_query_timeout', 'heartbeat_error'].includes(sample.event);
  if (queryEvent) {
    if (!Number.isSafeInteger(sample.query_sequence) || sample.query_sequence < 1 ||
        sample.query_sequence < diagnostics.heartbeat_query_sequence) return false;
    if (sample.event === 'heartbeat_request' && sample.query_sequence === diagnostics.heartbeat_query_sequence) return false;
    if (sample.event !== 'heartbeat_request' && (sample.query_sequence !== diagnostics.heartbeat_query_sequence ||
        diagnostics.heartbeat_outcome !== 'pending')) return false;
  }
  diagnostics.heartbeat_observed_at = now;
  if (sample.event === 'heartbeat_tick') {
    diagnostics.heartbeat_frame_age_ms = bounded(sample.frame_age_ms) ?? 0;
    diagnostics.heartbeat_timer_delay_ms = bounded(sample.timer_delay_ms) ?? 0;
  }
  if (sample.event === 'heartbeat_request') {
    diagnostics.heartbeat_requests++;
    diagnostics.heartbeat_query_sequence = sample.query_sequence;
    diagnostics.heartbeat_request_at = now;
    diagnostics.heartbeat_outcome = 'pending';
    diagnostics.heartbeat_rtt_ms = null;
    diagnostics.heartbeat_query_elapsed_ms = null;
  }
  if (['heartbeat_result', 'heartbeat_query_timeout', 'heartbeat_error'].includes(sample.event)) {
    diagnostics.heartbeat_query_completed_at = now;
    diagnostics.heartbeat_query_elapsed_ms = bounded(sample.query_elapsed_ms);
    diagnostics.heartbeat_rtt_ms = null;
    if (sample.event === 'heartbeat_result' && bounded(sample.rtt_ms) != null) {
      diagnostics.heartbeat_outcome = 'success';
      diagnostics.heartbeat_successes++;
      diagnostics.heartbeat_success_at = now;
      diagnostics.heartbeat_rtt_ms = bounded(sample.rtt_ms);
    } else {
      diagnostics.heartbeat_outcome = sample.event === 'heartbeat_query_timeout' ? 'timeout' : 'error';
      if (sample.event === 'heartbeat_query_timeout') diagnostics.heartbeat_query_timeouts++;
      else diagnostics.heartbeat_failures++;
      beginInterruption(diagnostics, now);
    }
  }
  if (sample.event === 'heartbeat_timeout') {
    diagnostics.heartbeat_timeouts++;
    diagnostics.heartbeat_rtt_ms = null;
    diagnostics.heartbeat_outcome = 'timeout';
    beginInterruption(diagnostics, now);
  }
  return true;
}

export class EventLoopDiagnostics {
  start() {
    if (this.histogram) return;
    this.histogram = monitorEventLoopDelay({ resolution: 20 });
    this.histogram.enable();
    this.lagSpikes = 0;
    this.timer = setInterval(() => {
      if (this.histogram.max / 1e6 > 1000) this.lagSpikes++;
      this.last = { lag_spikes_total: this.lagSpikes, sampled_at: Date.now(), p99_ms: this.histogram.percentile(99) / 1e6,
        max_ms: this.histogram.max / 1e6 };
      this.histogram.reset();
    }, 10000);
    this.timer.unref();
  }
  snapshot() {
    return { ...(this.last ?? { sampled_at: null, p99_ms: null, max_ms: null }),
      current_max_ms: this.histogram ? this.histogram.max / 1e6 : null };
  }
  close() { clearInterval(this.timer); this.histogram?.disable(); }
}
