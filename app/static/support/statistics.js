const statisticsMetrics = {
  active_seconds: 'Active dashboard time', tickets_claimed: 'Tickets claimed',
  tickets_resolved: 'Tickets resolved', messages_sent: 'Outbound messages sent',
};
let statisticsRequest = 0;
let statisticsAppliedQuery = null;
function canViewAgentStatistics() {
  return Boolean(state.agent && ['admin', 'supervisor'].includes(state.agent.role));
}
function clearAgentStatistics() {
  statisticsRequest += 1;
  statisticsAppliedQuery = null;
  $('statisticsPdfBtn').disabled = true;
  if (state.activeModal === 'agentStatisticsModal') closeDialog('agentStatisticsModal');
  for (const id of ['statisticsRows', 'statisticsTotals', 'statisticsCoverage', 'statisticsError', 'statisticsRanking']) $(id).replaceChildren();
  $('statisticsAgent').replaceChildren(new Option('All staff', ''));
}
function statisticsDuration(seconds) {
  return Math.floor(seconds / 3600) + 'h ' + Math.floor(seconds % 3600 / 60) + 'm ' + Math.floor(seconds % 60) + 's';
}
function statisticsValue(metric, value) {
  return metric === 'active_seconds' ? statisticsDuration(value) : String(value);
}
function renderAgentStatistics(report) {
  const selected = $('statisticsAgent').value;
  $('statisticsAgent').replaceChildren(new Option('All staff', ''));
  for (const agent of report.agents) $('statisticsAgent').add(new Option(agent.name + (agent.is_active ? '' : ' (inactive)'), agent.id));
  $('statisticsAgent').value = selected;
  $('statisticsTotals').replaceChildren();
  for (const [metric, label] of Object.entries(statisticsMetrics)) {
    const card = document.createElement('div');
    const title = document.createElement('span'); title.textContent = label;
    const value = document.createElement('strong'); value.textContent = statisticsValue(metric, report.totals[metric]);
    card.append(title, value); $('statisticsTotals').append(card);
  }
  $('statisticsRanking').textContent = 'Ranked by ' + statisticsMetrics[report.rank_by] + ' · ' + (report.direction === 'desc' ? 'Highest first' : 'Lowest first') + ' · ' + report.timezone;
  for (const th of document.querySelectorAll('#statisticsTable th[data-metric]')) {
    th.setAttribute('aria-sort', th.dataset.metric === report.rank_by ? (report.direction === 'desc' ? 'descending' : 'ascending') : 'none');
  }
  $('statisticsRows').replaceChildren();
  for (const row of report.rows) {
    const tr = document.createElement('tr');
    for (const value of [row.rank, row.name + ' · ' + staffRoleLabel(row.role) + (row.is_active ? '' : ' (inactive)'), ...Object.keys(statisticsMetrics).map(metric => statisticsValue(metric, row[metric]))]) {
      const td = document.createElement('td'); td.textContent = value; tr.append(td);
    }
    $('statisticsRows').append(tr);
  }
  const since = value => value ? new Date(value).toLocaleString(undefined, {timeZone: report.timezone}) : 'no retained records';
  $('statisticsCoverage').textContent = 'Active time tracked since ' + since(report.activity_tracking_started_at) + '. No earlier activity time is inferred. Retained ticket audit begins: ' + since(report.ticket_history_since) + '; confirmed staff sends: ' + since(report.message_history_since) + '. Active time excludes hidden, unfocused and idle dashboards (60-second timeout).';
}
async function loadAgentStatistics() {
  if (!canViewAgentStatistics()) { clearAgentStatistics(); return; }
  const request = ++statisticsRequest;
  statisticsAppliedQuery = null;
  $('statisticsPdfBtn').disabled = true;
  $('statisticsError').textContent = 'Loading statistics…';
  for (const id of ['statisticsRows', 'statisticsTotals', 'statisticsRanking', 'statisticsCoverage']) $(id).replaceChildren();
  const query = new URLSearchParams({start: $('statisticsStart').value, end: $('statisticsEnd').value,
    timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    rank_by: $('statisticsMetric').value, direction: $('statisticsDirection').value});
  if ($('statisticsAgent').value) query.set('agent_id', $('statisticsAgent').value);
  try {
    const report = await api('/api/support/statistics?' + query);
    if (request !== statisticsRequest || !state.authenticated || !canViewAgentStatistics()) return;
    renderAgentStatistics(report);
    statisticsAppliedQuery = query.toString();
    $('statisticsPdfBtn').disabled = false;
    $('statisticsError').textContent = report.rows.length ? '' : 'No staff match this filter.';
  } catch (error) {
    if (request !== statisticsRequest) return;
    $('statisticsRows').replaceChildren(); $('statisticsTotals').replaceChildren();
    $('statisticsError').textContent = error.message;
    if (error.status === 403) clearAgentStatistics();
  }
}
async function downloadAgentStatisticsPdf() {
  if (!canViewAgentStatistics() || statisticsAppliedQuery === null) return;
  const request = statisticsRequest;
  const agentId = state.agent.id;
  const query = statisticsAppliedQuery;
  $('statisticsPdfBtn').disabled = true;
  try {
    const response = await fetch('/api/support/statistics/pdf?' + query, {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) {
      if (response.status === 401) showLogin();
      if (response.status === 403) clearAgentStatistics();
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === 'string' ? body.detail : 'Could not export statistics.');
    }
    const blob = await response.blob();
    if (request !== statisticsRequest || !state.authenticated || state.agent?.id !== agentId || !canViewAgentStatistics()) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    const dates = new URLSearchParams(query);
    link.href = url; link.download = 'agent-statistics-' + dates.get('start') + '-to-' + dates.get('end') + '.pdf';
    document.body.append(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    if (request === statisticsRequest && canViewAgentStatistics()) $('statisticsError').textContent = error.message;
  } finally {
    if (request === statisticsRequest && canViewAgentStatistics()) $('statisticsPdfBtn').disabled = statisticsAppliedQuery === null;
  }
}
$('statisticsPdfBtn').addEventListener('click', downloadAgentStatisticsPdf);
$('agentStatisticsBtn').addEventListener('click', () => {
  if (!canViewAgentStatistics()) return;
  if (!$('statisticsStart').value) {
    const today = new Date(); const start = new Date(today); start.setDate(start.getDate() - 6);
    const day = value => [value.getFullYear(), String(value.getMonth()+1).padStart(2,'0'), String(value.getDate()).padStart(2,'0')].join('-');
    $('statisticsStart').value = day(start); $('statisticsEnd').value = day(today);
  }
  openDialog('agentStatisticsModal', 'statisticsStart'); loadAgentStatistics();
});
$('closeStatisticsBtn').addEventListener('click', () => closeDialog('agentStatisticsModal'));
$('statisticsFilters').addEventListener('submit', event => { event.preventDefault(); loadAgentStatistics(); });
for (const id of ['statisticsAgent', 'statisticsMetric', 'statisticsDirection']) $(id).addEventListener('change', loadAgentStatistics);
for (const button of document.querySelectorAll('#statisticsTable button[data-metric]')) button.addEventListener('click', () => {
  const metric = button.dataset.metric;
  $('statisticsDirection').value = $('statisticsMetric').value === metric && $('statisticsDirection').value === 'desc' ? 'asc' : 'desc';
  $('statisticsMetric').value = metric; loadAgentStatistics();
});

// Pure activity state machine: timers and input handlers supply a monotonic clock.
// A resume creates a baseline, never bridging an unobserved idle/hidden interval.
function createSupportActivityTracker(send, now, visible) {
  let running = false, lastInput = 0, wasActive = false, sequence = 0, tabId;
  function pulse(reset = false) {
    if (!running) return;
    const age = Math.max(0, (now() - lastInput) / 1000);
    const active = visible() && age < 60;
    if (!active && !wasActive && !reset) return;
    const baseline = reset || (active && !wasActive);
    wasActive = active;
    send({tab_id: tabId, sequence: ++sequence, active, reset: baseline, activity_age_seconds: Math.min(age, 86400)});
  }
  return {
    start(id) { if (running) return; running = true; sequence = 0; tabId = id; lastInput = now(); pulse(true); },
    stop() { running = false; wasActive = false; },
    tick() { pulse(); },
    input() {
      if (!running || !visible()) return;
      const idle = now() - lastInput >= 60000;
      lastInput = now();
      if (idle || !wasActive) pulse(true);
    },
    visibility() { if (visible()) { lastInput = now(); pulse(true); } else pulse(); },
  };
}
const supportActivity = createSupportActivityTracker(
  pulse => { if (state.authenticated) api('/api/support/statistics/heartbeat', {method: 'POST', body: JSON.stringify(pulse), keepalive: true}).catch(() => {}); },
  () => performance.now(), () => document.visibilityState === 'visible' && document.hasFocus(),
);
let supportActivityTimer = null;
function startSupportActivity() {
  if (supportActivityTimer !== null) return;
  supportActivity.start(crypto.randomUUID());
  supportActivityTimer = window.setInterval(() => supportActivity.tick(), 15000);
}
function stopSupportActivity() {
  window.clearInterval(supportActivityTimer); supportActivityTimer = null; supportActivity.stop();
}
for (const event of ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart']) document.addEventListener(event, input => {
  if (input.isTrusted) supportActivity.input();
}, {passive: true});
document.addEventListener('visibilitychange', () => supportActivity.visibility());
window.addEventListener('focus', () => supportActivity.visibility());
window.addEventListener('blur', () => supportActivity.visibility());
window.addEventListener('pagehide', stopSupportActivity);
