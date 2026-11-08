// Read-only management. Lifecycle mutations stay in the existing Super Admin dialog.
const bridgeManagement = {epoch: 0, selected: null, rows: new Map(), snapshot: null,
  loading: false, historyAt: 0, historyLine: null, historyLoading: false, assessmentLoading: false};
const bridgeManagementBase = '/api/support/whatsapp-linked-device';

function bridgeText(node, value) {
  const next = value == null ? 'Unknown' : String(value);
  if (node.textContent !== next) node.textContent = next;
}
function bridgeDate(value) {
  if (value == null) return 'Unknown';
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Unknown';
}
function bridgeBool(value) { return value === true ? 'Yes' : value === false ? 'No' : 'Unknown'; }
function bridgeLabel(value) { return value == null ? 'Unknown' : String(value).replaceAll('_', ' '); }

function resetBridgeManagement() {
  bridgeManagement.epoch++;
  Object.assign(bridgeManagement, {selected: null, snapshot: null, loading: false,
    historyAt: 0, historyLine: null, historyLoading: false, assessmentLoading: false});
  bridgeManagement.rows.clear();
  for (const id of ['bridgeIdentities', 'bridgeFacts', 'bridgeFutureActions', 'bridgeHistory', 'bridgeIncidents']) {
    $(id).replaceChildren(); delete $(id).dataset.signature;
  }
  for (const id of ['bridgeAssessment', 'bridgeBlockers', 'bridgeExplanation', 'bridgeHistoryNotice']) bridgeText($(id), '');
  $('bridgeAssessment').classList.add('hidden'); $('bridgeAssessmentBtn').classList.add('hidden');
  $('bridgeAssessmentBtn').disabled = false;
  bridgeText($('bridgeIdentityTitle'), 'Select a number');
}
function openBridgeManagement() {
  if (!canViewBridgeManagement()) return;
  resetBridgeManagement();
  bridgeText($('bridgeObservation'), 'Loading diagnostics…');
  $('bridgeTelemetryWarning').classList.add('hidden');
  $('bridgeLegacyWorkflowsBtn').classList.toggle('hidden', !canManageWhatsApp());
  openDialog('bridgeManagementModal', 'refreshBridgeManagementBtn');
  dashboardRefresh.request('bridge-management');
}
function closeBridgeManagement() { resetBridgeManagement(); closeDialog('bridgeManagementModal'); }

async function loadBridgeManagement() {
  if (!canViewBridgeManagement() || state.activeModal !== 'bridgeManagementModal' || bridgeManagement.loading) return;
  const epoch = bridgeManagement.epoch;
  bridgeManagement.loading = true; $('refreshBridgeManagementBtn').disabled = true;
  try {
    const payload = await api(bridgeManagementBase + '/management', {signal: AbortSignal.timeout(12000)});
    if (epoch !== bridgeManagement.epoch || state.activeModal !== 'bridgeManagementModal') return;
    bridgeManagement.snapshot = payload; renderBridgeManagement(payload);
    void loadBridgeHistory();
  } catch (_) {
    if (epoch !== bridgeManagement.epoch) return;
    $('bridgeTelemetryWarning').classList.remove('hidden');
    bridgeText($('bridgeTelemetryWarning'), 'Live diagnostics are unavailable. Any displayed observations are stale; no connection changes were made.');
  } finally {
    if (epoch === bridgeManagement.epoch) { bridgeManagement.loading = false; $('refreshBridgeManagementBtn').disabled = false; }
  }
}

function renderBridgeManagement(payload) {
  bridgeText($('bridgeObservation'), 'Observed ' + bridgeDate(payload.checked_at) + ' · Refreshes every 5 seconds');
  const warning = payload.issues?.length ? 'Monitoring gaps: ' + payload.issues.map(bridgeLabel).join(', ') + '. Unavailable values are unknown.' : '';
  bridgeText($('bridgeTelemetryWarning'), warning); $('bridgeTelemetryWarning').classList.toggle('hidden', !warning);
  const ids = new Set(payload.lines.map(line => line.line_id));
  for (const [id, row] of bridgeManagement.rows) if (!ids.has(id)) { row.remove(); bridgeManagement.rows.delete(id); }
  if (!ids.has(bridgeManagement.selected)) selectBridgeIdentity(payload.lines[0]?.line_id || null, false);
  for (const line of payload.lines) {
    let row = bridgeManagement.rows.get(line.line_id);
    if (!row) {
      row = document.createElement('button'); row.type = 'button'; row.className = 'bridge-identity';
      row.append(document.createElement('strong'), document.createElement('span'), document.createElement('small'));
      row.addEventListener('click', () => selectBridgeIdentity(line.line_id));
      bridgeManagement.rows.set(line.line_id, row); $('bridgeIdentities').append(row);
    }
    bridgeText(row.children[0], line.display_name);
    bridgeText(row.children[1], (line.phone || 'Phone unavailable') + ' · ' + line.line_id);
    bridgeText(row.children[2], bridgeLabel(line.connection.state) + ' · ' + (line.lifecycle === 'decommissioned' ? 'Removed locally' : line.is_enabled === false ? 'Disabled in app' : line.is_enabled === true ? 'Enabled in app' : 'Routing unknown'));
    row.setAttribute('aria-pressed', String(bridgeManagement.selected === line.line_id));
  }
  if (!payload.lines.length) bridgeText($('bridgeExplanation'), 'No identities are available in the current inventory. Check monitoring status above.');
  renderBridgeDetail();
  bridgeText($('bridgeIncidentNotice'), payload.incident_notice + ' Observation window: ' + bridgeDate(payload.incident_window_started_at));
  renderBridgeEntries($('bridgeIncidents'), payload.recent_incidents || [], item =>
    bridgeDate(item.observed_at) + ' · ' + item.line_id + ' · ' + item.message + ' (' + item.code + ')');
}

function selectBridgeIdentity(lineId, load = true) {
  if (bridgeManagement.selected !== lineId) {
    bridgeManagement.selected = lineId; bridgeManagement.historyLine = null;
    $('bridgeHistory').replaceChildren();
    bridgeText($('bridgeHistoryNotice'), lineId ? 'Loading saved operator history…' : '');
    bridgeText($('bridgeAssessment'), ''); $('bridgeAssessment').classList.add('hidden');
    if (!lineId) {
      $('bridgeFacts').replaceChildren(); $('bridgeFutureActions').replaceChildren();
      delete $('bridgeFutureActions').dataset.signature;
      bridgeText($('bridgeIdentityTitle'), 'Select a number'); bridgeText($('bridgeBlockers'), '');
      $('bridgeAssessmentBtn').classList.add('hidden');
    }
  }
  for (const [id, row] of bridgeManagement.rows) row.setAttribute('aria-pressed', String(id === lineId));
  if (load) { renderBridgeDetail(); void loadBridgeHistory(); }
}

function bridgeFactGroup(title, facts) {
  let group = [...$('bridgeFacts').children].find(node => node.dataset.group === title);
  if (!group) {
    group = document.createElement('section'); group.dataset.group = title;
    const heading = document.createElement('h4'); heading.textContent = title;
    group.append(heading, document.createElement('dl')); $('bridgeFacts').append(group);
  }
  const dl = group.lastElementChild;
  for (let i = 0; i < facts.length; i++) {
    if (!dl.children[i * 2]) dl.append(document.createElement('dt'), document.createElement('dd'));
    bridgeText(dl.children[i * 2], facts[i][0]); bridgeText(dl.children[i * 2 + 1], facts[i][1]);
  }
}

function renderBridgeDetail() {
  const line = bridgeManagement.snapshot?.lines.find(item => item.line_id === bridgeManagement.selected);
  if (!line) return;
  bridgeText($('bridgeIdentityTitle'), line.display_name + ' · ' + (line.phone || line.line_id));
  bridgeText($('bridgeExplanation'), line.explanation);
  bridgeText($('bridgeBlockers'), line.blockers.length ? 'Blockers: ' + line.blockers.join(', ') : 'No known blockers reported. This does not authorize recovery.');
  bridgeFactGroup('Connection', [['Status', bridgeLabel(line.connection.state)], ['Last observed', bridgeDate(line.observed_at || line.last_observed_at)],
    ['Current reason', line.connection.reason_code || 'None reported'], ['Last disconnect reason', line.connection.last_disconnect_reason || 'Unknown'],
    ['Unavailable since', bridgeDate(line.connection.disconnected_since_ms)], ['Outage seconds', line.connection.outage_seconds],
    ['Automatic retry scheduled', bridgeBool(line.connection.retry_scheduled)], ['Reconnect attempts', line.connection.reconnect_attempts],
    ['Last recovered outage (seconds)', line.connection.last_outage_seconds]]);
  bridgeFactGroup('Authentication', [['State', bridgeLabel(line.authentication.state)], ['Lifecycle', bridgeLabel(line.lifecycle)],
    ['Isolation verified', bridgeBool(line.authentication.isolation_safe)], ['Remote unlink confirmed', bridgeBool(line.authentication.remote_unlinked)]]);
  bridgeFactGroup('Storage', [['Health', bridgeLabel(line.storage.level)], ['Last storage sample', bridgeDate(line.storage.checked_at_ms)],
    ['Reason codes', line.storage.issues.map(issue => issue.code + (issue.kind ? ' (' + issue.kind + ')' : '')).join(', ') || 'None reported']]);
  bridgeFactGroup('Transport & heartbeat', [['Last observed endpoint', line.transport.remote_ip ? line.transport.remote_ip + ' · port ' + (line.transport.remote_port ?? 'unknown') : 'Unknown'],
    ['Observed address family', line.transport.family], ['Configured family', line.transport.configured_ip_family === 0 ? 'Automatic' : line.transport.configured_ip_family == null ? 'Unknown' : 'IPv' + line.transport.configured_ip_family],
    ['TLS verified', bridgeBool(line.transport.tls_authorized)], ['Network reason', line.transport.error_code || 'None reported'],
    ['Heartbeat instrumentation', line.heartbeat.supported ? 'Supported' : 'Unavailable'],
    ['Last successful heartbeat', bridgeDate(line.heartbeat.heartbeat_success_at)], ['Last heartbeat observation', bridgeDate(line.heartbeat.heartbeat_observed_at)],
    ['Heartbeat observation age', line.heartbeat.age_seconds == null ? 'Unknown' : line.heartbeat.age_seconds + ' seconds' + (line.heartbeat.age_seconds > 120 ? ' · Stale' : '')],
    ['Heartbeat round trip (ms)', line.heartbeat.heartbeat_rtt_ms], ['Heartbeat failures', line.heartbeat.heartbeat_failures],
    ['Heartbeat timeouts', line.heartbeat.heartbeat_timeouts], ['Heartbeat query timeouts', line.heartbeat.heartbeat_query_timeouts]]);
  bridgeFactGroup('Delivery evidence', [['Pending callbacks', line.delivery.pending_callbacks], ['Oldest callback (seconds)', line.delivery.oldest_callback_age_seconds],
    ['Failed callbacks', line.delivery.failed_callbacks], ['Waiting for history', line.delivery.waiting_history_reactions], ['Uncertain bridge sends', line.delivery.uncertain_sends]]);
  const signature = JSON.stringify(line.actions);
  if ($('bridgeFutureActions').dataset.signature !== signature) {
    $('bridgeFutureActions').replaceChildren(...line.actions.map(action => {
      const wrapper = document.createElement('div'), button = document.createElement('button'), reason = document.createElement('small');
      button.type = 'button'; button.className = 'btn small'; button.textContent = action.label; button.disabled = true;
      reason.textContent = action.reason + (action.blockers.length ? ' ' + action.blockers.join(', ') : '');
      wrapper.append(button, reason); return wrapper;
    }));
    $('bridgeFutureActions').dataset.signature = signature;
  }
  $('bridgeAssessmentBtn').classList.toggle('hidden', state.agent?.can_assess_whatsapp_authentication !== true);
}

function renderBridgeEntries(container, entries, format) {
  const signature = JSON.stringify(entries);
  if (container.dataset.signature === signature && container.childElementCount) return;
  container.dataset.signature = signature;
  const values = entries.length ? entries.map(format) : ['No events recorded in this observation window.'];
  container.replaceChildren(...values.map(value => { const li = document.createElement('li'); li.textContent = value; return li; }));
}

async function loadBridgeHistory() {
  const id = bridgeManagement.selected, epoch = bridgeManagement.epoch;
  if (!id || bridgeManagement.historyLoading || (bridgeManagement.historyLine === id && Date.now() - bridgeManagement.historyAt < 30000)) return;
  bridgeManagement.historyLoading = true;
  try {
    const payload = await api(bridgeManagementBase + '/lines/' + encodeURIComponent(id) + '/history', {signal: AbortSignal.timeout(12000)});
    if (epoch !== bridgeManagement.epoch || id !== bridgeManagement.selected) return;
    renderBridgeEntries($('bridgeHistory'), payload.events, event => bridgeDate(event.at) + ' · ' + event.description +
      ' · ' + (event.operator_id == null ? 'System / operator' : 'Staff #' + event.operator_id) + ' (' + event.action + ')');
    bridgeText($('bridgeHistoryNotice'), 'Latest 30 saved registry operations. Message contents and free-text audit details are excluded.');
  } catch (_) {
    if (epoch === bridgeManagement.epoch && id === bridgeManagement.selected) bridgeText($('bridgeHistoryNotice'), 'Saved operator history is unavailable. Any displayed history may be stale.');
  } finally {
    if (epoch === bridgeManagement.epoch) {
      bridgeManagement.historyLoading = false; bridgeManagement.historyLine = id; bridgeManagement.historyAt = Date.now();
      if (id !== bridgeManagement.selected) void loadBridgeHistory();
    }
  }
}

async function loadBridgeAssessment() {
  const id = bridgeManagement.selected, epoch = bridgeManagement.epoch;
  if (!id || bridgeManagement.assessmentLoading || state.agent?.can_assess_whatsapp_authentication !== true) return;
  bridgeManagement.assessmentLoading = true; $('bridgeAssessmentBtn').disabled = true;
  $('bridgeAssessment').classList.remove('hidden'); bridgeText($('bridgeAssessment'), 'Reading authentication evidence…');
  try {
    const payload = await api(bridgeManagementBase + '/lines/' + encodeURIComponent(id) + '/auth-recovery', {signal: AbortSignal.timeout(12000)});
    if (epoch !== bridgeManagement.epoch || id !== bridgeManagement.selected) return;
    const operations = (payload.operations || []).map(op => op.source + ': provider ' + op.provider_outcome + ', wrapper ' + op.wrapper_outcome).join('; ');
    bridgeText($('bridgeAssessment'), bridgeLabel(payload.status) + '. ' + payload.notice + '\nBlockers: ' +
      (payload.blockers?.join(', ') || 'None reported') + '\nObserved: ' + bridgeDate(payload.checked_at) +
      '\nEvidence reference: ' + (payload.evidence_reference || 'Unavailable') +
      '\nGeneration: ' + (payload.connection_generation ?? 'Unknown') + ' · Sequence: ' + (payload.sequence ?? 'Unknown') +
      (operations ? '\nOperation outcomes' + (payload.operations_truncated ? ' (first 100 of ' + payload.operation_count + ')' : '') + ': ' + operations : ''));
  } catch (_) {
    if (epoch === bridgeManagement.epoch && id === bridgeManagement.selected) bridgeText($('bridgeAssessment'), 'Authentication assessment is unavailable. The saved session remains unchanged.');
  } finally {
    if (epoch === bridgeManagement.epoch) { bridgeManagement.assessmentLoading = false; $('bridgeAssessmentBtn').disabled = false; }
  }
}

$('closeBridgeManagementBtn').addEventListener('click', closeBridgeManagement);
$('refreshBridgeManagementBtn').addEventListener('click', () => dashboardRefresh.request('bridge-management'));
$('bridgeAssessmentBtn').addEventListener('click', loadBridgeAssessment);
$('bridgeLegacyWorkflowsBtn').addEventListener('click', () => {
  if (!canManageWhatsApp()) return;
  resetBridgeManagement(); closeDialog('bridgeManagementModal', false); void openWhatsAppModal();
});
