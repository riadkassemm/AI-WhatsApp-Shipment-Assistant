// Diagnostics and versioned recovery. Pairing/removal remain Super Admin workflows.
const bridgeManagement = {epoch: 0, selected: null, rows: new Map(), snapshot: null,
  loading: false, historyAt: 0, historyLine: null, historyLoading: false, assessmentLoading: false,
  operationsLoading: false, freshAt: 0, submitting: false};
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
    historyAt: 0, historyLine: null, historyLoading: false, assessmentLoading: false,
    operationsLoading: false, freshAt: 0, submitting: false});
  bridgeManagement.rows.clear();
  for (const id of ['bridgeIdentities', 'bridgeFacts', 'bridgeFutureActions', 'bridgeHistory', 'bridgeIncidents', 'bridgeOperations']) {
    $(id).replaceChildren(); delete $(id).dataset.signature;
  }
  for (const id of ['bridgeAssessment', 'bridgeBlockers', 'bridgeExplanation', 'bridgeHistoryNotice']) bridgeText($(id), '');
  $('bridgeAssessment').classList.add('hidden'); $('bridgeAssessmentBtn').classList.add('hidden');
  $('bridgeAssessmentBtn').disabled = false;
  bridgeText($('bridgeIdentityTitle'), 'Select a number');
  bridgeText($('bridgeOperationNotice'), '');
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
    bridgeManagement.snapshot = payload; bridgeManagement.freshAt = Date.now(); renderBridgeManagement(payload);
    void loadBridgeHistory();
    void loadBridgeOperations();
  } catch (_) {
    if (epoch !== bridgeManagement.epoch) return;
    $('bridgeTelemetryWarning').classList.remove('hidden');
    bridgeText($('bridgeTelemetryWarning'), 'Live diagnostics are unavailable. Any displayed observations are stale; no connection changes were made.');
    bridgeManagement.freshAt = 0; renderBridgeDetail(); void loadBridgeOperations();
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
    bridgeText(row.children[2], bridgeLabel(line.connection.state) + ' · ' + (line.lifecycle === 'decommissioned' ? 'Removed locally' : line.is_enabled === false ? 'Disabled in app' : line.is_enabled === true ? 'Enabled in app' : 'Routing unknown') + (line.administration?.paused ? ' · Paused' : ''));
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
    $('bridgeOperations').replaceChildren(); delete $('bridgeOperations').dataset.signature;
    bridgeText($('bridgeOperationNotice'), '');
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
  if (load) { renderBridgeDetail(); void loadBridgeHistory(); void loadBridgeOperations(); }
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
  if (line.administration) bridgeFactGroup('Administration', [['Paused', bridgeBool(line.administration.paused)],
    ['Active operation', line.administration.active_operation_id || 'None'], ['State version', line.administration.version]]);
  const blockedReason = Date.now() - bridgeManagement.freshAt > 15000 ? 'Diagnostics are stale. Refresh before requesting an action.' :
    pendingBridgeOperation() ? 'Wait for the saved operation to be confirmed.' : bridgeManagement.submitting ? 'An operation request is being confirmed.' : '';
  const blocked = Boolean(blockedReason);
  const signature = JSON.stringify([line.actions, blockedReason, state.agent?.can_recover_whatsapp]);
  if ($('bridgeFutureActions').dataset.signature !== signature) {
    $('bridgeFutureActions').replaceChildren(...line.actions.map(action => {
      const wrapper = document.createElement('div'), button = document.createElement('button'), reason = document.createElement('small');
      button.type = 'button'; button.className = 'btn small'; button.textContent = action.label;
      button.disabled = !action.enabled || blocked || state.agent?.can_recover_whatsapp !== true;
      button.addEventListener('click', () => requestBridgeRecovery(action.action));
      reason.textContent = (action.enabled && blockedReason ? blockedReason : action.reason) + (action.blockers.length ? ' ' + action.blockers.join(', ') : '');
      wrapper.append(button, reason); return wrapper;
    }));
    $('bridgeFutureActions').dataset.signature = signature;
  }
  $('bridgeAssessmentBtn').classList.toggle('hidden', state.agent?.can_assess_whatsapp_authentication !== true);
}

function bridgeOperationKey() { return 'support.bridge-operation.' + state.agent?.id; }
// After this long, a 404 for the saved ID proves the request was never saved.
const bridgeOperationSaveGraceMs = 60000;
function pendingBridgeOperation() {
  try { const item = JSON.parse(sessionStorage.getItem(bridgeOperationKey()) || 'null');
    return item && /^[0-9a-f-]{36}$/.test(item.id) && /^\d{5,32}$/.test(item.line) ? item : null;
  } catch (_) { return null; }
}
function forgetBridgeOperation(id) {
  if (pendingBridgeOperation()?.id === id) sessionStorage.removeItem(bridgeOperationKey());
}
async function requestBridgeRecovery(action) {
  const line = bridgeManagement.snapshot?.lines.find(item => item.line_id === bridgeManagement.selected);
  const decision = line?.actions.find(item => item.action === action);
  if (!line?.administration || !decision?.enabled || state.agent?.can_recover_whatsapp !== true ||
      bridgeManagement.submitting || pendingBridgeOperation() || Date.now() - bridgeManagement.freshAt > 15000) return;
  const epoch = bridgeManagement.epoch;
  const guidance = {reconnect:'Reuse the saved paired session only if all safety checks pass.',
    pause:'Stop new sends and automatic reconnects. Keep the session connected and retain pending work.',
    resume:'Revalidate this session, then let retained work continue in order. Uncertain deliveries remain protected.'};
  if (!guidance[action]) return;
  bridgeManagement.submitting = true; renderBridgeDetail();
  try {
    const approved = await showConfirmDialog('confirm', {title:decision.label + ' · ' + line.display_name,
      message:(line.phone || line.line_id) + '\n' + guidance[action], confirmLabel:decision.label});
    if (!approved || epoch !== bridgeManagement.epoch) return;
    const id = crypto.randomUUID();
    // Save BEFORE submission. A lost HTTP response is resolved by GET, never
    // by an automatic second POST or a fresh idempotency key.
    sessionStorage.setItem(bridgeOperationKey(), JSON.stringify({id,line:line.line_id,at:Date.now()}));
    bridgeText($('bridgeOperationNotice'), action === 'reconnect' ? 'Reconnecting WhatsApp. Please wait for confirmation.' : 'Saving the administrative operation…');
    try {
      const op = await api(bridgeManagementBase + '/lines/' + line.line_id + '/operations', {method:'POST',
        headers:{'Idempotency-Key':id}, signal:AbortSignal.timeout(12000), body:JSON.stringify({operation_id:id, action,
          confirm_line_id:line.line_id, expected_epoch:line.administration.epoch,
          expected_generation:line.administration.generation, expected_version:line.administration.version})});
      if (epoch !== bridgeManagement.epoch) return;
      bridgeText($('bridgeOperationNotice'), op.message + ' Operation: ' + op.id);
      if (op.terminal) forgetBridgeOperation(id);
    } catch (error) {
      if (epoch !== bridgeManagement.epoch) return;
      if ([400,403,409,422,429].includes(error.status)) {
        forgetBridgeOperation(id); bridgeText($('bridgeOperationNotice'), error.message);
      } else bridgeText($('bridgeOperationNotice'), 'Confirmation is pending. Checking saved operation ' + id + '; do not submit another request.');
    }
  } catch (_) {
    if (epoch === bridgeManagement.epoch) bridgeText($('bridgeOperationNotice'), 'The operation could not be prepared safely. Refresh diagnostics and check operation history.');
  } finally {
    if (epoch === bridgeManagement.epoch) {
      bridgeManagement.submitting = false; renderBridgeDetail(); void loadBridgeOperations();
      dashboardRefresh.request('bridge-management');
    }
  }
}
async function loadBridgeOperations() {
  const id = bridgeManagement.selected, epoch = bridgeManagement.epoch;
  const line = bridgeManagement.snapshot?.lines.find(item => item.line_id === id);
  if (!id || bridgeManagement.operationsLoading || (!line?.administration && !pendingBridgeOperation())) return;
  bridgeManagement.operationsLoading = true;
  try {
    const pending = pendingBridgeOperation();
    if (pending) {
      try {
        const op = await api(bridgeManagementBase + '/operations/' + pending.id, {signal:AbortSignal.timeout(12000)});
        if (epoch !== bridgeManagement.epoch) return;
        bridgeText($('bridgeOperationNotice'), op.message + ' Number ' + op.line_id + ' · Operation ' + op.id);
        if (op.terminal) forgetBridgeOperation(op.id);
      } catch (error) {
        if (epoch !== bridgeManagement.epoch) return;
        // The server is the authority on active operations; this tab's marker
        // only prevents duplicate clicks. A persistent 404 long after
        // submission means the request was never saved, so nothing will run.
        if (error?.status === 404 && Number.isFinite(pending.at) && Date.now() - pending.at > bridgeOperationSaveGraceMs) {
          forgetBridgeOperation(pending.id);
          bridgeText($('bridgeOperationNotice'), 'Operation ' + pending.id + ' was not saved, so no change was requested. Refresh diagnostics before trying again.');
        } else bridgeText($('bridgeOperationNotice'), 'Operation ' + pending.id + ' is not yet confirmed. Its saved status will be checked automatically. No request was resubmitted.');
      }
    }
    const payload = await api(bridgeManagementBase + '/lines/' + id + '/operations', {signal:AbortSignal.timeout(12000)});
    if (epoch !== bridgeManagement.epoch || id !== bridgeManagement.selected) return;
    const signature = JSON.stringify(payload.operations.map(({updated_at, ...op}) => op));
    if ($('bridgeOperations').dataset.signature !== signature) {
      $('bridgeOperations').dataset.signature = signature;
      $('bridgeOperations').replaceChildren(...payload.operations.map(op => {
        const item = document.createElement('li');
        item.textContent = bridgeDate(op.created_at) + ' · ' + bridgeLabel(op.action) + ' · ' + op.message +
          ' (' + op.reason_code + ') · Staff #' + op.operator_id + ' · ' + op.id +
          (op.completed_at ? ' · Completed ' + bridgeDate(op.completed_at) : '');
        const details = document.createElement('button'); details.type = 'button'; details.className = 'btn small';
        details.textContent = 'View audit';
        details.onclick = async () => {
          details.disabled = true;
          try {
            const result = await api(bridgeManagementBase + '/operations/' + op.id, {signal:AbortSignal.timeout(12000)});
            if (epoch === bridgeManagement.epoch && id === bridgeManagement.selected) bridgeText($('bridgeOperationNotice'),
              result.message + ' · ' + result.id + '\n' + (result.audit || []).map(event => bridgeDate(event.created_at) +
                ' · ' + bridgeLabel(event.state) + ' · ' + event.reason_code + ' · Staff #' + event.operator_id).join('\n'));
          } catch (_) {
            if (epoch === bridgeManagement.epoch) bridgeText($('bridgeOperationNotice'), 'The saved audit is unavailable. Try reading it again.');
          } finally { details.disabled = false; }
        };
        item.append(details);
        if (op.can_cancel && state.agent?.can_recover_whatsapp === true) {
          const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'btn small';
          cancel.textContent = 'Cancel queued operation';
          cancel.onclick = async () => {
            cancel.disabled = true;
            try {
              const result = await api(bridgeManagementBase + '/operations/' + op.id + '/cancel', {method:'POST', signal:AbortSignal.timeout(12000)});
              if (epoch === bridgeManagement.epoch) { forgetBridgeOperation(op.id); bridgeText($('bridgeOperationNotice'), result.message); }
            } catch (_) {
              if (epoch === bridgeManagement.epoch) bridgeText($('bridgeOperationNotice'), 'Cancellation is unconfirmed or no longer safe. Check the existing operation status.');
            }
            dashboardRefresh.request('bridge-management');
          };
          item.append(cancel);
        }
        return item;
      }));
    }
  } catch (_) {
    if (epoch === bridgeManagement.epoch) bridgeText($('bridgeOperationNotice'), 'Operation history is unavailable. Any displayed results may be stale.');
  } finally {
    if (epoch === bridgeManagement.epoch) { bridgeManagement.operationsLoading = false; renderBridgeDetail(); }
  }
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
