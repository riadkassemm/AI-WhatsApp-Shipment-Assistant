// One visible-tab coordinator. SSE invalidates scopes; HTTP is a slow safety net.
// The clock/timers are injectable so regressions use deterministic scheduling.
class DashboardRefreshCoordinator {
  constructor({visible, enabled, run, interval, retryDelay = () => null,
      later = (callback, delay) => setTimeout(callback, delay), cancel = timer => clearTimeout(timer)}) {
    Object.assign(this, {visible, enabled, run, interval, retryDelay, later, cancel});
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
      // A failed background read retries sooner, with bounded backoff, then
      // falls back to the routine interval.
      if (this.started) this.arm(this.pending.size ? 100 : (this.retryDelay(this.interval()) ?? this.interval()));
    }
  }
}

const dashboardRefresh = new DashboardRefreshCoordinator({
  visible: () => !document.hidden,
  enabled: () => state.authenticated && !state.pageLeaving,
  interval: () => state.activeModal === 'whatsappModal' ? 2000 : Math.max(5000, __POLL_MS__),
  retryDelay: interval => backgroundRetryDelay(interval),
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
    if (routine) tasks.push(loadLineHealth());
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
// Live updates (events.js). Hidden tabs keep their connection for notification
// sounds, so they keep retrying too; the browser may throttle those timers.
document.addEventListener('visibilitychange', () => { if (!document.hidden) reconnectSupportEventsNow(); });
window.addEventListener('online', reconnectSupportEventsNow);
window.addEventListener('pagehide', event => { if (event.persisted) disconnectSupportEvents(); });
window.addEventListener('pageshow', event => {
  // pagehide stopped the scheduler and live updates. After a back/forward-cache
  // restore, resume both once the other handlers clear the page-leaving flag.
  if (!event.persisted) return;
  setTimeout(() => {
    if (!state.authenticated || state.pageLeaving) return;
    startDashboardRefresh();
    reconnectSupportEventsNow();
  }, 0);
});

// Connection health shown to staff. Live updates (SSE) and background reads are
// tracked separately; already loaded data stays visible while either recovers.
const CONNECTION_NOTICE_DELAY_MS = 4000;
const CONNECTION_ESCALATE_MS = 45000;
const BACKGROUND_RETRY_BASE_MS = 2000;
const connectionHealth = {liveDownSince: null, failingScopes: new Map(), lastReadAt: null,
  retryRuns: 0, accessDenied: false, timer: null, message: ''};

function transientReadError(error) {
  if (!error || error.name === 'AbortError') return false;
  if (error.network === true) return true;
  return [408, 429, 502, 503, 504].includes(Number(error.status));
}

function backgroundRetryDelay(interval) {
  if (!connectionHealth.failingScopes.size) { connectionHealth.retryRuns = 0; return null; }
  const ceiling = Math.min(interval, BACKGROUND_RETRY_BASE_MS * 2 ** connectionHealth.retryRuns++);
  return Math.max(1000, Math.round(ceiling * (0.5 + Math.random() * 0.5)));
}

// error=null records a successful read of the scope ('queue', 'conversation', 'counts').
function noteBackgroundRead(scope, error = null) {
  if (!error) {
    connectionHealth.failingScopes.delete(scope);
    connectionHealth.lastReadAt = Date.now();
  } else if (!connectionHealth.failingScopes.has(scope)) {
    connectionHealth.failingScopes.set(scope, Date.now());
  }
  renderConnectionStatus();
}

// A scope that is no longer shown (for example a closed conversation) cannot be stale.
function forgetBackgroundRead(scope) {
  if (connectionHealth.failingScopes.delete(scope)) renderConnectionStatus();
}

function markLiveUpdates(connected) {
  if (connected) connectionHealth.liveDownSince = null;
  else if (connectionHealth.liveDownSince === null) connectionHealth.liveDownSince = Date.now();
  renderConnectionStatus();
}

function markLiveUpdatesAccessDenied() {
  connectionHealth.accessDenied = true;
  renderConnectionStatus();
}

function resetConnectionHealth() {
  clearTimeout(connectionHealth.timer);
  Object.assign(connectionHealth, {liveDownSince: null, lastReadAt: null, retryRuns: 0,
    accessDenied: false, timer: null});
  connectionHealth.failingScopes.clear();
  renderConnectionStatus();
}

function connectionNotice(now = Date.now()) {
  if (!state.authenticated) return null;
  if (connectionHealth.accessDenied) {
    return {level: 'extended', text: 'Live updates stopped because this account no longer has dashboard access. Sign in again or contact an administrator.'};
  }
  const readsFailing = connectionHealth.failingScopes.size > 0;
  const starts = [connectionHealth.liveDownSince, ...connectionHealth.failingScopes.values()].filter(value => value !== null);
  if (!starts.length) return null;
  const age = now - Math.min(...starts);
  // A brief live-update reconnect stays quiet; a failed read is shown at once.
  if (!readsFailing && age < CONNECTION_NOTICE_DELAY_MS) return {pendingMs: CONNECTION_NOTICE_DELAY_MS - age};
  const extended = age >= CONNECTION_ESCALATE_MS;
  const minutes = Math.max(1, Math.round(age / 60000));
  const duration = minutes + (minutes === 1 ? ' minute' : ' minutes');
  const updated = connectionHealth.lastReadAt
    ? ' Showing information from ' + new Date(connectionHealth.lastReadAt).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) + '.'
    : '';
  const seconds = Math.round(dashboardRefresh.interval() / 1000);
  let text;
  if (readsFailing) {
    text = extended
      ? 'Connection lost for ' + duration + '. The information shown may be out of date, and sending or other actions may fail until it returns.' + updated
      : 'Connection problem. Reconnecting…' + updated + ' Recent changes may not be shown yet.';
  } else {
    text = extended
      ? 'Live updates have been unavailable for ' + duration + '. The dashboard still refreshes every ' + seconds + ' seconds.'
      : 'Live updates are reconnecting. The dashboard refreshes every ' + seconds + ' seconds until they return.';
  }
  return {level: extended ? 'extended' : 'reconnecting', text, pendingMs: extended ? null : CONNECTION_ESCALATE_MS - age};
}

function renderConnectionStatus() {
  clearTimeout(connectionHealth.timer);
  connectionHealth.timer = null;
  const notice = connectionNotice();
  // Re-render when the notice should appear or escalate, without polling.
  if (notice?.pendingMs) connectionHealth.timer = setTimeout(renderConnectionStatus, notice.pendingMs + 50);
  const banner = $('connectionBanner');
  if (!banner) return;
  const visible = Boolean(notice?.text);
  banner.classList.toggle('hidden', !visible);
  banner.classList.toggle('extended', visible && notice.level === 'extended');
  const message = visible ? notice.text : '';
  // Unchanged text is not rewritten, so screen readers announce changes only.
  if (message !== connectionHealth.message) {
    connectionHealth.message = message;
    text($('connectionBannerText'), message);
  }
}

$('connectionRetryBtn')?.addEventListener('click', () => {
  reconnectSupportEventsNow();
  requestDashboardRefresh('queue', 'conversation', 'counts');
});
