async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrf();
  const response = await fetch(path, {credentials: 'same-origin', ...options, cache:'no-store', headers});
  const serverVersion = response.headers && typeof response.headers.get === 'function' ? response.headers.get('X-Support-UI-Version') : null;
  if (serverVersion && serverVersion !== SUPPORT_UI_VERSION) noticeDashboardUpdate(serverVersion);
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (response.status === 401 && path !== '/api/support/login') {
    if (!state.pageLeaving) showLogin();
    const error = new Error('Your staff session expired. Please sign in again.');
    error.status = 401;
    throw error;
  }
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'string' ? detail : (detail && detail.message) || ('Request failed (' + response.status + ')');
    const error = new Error(message);
    error.detail = detail;
    error.status = response.status;
    throw error;
  }
  return data;
}

// After a deploy, reload as soon as nothing would be lost (no draft, upload,
// recording, unsent message or open dialog). Otherwise offer a Reload button.
function dashboardIsIdle() {
  return !state.busy && !state.activeModal && !state.mediaRecorder && !state.voiceRecording && !state.pendingMedia && !state.editTarget &&
    !state.imageEditor && !state.localMessages.size && !String($('message').value || '').trim();
}

function noticeDashboardUpdate(version) {
  if (state.uiUpdateVersion === version) return;
  state.uiUpdateVersion = version;
  $('updateBanner').classList.remove('hidden');
  let attempted = null;
  try { attempted = sessionStorage.getItem('shipment-support-ui-reload'); } catch (_) {}
  // Reload by itself only once per version, so a stale cache can never loop.
  if (attempted === version) return;
  const tryReload = () => {
    if (state.uiUpdateVersion !== version) return;
    if (!dashboardIsIdle()) { window.setTimeout(tryReload, 3000); return; }
    try { sessionStorage.setItem('shipment-support-ui-reload', version); } catch (_) {}
    saveSupportSelection();
    markPageLeaving();
    window.location.reload();
  };
  window.setTimeout(tryReload, 1000);
}

function setAgentIdentity(agent) {
  const name = agent?.name || 'Support agent';
  text($('agentName'), name);
  text($('agentLabel'), staffRoleLabel(agent?.role || 'agent'));
  text($('agentAvatar'), initials(name));
}

function canManageStaff() {
  return Boolean(state.agent && (state.agent.can_manage_staff || ['supervisor', 'admin'].includes(state.agent.role)));
}

function canManageAiSettings() {
  return Boolean(
    state.agent &&
    (state.agent.can_manage_ai || state.agent.can_manage_staff || ['supervisor', 'admin'].includes(state.agent.role))
  );
}

function canManageGoogleContacts() {
  return Boolean(state.agent && ['supervisor', 'admin'].includes(state.agent.role));
}

function canManageWhatsApp() {
  return Boolean(
    state.agent &&
    (state.agent.can_manage_whatsapp || state.agent.can_manage_staff || ['supervisor', 'admin'].includes(state.agent.role))
  );
}

function canViewActiveQueue() {
  return Boolean(state.agent && ['supervisor', 'admin'].includes(state.agent.role));
}

function configureStaffAccess() {
  // Active shows every live ticket, for Supervisors and Admins to monitor.
  $('tabs').querySelector('button[data-view="active"]')?.classList.toggle('hidden', !canViewActiveQueue());
  if (state.view === 'active' && !canViewActiveQueue()) setQueueView('unassigned', {load: false, persist: false});
  $('manageStaffBtn').classList.toggle('hidden', !canManageStaff());
  $('manageWhatsAppBtn').classList.toggle('hidden', !canManageWhatsApp());
  $('agentStatisticsBtn').classList.toggle('hidden', !canViewAgentStatistics());
  if (!canViewAgentStatistics()) clearAgentStatistics();
}

function emptyMarkup(title, body, iconName = 'inbox') {
  const outer = document.createElement('div');
  outer.className = 'empty-state';
  const inner = document.createElement('div');
  inner.className = 'empty-state-inner';
  const icon = document.createElement('div');
  icon.className = 'empty-icon';
  icon.append(createIcon(iconName, 'icon icon-lg'));
  const heading = document.createElement('h3');
  heading.textContent = title;
  const paragraph = document.createElement('p');
  paragraph.textContent = body;
  inner.append(icon, heading, paragraph);
  outer.append(inner);
  return {outer, inner};
}
