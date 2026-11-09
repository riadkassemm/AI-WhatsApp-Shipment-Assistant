function setButtonBusy(button, busy, busyLabel = 'Working…') {
  if (!button) return;
  if (busy) {
    if (!button.dataset.originalHtml) button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.replaceChildren(createIcon('refresh'), document.createTextNode(busyLabel));
  } else {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    if (button.dataset.originalHtml) {
      button.innerHTML = button.dataset.originalHtml;
      delete button.dataset.originalHtml;
    }
  }
}

function currentTheme() {
  return document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
}

function syncThemeControls() {
  const dark = currentTheme() === 'dark';
  const nextLabel = dark ? 'Switch to light theme' : 'Switch to dark theme';
  for (const id of ['themeBtn', 'loginThemeBtn']) {
    const button = $(id);
    if (!button) continue;
    button.setAttribute('aria-label', nextLabel);
    setIcon(button, dark ? 'sun' : 'moon');
  }
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  if (themeMeta) themeMeta.setAttribute('content', dark ? '#0b1115' : '#f4f6f8');
}

function toggleTheme() {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('shipment-support-theme', next); } catch (_) {}
  syncThemeControls();
  setAppStatus(next === 'dark' ? 'Dark theme enabled.' : 'Light theme enabled.');
}

function focusableElements(container) {
  return [...container.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')].filter(element => !element.closest('.hidden') && element.offsetParent !== null);
}

function openDialog(id, focusId) {
  const modal = $(id);
  if (!modal) return;
  state.lastFocused = document.activeElement;
  state.activeModal = id;
  modal.classList.remove('hidden');
  modal.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
  window.setTimeout(() => {
    const target = focusId ? $(focusId) : null;
    const fallback = focusableElements(modal)[0] || modal.querySelector('.modal-card');
    (target || fallback)?.focus();
  }, 20);
}

function closeDialog(id, restoreFocus = true) {
  const modal = $(id);
  if (!modal) return;
  modal.classList.add('hidden');
  modal.setAttribute('aria-hidden', 'true');
  if (state.activeModal === id) state.activeModal = null;
  if (!state.activeModal) document.body.classList.remove('modal-open');
  if (restoreFocus && state.lastFocused && typeof state.lastFocused.focus === 'function') {
    const target = state.lastFocused;
    state.lastFocused = null;
    window.setTimeout(() => target.focus(), 10);
  }
}

// In-app replacement for window.confirm/prompt/alert. It can open on top of
// another dialog and restores that dialog's focus trap when it settles.
const CONFIRM_TONE_ICONS = {default: 'info', danger: 'alert', success: 'check-circle', warning: 'alert'};
let pendingConfirmDialog = null;

function settleConfirmDialog(result) {
  const pending = pendingConfirmDialog;
  if (!pending) return;
  pendingConfirmDialog = null;
  closeDialog('confirmModal', false);
  state.activeModal = pending.previousModal;
  state.lastFocused = pending.previousLastFocused;
  if (state.activeModal) document.body.classList.add('modal-open');
  const focusTarget = pending.returnFocus;
  if (focusTarget && typeof focusTarget.focus === 'function') window.setTimeout(() => focusTarget.focus(), 10);
  pending.resolve(pending.kind === 'prompt' ? (result ? $('confirmInput').value : null) : Boolean(result));
}

function showConfirmDialog(kind, {title, message = '', confirmLabel = 'Confirm', cancelLabel = 'Cancel', tone = 'default', label = '', value = ''} = {}) {
  if (pendingConfirmDialog) settleConfirmDialog(false);
  const card = $('confirmModal').querySelector('.confirm-card');
  card.dataset.tone = tone;
  $('confirmIconUse').setAttribute('href', '#icon-' + (CONFIRM_TONE_ICONS[tone] || 'info'));
  text($('confirmTitle'), title || (kind === 'alert' ? 'Notice' : 'Are you sure?'));
  text($('confirmMessage'), message);
  $('confirmMessage').classList.toggle('hidden', !message);
  $('confirmInputField').classList.toggle('hidden', kind !== 'prompt');
  text($('confirmInputLabel'), label || title || 'Value');
  $('confirmInput').value = value || '';
  const accept = $('confirmAcceptBtn');
  accept.className = 'btn ' + (tone === 'danger' ? 'danger' : tone === 'success' ? 'success-action' : 'primary');
  text(accept, confirmLabel);
  text($('confirmCancelBtn'), cancelLabel);
  $('confirmCancelBtn').classList.toggle('hidden', kind === 'alert');
  const previousModal = state.activeModal;
  const previousLastFocused = state.lastFocused;
  const returnFocus = document.activeElement;
  const focusId = kind === 'prompt' ? 'confirmInput' : tone === 'danger' ? 'confirmCancelBtn' : 'confirmAcceptBtn';
  return new Promise(resolve => {
    pendingConfirmDialog = {kind, resolve, previousModal, previousLastFocused, returnFocus};
    openDialog('confirmModal', focusId);
    if (kind === 'prompt') window.setTimeout(() => $('confirmInput').select(), 30);
  });
}

function confirmDialog(options) {
  return showConfirmDialog('confirm', options);
}

function promptDialog(options) {
  return showConfirmDialog('prompt', options);
}

function alertDialog(options) {
  return showConfirmDialog('alert', {confirmLabel: 'OK', ...options});
}

function openShortcutsHelp() {
  if (!state.authenticated || state.activeModal) return;
  openDialog('shortcutsModal', 'closeShortcutsBtn');
}

function closeShortcutsHelp() {
  closeDialog('shortcutsModal');
}

const LAYOUT_STORAGE = {
  queueWidth: 'shipment-support-queue-width',
  contextWidth: 'shipment-support-context-width',
  contextCollapsed: 'shipment-support-context-collapsed'
};

function clampLayoutValue(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, Number(value) || minimum));
}

function readLayoutWidth(variableName, fallback) {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(variableName);
  const parsed = Number.parseFloat(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function setLayoutWidth(variableName, storageKey, value, minimum, maximum, handle = null, persist = true) {
  const width = Math.round(clampLayoutValue(value, minimum, maximum));
  document.documentElement.style.setProperty(variableName, width + 'px');
  if (handle) handle.setAttribute('aria-valuenow', String(width));
  if (persist) {
    try { localStorage.setItem(storageKey, String(width)); } catch (_) {}
  }
  return width;
}

function restoreLayoutPreferences() {
  try {
    const queueWidth = Number(localStorage.getItem(LAYOUT_STORAGE.queueWidth));
    if (Number.isFinite(queueWidth) && queueWidth > 0) setLayoutWidth('--queue-width', LAYOUT_STORAGE.queueWidth, queueWidth, 260, 520, $('queueResizer'), false);
    const contextWidth = Number(localStorage.getItem(LAYOUT_STORAGE.contextWidth));
    if (Number.isFinite(contextWidth) && contextWidth > 0) setLayoutWidth('--context-width', LAYOUT_STORAGE.contextWidth, contextWidth, 260, 520, $('contextResizer'), false);
    const collapsed = localStorage.getItem(LAYOUT_STORAGE.contextCollapsed);
    state.contextOpen = collapsed === null ? false : collapsed !== 'true';
  } catch (_) {
    state.contextOpen = false;
  }
  $('queueResizer').setAttribute('aria-valuenow', String(Math.round(readLayoutWidth('--queue-width', 340))));
  $('contextResizer').setAttribute('aria-valuenow', String(Math.round(readLayoutWidth('--context-width', 300))));
  syncContextLayout();
}

function syncContextLayout() {
  const desktop = window.innerWidth > 1100;
  const workspace = $('workspaceContent');
  const panel = $('contextPanel');
  const backdrop = $('contextBackdrop');
  const toggle = $('contextToggleBtn');
  if (desktop) {
    workspace.classList.toggle('context-collapsed', !state.contextOpen);
    panel.classList.remove('open');
    backdrop.classList.remove('visible');
  } else {
    workspace.classList.remove('context-collapsed');
    panel.classList.toggle('open', state.contextOpen);
    backdrop.classList.toggle('visible', state.contextOpen);
  }
  toggle.setAttribute('aria-expanded', state.contextOpen ? 'true' : 'false');
  toggle.setAttribute('aria-label', state.contextOpen ? 'Close customer details' : 'Open customer details');
  toggle.setAttribute('data-tooltip', state.contextOpen ? 'Hide customer details' : 'Customer details');
}

function rememberContextPreference() {
  try { localStorage.setItem(LAYOUT_STORAGE.contextCollapsed, state.contextOpen ? 'false' : 'true'); } catch (_) {}
}

function openContextPanel() {
  state.contextOpen = true;
  rememberContextPreference();
  syncContextLayout();
  if (window.innerWidth <= 1100) window.setTimeout(() => $('closeContextBtn').focus(), 20);
}

function closeContextPanel(restoreFocus = true) {
  state.contextOpen = false;
  rememberContextPreference();
  syncContextLayout();
  if (restoreFocus) $('contextToggleBtn').focus();
}

function toggleContextPanel() {
  if (state.contextOpen) closeContextPanel(false);
  else openContextPanel();
}

function installColumnResizer({id, variableName, storageKey, minimum, maximum, direction = 1, enabled}) {
  const handle = $(id);
  if (!handle) return;
  let startX = 0;
  let startWidth = 0;
  let activePointer = null;

  const setWidth = (value, persist = true) => setLayoutWidth(variableName, storageKey, value, minimum, maximum, handle, persist);
  const stop = () => {
    if (activePointer === null) return;
    activePointer = null;
    handle.classList.remove('active');
    document.body.classList.remove('column-resizing');
  };

  handle.addEventListener('pointerdown', event => {
    if (enabled && !enabled()) return;
    if (event.button !== 0) return;
    activePointer = event.pointerId;
    startX = event.clientX;
    startWidth = readLayoutWidth(variableName, minimum);
    handle.classList.add('active');
    document.body.classList.add('column-resizing');
    handle.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  });
  handle.addEventListener('pointermove', event => {
    if (activePointer !== event.pointerId) return;
    setWidth(startWidth + ((event.clientX - startX) * direction), false);
  });
  handle.addEventListener('pointerup', event => {
    if (activePointer !== event.pointerId) return;
    setWidth(readLayoutWidth(variableName, minimum), true);
    stop();
  });
  handle.addEventListener('pointercancel', stop);
  handle.addEventListener('lostpointercapture', stop);
  window.addEventListener('blur', stop);
  handle.addEventListener('keydown', event => {
    if (enabled && !enabled()) return;
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Home') return void setWidth(minimum);
    if (event.key === 'End') return void setWidth(maximum);
    const step = event.shiftKey ? 32 : 16;
    const current = readLayoutWidth(variableName, minimum);
    const delta = event.key === 'ArrowRight' ? step : -step;
    setWidth(current + (delta * direction));
  });
  handle.addEventListener('dblclick', () => {
    const defaultWidth = variableName === '--queue-width' ? 340 : 300;
    setWidth(defaultWidth);
  });
}

function isTypingTarget(target) {
  if (!target || !(target instanceof Element)) return false;
  return Boolean(target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]'));
}

function moveQueueSelection(delta) {
  const rows = [...$('queue').querySelectorAll('.ticket-row, .search-result-row')];
  if (!rows.length) return;
  const current = rows.findIndex(row => row.classList.contains('active') || row === document.activeElement);
  const index = current < 0 ? (delta > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, current + delta));
  const target = rows[index];
  if (!target || (target === rows[current] && target.classList.contains('active'))) return;
  for (const row of rows) row.tabIndex = row === target ? 0 : -1;
  target.focus();
  target.scrollIntoView({block: 'nearest'});
  target.click();
}

function handleAgentShortcut(event) {
  if (!state.authenticated || state.activeModal || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return false;
  if (isTypingTarget(event.target)) return false;
  const key = event.key;
  if (key === '?') { openShortcutsHelp(); return true; }
  if (key === '/') {
    if ($('queueSearch').offsetParent === null) return false;
    $('queueSearch').focus();
    $('queueSearch').select();
    return true;
  }
  if (event.shiftKey) return false;
  const lower = key.toLowerCase();
  if (lower === 'j' || lower === 'k') {
    if (state.view === 'contacts') return false;
    if (window.innerWidth <= 800 && document.body.classList.contains('mobile-ticket-open')) return false;
    moveQueueSelection(lower === 'j' ? 1 : -1);
    return true;
  }
  if (lower === 'r' && state.composerEnabled && !state.busy && !$('message').disabled) {
    const textarea = $('message');
    textarea.focus({preventScroll: true});
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    return true;
  }
  return false;
}

function handleGlobalKeydown(event) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && state.authenticated && !state.activeModal) {
    event.preventDefault();
    $('queueSearch').focus();
    return;
  }
  if (event.key !== 'Escape' && event.key !== 'Tab' && handleAgentShortcut(event)) {
    event.preventDefault();
    return;
  }
  if (event.key === 'Escape') {
    if (state.activeModal === 'confirmModal') {
      event.preventDefault();
      settleConfirmDialog(false);
      return;
    }
    if (state.activeModal === 'shortcutsModal') {
      event.preventDefault();
      closeShortcutsHelp();
      return;
    }
    if (state.contextOpen) {
      event.preventDefault();
      closeContextPanel();
      return;
    }
    if (state.activeModal === 'startChatModal') {
      event.preventDefault();
      closeStartChatModal();
      return;
    }
    if (state.activeModal === 'forwardModal') {
      event.preventDefault();
      closeForwardModal();
      return;
    }
    if (state.activeModal === 'transferModal') {
      event.preventDefault();
      closeTransferModal();
      return;
    }
    if (state.activeModal === 'messagePreferencesModal') {
      event.preventDefault();
      closeMessagePreferencesModal();
      return;
    }
    if (state.activeModal === 'contactEditModal') {
      event.preventDefault();
      closeContactEditModal();
      return;
    }
    if (state.activeModal === 'quickRepliesModal') {
      event.preventDefault();
      if (state.quickReplyEditing) showQuickReplyList();
      else closeQuickRepliesModal();
      return;
    }
    if (state.activeModal === 'aiSettingsModal') {
      event.preventDefault();
      closeAiSettingsModal();
      return;
    }
    if (state.activeModal === 'whatsappModal') {
      event.preventDefault();
      closeWhatsAppModal();
      return;
    }
    if (state.activeModal === 'locationModal') {
      event.preventDefault();
      closeLocationModal();
      return;
    }
    if (state.activeModal === 'mediaViewerModal') {
      event.preventDefault();
      if (state.imageEditor?.mode === 'crop') state.imageEditor.cancelCrop();
      else closeMediaViewer();
      return;
    }
    if (state.activeModal === 'messageDeleteModal') {
      event.preventDefault();
      closeMessageDelete();
      return;
    }
    if (state.activeModal === 'agentStatisticsModal') {
      closeDialog('agentStatisticsModal');
      return;
    }
    if (state.activeModal === 'staffModal') {
      event.preventDefault();
      closeStaffModal();
      return;
    }
  }
  if (event.key === 'Tab' && state.activeModal) {
    const modal = $(state.activeModal);
    const items = focusableElements(modal);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
}
