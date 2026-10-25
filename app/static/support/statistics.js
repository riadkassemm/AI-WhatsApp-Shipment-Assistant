const statisticsMetrics = {
  active_seconds: 'Active dashboard time', tickets_claimed: 'Tickets claimed',
  tickets_resolved: 'Tickets resolved', messages_sent: 'Outbound messages sent',
  tickets_transferred: 'Tickets transferred', tickets_received_by_transfer: 'Tickets received by transfer',
  average_handling_seconds: 'Average handling time', median_handling_seconds: 'Median handling time',
  average_resolution_seconds: 'Average resolver time', median_resolution_seconds: 'Median resolver time',
};
let statisticsRequest = 0;
let statisticsAppliedQuery = null;
function canViewAgentStatistics() {
  return Boolean(state.agent && ['admin', 'supervisor'].includes(state.agent.role));
}
function clearAgentStatistics() {
  statisticsRequest += 1;
  clearStatisticsInvestigation();
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
  if (value === null || value === undefined) return '—';
  return metric.endsWith('_seconds') ? statisticsDuration(value) : String(value);
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
    const value = document.createElement('strong'); value.textContent = statisticsValue(metric, (report.totals[metric] ?? report.timing_totals?.[metric]));
    card.append(title, value); $('statisticsTotals').append(card);
  }
  $('statisticsRanking').textContent = 'Ranked by ' + statisticsMetrics[report.rank_by] + ' · ' + (report.direction === 'desc' ? 'Highest first' : 'Lowest first') + ' · ' + report.timezone;
  for (const th of document.querySelectorAll('#statisticsTable th[data-metric]')) {
    th.setAttribute('aria-sort', th.dataset.metric === report.rank_by ? (report.direction === 'desc' ? 'descending' : 'ascending') : 'none');
  }
  $('statisticsRows').replaceChildren();
  for (const row of report.rows) {
    const tr = document.createElement('tr');
    for (const value of [row.rank ?? '—', row.name + ' · ' + staffRoleLabel(row.role) + (row.is_active ? '' : ' (inactive)'), ...Object.keys(statisticsMetrics).map(metric => statisticsValue(metric, row[metric]))]) {
      const td = document.createElement('td'); td.textContent = value; tr.append(td);
    }
    tr.title = (row.handling_samples || 0) + ' complete handling periods; ' + (row.resolution_samples || 0) + ' resolver periods';
    $('statisticsRows').append(tr);
  }
  const since = value => value ? new Date(value).toLocaleString(undefined, {timeZone: report.timezone}) : 'no retained records';
  $('statisticsCoverage').textContent = 'Active time tracked since ' + since(report.activity_tracking_started_at) + '. No earlier activity time is inferred. Retained ticket audit begins: ' + since(report.ticket_history_since) + '; confirmed staff sends: ' + since(report.message_history_since) + '; transfer audit: ' + since(report.transfer_history_since) + '. Active time excludes hidden, unfocused and idle dashboards (60-second timeout).';
  $('statisticsCoverage').textContent += ' Timing averages use complete responsibility periods ending in the selected dates, including waiting time. Open periods and resolved gaps are excluded. Resolver time uses only the final period owned by the resolver. ' + (report.timing_incomplete_tickets || 0) + ' candidate conversations have incomplete timing history; unknown values appear as —.';
}
async function loadAgentStatistics() {
  if (!canViewAgentStatistics()) { clearAgentStatistics(); return; }
  const request = ++statisticsRequest;
  clearStatisticsInvestigation();
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
    $('statisticsBrowseBtn').disabled = false;
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


let investigationRequest = 0;
let investigationTicket = null;
let investigationNext = null;
let investigationMessageBefore = null;
let investigationEventBefore = null;
function clearStatisticsInvestigation() {
  investigationRequest += 1;
  investigationTicket = null; investigationNext = null;
  for (const id of ['statisticsTicketRows','statisticsReviewNote','investigationPeriods','investigationEvents','investigationMessages','investigationTitle','investigationNotes']) $(id).replaceChildren();
  $('statisticsBrowseBtn').disabled = true;
  $('statisticsMoreTickets').classList.add('hidden');
  $('statisticsInvestigation').classList.add('hidden');
}
function investigationStillAllowed(request) {
  return request === investigationRequest && state.authenticated && canViewAgentStatistics() && state.activeModal === 'agentStatisticsModal';
}
async function browseStatisticsTickets(more = false) {
  if (!canViewAgentStatistics() || !statisticsAppliedQuery) return;
  const request = ++investigationRequest;
  if (!more) { $('statisticsTicketRows').replaceChildren(); investigationNext = null; }
  const query = new URLSearchParams(statisticsAppliedQuery);
  query.delete('rank_by'); query.delete('direction');
  query.set('flagged_only', String($('statisticsFlaggedOnly').checked));
  if (more && investigationNext) query.set('before', investigationNext);
  $('statisticsBrowseBtn').disabled = true;
  $('statisticsMoreTickets').disabled = true;
  try {
    const page = await api('/api/support/statistics/tickets?' + query);
    if (!investigationStillAllowed(request)) return;
    for (const item of page.items) {
      const row = document.createElement('div'); row.className = 'investigation-ticket';
      const button = document.createElement('button'); button.type = 'button'; button.className = 'btn';
      button.textContent = item.ticket_reference + ' · ' + (item.customer_name || 'Customer') + ' · ' + item.status;
      button.onclick = () => investigateStatisticsTicket(item.id);
      const note = document.createElement('span');
      note.textContent = item.flags.map(flag => (flag.agent_name || 'Staff #' + flag.agent_id) + ': ' + flag.explanation).join(' ') || (item.issues.length ? 'Incomplete timing history' : 'No review indicators');
      row.append(button, note); $('statisticsTicketRows').append(row);
    }
    investigationNext = page.next_before;
    $('statisticsMoreTickets').classList.toggle('hidden', !investigationNext);
    $('statisticsReviewNote').textContent = 'Indicators prompt manual review; fast resolutions, short periods and transfers are not misconduct findings. View/read tracking began ' + (page.activity_tracking_started_at ? new Date(page.activity_tracking_started_at).toLocaleString() : 'at this release') + '. ' + (page.items.length ? '' : 'No matches in this batch; use More if available.');
  } catch (error) {
    if (investigationStillAllowed(request)) $('statisticsError').textContent = error.message;
  } finally {
    if (investigationStillAllowed(request)) { $('statisticsBrowseBtn').disabled = false; $('statisticsMoreTickets').disabled = false; }
  }
}
async function investigateStatisticsTicket(ticketId, part = null) {
  if (!canViewAgentStatistics()) return;
  const request = ++investigationRequest;
  const query = new URLSearchParams();
  if (part === 'messages' && investigationMessageBefore) query.set('before_message', investigationMessageBefore);
  if (part === 'events' && investigationEventBefore) query.set('before_event', investigationEventBefore);
  try {
    const data = await api('/api/support/statistics/tickets/' + ticketId + '?' + query);
    if (!investigationStillAllowed(request)) return;
    investigationTicket = ticketId;
    $('statisticsInvestigation').classList.remove('hidden');
    $('investigationTitle').textContent = data.ticket.ticket_reference + ' · Read-only investigation';
    $('investigationNotes').textContent = 'Original claimant: ' + (data.original_claimant_id ? (data.original_claimant_name || 'Staff') + ' (#' + data.original_claimant_id + ')' : 'not recorded') + '. ' + (data.issues.join(' ') || 'Timing follows retained assignment events.') + ' Messages and audit are paginated separately. No read receipt or assignment change is made here.';
    $('investigationPeriods').replaceChildren();
    for (const period of data.periods) {
      const li = document.createElement('li');
      li.textContent = period.agent_name + ' · ' + new Date(period.started_at).toLocaleString() + ' → ' + (period.ended_at ? new Date(period.ended_at).toLocaleString() : 'ongoing') + ' · ' + period.end_reason + ' · ' + (period.valid ? statisticsDuration(period.seconds) : 'unknown duration') + (period.resolver_id ? ' · resolved/closed by staff #' + period.resolver_id : '') + (period.cycle_resolution_seconds !== null && period.cycle_resolution_seconds !== undefined ? ' · cycle total ' + statisticsDuration(period.cycle_resolution_seconds) : '');
      $('investigationPeriods').append(li);
    }
    if (part !== 'events') {
      if (!part) $('investigationMessages').replaceChildren();
      const group = document.createElement('div');
      for (const message of data.messages) {
        addMessage(group, message, 'investigation');
        const audit = document.createElement('p'); audit.className = 'form-note';
        audit.textContent = (message.direction || '') + ' · ' + (message.sender_type || '') + ' · ' + (message.sender_name || message.sender_id || 'Unknown sender') + ' · ' + (message.send_status || '') + ' · ' + (message.created_at ? new Date(message.created_at).toLocaleString() : 'unknown creation time');
        group.append(audit);
      }
      // Reuse conversation rendering and authenticated media without operational actions.
      group.querySelectorAll('.message-actions').forEach(element => element.remove());
      $('investigationMessages').prepend(group);
      investigationMessageBefore = data.next_message_before;
      $('investigationOlderMessages').classList.toggle('hidden', !investigationMessageBefore);
    }
    if (part !== 'messages') {
      if (!part) $('investigationEvents').replaceChildren();
      const group = document.createElement('div');
      for (const event of data.events) {
        const p = document.createElement('p');
        p.textContent = new Date(event.created_at).toLocaleString() + ' · ' + (event.actor_name || event.actor_type || 'Unknown') + ' · ' + event.event_type + ' · ' + (event.details_json || '{}');
        group.append(p);
      }
      $('investigationEvents').prepend(group);
      investigationEventBefore = data.next_event_before;
      $('investigationOlderEvents').classList.toggle('hidden', !investigationEventBefore);
    }
  } catch (error) {
    if (investigationStillAllowed(request)) $('statisticsError').textContent = error.message;
  }
}
$('statisticsBrowseBtn').addEventListener('click', () => browseStatisticsTickets());
$('statisticsMoreTickets').addEventListener('click', () => browseStatisticsTickets(true));
$('statisticsFlaggedOnly').addEventListener('change', () => browseStatisticsTickets());
$('investigationOlderMessages').addEventListener('click', () => investigateStatisticsTicket(investigationTicket, 'messages'));
$('investigationOlderEvents').addEventListener('click', () => investigateStatisticsTicket(investigationTicket, 'events'));
