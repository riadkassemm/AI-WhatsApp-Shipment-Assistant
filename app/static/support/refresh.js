// One visible-tab coordinator. SSE invalidates scopes; HTTP is a slow safety net.
// The clock/timers are injectable so regressions use deterministic scheduling.
class DashboardRefreshCoordinator {
  constructor({visible, enabled, run, interval,
      later = (callback, delay) => setTimeout(callback, delay), cancel = timer => clearTimeout(timer)}) {
    Object.assign(this, {visible, enabled, run, interval, later, cancel});
    this.pending = new Set(); this.running = false; this.started = false; this.timer = null; this.soon = false;
  }
  start() { this.stop(); this.started = true; this.arm(this.interval()); }
  stop() { this.started = false; this.cancel(this.timer); this.timer = null; this.soon = false; this.pending.clear(); }
  arm(delay) {
    this.cancel(this.timer); this.timer = null; this.soon = delay === 100;
    if (this.started && this.visible() && this.enabled()) this.timer = this.later(() => this.flush(), delay);
  }
  request(...scopes) {
    for (const scope of scopes) this.pending.add(scope);
    if (!this.running && !this.soon) this.arm(100);
  }
  visibilityChanged() {
    this.cancel(this.timer); this.timer = null; this.soon = false;
    if (this.visible()) this.request('queue', 'conversation', 'counts');
  }
  async flush() {
    this.timer = null; this.soon = false;
    if (!this.started || !this.visible() || !this.enabled() || this.running) return;
    const scopes = this.pending.size ? new Set(this.pending) : new Set(['routine']);
    this.pending.clear(); this.running = true;
    try { await this.run(scopes); } catch (_) { /* Next event or fallback retries. */ }
    finally {
      this.running = false;
      if (this.started) this.arm(this.pending.size ? 100 : this.interval());
    }
  }
}

const dashboardRefresh = new DashboardRefreshCoordinator({
  visible: () => !document.hidden,
  enabled: () => state.authenticated && !state.pageLeaving,
  interval: () => state.activeModal === 'whatsappModal' ? 2000 : Math.max(5000, __POLL_MS__),
  run: async scopes => {
    if (state.busy) return;
    if (state.activeModal) {
      if (state.activeModal === 'whatsappModal' && !state.whatsappBusy && !state.linkedDeviceBusy) await loadLinkedDeviceStatus();
      return;
    }
    const routine = scopes.has('routine');
    const tasks = [];
    if (routine || scopes.has('queue')) tasks.push(loadQueue({silent: true}));
    if (routine || scopes.has('counts')) tasks.push(refreshAllQueueCounts());
    const audioPlaying = [...document.querySelectorAll('#conversation audio')].some(audio => !audio.paused && !audio.ended);
    if ((routine || scopes.has('conversation') || scopes.has('history')) && state.ticketId &&
        !state.mediaRecorder && !state.voiceRecording && !state.conversationSearchOpen && !audioPlaying) {
      tasks.push(openTicket(state.ticketId, {silent: true, refreshHistory: scopes.has('history')}));
    }
    if (routine && !state.aiSettingsBusy) tasks.push(loadAiSettings().catch(error => renderAiSettingsUnavailable(error.message)));
    await Promise.allSettled(tasks);
  }
});
function requestDashboardRefresh(...scopes) {
  if (scopes.includes('counts')) invalidateQueueCounts();
  dashboardRefresh.request(...scopes);
}
function startDashboardRefresh() { dashboardRefresh.start(); }
function stopDashboardRefresh() { dashboardRefresh.stop(); resetDashboardRequests(); }
function dashboardVisibilityChanged() {
  if (!document.hidden) clearAttentionIndicator();
  dashboardRefresh.visibilityChanged();
}
document.addEventListener('visibilitychange', dashboardVisibilityChanged);
window.addEventListener('pagehide', stopDashboardRefresh);
