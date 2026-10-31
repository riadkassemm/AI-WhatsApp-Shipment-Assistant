const statisticsMetrics = {
  active_seconds: 'Active dashboard time', tickets_claimed: 'Tickets claimed',
  tickets_resolved: 'Tickets resolved', messages_sent: 'Outbound messages sent',
  tickets_transferred: 'Tickets transferred', tickets_received_by_transfer: 'Tickets received by transfer',
  average_handling_seconds: 'Average handling time', median_handling_seconds: 'Median handling time',
  average_resolution_seconds: 'Average resolution time', median_resolution_seconds: 'Median resolution time',
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
    const rank = document.createElement('td'); rank.textContent = row.rank ?? '—'; tr.append(rank);
    const staff = document.createElement('td');
    const name = document.createElement('button'); name.type = 'button'; name.className = 'statistics-agent-link';
    name.textContent = row.name; name.dataset.agentId = row.id; name.setAttribute('aria-label', 'Review ' + row.name + '’s tickets and insights');
    name.onclick = () => openStatisticsAgent(row);
    const role = document.createElement('div'); role.className = 'form-note';
    role.textContent = staffRoleLabel(row.role) + (row.is_active ? '' : ' · Inactive');
    staff.append(name, role); tr.append(staff);
    for (const value of Object.keys(statisticsMetrics).map(metric => statisticsValue(metric, row[metric]))) {
      const td = document.createElement('td'); td.textContent = value; tr.append(td);
    }
    tr.title = (row.handling_samples || 0) + ' complete handling periods; ' + (row.resolution_samples || 0) + ' resolver periods';
    $('statisticsRows').append(tr);
  }
  const since = value => value ? new Date(value).toLocaleString(undefined, {timeZone: report.timezone}) : 'no retained records';
  $('statisticsCoverage').textContent = 'Active time is available from ' + since(report.activity_tracking_started_at) + '. Ticket records begin ' + since(report.ticket_history_since) + '; sent messages ' + since(report.message_history_since) + '; transfers ' + since(report.transfer_history_since) + '. Inactivity of 60 seconds or more is excluded from active time.';
  $('statisticsCoverage').textContent += ' Timing averages use completed assignments ending in the selected dates. Ongoing assignments and time spent resolved are excluded. ' + (report.timing_incomplete_tickets || 0) + ' conversations have incomplete timing history; unavailable values appear as —.';
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
let investigationAgent = null;
let investigationTicket = null;
let investigationNext = null;
let investigationMessageBefore = null;
let investigationEventBefore = null;
function investigationElement(tag, text, className = '') {
  const element = document.createElement(tag); element.textContent = text; element.className = className; return element;
}
function investigationDate(value, options = {}) {
  if (!value || Number.isNaN(new Date(value).getTime())) return 'Time not recorded';
  const zone = new URLSearchParams(statisticsAppliedQuery || '').get('timezone_name');
  return new Date(value).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short', ...options, ...(zone ? {timeZone: zone} : {})});
}
function investigationLabel(value) {
  return {NEW:'New',ASSIGNED:'Assigned',IN_PROGRESS:'In progress',WAITING_CUSTOMER:'Waiting for customer',
    WAITING_INTERNAL:'Waiting internally',RESOLVED:'Resolved',CLOSED:'Archived',SENT:'Sent',RECEIVED:'Received',
    PENDING:'Waiting to send',DISPATCHING:'Sending',UNCERTAIN:'Delivery uncertain',FAILED:'Send failed'}[value] || 'Not recorded';
}
function investigationActivity(event) {
  let data;
  try { data = typeof event.details_json === 'string' ? JSON.parse(event.details_json) : event.details_json; } catch (_) { data = null; }
  if (!data || typeof data !== 'object' || Array.isArray(data)) data = {};
  const actor = event.actor_type === 'SYSTEM' ? 'Automation' : (event.actor_name || 'A staff member');
  switch (event.event_type) {
    case 'TICKET_CREATED': return 'Ticket opened for customer support.';
    case 'TICKET_CLAIMED': return actor + ' claimed the ticket.';
    case 'TICKET_TRANSFERRED': return actor + ' transferred the ticket' + (data.from_agent_name && String(data.from_agent_id) !== String(event.actor_id) ? ' from ' + data.from_agent_name : '') + ' to ' + (data.to_agent_name || 'another staff member') + '.';
    case 'TICKET_RESOLVED': return actor + ' resolved the ticket.';
    case 'TICKET_REOPENED': return actor + ' reopened the ticket' + (data.current_assignee_id && String(data.current_assignee_id) === String(event.actor_id) ? ' and took responsibility.' : '.');
    case 'CHAT_STARTED': return actor + ' started the conversation.';
    case 'CHAT_REOPENED': return actor + ' reopened the conversation.';
    case 'CHAT_CLOSED': return data.reason === 'whatsapp_line_removed' ? 'Archived when ' + actor + ' removed the WhatsApp number.' : actor + ' closed the conversation.';
    case 'TICKET_CLOSED': return data.reason === 'whatsapp_line_removed' ? 'Archived when ' + actor + ' removed the WhatsApp number.' : actor + ' closed the ticket.';
    case 'TICKET_RESTORED': case 'CHAT_RESTORED': return 'Restored when ' + actor + ' linked the WhatsApp number again' + (data.restored_status ? ' (' + investigationLabel(data.restored_status).toLowerCase() + ').' : '.');
    case 'RETURNED_TO_AI': return 'Staff handling ended.';
    case 'STATUS_CHANGED': return actor + ' changed the status' + (data.to ? ' to ' + investigationLabel(data.to).toLowerCase() : '') + '.';
    case 'CONVERSATION_VIEWED': return actor + ' opened the conversation.';
    case 'INCOMING_MARKED_READ': return data.mode === 'reply_sync' ? 'Customer message marked as read after ' + actor + ' replied.' : actor + ' marked a customer message as read.';
    case 'MESSAGE_FORWARDED': return actor + ' forwarded a message into this conversation.';
    case 'CLAIM_GREETING_SENT': return 'Automatic greeting sent.';
    case 'RESOLUTION_NOTICE_SENT': return 'Automatic resolution message sent.';
    case 'CLAIM_GREETING_SENT_SEND_FAILED': return 'Automatic greeting could not be confirmed as sent.';
    case 'RESOLUTION_NOTICE_SENT_SEND_FAILED': return 'Automatic resolution message could not be confirmed as sent.';
    default: return 'Additional conversation activity recorded.';
  }
}
function investigationFlag(flag) {
  return {fast_resolution:'Resolved in under a minute',short_handling:'Handled for under 30 seconds',
    resolved_without_response:'Resolved without a confirmed reply during this assignment',
    repeated_views_without_response:'Opened repeatedly without replying',read_without_response:'Marked as read without a confirmed reply',
    repeated_reads_without_response:'Repeated read actions without replying',frequent_transfers:'Transferred out at least three times'}[flag.code] || 'Activity worth reviewing';
}
function clearStatisticsInvestigation() {
  investigationRequest += 1;
  investigationAgent = null; investigationTicket = null; investigationNext = null;
  investigationMessageBefore = null; investigationEventBefore = null;
  for (const id of ['statisticsTicketRows','statisticsReviewNote','statisticsReviewStatus','statisticsAgentTitle','statisticsAgentDates','statisticsAgentInsights','statisticsAgentInsightNote','investigationPeriods','investigationEvents','investigationMessages','investigationTitle','investigationNotes','investigationStatus']) $(id).replaceChildren();
  for (const id of ['statisticsMoreTickets','statisticsAgentReview','statisticsInvestigation']) $(id).classList.add('hidden');
  $('statisticsOverview').classList.remove('hidden');
}
function investigationStillAllowed(request) {
  return request === investigationRequest && state.authenticated && canViewAgentStatistics() && state.activeModal === 'agentStatisticsModal';
}
function investigationFocus(id) { $(id).focus({preventScroll:true}); $(id).scrollIntoView({block:'nearest'}); }
async function openStatisticsAgent(row) {
  if (!canViewAgentStatistics() || !statisticsAppliedQuery) return;
  clearStatisticsInvestigation();
  investigationAgent = row;
  $('statisticsFlaggedOnly').checked = false;
  $('statisticsOverview').classList.add('hidden'); $('statisticsAgentReview').classList.remove('hidden');
  $('statisticsAgentTitle').textContent = row.name + ' · Performance review';
  const query = new URLSearchParams(statisticsAppliedQuery);
  $('statisticsAgentDates').textContent = query.get('start') + ' to ' + query.get('end') + ' · ' + query.get('timezone_name');
  for (const [metric,label] of Object.entries(statisticsMetrics).filter(([metric]) => !metric.startsWith('median_'))) {
    const card = document.createElement('div');
    card.append(investigationElement('span', label), investigationElement('strong', statisticsValue(metric,row[metric])));
    if (metric.startsWith('average_')) {
      const median = metric.replace('average_', 'median_');
      card.append(investigationElement('span', 'Median: ' + statisticsValue(median, row[median]), 'statistics-card-detail'));
    }
    $('statisticsAgentInsights').append(card);
  }
  $('statisticsAgentInsightNote').textContent = 'Timing is based on ' + (row.handling_samples || 0) + ' completed handling periods and ' + (row.resolution_samples || 0) + ' resolutions with a known start. Transfers stop the previous owner’s time. Ongoing and incomplete periods are excluded from averages.';
  investigationFocus('statisticsAgentTitle');
  await browseStatisticsTickets();
}
function backToStatisticsRanking() {
  const id = investigationAgent?.id;
  clearStatisticsInvestigation();
  const button = [...$('statisticsRows').querySelectorAll('button')].find(button => button.dataset.agentId === String(id));
  if (button) button.focus();
}
function backToStatisticsAgent() {
  investigationRequest += 1; investigationTicket = null;
  $('statisticsInvestigation').classList.add('hidden'); $('statisticsAgentReview').classList.remove('hidden');
  $('statisticsMoreTickets').disabled = false;
  investigationFocus('statisticsAgentTitle');
}
function showInvestigationPanel(panel) {
  for (const button of document.querySelectorAll('[data-investigation-panel]')) {
    const active = button.dataset.investigationPanel === panel;
    button.setAttribute('aria-pressed', String(active));
    $(button.getAttribute('aria-controls')).classList.toggle('hidden', !active);
  }
}
async function browseStatisticsTickets(more = false) {
  if (!canViewAgentStatistics() || !statisticsAppliedQuery || !investigationAgent) return;
  const request = ++investigationRequest;
  if (!more) { $('statisticsTicketRows').replaceChildren(); investigationNext = null; }
  const query = new URLSearchParams(statisticsAppliedQuery);
  query.delete('rank_by'); query.delete('direction'); query.set('agent_id', investigationAgent.id);
  query.set('flagged_only', String($('statisticsFlaggedOnly').checked));
  if (more && investigationNext) query.set('before', investigationNext);
  $('statisticsMoreTickets').disabled = true;
  $('statisticsReviewStatus').textContent = 'Loading tickets…';
  try {
    const page = await api('/api/support/statistics/tickets?' + query);
    if (!investigationStillAllowed(request)) return;
    for (const item of page.items) {
      const row = document.createElement('article'); row.className = 'investigation-ticket';
      const button = document.createElement('button'); button.type = 'button'; button.className = 'statistics-ticket-link';
      button.append(investigationElement('strong', item.customer_name || item.whatsapp_phone || 'Customer'), investigationElement('span', item.ticket_reference, 'form-note'));
      button.setAttribute('aria-label', 'Review ' + item.ticket_reference + ' with ' + (item.customer_name || 'customer'));
      button.onclick = () => investigateStatisticsTicket(item.id);
      const heading = document.createElement('div'); heading.className = 'investigation-ticket-heading';
      heading.append(button, investigationElement('span', investigationLabel(item.status), 'investigation-badge'));
      row.append(heading);
      if (item.current_assignee_name) row.append(investigationElement('p', (['RESOLVED','CLOSED'].includes(item.status) ? 'Last assigned to ' : 'Assigned to ') + item.current_assignee_name, 'form-note'));
      const flags = [...new Set(item.flags.map(investigationFlag))];
      if (flags.length) {
        const list = document.createElement('ul'); list.className = 'investigation-flags';
        for (const flag of flags) list.append(investigationElement('li', flag));
        row.append(list);
      } else row.append(investigationElement('p', 'No review indicators in the selected dates.', 'form-note'));
      if (item.issues.length) row.append(investigationElement('p', 'Some earlier timing information is unavailable.', 'form-note'));
      $('statisticsTicketRows').append(row);
    }
    investigationNext = page.next_before;
    $('statisticsMoreTickets').classList.toggle('hidden', !investigationNext);
    $('statisticsReviewStatus').textContent = page.items.length ? '' : (investigationNext ? 'No matches in this group. Load more conversations to continue.' : ($('statisticsTicketRows').children.length ? 'All matching conversations are shown.' : 'No matching conversations for these dates.'));
    $('statisticsReviewNote').textContent = 'Conversations this staff member handled or took part in during the selected dates. Review indicators are prompts to look closer, not findings of misconduct.' + (page.activity_tracking_started_at ? ' Opening and read activity is available from ' + investigationDate(page.activity_tracking_started_at, {timeStyle:undefined}) + '.' : ' Earlier opening and read activity may be unavailable.');
  } catch (error) {
    if (investigationStillAllowed(request)) {
      if (error.status === 403) clearAgentStatistics();
      else $('statisticsReviewStatus').textContent = error.message;
    }
  } finally {
    if (investigationStillAllowed(request)) $('statisticsMoreTickets').disabled = false;
  }
}
function renderInvestigationPeriods(data) {
  $('investigationPeriods').replaceChildren();
  for (const period of data.periods) {
    const li = document.createElement('li'); li.className = 'investigation-period';
    const outcome = {transferred:'Transferred to the next staff member',resolved:'Ticket resolved',closed:'Conversation closed',ongoing:'Still responsible',incomplete:'History incomplete'}[period.end_reason] || 'Outcome not recorded';
    li.append(investigationElement('strong', period.agent_name || 'Former staff member'),
      investigationElement('span', period.valid ? statisticsDuration(period.seconds) : 'Duration unavailable', 'investigation-badge'),
      investigationElement('p', investigationDate(period.started_at) + ' – ' + (period.ended_at ? investigationDate(period.ended_at) : 'Ongoing'), 'form-note'),
      investigationElement('p', outcome + (period.resolver_name ? ' by ' + period.resolver_name : '')));
    if (period.cycle_resolution_seconds !== null && period.cycle_resolution_seconds !== undefined) li.append(investigationElement('p', 'Total time to resolution across staff: ' + statisticsDuration(period.cycle_resolution_seconds), 'form-note'));
    $('investigationPeriods').append(li);
  }
  if (!data.periods.length) $('investigationPeriods').append(investigationElement('li', 'No recorded handling periods are available.', 'form-note'));
}
async function investigateStatisticsTicket(ticketId, part = null) {
  if (!canViewAgentStatistics() || !investigationAgent || (part && investigationTicket !== ticketId)) return;
  const request = ++investigationRequest;
  if (!part) {
    investigationTicket = ticketId;
    investigationMessageBefore = null; investigationEventBefore = null;
    for (const id of ['investigationPeriods','investigationEvents','investigationMessages','investigationTitle','investigationNotes']) $(id).replaceChildren();
    $('statisticsAgentReview').classList.add('hidden'); $('statisticsInvestigation').classList.remove('hidden');
    showInvestigationPanel('conversation');
    $('investigationTitle').textContent = 'Conversation review';
    investigationFocus('investigationTitle');
  }
  const query = new URLSearchParams();
  if (part === 'messages' && investigationMessageBefore) query.set('before_message', investigationMessageBefore);
  if (part === 'events' && investigationEventBefore) query.set('before_event', investigationEventBefore);
  for (const id of ['investigationOlderMessages','investigationOlderEvents']) $(id).disabled = true;
  $('investigationStatus').textContent = 'Loading conversation…';
  try {
    const data = await api('/api/support/statistics/tickets/' + ticketId + '?' + query);
    if (!investigationStillAllowed(request)) return;
    $('investigationTitle').textContent = (data.ticket.customer_name || data.ticket.whatsapp_phone || 'Customer') + ' · ' + data.ticket.ticket_reference;
    $('investigationNotes').textContent = investigationLabel(data.ticket.status) + ' · Originally claimed by ' + (data.original_claimant_name || 'staff not recorded') + '. Review only — opening this view leaves the conversation unchanged.' + (data.issues.length ? ' Some earlier history is incomplete, so not all handling times can be shown.' : '');
    renderInvestigationPeriods(data);
    if (part !== 'events') {
      if (!part) $('investigationMessages').replaceChildren();
      const group = document.createElement('div');
      for (const message of data.messages) {
        addMessage(group, message, 'investigation');
        const sender = message.sender_type === 'CUSTOMER' ? 'Customer' : (message.sender_type === 'AGENT' ? (message.sender_name || 'Staff member') : 'Automated support');
        group.append(investigationElement('p', sender + ' · ' + investigationDate(message.created_at) + (message.direction === 'OUTBOUND' ? ' · ' + investigationLabel(message.send_status) : ''), 'form-note investigation-message-meta'));
      }
      group.querySelectorAll('.message-actions').forEach(element => element.remove());
      if (!part && !data.messages.length) group.append(investigationElement('p', 'No messages are available.', 'form-note'));
      $('investigationMessages').prepend(group);
      investigationMessageBefore = data.next_message_before;
      $('investigationOlderMessages').classList.toggle('hidden', !investigationMessageBefore);
    }
    if (part !== 'messages') {
      if (!part) $('investigationEvents').replaceChildren();
      const group = document.createElement('ol'); group.className = 'investigation-timeline';
      for (const event of data.events) {
        const li = document.createElement('li');
        const time = investigationElement('time', investigationDate(event.created_at));
        if (event.created_at) { time.dateTime = event.created_at; time.title = investigationDate(event.created_at, {timeStyle:'long'}); }
        li.append(time, investigationElement('p', investigationActivity(event))); group.append(li);
      }
      if (!part && !data.events.length) group.append(investigationElement('li', 'No activity is recorded.', 'form-note'));
      $('investigationEvents').prepend(group);
      investigationEventBefore = data.next_event_before;
      $('investigationOlderEvents').classList.toggle('hidden', !investigationEventBefore);
    }
    $('investigationStatus').textContent = '';
  } catch (error) {
    if (investigationStillAllowed(request)) {
      if (error.status === 403) clearAgentStatistics();
      else $('investigationStatus').textContent = error.message;
    }
  } finally {
    if (investigationStillAllowed(request)) for (const id of ['investigationOlderMessages','investigationOlderEvents']) $(id).disabled = false;
  }
}
$('statisticsBackToRanking').addEventListener('click', backToStatisticsRanking);
$('investigationBackToAgent').addEventListener('click', backToStatisticsAgent);
$('statisticsMoreTickets').addEventListener('click', () => browseStatisticsTickets(true));
$('statisticsFlaggedOnly').addEventListener('change', () => browseStatisticsTickets());
$('investigationOlderMessages').addEventListener('click', () => investigateStatisticsTicket(investigationTicket, 'messages'));
$('investigationOlderEvents').addEventListener('click', () => investigateStatisticsTicket(investigationTicket, 'events'));
for (const button of document.querySelectorAll('[data-investigation-panel]')) button.addEventListener('click', () => showInvestigationPanel(button.dataset.investigationPanel));
