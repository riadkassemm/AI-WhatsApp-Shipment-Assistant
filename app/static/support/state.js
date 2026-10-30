const QUEUE_CONFIG = {
  unassigned: {label: 'Unassigned', emptyTitle: 'No unassigned tickets', emptyBody: 'New customer escalations will appear here when they need an owner.', icon: 'inbox'},
  mine: {label: 'My tickets', emptyTitle: 'No tickets assigned to you', emptyBody: 'Claim an unassigned ticket or wait for a transfer from another agent.', icon: 'user-check'},
  chats: {label: 'Chats', emptyTitle: 'No staff chats yet', emptyBody: 'Use New chat to start a WhatsApp conversation with a customer.', icon: 'message'},
  contacts: {label: 'Contacts', emptyTitle: 'No contacts yet', emptyBody: 'Save a customer contact, sync Google Contacts, or import a VCF address book.', icon: 'users'},
  watching: {label: 'Watching', emptyTitle: 'No watched conversations', emptyBody: 'Tickets you previously transferred remain here while they are active.', icon: 'eye'},
  active: {label: 'Active', emptyTitle: 'No active tickets', emptyBody: 'There are no active conversations available in this view.', icon: 'activity'},
  waiting: {label: 'Waiting', emptyTitle: 'Nothing is waiting', emptyBody: 'Tickets waiting on a customer or internal follow-up will appear here.', icon: 'clock'},
  history: {label: 'History', emptyTitle: 'No ticket history', emptyBody: 'Resolved and closed conversations will appear here.', icon: 'history'}
};
const STATUS_LABELS = {
  NEW: 'New',
  ASSIGNED: 'Assigned',
  IN_PROGRESS: 'In progress',
  WAITING_CUSTOMER: 'Waiting for customer',
  WAITING_INTERNAL: 'Waiting internally',
  RESOLVED: 'Resolved',
  CLOSED: 'Closed'
};
const MESSAGE_ROLE_LABELS = {
  CUSTOMER: 'Customer',
  AGENT: 'Support agent',
  AI: 'AI assistant',
  SYSTEM: 'System'
};
const REACTION_EMOJIS = ['👍', '❤️', '😂', '😮', '😢', '🙏'];
// Queue rows in these views tint their time as a customer keeps waiting.
const WAITING_AGE_VIEWS = ['unassigned', 'waiting'];
const SUPPORT_UI_VERSION = '__UI_VERSION__';
// Queues load this many rows per request at most (the API limit).
const QUEUE_MAX_REQUEST_SIZE = 500;
const WAITING_AGE_WARN_MINUTES = 5;
const WAITING_AGE_ALERT_MINUTES = 15;
const state = {
  view: 'unassigned',
  page: 1,
  pageSize: 50,
  total: 0,
  queueItems: [],
  queueFilters: {supportNumber: "", staffId: "", status: "all"},
  dashboardReadBoundaries: new Map(),
  chatNotificationIds: new Set(),
  queueLoadingMore: false,
  queueExhausted: false,
  queueMoreObserver: null,
  queueTotals: {},
  queueCountsBusy: false,
  queueCountsPending: false,
  query: '',
  globalSearchResults: [],
  globalSearchBusy: false,
  globalSearchTimer: null,
  globalSearchRequest: 0,
  conversationSearchOpen: false,
  conversationSearchQuery: '',
  conversationSearchResults: [],
  conversationSearchIndex: -1,
  conversationSearchTimer: null,
  searchTargetMessageId: null,
  queueRequest: 0,
  ticketRequest: 0,
  ticketPrefetch: new Map(),
  ticketId: null,
  currentTicketData: null,
  currentConversationOrigin: null,
  previousTicketHistory: [],
  previousTicketHistoryHasMore: false,
  previousHistoryExpanded: false,
  previousHistoryLoaded: false,
  previousHistoryPending: false,
  historyJumpActive: false,
  historyJumpReachedTop: false,
  cachedCurrentMessages: [],
  currentMessagesHasMore: false,
  currentMessagesExpanded: false,
  historyLoadBusy: false,
  historyLoadRequest: 0,
  historySelectionEpoch: 0,
  timelineSignature: '',
  pendingConversationRender: null,
  conversationSelectionPointer: null,
  agent: null,
  busy: false,
  authenticated: false,
  pageLeaving: false,
  staffBusy: false,
  messagePreferencesBusy: false,
  quickReplies: [],
  quickRepliesLoaded: false,
  quickRepliesBusy: false,
  quickRepliesRequest: 0,
  quickReplyEditing: null,
  quickReplySharing: null,
  quickReplyTab: "text",
  quickReplyKind: "text",
  quickReplyRecipients: [],
  quickReplyRecipientsLoading: false,
  contactEditTarget: null,
  contactEditBusy: false,
  aiSettingsBusy: false,
  aiSettings: null,
  whatsappBusy: false,
  whatsappRegistry: null,
  linkedDeviceStatus: null,
  linkedDeviceBusy: false,
  linkedDeviceRequest: 0,
  linkedDeviceAdditionId: null,
  chatBusy: false,
  chatLines: [],
  forwardTarget: null,
  forwardBusy: false,
  forwardSelection: null,
  forwardContacts: [],
  forwardConversations: [],
  forwardSearchTimer: null,
  forwardChoiceRequest: 0,
  googleContactsStatus: null,
  googleContactsBusy: false,
  vcfImportBusy: false,
  staffItems: [],
  staffView: 'list',
  editingStaffId: null,
  editingStaffOriginal: null,
  conflictStaff: null,
  composerEnabled: false,
  pendingMedia: null,
  pendingMediaItems: [],
  selectedMessages: new Map(),
  selectingMessages: false,
  replyTarget: null,
  mediaRecorder: null,
  mediaStream: null,
  voiceRecording: null,
  recordingTimer: null,
  recordingStartedAt: 0,
  transferBusy: false,
  transferTargets: [],
  activeModal: null,
  lastFocused: null,
  contextOpen: false,
  notificationAudioContext: null,
  notificationSoundGain: null,
  notificationSoundsMuted: readNotificationSoundPreference(),
  notificationUnlocked: false,
  notificationPermissionRequested: false,
  supportEventSource: null,
  // One reconnect lifecycle per page: at most one scheduled retry and one
  // session check; a generation bump invalidates late results after sign-out.
  supportEventsRetryTimer: null,
  supportEventsAttempt: 0,
  supportEventsGeneration: 0,
  supportEventsProbe: null,
  queueLoadedIdentity: null,
  mediaZoom: 1,
  mediaTranslateX: 0,
  mediaTranslateY: 0,
  mediaSource: null,
  editTarget: null,
  deleteTarget: null,
  imageEditor: null,
  unassignedSnapshot: null,
  lastInboundByTicket: {},
  localMessages: new Map(),
  realtimeMessages: new Map(),
  textSendChain: Promise.resolve()
};

const SUPPORT_SELECTION_STORAGE_PREFIX = 'shipment-support-workspace-v1:';

function supportSelectionStorageKey() {
  const staffId = String(state.agent?.id || '').trim();
  return staffId ? SUPPORT_SELECTION_STORAGE_PREFIX + encodeURIComponent(staffId) : null;
}

function saveSupportSelection() {
  if (!state.authenticated) return;
  const key = supportSelectionStorageKey();
  if (!key) return;
  const ticketId = Number(state.ticketId);
  const origin = String(state.currentConversationOrigin || '').toUpperCase();
  const selected = Number.isSafeInteger(ticketId) && ticketId > 0 && ['STAFF', 'CUSTOMER'].includes(origin);
  try {
    sessionStorage.setItem(key, JSON.stringify({
      view: Object.prototype.hasOwnProperty.call(QUEUE_CONFIG, state.view) ? state.view : 'unassigned',
      page: Number.isSafeInteger(state.page) && state.page > 0 ? state.page : 1,
      ticketId: selected ? ticketId : null,
      conversationOrigin: selected ? origin : null
    }));
  } catch (_) {}
}

function readSupportSelection() {
  if (!state.authenticated) return null;
  const key = supportSelectionStorageKey();
  if (!key) return null;
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const saved = JSON.parse(raw);
    if (!saved || !Object.prototype.hasOwnProperty.call(QUEUE_CONFIG, saved.view)) throw new Error('Invalid view');
    const ticketId = Number.isSafeInteger(saved.ticketId) && saved.ticketId > 0 ? saved.ticketId : null;
    const origin = ['STAFF', 'CUSTOMER'].includes(saved.conversationOrigin) ? saved.conversationOrigin : null;
    return {
      view: saved.view,
      page: Number.isSafeInteger(saved.page) && saved.page > 0 && saved.page <= 10000 ? saved.page : 1,
      ticketId: ticketId && origin ? ticketId : null,
      conversationOrigin: ticketId && origin ? origin : null
    };
  } catch (_) {
    try { sessionStorage.removeItem(key); } catch (_) {}
    return null;
  }
}

function clearSupportSelections() {
  try {
    for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
      const key = sessionStorage.key(index);
      if (key?.startsWith(SUPPORT_SELECTION_STORAGE_PREFIX)) sessionStorage.removeItem(key);
    }
  } catch (_) {}
}

const $ = id => document.getElementById(id);

function createIcon(name, className = 'icon') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', className);
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#icon-' + name);
  svg.append(use);
  return svg;
}

function setIcon(button, name) {
  const use = button && button.querySelector('use');
  if (use) use.setAttribute('href', '#icon-' + name);
}

function cookie(name) {
  return document.cookie.split(';').map(value => value.trim()).find(value => value.startsWith(name + '='))?.slice(name.length + 1) || '';
}

function csrf() {
  return decodeURIComponent(cookie('__CSRF_COOKIE__'));
}

function text(element, value) {
  if (element) element.textContent = value ?? '';
}

function initials(value) {
  const words = String(value || '').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '—';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

// Server timestamps are UTC, but some API fields arrive without an offset
// ("2026-10-18T00:20:00"), which Date() would read as the viewer's local time.
const NAIVE_SERVER_TIMESTAMP = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/;

function parseServerDate(value) {
  if (value instanceof Date) return value;
  const text = typeof value === 'string' ? value.trim() : '';
  if (NAIVE_SERVER_TIMESTAMP.test(text)) return new Date(text.replace(' ', 'T') + 'Z');
  return new Date(value);
}

function messageOccurredAt(message) {
  const numeric = Number(message?.whatsapp_timestamp);
  if (Number.isFinite(numeric) && numeric > 0) {
    const milliseconds = numeric < 100_000_000_000 ? numeric * 1000 : numeric;
    const occurred = new Date(milliseconds);
    if (!Number.isNaN(occurred.getTime()) && milliseconds <= Date.now() + 300_000) {
      return occurred.toISOString();
    }
  }
  return message?.created_at || null;
}

function fmt(value) {
  if (!value) return '—';
  const date = parseServerDate(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'}).format(date);
}

function fmtInTimezone(value, timezone) {
  if (!value) return '—';
  const date = parseServerDate(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try {
    return new Intl.DateTimeFormat(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      timeZone: timezone || 'UTC',
      timeZoneName: 'short'
    }).format(date);
  } catch (_) {
    return fmt(value);
  }
}

function relativeTime(value) {
  if (!value) return '—';
  const date = parseServerDate(value);
  if (Number.isNaN(date.getTime())) return '—';
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const absolute = Math.abs(seconds);
  const formatter = new Intl.RelativeTimeFormat(undefined, {numeric: 'auto', style: 'narrow'});
  if (absolute < 60) return formatter.format(seconds, 'second');
  if (absolute < 3600) return formatter.format(Math.round(seconds / 60), 'minute');
  if (absolute < 86400) return formatter.format(Math.round(seconds / 3600), 'hour');
  if (absolute < 604800) return formatter.format(Math.round(seconds / 86400), 'day');
  return new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric'}).format(date);
}

function formatStatus(value) {
  return STATUS_LABELS[String(value || '').toUpperCase()] || String(value || 'Unknown').replaceAll('_', ' ').toLowerCase().replace(/^./, char => char.toUpperCase());
}

function statusClass(value) {
  return 'status-' + String(value || 'default').toLowerCase().replaceAll('_', '-');
}

function statusBadge(value) {
  const badge = document.createElement('span');
  badge.className = 'status-badge ' + statusClass(value);
  badge.textContent = formatStatus(value);
  return badge;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return value + ' B';
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + ' KB';
  return (value / (1024 * 1024)).toFixed(1) + ' MB';
}

function setAppStatus(message) {
  const element = $('appStatus');
  element.textContent = '';
  window.setTimeout(() => { element.textContent = message || ''; }, 20);
}

function showLoginError(message) {
  const element = $('loginError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function showError(message) {
  if (state.pageLeaving) return;
  const banner = $('error');
  text($('errorText'), message || '');
  banner.classList.toggle('visible', Boolean(message));
}

function markPageLeaving() {
  if (state.pageLeaving) return;
  state.pageLeaving = true;
  state.sessionCheckController?.abort();
  state.queueRequest += 1;
  state.ticketRequest += 1;
}

function notify(message, type = 'success', onOpen = null) {
  if (!message || state.pageLeaving) return;
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
  toast.append(createIcon(type === 'error' ? 'alert' : 'check-circle'));
  const copy = document.createElement('div');
  copy.className = 'toast-copy';
  copy.textContent = message;
  if (onOpen) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'toast-open'; button.textContent = message;
    button.onclick = () => { toast.remove(); onOpen(); };
    copy.replaceChildren(button);
  }
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'icon-btn';
  close.setAttribute('aria-label', 'Dismiss notification');
  close.append(createIcon('x', 'icon icon-sm'));
  close.onclick = () => toast.remove();
  toast.append(copy, close);
  positionToastRegion();
  $('toastRegion').append(toast);
  window.setTimeout(() => toast.remove(), 5200);
}

// Keep notifications above the reply composer so they never cover Send or
// the attachment controls while they are visible.
function positionToastRegion() {
  const region = $('toastRegion');
  const composer = document.querySelector('.composer-shell');
  const rect = composer && composer.offsetParent !== null ? composer.getBoundingClientRect() : null;
  const lift = rect && rect.height ? Math.max(0, window.innerHeight - rect.top) : 0;
  region.style.bottom = lift ? Math.round(lift + 12) + 'px' : '';
}

if (typeof ResizeObserver === 'function') {
  // The composer grows with attachments and long drafts; move visible toasts with it.
  new ResizeObserver(() => positionToastRegion()).observe(document.querySelector('.composer-shell'));
}
window.addEventListener('resize', positionToastRegion);

function readNotificationSoundPreference() {
  try { return localStorage.getItem('shipment-support-sounds-muted') === 'true'; }
  catch (_) { return false; }
}

function syncNotificationSoundControls() {
  const button = $('notificationSoundBtn');
  if (!button) return;
  button.setAttribute('aria-pressed', String(state.notificationSoundsMuted));
  button.title = state.notificationSoundsMuted ? 'Unmute notification sounds' : 'Mute notification sounds';
  setIcon(button, state.notificationSoundsMuted ? 'volume-off' : 'volume');
  $('notificationSoundLabel').textContent = state.notificationSoundsMuted ? 'Sound muted' : 'Sound on';
  if (state.notificationSoundGain) state.notificationSoundGain.gain.value = state.notificationSoundsMuted ? 0 : 1;
}

function toggleNotificationSounds() {
  state.notificationSoundsMuted = !state.notificationSoundsMuted;
  try { localStorage.setItem('shipment-support-sounds-muted', String(state.notificationSoundsMuted)); } catch (_) {}
  syncNotificationSoundControls();
  if (!state.notificationSoundsMuted) unlockNotificationAudio();
}

function syncNotificationSoundStorage(event) {
  if (event.key !== 'shipment-support-sounds-muted' && event.key !== null) return;
  state.notificationSoundsMuted = readNotificationSoundPreference();
  syncNotificationSoundControls();
}

function unlockNotificationAudio() {
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass && !state.notificationAudioContext) state.notificationAudioContext = new AudioContextClass();
    if (state.notificationAudioContext && state.notificationAudioContext.state === 'suspended') state.notificationAudioContext.resume().catch(() => {});
    state.notificationUnlocked = Boolean(state.notificationAudioContext);
  } catch (_) {}
  if (state.authenticated && !state.notificationPermissionRequested && 'Notification' in window && Notification.permission === 'default') {
    state.notificationPermissionRequested = true;
    try { Notification.requestPermission().catch(() => {}); } catch (_) {}
  }
}

function playNewConversationSound() {
  const context = state.notificationAudioContext;
  if (state.notificationSoundsMuted || !state.notificationUnlocked || !context || context.state === 'closed') return;
  const play = () => {
    // A mute click may happen while the browser is resuming the audio context.
    if (state.notificationSoundsMuted) return;
    try {
      if (!state.notificationSoundGain) {
        state.notificationSoundGain = context.createGain();
        state.notificationSoundGain.connect(context.destination);
      }
      const start = context.currentTime + 0.01;
      for (const [offset, frequency] of [[0, 740], [0.16, 980], [0.32, 820]]) {
        const oscillator = context.createOscillator();
        const gain = context.createGain();
        oscillator.type = 'sine';
        oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0.0001, start + offset);
        gain.gain.exponentialRampToValueAtTime(0.22, start + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + 0.14);
        oscillator.connect(gain);
        gain.connect(state.notificationSoundGain);
        oscillator.start(start + offset);
        oscillator.stop(start + offset + 0.15);
      }
    } catch (_) {}
  };
  if (context.state === 'suspended') context.resume().then(play).catch(() => {});
  else play();
}

function notifyNewCustomerActivity(message) {
  const body = message || 'New customer conversation requires attention.';
  playNewConversationSound();
  document.title = '● MIK Support';
  if ('setAppBadge' in navigator) navigator.setAppBadge(1).catch(() => {});
  if ('Notification' in window && Notification.permission === 'granted') {
    try { new Notification('Shipment Support', {body, tag: 'shipment-support', renotify: true, silent: state.notificationSoundsMuted}); } catch (_) {}
  }
}

function clearAttentionIndicator() {
  document.title = 'MIK Support';
  if ('clearAppBadge' in navigator) navigator.clearAppBadge().catch(() => {});
}

function staffRoleLabel(role) {
  return ({agent: 'Agent', admin: 'Admin', supervisor: 'Super Admin'})[role] || role || '—';
}
