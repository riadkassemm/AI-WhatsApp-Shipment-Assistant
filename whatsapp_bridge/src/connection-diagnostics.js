import { monitorEventLoopDelay } from 'node:perf_hooks';

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
  return { supported: false, disconnects: {}, disconnects_total: 0, reconnect_attempts: 0,
    recovery_count: 0, recovery_seconds_sum: 0, recovery_seconds_last: 0,
    last_frame_at: null, heartbeat_observed_at: null, heartbeat_rtt_ms: null,
    heartbeat_frame_age_ms: 0, heartbeat_timer_delay_ms: 0,
    heartbeat_failures: 0, heartbeat_timeouts: 0, last_category: null };
}

export function observeHeartbeat(diagnostics, sample, now = Date.now()) {
  if (sample.event === 'decoded_frame') { diagnostics.last_frame_at = now; return false; }
  if (!['heartbeat_tick', 'heartbeat_result', 'heartbeat_error', 'heartbeat_timeout'].includes(sample.event)) return false;
  diagnostics.heartbeat_observed_at = now;
  const bounded = value => Number.isFinite(value) ? Math.max(0, Math.min(value, 86400000)) : null;
  if (sample.event === 'heartbeat_tick') {
    diagnostics.heartbeat_frame_age_ms = bounded(sample.frame_age_ms) ?? 0;
    diagnostics.heartbeat_timer_delay_ms = bounded(sample.timer_delay_ms) ?? 0;
  }
  if (sample.event === 'heartbeat_result') diagnostics.heartbeat_rtt_ms = bounded(sample.rtt_ms);
  if (sample.event === 'heartbeat_error') diagnostics.heartbeat_failures++;
  if (sample.event === 'heartbeat_timeout') diagnostics.heartbeat_timeouts++;
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
  snapshot() { return this.last ?? { sampled_at: null, p99_ms: null, max_ms: null }; }
  close() { clearInterval(this.timer); this.histogram?.disable(); }
}
