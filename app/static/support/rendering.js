function renderQueueSkeleton() {
  const container = document.createElement('div');
  container.className = 'loading-list';
  for (let index = 0; index < 6; index += 1) {
    const row = document.createElement('div');
    row.className = 'skeleton-row';
    for (const size of ['medium', 'long', 'short']) {
      const line = document.createElement('div');
      line.className = 'skeleton skeleton-line ' + size;
      row.append(line);
    }
    container.append(row);
  }
  $('queue').replaceChildren(container);
}

function clearTicketWorkspace() {
  cancelMessageEdit(false);
  state.ticketId = null;
  state.currentTicketData = null;
  state.currentConversationOrigin = null;
  state.previousTicketHistory = [];
  state.previousTicketHistoryHasMore = false;
  state.previousHistoryExpanded = false;
  state.previousHistoryLoaded = false;
  state.previousHistoryPending = false;
  state.historyJumpActive = false;
  state.historyJumpReachedTop = false;
  updateSharedHistoryShortcut();
  $('scrollToLatestBtn').classList.add('hidden');
  state.cachedCurrentMessages = [];
  state.currentMessagesHasMore = false;
  state.currentMessagesExpanded = false;
  state.historyLoadBusy = false;
  state.historyLoadRequest += 1;
  state.historySelectionEpoch += 1;
  state.timelineSignature = '';
  state.pendingConversationRender = null;
  state.composerEnabled = false;
  closeConversationSearch(false);
  state.queueItems = [];
  state.total = 0;
  $('queue').replaceChildren(emptyMarkup('No tickets loaded', 'Your selected queue will appear here after sign-in.', 'inbox').outer);
  text($('ticketTitle'), 'Select a ticket');
  text($('ticketSubtitle'), 'Choose a conversation from the queue to begin.');
  $('ticketHeadingStatus').replaceChildren();
  $('ticketHeaderMeta').replaceChildren();
  $('conversation').innerHTML = '<div class="conversation-empty"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-message"></use></svg></div><h3>Open a customer conversation</h3><p>Ticket history, messages, and activity will appear here.</p></div></div>';
  $('context').innerHTML = '<div class="empty-state"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-user"></use></svg></div><h3>No ticket selected</h3><p>Customer and shipment context will appear here.</p></div></div>';
  $('actions').replaceChildren();
  $('readOnlyBanner').textContent = '';
  $('readOnlyBanner').classList.add('hidden');
  $('message').value = '';
  clearReplyTarget();
  text($('ticketAvatar'), '—');
  showError('');
  updateQueueSummary();
  setComposerEnabled(false);
  closeContextPanel(false);
  setMobileTicketOpen(false);
  saveSupportSelection();
}

function showLogin() {
  state.dashboardReadBoundaries.clear();
  state.chatNotificationIds.clear();
  resetQueueFilters();

  $('sessionLoading').classList.add('hidden');
  if (state.imageEditor) { state.imageEditor.dispose(); state.imageEditor = null; }
  $('mediaViewerModal').classList.add('hidden');
  $('imageEditorToolbar').classList.add('hidden');
  $('messageDeleteModal').classList.add('hidden');
  state.deleteTarget = null;
  clearSupportSelections();
  state.authenticated = false;
  stopSupportActivity();
  clearAgentStatistics();
  $('agentStatisticsBtn').classList.add('hidden');
  disconnectSupportEvents();
  saveLinkedDeviceAddition(null);
  state.agent = null;
  state.staffItems = [];
  state.editingStaffId = null;
  state.composerEnabled = false;
  state.queueRequest += 1;
  state.ticketRequest += 1;
  if (state.mediaRecorder) cancelVoiceRecording();
  clearMediaDraft();
  clearTicketWorkspace();$('dashboard').classList.add('hidden');
  state.page = 1;
  state.query = '';
  state.globalSearchResults = [];
  state.globalSearchRequest += 1;
  $('queueSearch').value = '';
  setQueueView('unassigned', {load: false, persist: false});
  $('loginPage').classList.remove('hidden');
  closeDialog('messagePreferencesModal', false);
  closeDialog('quickRepliesModal', false);
  resetQuickReplies();
  closeDialog('contactEditModal', false);
  state.contactEditTarget = null;
  closeDialog('aiSettingsModal', false);
  closeDialog('whatsappModal', false);
  closeDialog('staffModal', false);
  closeDialog('transferModal', false);
  closeDialog('startChatModal', false);
  closeDialog('forwardModal', false);
  closeDialog('shortcutsModal', false);
  if (typeof settleConfirmDialog === 'function') settleConfirmDialog(false);
  text($('aiHeaderStatus'), '—');
  $('aiHeaderDot').className = 'ai-status-dot';
  $('aiHeaderBtn').setAttribute('title', 'AI assistant status');
  $('manageStaffBtn').classList.add('hidden');
  $('manageWhatsAppBtn').classList.add('hidden');
  state.aiSettings = null;
  state.whatsappRegistry = null;
  state.linkedDeviceRequest++;
  state.linkedDeviceStatus = null;
  state.linkedDeviceAdditionId = null;
  $('linkedDeviceLines').replaceChildren();
  $('linkedDeviceSection').classList.add('hidden');
  state.unassignedSnapshot = null;
  state.lastInboundByTicket = {};
  $('password').value = '';
  $('password').type = 'password';
  $('passwordToggleBtn').setAttribute('aria-pressed', 'false');
  $('passwordToggleBtn').setAttribute('aria-label', 'Show password');
  $('passwordToggleBtn').setAttribute('data-tooltip', 'Show password');
  showLoginError('');
  window.setTimeout(() => $('email').focus(), 20);
}

function showDashboard() {
  state.authenticated = true;
  $('sessionLoading').classList.add('hidden');
  $('loginPage').classList.add('hidden');
  $('dashboard').classList.remove('hidden');
  configureStaffAccess();
  startSupportActivity();
  connectSupportEvents();
}

async function restoreSupportWorkspace() {
  const saved = readSupportSelection();
  if (saved) {
    setQueueView(saved.view, {load: false, persist: false});
    state.page = saved.page;
  }
  await loadQueue();
  if (saved?.ticketId) {
    await openTicket(saved.ticketId, {
      silent: true, conversationOrigin: saved.conversationOrigin, restoring: true
    });
  } else saveSupportSelection();
}

async function login(event) {
  event.preventDefault();
  unlockNotificationAudio();
  const form = $('loginForm');
  if (state.busy || !form.reportValidity()) return;
  state.busy = true;
  setButtonBusy($('loginBtn'), true, 'Signing in…');
  showLoginError('');
  try {
    state.agent = await api('/api/support/login', {method:'POST', body: JSON.stringify({email: $('email').value.trim(), password: $('password').value})});
    setAgentIdentity(state.agent);
    showDashboard();
    try { await loadAiSettings(); } catch (error) { renderAiSettingsUnavailable(error.message); }
    await restoreSupportWorkspace();
    await refreshAllQueueCounts();
    setAppStatus('Signed in to Shipment Support.');
  } catch (error) {
    showLoginError(error.message);
    $('password').select();
  } finally {
    state.busy = false;
    setButtonBusy($('loginBtn'), false);
  }
}

async function bootstrap() {
  syncThemeControls();
  text($('sessionLoadingStatus'), 'Checking your session…');
  $('sessionRetryBtn').classList.add('hidden');
  try {
    state.agent = await api('/api/support/me');
  } catch (error) {
    if (state.pageLeaving || error.status === 401) return;
    text($('sessionLoadingStatus'), 'Unable to check your session. Check your connection and try again.');
    $('sessionRetryBtn').classList.remove('hidden');
    return;
  }
  setAgentIdentity(state.agent);
  showDashboard();
  try {
    try { await loadAiSettings(); } catch (error) { renderAiSettingsUnavailable(error.message); }
    await restoreSupportWorkspace();
    await refreshAllQueueCounts();
  } catch (error) {
    if (state.authenticated && !state.pageLeaving) showError(error.message);
  }
}

async function logout() {
  if (state.busy) return;
  state.busy = true;
  const button = $('logoutBtn');
  button.disabled = true;
  try {
    await api('/api/support/logout', {method:'POST'});
  } catch (_) {
  } finally {
    state.busy = false;
    button.disabled = false;
    showLogin();
  }
}

function updateQueueCount(view, total) {
  state.queueTotals[view] = Number(total || 0);
  const badge = document.querySelector('[data-count-for="' + view + '"]');
  if (badge) {
    badge.textContent = String(state.queueTotals[view]);
    badge.toggleAttribute('data-zero', state.queueTotals[view] === 0);
  }
}

async function refreshAllQueueCounts() {
  if (!state.authenticated) return;
  if (state.queueCountsBusy) { state.queueCountsPending = true; return; }
  state.queueCountsBusy = true;
  try {
    const data = await api('/api/support/queue-counts');
    for (const [view, total] of Object.entries(data.counts || {})) updateQueueCount(view, total);
  } catch (_) {
    // Individual queue loading remains authoritative; count refresh is best-effort.
  } finally {
    state.queueCountsBusy = false;
    if (state.queueCountsPending) {
      state.queueCountsPending = false;
      window.setTimeout(() => refreshAllQueueCounts(), 100);
    }
  }
}

function updateQueueSummary(visibleCount = null) {
  const config = QUEUE_CONFIG[state.view] || QUEUE_CONFIG.unassigned;
  text($('queueTitle'), config.label);
  const total = Number(state.total || 0);
  const shown = Math.min(state.queueItems.length, total);
  const searchCount = total;
  if (state.query) {
    text($('queueTotal'), searchCount + ' match' + (searchCount === 1 ? '' : 'es'));
  } else {
    const noun = state.view === 'chats' ? 'chat' : state.view === 'contacts' ? 'contact' : 'ticket';
    text($('queueTotal'), total + ' ' + noun + (total === 1 ? '' : 's'));
  }
  text($('pageSummary'), !total ? '' : shown < total ? ('Showing ' + shown + ' of ' + total + ' · scroll for more') : ('Showing all ' + total));
}

function queueSearchText(ticket) {
  return [
    ticket.ticket_reference,
    ticket.customer_name,
    ticket.customer_userid,
    ticket.whatsapp_phone,
    ticket.ai_summary,
    ticket.reason,
    ticket.status,
    ticket.tracking_reference,
    ticket.requested_action,
    ticket.current_assignee_name,
    ticket.display_name,
    ticket.business_display_phone_number,
    ticket.business_phone_number_id
  ].filter(Boolean).join(' ').toLocaleLowerCase();
}

function supportNumberAlias(item) {
  if (!item) return '';
  const direct = String(item.display_name || '').trim();
  if (direct) return direct;
  const id = item.business_phone_number_id || item.phone_number_id || item.line_id;
  const registered = (state.whatsappRegistry?.numbers || []).find(number => String(number.phone_number_id) === String(id));
  return String(registered?.display_name || '').trim();
}

function supportNumberRaw(item) {
  if (item && (item.business_display_phone_number || item.display_phone_number)) {
    return String(item.business_display_phone_number || item.display_phone_number);
  }
  const id = item?.business_phone_number_id || item?.phone_number_id || item?.line_id;
  if (id) return 'WhatsApp number ID ' + id;
  return 'Default WhatsApp number';
}

function supportNumberLabel(item) {
  return supportNumberAlias(item) || supportNumberRaw(item);
}

function createSupportNumberLabel(item, {focusable = true} = {}) {
  const alias = supportNumberAlias(item);
  const raw = supportNumberRaw(item);
  const label = document.createElement('span');
  label.className = 'support-number-label' + (alias && alias !== raw ? ' has-alias' : '');
  label.title = raw;
  if (alias && alias !== raw) {
    const name = document.createElement('span');
    name.className = 'support-number-alias';
    name.textContent = alias;
    const number = document.createElement('span');
    number.className = 'support-number-raw';
    number.textContent = raw;
    label.append(name, number);
    label.setAttribute('aria-label', alias + ', WhatsApp number ' + raw);
    if (focusable) {
      label.tabIndex = 0;
      label.onpointerdown = event => { if (event.pointerType === 'touch') label.focus(); };
    }
  } else {
    label.textContent = raw;
  }
  return label;
}

function isStaffChat(ticket) {
  return String(ticket?.conversation_origin || '').toUpperCase() === 'STAFF';
}

function conversationStatusBadge(ticket) {
  if (!isStaffChat(ticket)) return statusBadge(ticket?.status);
  const badge = document.createElement('span');
  const open = Boolean(ticket?.active_key);
  badge.className = 'status-badge ' + (open ? 'status-assigned' : 'status-resolved');
  badge.textContent = open ? 'Open' : 'Closed';
  return badge;
}

function createMetaChip(label, iconName, className = 'meta-chip') {
  const chip = document.createElement('span');
  chip.className = className;
  if (iconName) chip.append(createIcon(iconName, 'icon icon-sm'));
  const copy = document.createElement('span');
  if (typeof label === 'string') copy.textContent = label;
  else copy.append(label);
  chip.append(copy);
  chip.title = typeof label === 'string' ? label : label.title;
  return chip;
}

function createContactRow(contact) {
  const item = document.createElement('div');
  item.className = 'contact-item';
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'contact-row';
  row.setAttribute('role', 'option');
  const avatar = document.createElement('div');
  avatar.className = 'avatar customer';
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = initials(contact.display_name || contact.normalized_phone || 'Contact');
  const copy = document.createElement('div');
  copy.className = 'contact-row-copy';
  const name = document.createElement('div');
  name.className = 'contact-row-name';
  name.dir = 'auto';
  name.textContent = contact.display_name || contact.normalized_phone || 'Unnamed contact';
  const phone = document.createElement('div');
  phone.className = 'contact-row-phone';
  // A number stored without a country code (common in Google/VCF imports) is not
  // shown with a '+', so it does not look like a valid international number.
  const phoneDigits = String(contact.normalized_phone || '').replace(/^\+/, '');
  phone.textContent = phoneDigits.startsWith('0') ? phoneDigits : '+' + phoneDigits;
  const meta = document.createElement('div');
  meta.className = 'contact-row-meta';
  const source = document.createElement('span');
  source.className = 'contact-source' + (contact.source === 'google' ? ' google' : contact.source === 'vcf' ? ' vcf' : '');
  source.textContent = contact.source === 'google' ? 'Google' : contact.source === 'vcf' ? 'VCF' : 'Saved';
  meta.append(source);
  if (contact.email) meta.append(document.createTextNode(contact.email));
  copy.append(name, phone, meta);
  const chat = document.createElement('span');
  chat.className = 'contact-chat-chip';
  chat.setAttribute('aria-hidden', 'true');
  chat.append(createIcon('message', 'icon icon-sm'), document.createTextNode('Chat'));
  row.setAttribute('aria-label', 'Start chat with ' + name.textContent);
  row.setAttribute('aria-keyshortcuts', 'E');
  row.tabIndex = -1;
  row.append(avatar, copy, chat);
  row.onclick = () => openStartChatModal(contact);
  row.contact = contact;
  // Every contact can be edited: saved, Google, and VCF. Keyboard users press E on a row.
  const edit = document.createElement('button');
  edit.type = 'button';
  edit.className = 'icon-btn contact-edit-btn';
  edit.tabIndex = -1;
  edit.setAttribute('aria-label', 'Edit contact ' + name.textContent);
  edit.dataset.tooltip = 'Edit contact';
  edit.append(createIcon('edit', 'icon icon-sm'));
  edit.onclick = event => { event.stopPropagation(); openContactEditModal(contact); };
  item.append(row, edit);
  return item;
}

function createTicketRow(ticket) {
  const selected = String(state.ticketId || '') === String(ticket.id || '');
  const row = document.createElement('button');
  row.type = 'button';
  row.className = 'ticket-row' + (selected ? ' active' : '');
  row.setAttribute('role', 'option');
  row.setAttribute('aria-selected', selected ? 'true' : 'false');
  row.tabIndex = selected ? 0 : -1;
  row.dataset.ticketId = String(ticket.id);

  const head = document.createElement('div');
  head.className = 'ticket-row-head';
  const avatar = document.createElement('div');
  avatar.className = 'avatar customer';
  avatar.setAttribute('aria-hidden', 'true');
  const customer = ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'Unknown customer';
  avatar.textContent = initials(customer);

  const copy = document.createElement('div');
  copy.className = 'ticket-row-copy';
  const name = document.createElement('div');
  name.className = 'ticket-customer';
  name.dir = 'auto';
  name.textContent = customer;
  if (['chats', 'mine'].includes(state.view) && Number(ticket.dashboard_unread_count) > 0) {
    const dot = document.createElement('span');
    dot.className = 'unread-dot'; dot.setAttribute('role', 'img');
    dot.setAttribute('aria-label', 'Unread messages'); dot.title = 'Unread messages';
    name.append(dot);
  }
  const reference = document.createElement('div');
  reference.className = 'ticket-reference';
  reference.textContent = isStaffChat(ticket) ? ('Chat · ' + (ticket.whatsapp_phone || ticket.id)) : (ticket.ticket_reference || ('Ticket #' + ticket.id));
  copy.append(name, reference);

  const time = document.createElement('time');
  time.className = 'ticket-time';
  time.dateTime = ticket.last_activity_at || '';
  time.title = fmt(ticket.last_activity_at);
  time.textContent = relativeTime(ticket.last_activity_at);
  const waitedMinutes = (Date.now() - parseServerDate(ticket.last_activity_at || '').getTime()) / 60000;
  if (WAITING_AGE_VIEWS.includes(state.view) && Number.isFinite(waitedMinutes) && waitedMinutes >= WAITING_AGE_WARN_MINUTES) {
    const overdue = waitedMinutes >= WAITING_AGE_ALERT_MINUTES;
    time.classList.add(overdue ? 'age-alert' : 'age-warn');
    if (overdue) time.prepend(createIcon('alert', 'icon icon-sm'));
    time.title = 'Waiting ' + Math.floor(waitedMinutes) + ' min · last activity ' + fmt(ticket.last_activity_at);
    time.setAttribute('aria-label', 'Waiting ' + Math.floor(waitedMinutes) + ' minutes');
  }
  head.append(avatar, copy, time);

  const summary = document.createElement('div');
  summary.className = 'ticket-summary';
  summary.dir = 'auto';
  summary.textContent = isStaffChat(ticket) ? ('WhatsApp conversation' + (ticket.current_assignee_name ? ' · ' + ticket.current_assignee_name : '')) : (ticket.ai_summary || ticket.reason || ticket.requested_action || 'No summary available.');

  if (ticket.search_message_id) {
    summary.textContent = ticket.search_preview || 'Matching message';
    highlightSearchTerm(summary, state.query);
  }

  const meta = document.createElement('div');
  meta.className = 'ticket-row-meta';
  meta.append(conversationStatusBadge(ticket));
  const unread = ['chats', 'mine'].includes(state.view) ? ticket.dashboard_unread_count : ticket.unread_count;
  if (unread) meta.append(createMetaChip(unread + ' unread', 'message'));
  if (ticket.awaiting_staff_reply) meta.append(createMetaChip('Awaiting staff reply', 'clock'));
  meta.append(createMetaChip(createSupportNumberLabel(ticket, {focusable: false}), 'message', 'channel-pill'));
  if (ticket.current_assignee_name) meta.append(createMetaChip(ticket.current_assignee_name, 'user'));
  else if (ticket.phone_app_replied && ticket.status === 'NEW') {
    const phoneChip = createMetaChip('Answered in WhatsApp app', 'phone');
    phoneChip.title = 'Answered in WhatsApp app. Resolves automatically after 2 hours with no messages.';
    meta.append(phoneChip);
  }
  if (ticket.tracking_reference) meta.append(createMetaChip(ticket.tracking_reference, 'truck'));

  row.append(head, summary, meta);
  row.onclick = () => ticket.search_message_id
    ? openSearchResult({ticket_id: ticket.id, message_id: ticket.search_message_id, conversation_origin: ticket.conversation_origin}, state.query)
    : openTicket(ticket.id, {conversationOrigin: ticket.conversation_origin});
  // Start loading as soon as the pointer rests on (or presses) a row, so the
  // conversation is usually ready by the time the click lands.
  let hoverTimer = null;
  row.addEventListener('pointerenter', event => {
    if (event.pointerType !== 'mouse') return;
    hoverTimer = setTimeout(() => prefetchTicketDetail(ticket.id, ticket.conversation_origin), TICKET_HOVER_PREFETCH_DELAY_MS);
  });
  row.addEventListener('pointerleave', () => { clearTimeout(hoverTimer); hoverTimer = null; });
  row.addEventListener('pointerdown', () => prefetchTicketDetail(ticket.id, ticket.conversation_origin));
  return row;
}

function refreshMessageSearchAfterChange(messageId) {
  state.globalSearchResults = state.globalSearchResults.filter(item => Number(item.message_id) !== Number(messageId));
  state.conversationSearchResults = state.conversationSearchResults.filter(item => Number(item.message_id) !== Number(messageId));
  if (state.query.trim()) loadQueue({silent: true}).catch(() => {});
  if (state.conversationSearchOpen && state.conversationSearchQuery.length >= 2) searchCurrentConversation({preserveQuery: true}).catch(() => {});
}

async function openSearchResult(item, query) {
  if (!item || !item.ticket_id) return;
  if (item.result_type === 'conversation') {
    closeConversationSearch(false);
    await openTicket(Number(item.ticket_id), {silent: false, conversationOrigin: item.conversation_origin});
    return;
  }
  if (!item.message_id) return;
  state.conversationSearchOpen = true;
  state.conversationSearchQuery = query || '';
  $('conversationSearchInput').value = state.conversationSearchQuery;
  $('conversationSearchToolbar').classList.remove('hidden');
  await openTicket(Number(item.ticket_id), {silent: false, aroundMessageId: Number(item.message_id), preserveSearch: true, conversationOrigin: item.conversation_origin});
  await searchCurrentConversation({focusMessageId: Number(item.message_id), preserveQuery: true});
}

// Only one queue row is a Tab stop; arrows, Home/End, and J/K move between rows.
function syncQueueRovingFocus(restoreFocus = false) {
  const rows = [...$('queue').querySelectorAll('.ticket-row, .contact-row, .search-result-row')];
  if (!rows.length) return;
  const current = rows.find(row => row.classList.contains('active')) || rows[0];
  for (const row of rows) row.tabIndex = row === current ? 0 : -1;
  if (restoreFocus && !$('queue').contains(document.activeElement)) current.focus({preventScroll: true});
}

function renderQueue() {
  const queue = $('queue');
  const queueHadFocus = queue.contains(document.activeElement);
  const rawQuery = state.query.trim();
  const items = state.queueItems;
  queue.replaceChildren();

  if (state.view === 'contacts') {
    if (!state.queueItems.length) {
      const config = QUEUE_CONFIG.contacts;
      queue.append(emptyMarkup(config.emptyTitle, rawQuery ? 'No contact matches this search.' : config.emptyBody, config.icon).outer);
    } else {
      const fragment = document.createDocumentFragment();
      for (const contact of state.queueItems) fragment.append(createContactRow(contact));
      queue.append(fragment);
      appendQueueMoreSentinel(queue);
    }
    updateQueueSummary(state.queueItems.length);
    $('clearQueueSearchBtn').classList.toggle('hidden', !state.query);
    syncQueueRovingFocus(queueHadFocus);
    return;
  }

  if (!state.queueItems.length) {
    const config = QUEUE_CONFIG[state.view] || QUEUE_CONFIG.unassigned;
    queue.append(emptyMarkup(config.emptyTitle, config.emptyBody, config.icon).outer);
  } else if (!items.length) {
    const empty = emptyMarkup(state.view === 'chats' ? 'No chats match this search' : 'No tickets match this search', state.view === 'chats' ? 'Try a customer name, phone number, owner, or support line.' : 'Try a customer name, ticket reference, shipment number, status, or support line.', 'search');
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'btn';
    clear.append(createIcon('x'), document.createTextNode('Clear search'));
    clear.onclick = clearQueueSearch;
    empty.inner.append(clear);
    queue.append(empty.outer);
  } else {
    const fragment = document.createDocumentFragment();
    for (const ticket of items) fragment.append(createTicketRow(ticket));
    queue.append(fragment);
    appendQueueMoreSentinel(queue);
  }

  updateQueueSummary(items.length);
  $('clearQueueSearchBtn').classList.toggle('hidden', !state.query);
  syncQueueRovingFocus(queueHadFocus);
}

function renderQueueError(message) {
  const empty = emptyMarkup('Could not load this queue', message || 'Check your connection and try again.', 'alert');
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'btn primary';
  retry.append(createIcon('refresh'), document.createTextNode('Retry'));
  retry.onclick = () => loadQueue();
  empty.inner.append(retry);
  $('queue').replaceChildren(empty.outer);
}

function renderQueueFilters(options = null) {
  const filters = state.queueFilters;
  const supported = ['unassigned', 'mine', 'chats', 'active', 'history', 'waiting'].includes(state.view);
  $('queueFilters').classList.toggle('hidden', !supported);
  $('queueStaffLabel').classList.toggle('hidden', !['chats', 'active', 'history', 'waiting'].includes(state.view));
  $('queueStatusLabel').classList.toggle('hidden', state.view !== 'chats');
  for (const [id, key, items, label] of [
    ['queueSupportNumber', 'supportNumber', options?.support_numbers, 'All support numbers'],
    ['queueStaff', 'staffId', options?.staff, 'All staff members']
  ]) {
    const select = $(id);
    if (items) {
      const choices = [new Option(label, ''), ...items.map(item => new Option(item.name || item.id, item.id))];
      if (filters[key] && !items.some(item => String(item.id) === filters[key])) choices.push(new Option(select.selectedOptions[0]?.textContent || filters[key], filters[key]));
      select.replaceChildren(...choices);
    }
    select.value = filters[key];
  }
  $('queueStatus').value = filters.status;
}

function resetQueueFilters() {
  state.queueFilters = {supportNumber: '', staffId: '', status: 'all'};
  renderQueueFilters();
}

function queueCriteriaChanged() {
  state.page = 1;
  state.queueRequest += 1; // Invalidate an in-flight page before debounce fires.
  state.queueExhausted = false;
  if (state.globalSearchTimer) clearTimeout(state.globalSearchTimer);
  state.globalSearchTimer = setTimeout(() => loadQueue({silent: true}), 220);
}

function queuePath(page, pageSize) {
  if (state.view === 'contacts') return '/api/support/contacts?page=' + page + '&page_size=' + pageSize + '&q=' + encodeURIComponent(state.query.trim());
  const params = new URLSearchParams({page, page_size: pageSize, q: state.query.trim()});
  const filters = state.queueFilters || {};
  if (filters.supportNumber) params.set('support_number', filters.supportNumber);
  if (filters.staffId && ['chats', 'active', 'history', 'waiting'].includes(state.view)) params.set('staff_id', filters.staffId);
  if (state.view === 'chats') params.set('chat_status', filters.status || 'all');
  else params.set('view', state.view);
  return (state.view === 'chats' ? '/api/support/chats?' : '/api/support/tickets?') + params;
}

function uniqueQueueItems(items) {
  const seen = new Set();
  return items.filter(item => {
    const key = state.view === 'contacts' ? String(item?.contact_key || '') : String(item?.id || '');
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function queueHasMore() {
  if (state.queueExhausted) return false;
  return state.queueItems.length < Number(state.total || 0);
}

function appendQueueMoreSentinel(queue) {
  if (!queueHasMore()) return;
  const more = document.createElement('div');
  more.className = 'queue-more';
  more.setAttribute('role', 'presentation');
  more.textContent = 'Loading more…';
  queue.append(more);
  if (typeof IntersectionObserver === 'undefined') return;
  if (!state.queueMoreObserver) {
    state.queueMoreObserver = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) loadMoreQueue();
    }, {root: queue, rootMargin: '0px 0px 320px 0px'});
  }
  state.queueMoreObserver.disconnect();
  state.queueMoreObserver.observe(more);
}

async function loadMoreQueue() {
  const queue = $('queue');
  if (!state.authenticated || state.queueLoadingMore || queue.getAttribute('aria-busy') === 'true' || !queueHasMore()) return;
  const requestId = state.queueRequest;
  const view = state.view;
  state.queueLoadingMore = true;
  try {
    const nextPage = Math.floor(state.queueItems.length / state.pageSize) + 1;
    const data = await api(queuePath(nextPage, state.pageSize));
    if (requestId !== state.queueRequest || view !== state.view) return;
    const before = state.queueItems.length;
    state.queueItems = uniqueQueueItems([...state.queueItems, ...(Array.isArray(data.items) ? data.items : [])]);
    state.total = Number(data.total || 0);
    // Rows that moved while scrolling can make a page add nothing new; stop
    // instead of requesting the same page forever. A refresh starts over.
    if (state.queueItems.length === before) state.queueExhausted = true;
    state.page = Math.max(1, Math.ceil(state.queueItems.length / state.pageSize));
    saveSupportSelection();
    refreshAllQueueCounts().catch(() => {});
    const scrollTop = queue.scrollTop;
    renderQueue();
    queue.scrollTop = scrollTop;
  } catch (error) {
    if (requestId === state.queueRequest && !state.pageLeaving) {
      state.queueExhausted = true;
      renderQueue();
      notify(error.message || 'Could not load more conversations.', 'error');
    }
  } finally {
    state.queueLoadingMore = false;
  }
}

async function loadQueue({silent = false} = {}) {
  if (!state.authenticated) return;
  const requestId = ++state.queueRequest;
  const queue = $('queue');
  const previousScrollTop = queue.scrollTop;
  queue.setAttribute('aria-busy', 'true');
  $('refreshQueueBtn').disabled = true;
  if (!silent) renderQueueSkeleton();
  try {
    // Reload every row already scrolled into view, so a refresh never
    // shortens the list under the reader.
    const wanted = Math.max(1, state.page) * state.pageSize;
    const chunk = Math.min(wanted, QUEUE_MAX_REQUEST_SIZE);
    const responses = await Promise.all(
      Array.from({length: Math.ceil(wanted / chunk)}, (_, index) => api(queuePath(index + 1, chunk)))
    );
    if (requestId !== state.queueRequest) return;
    const total = Number(responses[0].total || 0);
    state.queueItems = uniqueQueueItems(responses.flatMap(data => Array.isArray(data.items) ? data.items : [])).slice(0, wanted);
    state.page = Math.max(1, Math.ceil(state.queueItems.length / state.pageSize));
    state.queueExhausted = false;
    state.total = total;
    renderQueueFilters(responses[0].filter_options);
    refreshAllQueueCounts().catch(() => {});
    if (state.view === 'unassigned' && !(state.unassignedSnapshot instanceof Map)) {
      state.unassignedSnapshot = new Map(
        state.queueItems.map(item => [String(item.id), String(item.last_activity_at || '')])
      );
    }
    renderQueue();
    if (silent) queue.scrollTop = previousScrollTop;
  } catch (error) {
    if (requestId !== state.queueRequest) return;
    if (state.pageLeaving) return;
    renderQueueError(error.message);
    text($('queueTotal'), 'Unavailable');
  } finally {
    if (requestId === state.queueRequest) {
      queue.setAttribute('aria-busy', 'false');
      $('refreshQueueBtn').disabled = false;
    }
  }
}

async function pollUnassignedActivity() {
  if (!state.authenticated) return;
  try {
    const data = await api('/api/support/tickets?view=unassigned&page=1&page_size=30');
    const items = Array.isArray(data.items) ? data.items : [];
    updateQueueCount('unassigned', Number(data.total || 0));
    const snapshot = new Map(items.map(item => [String(item.id), String(item.last_activity_at || '')]));
    if (state.unassignedSnapshot instanceof Map) {
      let newCount = 0;
      let updatedCount = 0;
      for (const [id, activity] of snapshot.entries()) {
        if (!state.unassignedSnapshot.has(id)) newCount += 1;
        else if (state.unassignedSnapshot.get(id) !== activity) updatedCount += 1;
      }
      if (newCount || updatedCount) {
        const message = newCount
          ? (newCount === 1 ? 'New unassigned customer conversation.' : newCount + ' new unassigned customer conversations.')
          : 'New customer message in the Unassigned queue.';
        notifyNewCustomerActivity(message);
        if (state.view === 'unassigned') loadQueue({silent: true}).catch(() => {});
      }
    }
    state.unassignedSnapshot = snapshot;
  } catch (_) {
    // The normal queue error handling remains authoritative; notification polling is best-effort.
  }
}

function setQueueView(view, {load = true, persist = true} = {}) {
  if (view === 'active' && !canViewActiveQueue()) view = 'unassigned';
  if (!Object.prototype.hasOwnProperty.call(QUEUE_CONFIG, view) || state.view === view) return;
  state.view = view;
  resetQueueFilters();
  state.page = 1;
  state.query = '';
  state.globalSearchResults = [];
  state.globalSearchRequest += 1;
  $('queueSearch').value = '';
  for (const button of $('tabs').querySelectorAll('button[data-view]')) {
    const active = button.dataset.view === view;
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'page' : 'false');
  }
  const contacts = view === 'contacts';
  $('contactsToolbar').classList.toggle('hidden', !contacts);
  $('queueSearch').placeholder = contacts ? 'Search contacts' : 'Search conversations and messages';
  text($('searchScope'), contacts ? 'Search saved, Google-synced, and VCF-imported contacts by name, phone, or email.' : 'Searches customers, tickets and message text within this queue and the selected filters.');
  if (contacts) loadGoogleContactsStatus().catch(() => {});
  updateQueueSummary();
  if (persist) saveSupportSelection();
  if (load) loadQueue();
}

function clearQueueSearch() {
  state.query = '';
  state.globalSearchResults = [];
  state.globalSearchRequest += 1;
  if (state.globalSearchTimer) { clearTimeout(state.globalSearchTimer); state.globalSearchTimer = null; }
  $('queueSearch').value = '';
  text($('searchScope'), state.view === 'contacts' ? 'Search saved, Google-synced, and VCF-imported contacts by name, phone, or email.' : 'Searches customers, tickets and message text within this queue and the selected filters.');
  queueCriteriaChanged();
  $('queueSearch').focus();
}

function handleQueueKeydown(event) {
  const current = event.target.closest('.ticket-row, .contact-row, .search-result-row');
  if (current?.contact && event.key.toLowerCase() === 'e' && !event.ctrlKey && !event.metaKey && !event.altKey) {
    event.preventDefault();
    openContactEditModal(current.contact);
    return;
  }
  if (!current || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  const rows = [...$('queue').querySelectorAll('.ticket-row, .contact-row, .search-result-row')];
  const index = rows.indexOf(current);
  let target = null;
  if (event.key === 'ArrowDown') target = rows[Math.min(rows.length - 1, index + 1)];
  if (event.key === 'ArrowUp') target = rows[Math.max(0, index - 1)];
  if (event.key === 'Home') target = rows[0];
  if (event.key === 'End') target = rows[rows.length - 1];
  if (target) {
    event.preventDefault();
    for (const row of rows) row.tabIndex = row === target ? 0 : -1;
    target.focus();
  }
}

function setMobileTicketOpen(open) {
  document.body.classList.toggle('mobile-ticket-open', Boolean(open));
}

function closeMobileTicket() {
  setMobileTicketOpen(false);
  window.setTimeout(() => {
    const selected = $('queue').querySelector('.ticket-row.active');
    (selected || $('queueSearch')).focus();
  }, 20);
}

function messageActor(message) {
  const type = String(message.sender_type || message.role || '').toUpperCase();
  if (message.sender_name) return message.sender_name;
  return MESSAGE_ROLE_LABELS[type] || (type ? formatStatus(type) : 'Message');
}

function messageRole(message) {
  const type = String(message.sender_type || message.role || '').toUpperCase();
  if (type === 'CUSTOMER') return 'Customer';
  if (type === 'AI') return 'AI assistant';
  if (type === 'AGENT') return 'Agent reply';
  return type ? formatStatus(type) : 'Message';
}

function messageClass(message) {
  const type = String(message.sender_type || message.role || '').toUpperCase();
  if (type === 'CUSTOMER') return 'customer';
  if (type === 'AI') return 'ai';
  if (type === 'AGENT') return 'agent';
  return 'system';
}

function messagePreview(message) {
  if (message.deleted_for_everyone_at) return 'Original message deleted';
  const kind = String(message.media_kind || message.message_type || '').toLowerCase();
  if (kind === 'image') return 'Image';
  if (kind === 'video') return 'Video';
  if (kind === 'sticker') return 'Sticker';
  if (kind === 'audio') return 'Voice note';
  if (kind === 'document') return message.media_filename || 'PDF document';
  if (kind === 'location') return message.location_name || message.location_address || 'Location';
  const body = String(message.body || message.content || '').trim();
  return body || 'Message';
}

function messageTextForCopy(message) {
  if (!message || message.deleted_for_everyone_at) return '';
  const body = String(message.body || message.content || '');
  if (!body.trim()) return '';
  const kind = String(message.media_kind || message.message_type || '').toLowerCase();
  const defaultMediaLabel = ['Image', 'Video', 'Sticker', 'Voice note', 'Audio message', 'PDF document', 'Location'].includes(body);
  return (message.media_url || kind === 'location') && defaultMediaLabel ? '' : body;
}

async function copyMessageText(value, copiedLabel = 'Message copied.') {
  if (!value) return false;
  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        notify(copiedLabel);
        return true;
      } catch (_) {
        // Some browsers deny the Clipboard API while still allowing a user-initiated copy.
      }
    }
    const field = document.createElement('textarea');
    field.value = value;
    field.readOnly = true;
    field.style.position = 'fixed';
    field.style.left = '-9999px';
    document.body.append(field);
    try {
      field.select();
      if (!document.execCommand?.('copy')) throw new Error('Copy failed');
    } finally {
      field.remove();
    }
    notify(copiedLabel);
    return true;
  } catch (_) {
    notify('Could not copy the message. Try selecting its text.', 'error');
    return false;
  }
}

// The clipboard only accepts PNG images, so other formats are converted first.
async function imageBlobAsPng(blob) {
  if (blob.type === 'image/png') return blob;
  const bitmap = await createImageBitmap(blob);
  try {
    const canvas = document.createElement('canvas');
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext('2d').drawImage(bitmap, 0, 0);
    const png = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
    if (!png) throw new Error('Image conversion failed.');
    return png;
  } finally {
    bitmap.close?.();
  }
}

async function fetchImageBlob(src) {
  const response = await fetch(src, {credentials: 'same-origin', cache: 'no-store'});
  if (!response.ok) throw new Error('Image could not be loaded.');
  return response.blob();
}

// source is an image URL or a function returning a Promise<Blob>. The clipboard
// write starts inside the click with a pending item, so browsers keep the gesture.
async function copyImageToClipboard(source) {
  if (!source) return false;
  if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') {
    notify('This browser cannot copy images. Use Download instead.', 'error');
    return false;
  }
  try {
    const png = Promise.resolve(typeof source === 'function' ? source() : fetchImageBlob(source)).then(imageBlobAsPng);
    await navigator.clipboard.write([new ClipboardItem({'image/png': png})]);
    notify('Image copied.');
    return true;
  } catch (_) {
    notify('Could not copy the image. Try Download instead.', 'error');
    return false;
  }
}

function renderReplyDraft() {
  const container = $('replyDraft');
  const target = state.replyTarget;
  if (!target) {
    container.classList.add('hidden');
    text($('replyDraftTitle'), 'Replying to message');
    text($('replyDraftBody'), '');
    return;
  }
  text($('replyDraftTitle'), 'Replying to ' + target.actor);
  text($('replyDraftBody'), target.preview);
  container.classList.remove('hidden');
}

function clearReplyTarget() {
  state.replyTarget = null;
  renderReplyDraft();
}

function startReplyToMessage(message) {
  if (!state.composerEnabled || !message || !message.id || !message.whatsapp_message_id) return;
  state.replyTarget = {
    id: Number(message.id),
    actor: messageActor(message),
    preview: messagePreview(message)
  };
  renderReplyDraft();
  $('message').focus();
  setAppStatus('Replying to ' + state.replyTarget.actor + '.');
}

function canChangeWhatsAppMessage(message, action) {
  if (!message || message.deleted_for_everyone_at || !message.id) return false;
  if (String(message.sender_type || '').toUpperCase() !== 'AGENT' || String(message.sender_id) !== String(state.agent?.id)) return false;
  return action === 'edit' ? message.can_edit === true : message.can_delete_for_everyone === true;
}

function messageTicketId(message) {
  return Number(message?.ticket_id || state.ticketId);
}

function startMessageEdit(message) {
  if (!canChangeWhatsAppMessage(message, 'edit')) return;
  if (state.busy || state.pendingMedia || state.mediaRecorder || state.editTarget) return;
  state.editTarget = {
    id: Number(message.id), ticketId: messageTicketId(message), version: Number(message.version || 1),
    draft: $('message').value, reply: state.replyTarget
  };
  clearReplyTarget();
  $('message').value = message.body || '';
  $('editDraft').classList.remove('hidden');
  updateComposerCount(); resizeComposer(); updateComposerControls(); focusMessageComposer();
}

function cancelMessageEdit(restore = true) {
  const target = state.editTarget;
  if (!target) return;
  state.editTarget = null;
  $('editDraft').classList.add('hidden');
  $('message').value = restore ? target.draft : '';
  state.replyTarget = restore ? target.reply : null;
  renderReplyDraft(); updateComposerCount(); resizeComposer(); updateComposerControls();
}

function openMessageDelete(message) {
  const canDeleteEveryone = canChangeWhatsAppMessage(message, 'everyone');
  state.deleteTarget = {id: Number(message.id), ticketId: messageTicketId(message), canDeleteEveryone};
  $('deleteEveryoneBtn').classList.toggle('hidden', !canDeleteEveryone);
  text($('messageDeleteSubtitle'), canDeleteEveryone
    ? 'Delete for everyone removes the message content from your support view and the customer’s WhatsApp chat. WhatsApp will show a deleted-message placeholder.'
    : 'Delete for everyone is unavailable for this message on WhatsApp. Delete for me removes it from your support view; the customer’s copy remains unchanged.');
  openDialog('messageDeleteModal', 'deleteMeBtn');
}

function closeMessageDelete() {
  state.deleteTarget = null;
  closeDialog('messageDeleteModal');
}

async function deleteMessage(scope) {
  const target = state.deleteTarget;
  if (!target || state.busy || !state.ticketId) return;
  if (scope === 'everyone' && !target.canDeleteEveryone) return;
  const visibleTicketId = state.ticketId;
  state.busy = true;
  $('deleteMeBtn').disabled = true; $('deleteEveryoneBtn').disabled = true;
  try {
    const result = await api('/api/support/tickets/' + target.ticketId + '/messages/' + target.id + '/delete', {
      method: 'POST', body: JSON.stringify({scope})
    });
    changeCachedMessage(target.ticketId, target.id, result.message, scope === 'me');
    state.realtimeMessages.delete(String(target.ticketId) + ':' + target.id);
    if (state.editTarget?.id === target.id) cancelMessageEdit();
    refreshMessageSearchAfterChange(target.id);
    closeMessageDelete();
    if (String(state.ticketId) === String(visibleTicketId)) {
      await openTicket(visibleTicketId, {silent: true, refreshHistory: target.ticketId !== Number(visibleTicketId)});
    }
    notify(scope === 'me' ? 'Message removed from your support history.' : 'Message deleted for everyone on WhatsApp.');
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    $('deleteMeBtn').disabled = false; $('deleteEveryoneBtn').disabled = false;
  }
}

async function sendMessageReaction(message, selectedEmoji, menu = null) {
  if (!state.composerEnabled || state.busy || !state.ticketId || !message?.id || !message.whatsapp_message_id) return;
  const ownReaction = (Array.isArray(message.reactions) ? message.reactions : []).find(item => String(item.reactor_type || '').toUpperCase() === 'AGENT');
  const emoji = ownReaction && ownReaction.emoji === selectedEmoji ? '' : selectedEmoji;
  if (menu) menu.open = false;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  let succeeded = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + '/messages/' + message.id + '/reaction', {
      method: 'POST',
      body: JSON.stringify({emoji, client_reaction_id: crypto.randomUUID()})
    });
    notify(emoji ? 'Reaction sent.' : 'Reaction removed.');
    succeeded = true;
    await openTicket(state.ticketId, {silent: true, refreshQueue: true});
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
  if (succeeded) focusMessageComposer();
}

function messageReactionBar(message) {
  const reactions = Array.isArray(message.reactions) ? message.reactions.filter(item => item && item.emoji) : [];
  if (!reactions.length) return null;
  const grouped = new Map();
  for (const reaction of reactions) {
    const emoji = String(reaction.emoji || '');
    if (!grouped.has(emoji)) grouped.set(emoji, []);
    grouped.get(emoji).push(reaction);
  }
  const bar = document.createElement('div');
  bar.className = 'message-reactions';
  bar.setAttribute('aria-label', 'Message reactions');
  for (const [emoji, items] of grouped.entries()) {
    const chip = document.createElement('div');
    chip.className = 'reaction-chip';
    const names = items.map(item => item.reactor_name || (String(item.reactor_type || '').toUpperCase() === 'CUSTOMER' ? 'Customer' : 'Support agent'));
    chip.title = names.join(', ');
    chip.append(document.createTextNode(emoji));
    if (items.length > 1) {
      const count = document.createElement('span');
      count.textContent = String(items.length);
      chip.append(count);
    }
    bar.append(chip);
  }
  return bar;
}

// Quote navigation leaves the selected ticket, composer, and media drafts intact.
let replyNavigationRequest = 0;
let replyHighlightTimer = null;
function highlightReplyMessage(messageId, conversation = $('conversation')) {
  const target = conversation.querySelector('[data-message-id="' + Number(messageId) + '"]');
  if (!target) return false;
  document.querySelectorAll('.reply-target').forEach(row => row.classList.remove('reply-target'));
  clearTimeout(replyHighlightTimer);
  // Restart the animation when the same quote is clicked again.
  void target.offsetWidth;
  target.classList.add('reply-target');
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const center = target.getBoundingClientRect().top - conversation.getBoundingClientRect().top +
    conversation.scrollTop - (conversation.clientHeight - target.clientHeight) / 2;
  conversation.scrollTo({top: Math.max(0, center), behavior: reduced ? 'auto' : 'smooth'});
  replyHighlightTimer = setTimeout(() => target.classList.remove('reply-target'), 2400);
  if (conversation.id === 'conversation') updateScrollLatestButton();
  return true;
}

async function navigateToReply(reply, button) {
  const request = ++replyNavigationRequest;
  const review = button.closest('#investigationMessages');
  const container = review || $('conversation');
  if (reply.id && highlightReplyMessage(reply.id, container)) return;
  if (!reply.source_message_id || (!review && !state.currentTicketData)) {
    notify('Original message is unavailable.', 'error');
    return;
  }
  const ticketId = review ? investigationTicket : state.ticketId;
  const epoch = review ? investigationRequest : state.historySelectionEpoch;
  const isCurrent = () => request === replyNavigationRequest && state.authenticated && (review
    ? investigationStillAllowed(epoch) && String(investigationTicket) === String(ticketId)
    : String(state.ticketId) === String(ticketId) && state.historySelectionEpoch === epoch);
  const base = review ? '/api/support/tickets/' + ticketId : historyDetailBase();
  button.dataset.replyRequest = String(request);
  button.setAttribute('aria-busy', 'true');
  button.classList.add('reply-loading');
  try {
    const result = await api(base + '/messages/' + Number(reply.source_message_id) + '/reply');
    if (!isCurrent()) return;
    const targetTicketId = Number(result.ticket?.id);
    const targetId = Number(result.message_id);
    const previous = targetTicketId !== Number(ticketId);
    let messages = result.messages || [];
    let hasMore = Boolean(result.messages_has_more);
    // Walk backward with the existing keyset pages. Keeping every intervening
    // message avoids timeline gaps and keeps Load older messages reliable.
    while (!messages.some(message => Number(message.id) === targetId) && hasMore) {
      const oldest = Number(messages[0]?.id);
      if (!oldest || oldest <= targetId) break;
      const page = await api(base + (previous ? '/history/' + targetTicketId : '') +
        '/messages?before_message_id=' + oldest + '&limit=100');
      if (!isCurrent()) return;
      const older = (page.messages || []).filter(message => Number(message.id) < oldest);
      if (!older.length) break;
      messages = mergeTimelineMessages(older, messages);
      hasMore = Boolean(page.messages_has_more);
    }
    if (!isCurrent()) return;
    if (!messages.some(message => Number(message.id) === targetId)) {
      throw new Error('Original message is unavailable.');
    }
    if (review) {
      // The manager's read-only review uses the same renderer but must never
      // navigate or mutate the operational ticket behind the review panel.
      const group = document.createElement('div');
      if (previous) {
        const heading = document.createElement('p');
        heading.className = 'form-note';
        heading.textContent = 'Referenced conversation · ' + (result.ticket.ticket_reference || targetTicketId);
        group.append(heading);
      }
      for (const message of messages) {
        if (!review.querySelector('[data-message-id="' + Number(message.id) + '"]')) addMessage(group, message, 'investigation');
      }
      group.querySelectorAll('.message-actions').forEach(element => element.remove());
      review.prepend(group);
      if (!previous && investigationMessageBefore && Number(messages[0]?.id) < Number(investigationMessageBefore)) {
        investigationMessageBefore = hasMore ? Number(messages[0].id) : null;
        $('investigationOlderMessages').classList.toggle('hidden', !investigationMessageBefore);
      }
      highlightReplyMessage(targetId, review);
      return;
    }
    const data = state.currentTicketData;
    if (previous) {
      let thread = state.previousTicketHistory.find(item => Number(item.ticket?.id) === targetTicketId);
      if (!thread) {
        // Preserve the history-pagination boundary when inserting a referenced
        // conversation older than the currently loaded history page.
        const oldest = Number(state.previousTicketHistory[0]?.ticket?.id);
        if (oldest && targetTicketId < oldest) state.replyHistoryBoundary = state.replyHistoryBoundary || oldest;
        thread = {ticket: result.ticket, context: {}, messages: []};
        state.previousTicketHistory.push(thread);
        state.previousTicketHistory.sort((a, b) => Number(a.ticket.id) - Number(b.ticket.id));
      }
      thread.messages = mergeTimelineMessages(messages, thread.messages);
      thread.messages_has_more = hasMore;
      thread._messagesExpanded = true;
      state.previousHistoryExpanded = true;
      data.previous_ticket_history = state.previousTicketHistory;
    } else {
      state.cachedCurrentMessages = mergeTimelineMessages(messages, state.cachedCurrentMessages);
      state.currentMessagesHasMore = hasMore;
      state.currentMessagesExpanded = true;
      data.messages = state.cachedCurrentMessages.slice();
      data.messages_has_more = hasMore;
    }
    updateSharedHistoryShortcut();
    renderConversation(data, {preserveAudio: true, preserveScrollAnchor: '[data-message-id="' + Number(reply.source_message_id) + '"]'});
    state.timelineSignature = timelineSignature(data);
    highlightReplyMessage(targetId);
  } catch (error) {
    if (isCurrent() && !state.pageLeaving) notify(error.message || 'Original message could not be loaded. Please try again.', 'error');
  } finally {
    if (button.dataset.replyRequest === String(request)) {
      button.removeAttribute('aria-busy');
      button.classList.remove('reply-loading');
    }
  }
}

function renderReplyQuote(parent, reply) {
  if (!reply) return;
  const quote = document.createElement('button');
  quote.type = 'button';
  quote.className = 'message-quote';
  quote.setAttribute('aria-label', 'Go to original message');
  quote.onclick = event => {
    event.stopPropagation();
    const selection = window.getSelection();
    // Drag-selecting quote text is still copyable. A normal click or keyboard
    // activation clears an older selection so deferred rendering cannot hide
    // the requested destination.
    if (event.detail && selection && !selection.isCollapsed &&
        quote.contains(selection.anchorNode) && quote.contains(selection.focusNode)) return;
    if (selectedConversationText()) selection.removeAllRanges();
    navigateToReply(reply, quote);
  };
  const actor = document.createElement('strong');
  actor.textContent = reply.sender_name || MESSAGE_ROLE_LABELS[String(reply.sender_type || '').toUpperCase()] || 'Message';
  const preview = document.createElement('span');
  preview.dir = 'auto';
  preview.textContent = messagePreview(reply);
  quote.append(actor, preview);
  parent.append(quote);
}

function applyMediaZoom() {
  const surface = state.imageEditor?.canvas || $('mediaViewerBody').querySelector('img');
  if (!surface) return;
  const body = $('mediaViewerBody');
  const width = surface.naturalWidth || surface.width;
  const height = surface.naturalHeight || surface.height;
  const fit = Math.min((body.clientWidth - 28) / width, (body.clientHeight - 28) / height, 1);
  if (!Number.isFinite(fit) || fit <= 0) return;
  surface.style.width = Math.round(width * fit) + 'px';
  surface.style.height = Math.round(height * fit) + 'px';
  surface.style.left = '50%'; surface.style.top = '50%';
  surface.style.transform = 'translate(-50%, -50%) translate(' + state.mediaTranslateX + 'px, ' + state.mediaTranslateY + 'px) scale(' + state.mediaZoom + ')';
  body.classList.toggle('can-pan', state.mediaZoom > 1);
  text($('mediaViewerZoom'), Math.round(state.mediaZoom * 100) + '%');
}

function zoomMediaAt(next, clientX, clientY) {
  const body = $('mediaViewerBody');
  const rect = body.getBoundingClientRect();
  const x = clientX - rect.left - rect.width / 2;
  const y = clientY - rect.top - rect.height / 2;
  const old = state.mediaZoom;
  const zoom = Math.max(1, Math.min(8, next));
  state.mediaTranslateX = x - (x - state.mediaTranslateX) * zoom / old;
  state.mediaTranslateY = y - (y - state.mediaTranslateY) * zoom / old;
  state.mediaZoom = zoom;
  if (zoom === 1) { state.mediaTranslateX = 0; state.mediaTranslateY = 0; }
  applyMediaZoom();
}

function changeMediaZoom(delta) {
  const rect = $('mediaViewerBody').getBoundingClientRect();
  zoomMediaAt(state.mediaZoom + delta, rect.left + rect.width / 2, rect.top + rect.height / 2);
}

function resetMediaZoom() {
  state.mediaZoom = 1; state.mediaTranslateX = 0; state.mediaTranslateY = 0;
  applyMediaZoom();
}

function openMediaViewer({kind, src, title, subtitle}) {
  if (!src) return;
  text($('mediaViewerTitle'), title || (kind === 'document' ? 'PDF document' : 'Image'));
  text($('mediaViewerSubtitle'), subtitle || 'WhatsApp attachment');
  const body = $('mediaViewerBody');
  body.replaceChildren();
  body.classList.toggle('image-mode', kind === 'image');
  state.mediaSource = kind === 'image' ? src : null;
  resetMediaZoom();
  $('mediaViewerTools').classList.toggle('hidden', kind !== 'image');
  // Editing is always available: without reply access the result can still be copied.
  $('editMediaBtn').classList.toggle('hidden', kind !== 'image');
  $('copyMediaBtn').classList.toggle('hidden', kind !== 'image');
  $('downloadMediaBtn').classList.toggle('hidden', kind !== 'image');
  if (kind === 'image') $('downloadMediaBtn').href = src;
  if (kind === 'document') {
    const frame = document.createElement('iframe');
    frame.src = src;
    frame.title = title || 'PDF document';
    body.append(frame);
  } else {
    const img = document.createElement('img');
    img.src = src;
    img.alt = title || 'Image attachment';
    img.onload = applyMediaZoom;
    img.onerror = () => notify('Image could not be loaded.', 'error');
    body.append(img);
  }
  text($('mediaViewerZoom'), '100%');
  openDialog('mediaViewerModal', 'closeMediaViewerBtn');
}

async function closeMediaViewer() {
  if (state.imageEditor?.sending) return;
  if (state.imageEditor?.dirty && !(await confirmDialog({title: 'Discard image edits?', message: 'Discard your changes?', confirmLabel: 'Discard changes', cancelLabel: 'Keep editing', tone: 'danger'}))) return;
  if (state.imageEditor) { state.imageEditor.dispose(); state.imageEditor = null; }
  $('imageEditorToolbar').classList.add('hidden');
  $('mediaViewerBody').classList.remove('editing', 'image-mode', 'can-pan', 'panning');
  mediaPointers.clear();
  state.mediaSource = null;
  resetMediaZoom();
  $('mediaViewerTools').classList.add('hidden');
  $('mediaViewerBody').replaceChildren();
  closeDialog('mediaViewerModal');
}

function updateImageEditorToolbar() {
  const editor = state.imageEditor;
  if (!editor) return;
  for (const [id, mode] of [['editorDrawBtn', 'draw'], ['editorTextBtn', 'text'], ['editorSelectBtn', 'select'], ['editorCropBtn', 'crop']]) {
    $(id).setAttribute('aria-pressed', editor.mode === mode ? 'true' : 'false');
  }
  $('editorUndoBtn').disabled = !editor.canUndo;
  $('editorRedoBtn').disabled = !editor.canRedo;
  $('editorEditTextBtn').disabled = editor.selectedType !== 'text';
  $('editorDeleteBtn').disabled = editor.selected < 0;
  const cropping = editor.mode === 'crop';
  $('editorCropActions').classList.toggle('hidden', !cropping);
  $('editorSelectionActions').classList.toggle('hidden', cropping);
  $('editorApplyCropBtn').disabled = !editor.normalizedCrop();
}

function setImageEditorMode(mode) {
  if (state.imageEditor && !state.imageEditor.sending) state.imageEditor.setMode(mode);
}

// target 'send' edits a conversation image and sends the result as a new image;
// target 'draft' edits the image attached in the composer before it is sent.
async function startImageEditor(blob, target) {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(blob.type)) throw new Error('This image format cannot be edited.');
  const bitmap = await createImageBitmap(blob, {imageOrientation: 'from-image'});
  let editor;
  try {
    editor = new window.ChatImageEditor(bitmap, blob.type);
  } catch (error) {
    bitmap.close?.();
    throw error;
  }
  editor.target = target;
  editor.onChange = updateImageEditorToolbar;
  editor.onResize = () => resetMediaZoom();
  const body = $('mediaViewerBody');
  body.replaceChildren(); body.classList.remove('can-pan', 'panning'); body.classList.add('editing');
  state.mediaZoom = 1; state.mediaTranslateX = 0; state.mediaTranslateY = 0;
  for (const id of ['editMediaBtn', 'copyMediaBtn', 'downloadMediaBtn']) $(id).classList.add('hidden');
  $('imageEditorToolbar').classList.remove('hidden');
  $('editorCaption').value = '';
  $('editorCaption').classList.toggle('hidden', target === 'draft');
  text($('editorSendBtn'), target === 'draft' ? 'Apply edits' : 'Send image');
  $('editorSendBtn').classList.toggle('hidden', target === 'send' && !state.composerEnabled);
  state.imageEditor = editor;
  body.append(editor.canvas);
  editor.resize();
  applyMediaZoom();
  updateImageEditorToolbar();
  editor.canvas.focus();
}

async function enterImageEditor() {
  if (!state.mediaSource || state.imageEditor) return;
  if (!window.ChatImageEditor) { notify('Image editor is unavailable. Reload and try again.', 'error'); return; }
  const source = new URL(state.mediaSource, location.href);
  if (source.origin !== location.origin || !source.pathname.startsWith('/api/support/tickets/')) return;
  $('editMediaBtn').disabled = true;
  try {
    const response = await fetch(source.href, {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error('Image could not be loaded for editing.');
    await startImageEditor(await response.blob(), 'send');
  } catch (error) {
    notify(error.message || 'Image could not be edited.', 'error');
  }
  finally { $('editMediaBtn').disabled = false; }
}

async function editPendingImage(id = state.pendingMedia?.id) {
  const draft = state.pendingMediaItems.find(item => item.id === id);
  if (!draft || draft.kind !== 'image' || state.busy || state.imageEditor) return;
  if (!window.ChatImageEditor) { notify('Image editor is unavailable. Reload and try again.', 'error'); return; }
  openMediaViewer({kind: 'image', src: draft.url, title: 'Edit image', subtitle: draft.file.name || 'Image to send'});
  try {
    await startImageEditor(draft.file, 'draft');
    state.imageEditor.draftId = draft.id;
  } catch (error) {
    notify(error.message || 'Image could not be edited.', 'error');
  }
}

async function applyDraftImageEdits() {
  const editor = state.imageEditor;
  if (!editor || editor.sending) return;
  const draft = state.pendingMediaItems.find(item => item.id === editor.draftId);
  if (!draft || draft.kind !== 'image') { notify('The attached image was removed, so the edits cannot be applied.', 'error'); return; }
  editor.sending = true;
  setButtonBusy($('editorSendBtn'), true, 'Applying…');
  try {
    const blob = await editor.blob();
    const name = (draft.file.name || 'image').replace(/\.[^.]+$/, '') + (blob.type === 'image/png' ? '.png' : '.jpg');
    const file = new File([blob], name, {type: blob.type});
    editor.dispose(); state.imageEditor = null;
    $('imageEditorToolbar').classList.add('hidden');
    $('mediaViewerBody').classList.remove('editing');
    await closeMediaViewer();
    setPendingMedia('image', file, {replaceId: draft.id});
    notify('Edits applied. Add a caption if you like, then send.');
    focusMessageComposer();
  } catch (error) {
    notify(error.message || 'The edits could not be applied.', 'error');
  } finally {
    editor.sending = false;
    setButtonBusy($('editorSendBtn'), false);
  }
}

async function sendEditedImage() {
  const editor = state.imageEditor;
  if (editor?.target === 'draft') return applyDraftImageEdits();
  if (editor?.mode === 'crop') { editor.applyCrop(); return; }
  if (!editor || editor.sending || !state.ticketId || !state.composerEnabled) return;
  editor.sending = true;
  setButtonBusy($('editorSendBtn'), true, 'Sending…');
  try {
    const sourceCheck = await fetch(state.mediaSource, {credentials: 'same-origin', cache: 'no-store'});
    await sourceCheck.body?.cancel();
    if (!sourceCheck.ok) throw new Error('The original image is no longer available. Your edits are still here.');
    const blob = await editor.blob();
    const mime = blob.type;
    const form = new FormData();
    form.append('media_type', 'image');
    form.append('client_message_id', editor.sendId);
    form.append('caption', $('editorCaption').value.trim());
    form.append('file', new File([blob], mime === 'image/png' ? 'edited-image.png' : 'edited-image.jpg', {type: mime}));
    await api('/api/support/tickets/' + state.ticketId + '/media', {method: 'POST', body: form});
    editor.dispose(); state.imageEditor = null;
    $('imageEditorToolbar').classList.add('hidden');
    $('mediaViewerBody').classList.remove('editing');
    closeMediaViewer();
    await openTicket(state.ticketId, {silent: true, refreshQueue: true});
    notify('Edited image sent. The original remains unchanged.');
  } catch (error) {
    if (error.status === 502) editor.sendId = crypto.randomUUID();
    notify(error.message || 'Image upload failed. Your edits are still here.', 'error');
  }
  finally { editor.sending = false; setButtonBusy($('editorSendBtn'), false); }
}

function appendMessageTextWithLinks(container, value) {
  const source = String(value ?? '');
  const pattern = /(?:https?:\/\/|www\.)[^\s<>"']+/gi;
  let cursor = 0;

  for (const match of source.matchAll(pattern)) {
    const start = match.index ?? 0;
    const raw = match[0];
    if (start > cursor) container.append(document.createTextNode(source.slice(cursor, start)));

    let linkText = raw;
    let trailing = '';
    while (/[.,!?;:]$/.test(linkText)) {
      trailing = linkText.slice(-1) + trailing;
      linkText = linkText.slice(0, -1);
    }
    for (const [closing, opening] of [[')', '('], [']', '['], ['}', '{']]) {
      while (linkText.endsWith(closing)) {
        const closes = linkText.split(closing).length - 1;
        const opens = linkText.split(opening).length - 1;
        if (closes <= opens) break;
        trailing = closing + trailing;
        linkText = linkText.slice(0, -1);
      }
    }

    const href = /^www\./i.test(linkText) ? 'https://' + linkText : linkText;
    let safeHref = '';
    try {
      const parsed = new URL(href);
      if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('Unsupported URL protocol');
      safeHref = parsed.href;
    } catch (_) {
      container.append(document.createTextNode(raw));
      cursor = start + raw.length;
      continue;
    }

    const link = document.createElement('a');
    link.href = safeHref;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = linkText;
    link.title = 'Open link in a new tab';
    container.append(link);
    if (trailing) container.append(document.createTextNode(trailing));
    cursor = start + raw.length;
  }

  if (cursor < source.length) container.append(document.createTextNode(source.slice(cursor)));
}

function highlightSearchTerm(root, term) {
  const query = String(term || '').trim();
  if (!root || !query) return;
  const lower = query.toLocaleLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const value = node.nodeValue || '';
    const valueLower = value.toLocaleLowerCase();
    let start = 0;
    let index = valueLower.indexOf(lower, start);
    if (index < 0) continue;
    const fragment = document.createDocumentFragment();
    while (index >= 0) {
      if (index > start) fragment.append(document.createTextNode(value.slice(start, index)));
      const mark = document.createElement('mark');
      mark.className = 'message-search-highlight';
      mark.textContent = value.slice(index, index + query.length);
      fragment.append(mark);
      start = index + query.length;
      index = valueLower.indexOf(lower, start);
    }
    if (start < value.length) fragment.append(document.createTextNode(value.slice(start)));
    node.replaceWith(fragment);
  }
}

function createVoiceNotePlayer(src, label) {
  const player = document.createElement('div');
  player.className = 'voice-note-player';
  const audio = document.createElement('audio');
  audio.controls = true;
  audio.preload = 'metadata';
  audio.src = src;
  audio.dataset.mediaUrl = src;
  audio.setAttribute('aria-label', label);
  const speed = document.createElement('button');
  speed.type = 'button';
  speed.className = 'voice-speed-btn';
  const rates = [1, 1.5, 2];
  let rateIndex = 0;
  const updateSpeed = () => {
    speed.textContent = rates[rateIndex] + 'x';
    speed.setAttribute('aria-label', 'Playback speed ' + rates[rateIndex] + 'x. Click to change speed.');
    speed.title = 'Playback speed ' + rates[rateIndex] + 'x';
  };
  speed.onclick = () => {
    rateIndex = (rateIndex + 1) % rates.length;
    audio.playbackRate = rates[rateIndex];
    updateSpeed();
  };
  updateSpeed();
  player.append(audio, speed);
  return {player, audio};
}

function addMessage(container, m, extraClass = '') {
  const outbound = m.direction === 'OUTBOUND';
  const deleted = Boolean(m.deleted_for_everyone_at);
  const row = document.createElement('article');
  row.className = 'message-row ' + (outbound ? 'outbound ' : 'inbound ') + messageClass(m) + (extraClass ? ' ' + extraClass : '');
  if (m.id) row.dataset.messageId = String(m.id);

  const avatar = document.createElement('div');
  avatar.className = 'avatar message-avatar ' + (messageClass(m) === 'customer' ? 'customer' : messageClass(m) === 'ai' ? 'ai' : '');
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = initials(messageActor(m));

  const stack = document.createElement('div');
  stack.className = 'message-stack';
  const bubble = document.createElement('div');
  bubble.className = 'message-bubble';

  if (m.is_forwarded && !deleted) {
    const forwarded = document.createElement('div');
    forwarded.className = 'forwarded-label';
    forwarded.append(createIcon('transfer', 'icon icon-sm'), document.createTextNode('Forwarded'));
    bubble.append(forwarded);
  }

  const sender = document.createElement('div');
  sender.className = 'message-sender';
  sender.append(document.createTextNode(messageActor(m)));
  bubble.append(sender);
  if (!deleted) renderReplyQuote(bubble, m.reply_to);

  const kind = String(m.media_kind || m.message_type || '').toLowerCase();
  if (deleted) {
    const tombstone = document.createElement('div');
    tombstone.className = 'message-body';
    tombstone.textContent = 'This message was deleted';
    bubble.append(tombstone);
  } else if (['image', 'audio', 'document', 'sticker', 'video'].includes(kind)) {
    const media = document.createElement('div');
    media.className = 'msg-media' + (kind === 'sticker' ? ' sticker' : kind === 'video' ? ' video' : '');
    if (m.media_url) {
      if (kind === 'image' || kind === 'sticker') {
        const open = document.createElement('button');
        open.type = 'button';
        open.className = 'media-open';
        const mediaLabel = kind === 'sticker' ? 'sticker' : 'image';
        open.setAttribute('aria-label', 'View ' + mediaLabel + ' from ' + messageActor(m));
        const img = document.createElement('img');
        img.src = m.media_url;
        img.alt = (kind === 'sticker' ? 'Sticker' : 'Image') + ' from ' + messageActor(m);
        img.loading = 'lazy';
        const recovery = document.createElement('div');
        recovery.className = 'media-unavailable hidden';
        const recoveryText = document.createElement('span');
        recoveryText.textContent = (kind === 'sticker' ? 'Sticker' : 'Image') + ' loading failed. The server can retry the WhatsApp media download.';
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn small';
        retry.textContent = kind === 'sticker' ? 'Retry sticker' : 'Retry image';
        retry.onclick = event => {
          event.stopPropagation();
          recovery.classList.add('hidden');
          img.src = m.media_url + (m.media_url.includes('?') ? '&' : '?') + 'retry=' + Date.now();
        };
        recovery.append(recoveryText, retry);
        img.onerror = () => recovery.classList.remove('hidden');
        img.onload = () => recovery.classList.add('hidden');
        open.onclick = () => openMediaViewer({kind: 'image', src: m.media_url, title: (kind === 'sticker' ? 'Sticker' : 'Image') + ' from ' + messageActor(m), subtitle: fmt(messageOccurredAt(m))});
        open.append(img);
        media.append(open, recovery);
      } else if (kind === 'video') {
        const video = document.createElement('video');
        video.controls = true;
        video.preload = 'metadata';
        video.playsInline = true;
        video.src = m.media_url;
        video.setAttribute('aria-label', 'Video from ' + messageActor(m));
        const recovery = document.createElement('div');
        recovery.className = 'media-unavailable hidden';
        const recoveryText = document.createElement('span');
        recoveryText.textContent = 'Video loading failed. The server can retry the WhatsApp media download.';
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn small';
        retry.textContent = 'Retry video';
        retry.onclick = () => {
          recovery.classList.add('hidden');
          video.src = m.media_url + (m.media_url.includes('?') ? '&' : '?') + 'retry=' + Date.now();
          video.load();
        };
        recovery.append(recoveryText, retry);
        video.onerror = () => recovery.classList.remove('hidden');
        video.onloadedmetadata = () => recovery.classList.add('hidden');
        media.append(video, recovery);
      } else if (kind === 'document') {
        const card = document.createElement('div');
        card.className = 'document-card';
        card.append(createIcon('file'));
        const copy = document.createElement('div');
        copy.className = 'document-card-copy';
        const name = document.createElement('strong');
        name.textContent = m.media_filename || 'PDF document';
        const description = document.createElement('span');
        description.textContent = 'PDF attachment';
        copy.append(name, description);
        const view = document.createElement('button');
        view.type = 'button';
        view.className = 'btn small';
        view.textContent = 'View PDF';
        view.onclick = () => openMediaViewer({kind: 'document', src: m.media_url, title: m.media_filename || 'PDF document', subtitle: 'WhatsApp document'});
        card.append(copy, view);
        media.append(card);
      } else {
        const {player, audio} = createVoiceNotePlayer(m.media_url, 'Voice note from ' + messageActor(m));
        if (outbound) {
          const mic = createIcon('mic', 'icon voice-playback' + (m.delivery_status === 'played' ? ' confirmed-played' : ''));
          mic.setAttribute('aria-label', m.delivery_status === 'played' ? 'Played by recipient' : 'Playback not confirmed');
          const indicator = document.createElement('span');
          indicator.title = mic.getAttribute('aria-label');
          indicator.append(mic);
          player.prepend(indicator);
        }
        const recovery = document.createElement('div');
        recovery.className = 'media-unavailable hidden';
        const recoveryText = document.createElement('span');
        recoveryText.textContent = 'Playback failed. Retry the WhatsApp media download.';
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn small';
        retry.textContent = 'Retry voice note';
        retry.onclick = () => {
          recovery.classList.add('hidden');
          audio.src = m.media_url + (m.media_url.includes('?') ? '&' : '?') + 'retry=' + Date.now();
          audio.load();
        };
        recovery.append(recoveryText, retry);
        audio.onerror = () => recovery.classList.remove('hidden');
        audio.oncanplay = () => recovery.classList.add('hidden');
        media.append(player, recovery);
      }
    } else {
      const unavailable = document.createElement('div');
      unavailable.className = 'media-unavailable';
      unavailable.textContent = kind === 'image'
        ? 'Image is not available in support storage.'
        : kind === 'video'
          ? 'Video is not available in support storage.'
        : kind === 'sticker'
          ? 'Sticker is not available in support storage.'
        : kind === 'document'
          ? 'PDF is not available in support storage.'
          : 'Voice note is not available in support storage.';
      media.append(unavailable);
    }
    bubble.append(media);
  } else if (kind === 'contacts') {
    const label = document.createElement('div');
    label.className = 'shared-contact-label';
    label.textContent = 'Shared contact';
    bubble.append(label);
  } else if (kind === 'location') {
    const card = document.createElement('div');
    card.className = 'location-card';
    card.append(createIcon('map-pin'));
    const copy = document.createElement('div');
    copy.className = 'location-card-copy';
    const name = document.createElement('strong');
    name.dir = 'auto';
    name.textContent = m.location_name || 'Shared location';
    const detail = document.createElement('span');
    detail.dir = 'auto';
    const coordinates = [m.location_latitude, m.location_longitude].every(value => value !== null && value !== undefined)
      ? Number(m.location_latitude).toFixed(6) + ', ' + Number(m.location_longitude).toFixed(6)
      : '';
    detail.textContent = m.location_address || coordinates || 'Location shared on WhatsApp';
    copy.append(name, detail);
    card.append(copy);
    if (coordinates) {
      const map = document.createElement('a');
      map.className = 'btn small';
      map.href = 'https://www.google.com/maps?q=' + encodeURIComponent(String(m.location_latitude) + ',' + String(m.location_longitude));
      map.target = '_blank';
      map.rel = 'noopener noreferrer';
      map.textContent = 'Open map';
      card.append(map);
    }
    bubble.append(card);
  }

  const bodyText = messageTextForCopy(m);
  if (bodyText) {
    const body = document.createElement('div');
    body.className = 'message-body' + (kind === 'contacts' ? ' shared-contact-body' : '');
    body.dir = 'auto';
    appendMessageTextWithLinks(body, bodyText);
    if (state.conversationSearchOpen && state.conversationSearchQuery) highlightSearchTerm(body, state.conversationSearchQuery);
    bubble.append(body);
  }

  const meta = document.createElement('div');
  meta.className = 'message-meta';
  if (m.replacement_for_message_id) {
    const replacement = document.createElement('span');
    replacement.className = 'muted';
    replacement.textContent = 'New copy of message #' + m.replacement_for_message_id;
    meta.append(replacement);
  }
  const time = document.createElement('time');
  time.dateTime = m.created_at || '';
  const occurredAt = messageOccurredAt(m);
  time.title = fmt(occurredAt);
  time.textContent = occurredAt ? fmt(occurredAt) : (extraClass ? 'Earlier context' : 'Time unavailable');
  meta.append(time);
  if (m.edited_at && !deleted) {
    const edited = document.createElement('span');
    edited.textContent = 'edited';
    edited.title = 'Edited ' + fmt(m.edited_at);
    meta.append(edited);
  }
  const actions = document.createElement('div');
  actions.className = 'message-actions';
  actions.setAttribute('role', 'group');
  actions.setAttribute('aria-label', 'Message actions');
  if (canChangeWhatsAppMessage(m, 'edit')) {
    const edit = document.createElement('button');
    edit.type = 'button'; edit.className = 'btn message-action-btn'; edit.textContent = 'Edit';
    labelMessageAction(edit, 'edit', 'Edit message');
    edit.onclick = () => startMessageEdit(m);
    actions.append(edit);
  }
  if (bodyText) {
    const copy = document.createElement('button');
    copy.type = 'button'; copy.className = 'btn message-action-btn'; copy.textContent = 'Copy';
    copy.setAttribute('aria-label', 'Copy message text');
    labelMessageAction(copy, 'copy', 'Copy text');
    copy.onclick = () => copyMessageText(bodyText);
    actions.append(copy);
  }
  if ((kind === 'image' || kind === 'sticker') && m.media_url && !deleted) {
    const copyImage = document.createElement('button');
    copyImage.type = 'button'; copyImage.className = 'btn message-action-btn'; copyImage.textContent = 'Copy image';
    labelMessageAction(copyImage, 'copy-image', 'Copy image');
    copyImage.onclick = () => copyImageToClipboard(m.media_url);
    actions.append(copyImage);
  }
  if (kind === 'location' && !deleted && m.location_latitude != null && m.location_longitude != null) {
    const saveLocation = document.createElement('button');
    saveLocation.type = 'button'; saveLocation.className = 'btn message-action-btn';
    saveLocation.textContent = 'Save location';
    labelMessageAction(saveLocation, 'save-location', 'Save location to quick replies');
    saveLocation.onclick = () => saveLocationQuickReply(m);
    actions.append(saveLocation);
  }
  if (m.id && (!extraClass || canChangeWhatsAppMessage(m, 'everyone'))) {
    const remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'btn message-action-btn'; remove.textContent = 'Delete';
    labelMessageAction(remove, 'delete', 'Delete message');
    remove.onclick = () => openMessageDelete(m);
    actions.append(remove);
  }
  if (!extraClass && !m.id && m.client_message_id && m.send_status === 'FAILED') {
    const dismiss = document.createElement('button');
    dismiss.type = 'button'; dismiss.className = 'btn message-action-btn'; dismiss.textContent = 'Dismiss';
    dismiss.onclick = () => {
      state.localMessages.delete(String(m.client_message_id));
      state.currentTicketData.messages = (state.currentTicketData.messages || []).filter(item => item !== m);
      state.timelineSignature = timelineSignature(state.currentTicketData);
      renderConversation(state.currentTicketData);
    };
    labelMessageAction(dismiss, 'dismiss', 'Dismiss failed message');
    actions.append(dismiss);
  }
  if (!extraClass && m.id && m.whatsapp_message_id && !deleted) {
    const reply = document.createElement('button');
    reply.type = 'button';
    reply.className = 'btn message-action-btn message-reply-btn';
    reply.append(createIcon('reply', 'icon icon-sm'), document.createTextNode('Reply'));
    labelMessageAction(reply, 'reply', 'Reply');
    reply.onclick = () => startReplyToMessage(m);
    actions.append(reply);
  }
  if (!extraClass && m.id && m.whatsapp_message_id && !deleted) {
    const reactionMenu = document.createElement('details');
    reactionMenu.className = 'message-reaction-menu';
    const summary = document.createElement('summary');
    summary.className = 'btn message-action-btn message-reaction-btn';
    summary.append(document.createTextNode('React'));
    labelMessageAction(summary, 'react', 'React');
    const picker = document.createElement('div');
    picker.className = 'message-reaction-picker';
    const ownReaction = (Array.isArray(m.reactions) ? m.reactions : []).find(item => String(item.reactor_type || '').toUpperCase() === 'AGENT');
    for (const emoji of REACTION_EMOJIS) {
      const option = document.createElement('button');
      option.type = 'button';
      option.className = 'reaction-option' + (ownReaction?.emoji === emoji ? ' active' : '');
      option.textContent = emoji;
      option.title = ownReaction?.emoji === emoji ? 'Remove ' + emoji + ' reaction' : 'React with ' + emoji;
      option.setAttribute('aria-label', option.title);
      option.onclick = event => {
        event.preventDefault();
        event.stopPropagation();
        sendMessageReaction(m, emoji, reactionMenu);
      };
      picker.append(option);
    }
    reactionMenu.append(summary, picker);
    actions.append(reactionMenu);
  }
  if (!extraClass && m.id && !deleted && state.currentTicketData?.permissions?.can_forward !== false) {
    const forward = document.createElement('button');
    forward.type = 'button';
    forward.className = 'btn message-action-btn message-forward-btn';
    forward.append(createIcon('transfer', 'icon icon-sm'), document.createTextNode('Forward'));
    labelMessageAction(forward, 'forward', 'Forward');
    forward.onclick = () => openForwardModal(m);
    actions.append(forward);
    const select = document.createElement('button');
    select.type = 'button'; select.className = 'btn message-action-btn'; select.textContent = 'Select';
    labelMessageAction(select, 'select', 'Select messages');
    select.onclick = () => selectChatMessage(m);
    actions.append(select);
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.className = 'message-select'; checkbox.dataset.messageId = String(m.id);
    checkbox.checked = Boolean(state.selectedMessages?.has(String(m.id)));
    checkbox.setAttribute('aria-label', 'Select message ' + m.id);
    checkbox.onchange = () => selectChatMessage(m, checkbox.checked);
    row.prepend(checkbox);
  }
  if (actions.children.length) meta.append(actions);
  if (m.send_status === 'FAILED') {
    const failed = document.createElement('span');
    failed.className = 'send-state failed';
    failed.title = m.error_message || 'WhatsApp could not send this message.';
    const failureLabel = String(m.error_message || '').startsWith('WhatsApp 131047:')
      ? 'Not sent · WhatsApp reply window closed' : 'Not sent';
    failed.append(createIcon('alert', 'icon icon-sm'), document.createTextNode(failureLabel));
    meta.append(failed);
    if (!extraClass && canResendMessage(m)) {
      const resend = document.createElement('button');
      resend.type = 'button';
      resend.className = 'btn send-retry-btn';
      resend.textContent = 'Resend';
      resend.setAttribute('aria-label', 'Resend this message');
      resend.onclick = () => resendMessage(m, resend);
      meta.append(resend);
    }
  } else if (m.send_status === 'UNCERTAIN') {
    const uncertain = document.createElement('span');
    uncertain.className = 'send-state failed';
    uncertain.textContent = 'Delivery uncertain';
    uncertain.title = 'WhatsApp may have received this message. Check its status before deciding whether to send a new copy.';
    meta.append(uncertain);
    if (!extraClass && m.id && !m.deleted_for_everyone_at) {
      const check = document.createElement('button');
      check.type = 'button';
      check.className = 'btn send-retry-btn';
      check.textContent = 'Check status';
      check.onclick = () => checkUncertainDelivery(m, check);
      meta.append(check);
      if (canCopyUncertainMessage(m)) {
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'btn send-retry-btn';
        copy.textContent = 'Send a new copy';
        copy.onclick = () => sendUncertainCopy(m, copy);
        meta.append(copy);
      }
    }
  } else if (m.send_status === 'PENDING' || m.send_status === 'DISPATCHING') {
    const pending = document.createElement('span');
    pending.className = 'send-state pending';
    pending.title = 'Sending';
    pending.setAttribute('aria-label', 'Sending');
    pending.append(createIcon('clock', 'icon icon-sm'));
    meta.append(pending);
    if (!extraClass && canResendMessage(m)) {
      const resume = document.createElement('button');
      resume.type = 'button';
      resume.className = 'btn send-retry-btn';
      resume.textContent = 'Resume send';
      resume.title = 'Resume abandoned preparation. An active send will not be repeated.';
      resume.onclick = () => resendMessage(m, resume);
      meta.append(resume);
    }
  } else if (outbound && m.send_status) {
    // WhatsApp-style ticks: one grey when sent, two grey when delivered, two blue when read.
    const receipt = ['read', 'played'].includes(m.delivery_status) ? 'read' : m.delivery_status === 'delivered' ? 'delivered' : 'sent';
    const label = receipt === 'read' ? 'Read' : receipt === 'delivered' ? 'Delivered' : 'Sent';
    const sent = document.createElement('span');
    sent.className = 'send-state ticks ' + receipt;
    sent.title = label;
    sent.setAttribute('aria-label', label);
    sent.append(createIcon(receipt === 'sent' ? 'check' : 'check-double', 'icon icon-sm'));
    meta.append(sent);
  }

  const reactionBar = messageReactionBar(m);
  if (reactionBar) stack.append(bubble, reactionBar, meta);
  else stack.append(bubble, meta);
  if (outbound) row.append(stack, avatar);
  else row.append(avatar, stack);
  container.append(row);
}
// Compact message toolbar: the visible label stays in textContent (accessible
// name); CSS draws the glyph from data-action and shows data-tooltip.
function canCopyUncertainMessage(m) {
  return Boolean(m.id && m.direction === 'OUTBOUND' && m.send_status === 'UNCERTAIN' &&
    !m.deleted_for_everyone_at && state.currentTicketData?.permissions?.can_reply &&
    String(m.ticket_id || state.ticketId) === String(state.ticketId));
}

async function checkUncertainDelivery(m, button) {
  const ticketId = m.ticket_id || state.ticketId;
  setButtonBusy(button, true, 'Checking…');
  try {
    const result = await api('/api/support/tickets/' + ticketId + '/messages/' + m.id + '/check-delivery', {method: 'POST'});
    if (result.message) upsertTimelineMessage(result.message, ticketId);
    await alertDialog({title: 'Delivery status', message: result.notice});
  } catch (error) {
    notify(error.message || 'Delivery status could not be checked.', 'error');
  } finally { setButtonBusy(button, false); }
}

async function sendUncertainCopy(m, button) {
  const ticketId = m.ticket_id || state.ticketId;
  const accepted = await confirmDialog({title: 'Send a new copy?',
    message: 'The original may already have reached the customer or could arrive later. Sending a new copy could deliver the message twice. The original will remain in the history.',
    confirmLabel: 'Send a new copy', tone: 'danger'});
  if (!accepted) return;
  setButtonBusy(button, true, 'Sending…');
  try {
    const result = await api('/api/support/tickets/' + ticketId + '/messages/' + m.id + '/new-copy',
      {method: 'POST', body: JSON.stringify({accept_duplicate_risk: true})});
    if (result.message) upsertTimelineMessage(result.message, ticketId);
    loadQueue({silent: true}).catch(() => {});
  } catch (error) {
    notify(error.message || 'The new copy could not be sent.', 'error');
  } finally { setButtonBusy(button, false); }
}

function canResendMessage(m) {
  if (m.direction !== 'OUTBOUND' || !['FAILED', 'PENDING'].includes(m.send_status) || m.deleted_for_everyone_at) return false;
  if (String(m.sender_id || '') !== String(state.agent?.id || '')) return false;
  if (!m.id) return Boolean(m.client_message_id && m.message_type === 'text');
  return (Boolean(m.delivery_retry_safe) || (m.send_status === 'PENDING' && !m.delivery_started_at)) && state.currentTicketData?.permissions?.can_reply !== false;
}

async function resendMessage(m, button) {
  if (button) { button.disabled = true; button.textContent = 'Sending…'; }
  const ticketId = m.ticket_id || state.ticketId;
  if (!m.id) {
    // Never reached the server: send it again with the same client ID, so a
    // request that did arrive is not sent twice.
    m.send_status = 'PENDING';
    m.error_message = null;
    upsertTimelineMessage(m, ticketId);
    await deliverTextMessage(m, ticketId);
    return;
  }
  try {
    const result = await api('/api/support/tickets/' + ticketId + '/messages/' + m.id + '/resend', {method: 'POST'});
    if (result.message) upsertTimelineMessage(result.message, ticketId);
    loadQueue({silent: true}).catch(() => {});
  } catch (error) {
    notify(error.message || 'The message could not be sent again.', 'error');
    if (state.ticketId && String(state.ticketId) === String(ticketId)) openTicket(state.ticketId, {silent: true}).catch(() => {});
  }
}

function labelMessageAction(button, action, label) {
  button.dataset.action = action;
  button.dataset.tooltip = label;
  button.dataset.tooltipPlacement = 'top';
  if (action !== 'copy') button.setAttribute('aria-label', label);
}

function addEvent(container, event) {
  const element = document.createElement('div');
  element.className = 'event';
  let label = String(event.event_type || 'EVENT').replaceAll('_', ' ');
  if (event.event_type === 'TICKET_TRANSFERRED') {
    try {
      const details = typeof event.details_json === 'string' ? JSON.parse(event.details_json) : (event.details_json || {});
      if (details.from_agent_name && details.to_agent_name) label = 'Transferred: ' + details.from_agent_name + ' → ' + details.to_agent_name;
    } catch (_) {}
  }
  const actor = event.actor_name || event.actor_type || 'System';
  element.textContent = formatStatus(label) + ' · ' + actor + ' · ' + fmt(event.created_at);
  container.append(element);
}

function dayKey(value) {
  if (!value) return '';
  const date = parseServerDate(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.getFullYear() + '-' + date.getMonth() + '-' + date.getDate();
}

function dayLabel(value) {
  if (!value) return '';
  const date = parseServerDate(value);
  if (Number.isNaN(date.getTime())) return '';
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  if (dayKey(date) === dayKey(today)) return 'Today';
  if (dayKey(date) === dayKey(yesterday)) return 'Yesterday';
  return new Intl.DateTimeFormat(undefined, {weekday: 'short', month: 'short', day: 'numeric', year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric'}).format(date);
}

function addDateDivider(container, value) {
  const divider = document.createElement('div');
  divider.className = 'date-divider';
  divider.textContent = dayLabel(value);
  container.append(divider);
}

function timelineSignature(data) {
  const messageSignature = message => [
    message.id, message.sender_name, message.reply_to?.sender_name, message.updated_at, message.send_status, message.delivery_status, message.error_message,
    message.media_available, message.body, message.version, message.edited_at,
    message.deleted_for_everyone_at, message.can_edit, message.can_delete_for_everyone,
    (message.reactions || []).map(reaction => [reaction.id, reaction.emoji, reaction.reactor_type, reaction.reactor_name, reaction.updated_at])
  ];
  const messages = (data.messages || []).map(messageSignature);
  const events = (data.events || []).map(event => [event.id, event.event_type, event.actor_name, event.created_at, event.details_json]);
  const recent = ((data.context || {}).recent_messages || []).map(message => [message.role, message.content]);
  const previous = (data.previous_ticket_history || []).map(thread => [
    thread.ticket?.id,
    thread.ticket?.updated_at,
    thread.ticket?.display_name, thread.ticket?.current_assignee_name, thread.ticket?.initiated_by_staff_name,
    thread.messages_has_more,
    ((thread.context || {}).recent_messages || []).map(message => [message.role, message.content]),
    (thread.messages || []).map(messageSignature)
  ]);
  return JSON.stringify([data.ticket?.id, data.ticket?.updated_at, data.ticket?.display_name, data.messages_has_more,
    data.previous_ticket_history_has_more, messages, events, recent, previous]);
}

function mergeTimelineMessages(loaded, incoming) {
  const byId = new Map();
  for (const message of [...(loaded || []), ...(incoming || [])]) {
    if (message?.id) byId.set(String(message.id), message);
  }
  return [...byId.values()].sort((left, right) => Number(left.id) - Number(right.id));
}

function mergePreviousHistoryThreads(loaded, fresh) {
  const existing = new Map((loaded || []).map(thread => [String(thread.ticket?.id), thread]));
  const freshIds = new Set((fresh || []).map(thread => String(thread.ticket?.id)));
  return [
    ...(loaded || []).filter(thread => !freshIds.has(String(thread.ticket?.id))),
    ...(fresh || []).map(thread => {
      const old = existing.get(String(thread.ticket?.id));
      if (!old) return thread;
      return {
        ...thread,
        messages: mergeTimelineMessages(old.messages, thread.messages),
        messages_has_more: old._messagesExpanded ? old.messages_has_more : thread.messages_has_more,
        _messagesExpanded: Boolean(old._messagesExpanded)
      };
    })
  ];
}

function changeCachedMessage(ticketId, messageId, updated = null, hidden = false) {
  const change = messages => (messages || []).filter(message => !hidden || Number(message.id) !== Number(messageId))
    .map(message => updated && Number(message.id) === Number(messageId) ? updated : message);
  if (String(ticketId) === String(state.ticketId)) {
    state.cachedCurrentMessages = change(state.cachedCurrentMessages);
    if (state.currentTicketData) state.currentTicketData.messages = change(state.currentTicketData.messages);
  } else {
    const thread = state.previousTicketHistory.find(item => String(item.ticket?.id) === String(ticketId));
    if (thread) thread.messages = change(thread.messages);
  }
}

function upsertTimelineMessage(message, ticketId) {
  if (!message || !state.currentTicketData || String(state.ticketId) !== String(ticketId)) return;
  if (state.currentTicketData.ticket?.id && String(state.currentTicketData.ticket.id) !== String(ticketId)) return;
  const messages = state.currentTicketData.messages || (state.currentTicketData.messages = []);
  const clientId = String(message.client_message_id || '');
  const index = messages.findIndex(item =>
    (message.id && String(item.id) === String(message.id)) ||
    (clientId && String(item.client_message_id || '') === clientId));
  if (index >= 0) messages[index] = message;
  else messages.push(message);
  if (message.id) {
    const cacheIndex = state.cachedCurrentMessages.findIndex(item => String(item.id) === String(message.id));
    if (cacheIndex >= 0) state.cachedCurrentMessages[cacheIndex] = message;
    else state.cachedCurrentMessages.push(message);
  }
  if (clientId && message.id) state.localMessages.delete(clientId);
  if (message.id) state.realtimeMessages.set(String(ticketId) + ':' + message.id, message);
  state.timelineSignature = timelineSignature(state.currentTicketData);
  renderConversation(state.currentTicketData);
  if (message.direction === 'INBOUND' && message.id) {
    const key = String(ticketId);
    state.lastInboundByTicket[key] = Math.max(Number(state.lastInboundByTicket[key] || 0), Number(message.id));
  }
}

function mergeLiveMessages(data, ticketId) {
  if (!Array.isArray(data.messages)) data.messages = [];
  const seen = new Set((data.messages || []).map(message => String(message.id)));
  for (const [key, message] of state.realtimeMessages) {
    if (!key.startsWith(String(ticketId) + ':')) continue;
    if (seen.has(String(message.id))) { state.realtimeMessages.delete(key); continue; }
    data.messages.push(message);
    seen.add(String(message.id));
  }
  const confirmed = new Set(data.messages.map(message => String(message.client_message_id || '')));
  for (const [clientId, item] of state.localMessages) {
    if (String(item.ticketId) !== String(ticketId)) continue;
    if (confirmed.has(clientId)) { state.localMessages.delete(clientId); continue; }
    data.messages.push(item.message);
  }
}

function updateAudioMessageRow(existing, rendered) {
  const existingStack = existing.querySelector('.message-stack');
  const renderedStack = rendered.querySelector('.message-stack');
  const existingBubble = existingStack.querySelector('.message-bubble');
  const renderedBubble = renderedStack.querySelector('.message-bubble');
  const existingMedia = existingBubble.querySelector('.msg-media');
  const renderedMedia = renderedBubble.querySelector('.msg-media');

  // The receipt indicator lives inside the retained player. Refresh this
  // sibling of the audio controls without disconnecting or resetting playback.
  const existingPlayback = existingMedia.querySelector('.voice-playback')?.parentElement;
  const renderedPlayback = renderedMedia.querySelector('.voice-playback')?.parentElement;
  if (existingPlayback && renderedPlayback) existingPlayback.replaceWith(renderedPlayback);

  // Keep the media element and all of its ancestors connected while updating
  // the sender, caption, reactions, and delivery status around it.
  function updateAround(parent, fresh, kept, freshKept) {
    const children = [...fresh.children];
    const split = children.indexOf(freshKept);
    for (const child of children.slice(0, split)) parent.insertBefore(child, kept);
    for (const child of children.slice(split + 1)) parent.append(child);
    for (const child of [...parent.children]) {
      if (child !== kept && !children.includes(child)) child.remove();
    }
  }

  updateAround(existingBubble, renderedBubble, existingMedia, renderedMedia);
  updateAround(existingStack, renderedStack, existingBubble, renderedBubble);
  existing.querySelector('.message-avatar').replaceWith(rendered.querySelector('.message-avatar'));
  existing.className = rendered.className;
}

function updateConversationKeepingAudio(conversation, rendered) {
  const existing = conversation.firstElementChild;
  if (!existing?.classList.contains('conversation-inner')) return false;
  const audioRows = new Map();
  for (const row of existing.querySelectorAll('.message-row[data-message-id]')) {
    const audio = row.querySelector('.voice-note-player audio[data-media-url]');
    if (audio) audioRows.set(row.dataset.messageId, row);
  }
  if (!audioRows.size) return false;

  let reused = 0;
  const desired = [...rendered.children].map(row => {
    const previous = row.dataset.messageId && audioRows.get(row.dataset.messageId);
    if (!previous) return row;
    const oldAudio = previous.querySelector('.voice-note-player audio[data-media-url]');
    const newAudio = row.querySelector('.voice-note-player audio[data-media-url]');
    if (!newAudio || oldAudio.dataset.mediaUrl !== newAudio.dataset.mediaUrl) return row;
    audioRows.delete(row.dataset.messageId);
    updateAudioMessageRow(previous, row);
    reused++;
    return previous;
  });
  if (!reused) return false;

  const wanted = new Set(desired);
  let cursor = existing.firstElementChild;
  for (const row of desired) {
    if (row === cursor) {
      cursor = cursor.nextElementSibling;
      continue;
    }
    while (cursor && cursor !== row && !wanted.has(cursor)) {
      const next = cursor.nextElementSibling;
      cursor.remove();
      cursor = next;
    }
    if (row === cursor) cursor = cursor.nextElementSibling;
    else existing.insertBefore(row, cursor);
  }
  while (cursor) {
    const next = cursor.nextElementSibling;
    cursor.remove();
    cursor = next;
  }
  return true;
}

function historyLoadButton(label, handler, ariaLabel = label) {
  const row = document.createElement('div');
  row.className = 'history-load-row';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn small';
  button.textContent = label;
  button.setAttribute('aria-label', ariaLabel);
  button.disabled = state.historyLoadBusy;
  button.onclick = () => handler(button);
  row.append(button);
  return row;
}

function selectedConversationText() {
  if (typeof window === 'undefined' || typeof window.getSelection !== 'function') return '';
  const selection = window.getSelection();
  const conversation = $('conversation');
  if (!selection || selection.isCollapsed || !conversation?.contains ||
      !conversation.contains(selection.anchorNode) || !conversation.contains(selection.focusNode)) return '';
  return selection.toString();
}

function beginConversationSelection(event) {
  if (event.button === 0 && event.target.closest?.('.message-body, .message-quote, .message-sender')) {
    state.conversationSelectionPointer = event.pointerId;
  }
}

function finishConversationSelection(event) {
  if (state.conversationSelectionPointer !== event.pointerId) return;
  state.conversationSelectionPointer = null;
  window.setTimeout(flushPendingConversationRender, 0);
}

function handleConversationCopy(event) {
  const selected = selectedConversationText();
  if (!selected || !event.clipboardData?.setData) return;
  event.clipboardData.setData('text/plain', selected);
  event.preventDefault();
}

function flushPendingConversationRender() {
  const pending = state.pendingConversationRender;
  if (!pending || state.conversationSelectionPointer !== null || selectedConversationText()) return;
  state.pendingConversationRender = null;
  if (String(pending.data?.ticket?.id) !== String(state.ticketId)) return;
  const data = state.currentTicketData || pending.data;
  renderConversation(data, pending.options);
  state.timelineSignature = timelineSignature(data);
}

function renderConversation(data, {forceScroll = false, preserveAudio = true, preserveScrollAnchor = null} = {}) {
  if (!forceScroll && (state.conversationSelectionPointer !== null || selectedConversationText())) {
    state.pendingConversationRender = {data, options: {forceScroll, preserveAudio, preserveScrollAnchor}};
    return;
  }
  state.pendingConversationRender = null;
  const conversation = $('conversation');
  const nearBottom = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 140;
  const previousScrollTop = conversation.scrollTop;
  const previousHeight = conversation.scrollHeight;
  const anchorTop = preserveScrollAnchor ? (conversation.querySelector(preserveScrollAnchor)?.getBoundingClientRect().top ?? null) : null;
  const inner = document.createElement('div');
  inner.className = 'conversation-inner';

  const previousThreads = Array.isArray(data.previous_ticket_history) ? data.previous_ticket_history : [];
  if (previousThreads.length || data.previous_ticket_history_has_more) {
    const historyHeading = document.createElement('div');
    historyHeading.className = 'context-divider';
    historyHeading.append(createIcon('history', 'icon icon-sm'), document.createTextNode('Shared customer history'));
    inner.append(historyHeading);
  }
  if (data.previous_ticket_history_has_more && !state.conversationSearchOpen) {
    inner.append(historyLoadButton('Load earlier conversations', loadEarlierConversations));
  }
  for (const thread of previousThreads) {
    const historicalTicket = thread.ticket || {};
    const divider = document.createElement('div');
    divider.className = 'previous-ticket-divider';
    divider.dataset.historyTicketId = String(historicalTicket.id || '');
    const title = document.createElement('strong');
    title.textContent = (isStaffChat(historicalTicket) ? 'Chat · ' : 'Ticket · ') +
      (historicalTicket.ticket_reference || ('#' + historicalTicket.id));
    const details = document.createElement('span');
    const closedAt = historicalTicket.closed_at || historicalTicket.resolved_at || historicalTicket.last_activity_at;
    details.append(document.createTextNode((closedAt ? fmt(closedAt) + ' · ' : '') +
      formatStatus(historicalTicket.status || 'Resolved') + ' · '), createSupportNumberLabel(historicalTicket));
    divider.append(title, details);
    inner.append(divider);
    const historicalMessages = Array.isArray(thread.messages) ? thread.messages : [];
    const historicalContext = ((thread.context || {}).recent_messages || []).filter(message => ['user', 'assistant'].includes(message.role) && message.content);
    const firstHistoricalInbound = historicalMessages.find(message => message.sender_type === 'CUSTOMER' && message.direction === 'INBOUND');
    if (firstHistoricalInbound && historicalContext.length && historicalContext[historicalContext.length - 1].role === 'user' && historicalContext[historicalContext.length - 1].content === firstHistoricalInbound.body) historicalContext.pop();
    if (historicalContext.length) {
      const contextLabel = document.createElement('div');
      contextLabel.className = 'context-divider';
      contextLabel.append(createIcon('history', 'icon icon-sm'), document.createTextNode('AI context before this handoff'));
      inner.append(contextLabel);
      for (const message of historicalContext) {
        addMessage(inner, {body: message.content, sender_type: message.role === 'user' ? 'CUSTOMER' : 'AI', direction: message.role === 'user' ? 'INBOUND' : 'OUTBOUND'}, 'context-msg previous-ticket-msg');
      }
    }
    if (thread.messages_has_more && !state.conversationSearchOpen) {
      inner.append(historyLoadButton('Load older messages', button => loadOlderConversationMessages(historicalTicket.id, true, button),
        'Load older messages in ' + (historicalTicket.ticket_reference || ('conversation #' + historicalTicket.id))));
    }
    for (const message of [...historicalMessages].sort((left, right) => String(messageOccurredAt(left) || '').localeCompare(String(messageOccurredAt(right) || '')))) {
      addMessage(inner, message, 'previous-ticket-msg');
    }
  }
  if (previousThreads.length) {
    const currentDivider = document.createElement('div');
    currentDivider.className = 'context-divider';
    currentDivider.append(createIcon('message', 'icon icon-sm'), document.createTextNode(
      (isStaffChat(data.ticket) ? 'Current chat · ' : 'Current ticket · ') +
      (data.ticket?.ticket_reference || ('#' + data.ticket?.id))));
    inner.append(currentDivider);
  }

  const recent = ((data.context || {}).recent_messages || []).filter(message => ['user', 'assistant'].includes(message.role) && message.content);
  const firstInbound = (data.messages || []).find(message => message.sender_type === 'CUSTOMER' && message.direction === 'INBOUND');
  if (firstInbound && recent.length && recent[recent.length - 1].role === 'user' && recent[recent.length - 1].content === firstInbound.body) recent.pop();
  if (recent.length) {
    const divider = document.createElement('div');
    divider.className = 'context-divider';
    divider.append(createIcon('history', 'icon icon-sm'), document.createTextNode('Earlier AI conversation context'));
    inner.append(divider);
    for (const message of recent) {
      addMessage(inner, {body: message.content, sender_type: message.role === 'user' ? 'CUSTOMER' : 'AI', direction: message.role === 'user' ? 'INBOUND' : 'OUTBOUND'}, 'context-msg');
    }
  }
  if (data.messages_has_more && !state.conversationSearchOpen) {
    inner.append(historyLoadButton('Load older messages', button => loadOlderConversationMessages(data.ticket?.id, false, button),
      'Load older messages in this conversation'));
  }

  const timeline = [
    ...(data.messages || []).map(item => ({kind: 'message', at: messageOccurredAt(item), data: item})),
    ...(data.events || []).map(item => ({kind: 'event', at: item.created_at, data: item}))
  ].sort((left, right) => String(left.at || '').localeCompare(String(right.at || '')));

  let currentDay = '';
  for (const item of timeline) {
    const itemDay = dayKey(item.at);
    if (itemDay && itemDay !== currentDay) {
      currentDay = itemDay;
      addDateDivider(inner, item.at);
    }
    if (item.kind === 'message') addMessage(inner, item.data);
    else addEvent(inner, item.data);
  }

  if (!previousThreads.length && !recent.length && !timeline.length) {
    const empty = emptyMarkup('No messages yet', 'The customer conversation has no message history to display.', 'message');
    empty.outer.classList.add('conversation-empty');
    conversation.replaceChildren(empty.outer);
  } else {
    if (!preserveAudio || !updateConversationKeepingAudio(conversation, inner)) conversation.replaceChildren(inner);
  }

  if (preserveScrollAnchor) {
    const newAnchor = conversation.querySelector(preserveScrollAnchor);
    conversation.scrollTop = previousScrollTop + (anchorTop !== null && newAnchor
      ? newAnchor.getBoundingClientRect().top - anchorTop
      : conversation.scrollHeight - previousHeight);
  } else if (forceScroll || nearBottom) conversation.scrollTop = conversation.scrollHeight;
  else conversation.scrollTop = previousScrollTop;
  updateScrollLatestButton();
}

function beginHistoryLoad(button) {
  if (state.historyLoadBusy || !state.authenticated || !state.ticketId || !state.currentTicketData) return null;
  state.historyLoadBusy = true;
  const request = {
    ticketId: state.ticketId,
    selectionEpoch: state.historySelectionEpoch,
    requestId: ++state.historyLoadRequest,
    button,
    label: button?.textContent || ''
  };
  if (button) { button.disabled = true; button.textContent = 'Loading…'; }
  return request;
}

function historyLoadIsCurrent(request) {
  return request.requestId === state.historyLoadRequest &&
    request.selectionEpoch === state.historySelectionEpoch &&
    String(request.ticketId) === String(state.ticketId) && state.authenticated;
}

function finishHistoryLoad(request) {
  if (request.requestId !== state.historyLoadRequest) return;
  state.historyLoadBusy = false;
  if (request.button?.isConnected) {
    request.button.disabled = false;
    request.button.textContent = request.label;
  }
}

function historyDetailBase() {
  const origin = state.currentTicketData?.ticket?.conversation_origin || state.currentConversationOrigin;
  return '/api/support/' + (String(origin).toUpperCase() === 'STAFF' ? 'chats/' : 'tickets/') + state.ticketId;
}

async function loadEarlierConversations(button = null) {
  const oldest = state.previousTicketHistory[0];
  const beforeTicketId = Number(state.replyHistoryBoundary || oldest?.ticket?.id);
  if (!state.previousTicketHistoryHasMore || !beforeTicketId) return;
  const request = beginHistoryLoad(button);
  if (!request) return;
  const anchor = '[data-history-ticket-id="' + beforeTicketId + '"]';
  try {
    const result = await api(historyDetailBase() + '/history?before_ticket_id=' + beforeTicketId + '&limit=5');
    if (!historyLoadIsCurrent(request)) return;
    if (!Array.isArray(result.previous_ticket_history)) throw new Error('Earlier conversation history could not be loaded.');
    const existing = new Set(state.previousTicketHistory.map(thread => String(thread.ticket?.id)));
    const older = result.previous_ticket_history.filter(thread => thread.ticket?.id && !existing.has(String(thread.ticket.id)));
    state.previousTicketHistory = [...older, ...state.previousTicketHistory]
      .sort((a, b) => Number(a.ticket.id) - Number(b.ticket.id));
    state.replyHistoryBoundary = result.previous_ticket_history[0]?.ticket?.id || null;
    state.previousTicketHistoryHasMore = result.previous_ticket_history.length > 0 && Boolean(result.previous_ticket_history_has_more);
    state.previousHistoryExpanded = true;
    updateSharedHistoryShortcut();
    const data = state.currentTicketData;
    data.previous_ticket_history = state.previousTicketHistory;
    data.previous_ticket_history_has_more = state.previousTicketHistoryHasMore;
    state.historyLoadBusy = false;
    renderConversation(data, {preserveScrollAnchor: anchor});
    state.timelineSignature = timelineSignature(data);
  } catch (error) {
    if (historyLoadIsCurrent(request) && !state.pageLeaving) {
      state.historyLoadBusy = false;
      renderConversation(state.currentTicketData, {preserveScrollAnchor: anchor});
      showError(error.message);
    }
  } finally {
    finishHistoryLoad(request);
  }
}

async function loadOlderConversationMessages(ticketId, previous = false, button = null) {
  const targetId = Number(ticketId);
  const thread = previous ? state.previousTicketHistory.find(item => Number(item.ticket?.id) === targetId) : null;
  const messages = previous ? thread?.messages : state.cachedCurrentMessages;
  const hasMore = previous ? thread?.messages_has_more : state.currentMessagesHasMore;
  const oldestMessageId = Number((messages || []).find(message => message?.id)?.id);
  if (!targetId || !hasMore || !oldestMessageId || (previous && !thread)) return;
  const request = beginHistoryLoad(button);
  if (!request) return;
  const anchor = '[data-message-id="' + oldestMessageId + '"]';
  const path = historyDetailBase() + (previous ? '/history/' + targetId : '') +
    '/messages?before_message_id=' + oldestMessageId + '&limit=' + (previous ? '100' : '150');
  try {
    const result = await api(path);
    if (!historyLoadIsCurrent(request)) return;
    if (!Array.isArray(result.messages)) throw new Error('Older messages could not be loaded.');
    const data = state.currentTicketData;
    if (previous) {
      const currentThread = state.previousTicketHistory.find(item => Number(item.ticket?.id) === targetId);
      if (!currentThread) return;
      const oldCount = (currentThread.messages || []).length;
      currentThread.messages = mergeTimelineMessages(result.messages, currentThread.messages);
      currentThread.messages_has_more = currentThread.messages.length > oldCount && Boolean(result.messages_has_more);
      currentThread._messagesExpanded = true;
      data.previous_ticket_history = state.previousTicketHistory;
    } else {
      const oldCount = state.cachedCurrentMessages.length;
      state.cachedCurrentMessages = mergeTimelineMessages(result.messages, state.cachedCurrentMessages);
      state.currentMessagesHasMore = state.cachedCurrentMessages.length > oldCount && Boolean(result.messages_has_more);
      state.currentMessagesExpanded = true;
      data.messages = state.cachedCurrentMessages.slice();
      data.messages_has_more = state.currentMessagesHasMore;
      mergeLiveMessages(data, state.ticketId);
    }
    state.historyLoadBusy = false;
    renderConversation(data, {preserveScrollAnchor: anchor});
    state.timelineSignature = timelineSignature(data);
  } catch (error) {
    if (historyLoadIsCurrent(request) && !state.pageLeaving) {
      state.historyLoadBusy = false;
      renderConversation(state.currentTicketData, {preserveScrollAnchor: anchor});
      showError(error.message);
    }
  } finally {
    finishHistoryLoad(request);
  }
}

function updateSharedHistoryShortcut() {
  const button = $('viewSharedHistoryBtn');
  const count = state.previousTicketHistory.length;
  const hasHistory = count > 0 || state.previousTicketHistoryHasMore;
  button.classList.toggle('hidden', !hasHistory);
  text($('sharedHistoryCount'), hasHistory ? '(' + count + (state.previousTicketHistoryHasMore ? '+' : '') + ')' : '');
  button.setAttribute('aria-label', hasHistory
    ? 'View shared customer history, ' + count + (state.previousTicketHistoryHasMore ? ' or more' : '') + ' conversations'
    : 'View shared customer history');
  if (!hasHistory) {
    state.historyJumpActive = false;
    state.historyJumpReachedTop = false;
  }
}

function jumpToSharedHistory() {
  if (!state.currentTicketData || $('viewSharedHistoryBtn').classList.contains('hidden')) return;
  state.historyJumpActive = true;
  state.historyJumpReachedTop = false;
  $('conversation').scrollTo({top: 0, behavior: 'smooth'});
  updateScrollLatestButton();
}

function jumpToCurrentConversation() {
  state.historyJumpActive = false;
  state.historyJumpReachedTop = false;
  const conversation = $('conversation');
  conversation.scrollTo({top: conversation.scrollHeight, behavior: 'smooth'});
  updateScrollLatestButton();
}

function updateScrollLatestButton() {
  const conversation = $('conversation');
  const away = conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight > 220;
  if (state.historyJumpActive) {
    if (conversation.scrollTop <= 20) state.historyJumpReachedTop = true;
    else if (state.historyJumpReachedTop && !away) {
      state.historyJumpActive = false;
      state.historyJumpReachedTop = false;
    }
  }
  const button = $('scrollToLatestBtn');
  button.classList.toggle('hidden', !away && !state.historyJumpActive);
  const label = state.historyJumpActive ? 'Back to current' : 'Latest';
  text($('scrollToLatestLabel'), label);
  button.setAttribute('aria-label', state.historyJumpActive ? 'Back to current conversation' : 'Scroll to latest message');
}

function contextValue(value) {
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

const CONTEXT_CARD_ICONS = {Customer: 'user', Escalation: 'alert', 'AI summary': 'activity', Shipment: 'truck', Ticket: 'tag', Chat: 'message'};

function appendContextCard(container, title, entries, {summary = false} = {}) {
  const card = document.createElement('section');
  card.className = 'context-card' + (title === 'AI summary' ? ' ai' : '');
  const heading = document.createElement('h3');
  if (CONTEXT_CARD_ICONS[title]) heading.append(createIcon(CONTEXT_CARD_ICONS[title], 'icon icon-sm'));
  heading.append(document.createTextNode(title));
  card.append(heading);
  const available = entries.filter(([, value]) => value !== null && value !== undefined && value !== '');
  if (!available.length) {
    const empty = document.createElement('div');
    empty.className = 'context-empty';
    empty.textContent = 'No information available.';
    card.append(empty);
  } else if (summary) {
    const copy = document.createElement('div');
    copy.className = 'context-summary';
    copy.dir = 'auto';
    copy.textContent = contextValue(available[0][1]);
    card.append(copy);
  } else {
    const list = document.createElement('dl');
    list.className = 'context-list';
    for (const [label, value, kind] of available) {
      const item = document.createElement('div');
      item.className = 'context-item';
      const term = document.createElement('dt');
      term.textContent = label;
      const description = document.createElement('dd');
      description.dir = 'auto';
      if (value && typeof value === 'object' && typeof value.append === 'function') description.append(value);
      else description.textContent = contextValue(value);
      if (kind === 'code') description.classList.add('code');
      item.append(term, description);
      list.append(item);
    }
    card.append(list);
  }
  container.append(card);
}

async function saveCurrentContact(ticket, identity) {
  if (!ticket || !state.ticketId || state.busy) return;
  const registered = Boolean(identity && identity.registered_user);
  const isContact = Boolean(identity && identity.contact_key) || registered;
  const current = isContact ? (identity.name || ticket.customer_name || '') : '';
  const name = await promptDialog({
    title: isContact ? 'Edit contact name' : 'Save this guest as a contact',
    // Registered customers keep their account name; the dashboard name only changes what staff see.
    message: registered
      ? 'This changes the name shown in the dashboard. The customer account' + (identity.account_name ? ' (' + identity.account_name + ')' : '') + ' is not changed.'
      : '',
    label: 'Contact name',
    value: current,
    confirmLabel: isContact ? 'Save name' : 'Save contact'
  });
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed) { notify('Contact name is required.', 'error'); return; }
  state.busy = true;
  setWorkspaceBusy(true);
  try {
    await api('/api/support/tickets/' + state.ticketId + '/contact', {method:'PUT', body: JSON.stringify({name: trimmed})});
    notify('Contact saved.');
    await openTicket(state.ticketId, {silent: true, refreshQueue: true});
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
}

function renderContext(ticket, context, shipment, identity = null, permissions = null) {
  const container = $('context');
  const previousScrollTop = $('contextPanel').scrollTop;
  container.replaceChildren();
  const customer = ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'Unknown customer';
  const hero = document.createElement('div');
  hero.className = 'context-hero';
  const avatar = document.createElement('div');
  avatar.className = 'avatar customer large';
  avatar.setAttribute('aria-hidden', 'true');
  avatar.textContent = initials(customer);
  const copy = document.createElement('div');
  copy.className = 'context-hero-copy';
  const name = document.createElement('strong');
  name.dir = 'auto';
  name.textContent = customer;
  const detail = document.createElement('span');
  detail.dir = 'auto';
  detail.textContent = ticket.whatsapp_phone || ticket.customer_userid || 'Customer identity unavailable';
  copy.append(name, detail);
  hero.append(avatar, copy);
  if (identity && permissions && permissions.can_operate) {
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'btn small context-contact-action';
    save.append(createIcon('user', 'icon icon-sm'), document.createTextNode(identity.contact_key || identity.registered_user ? 'Edit contact' : 'Save contact'));
    save.onclick = () => saveCurrentContact(ticket, identity);
    hero.append(save);
  }
  container.append(hero);

  appendContextCard(container, 'Customer', [
    ['Name', ticket.customer_name],
    ['Account name', identity?.registered_user && identity.account_name && identity.account_name !== ticket.customer_name ? identity.account_name : null],
    ['User ID', ticket.customer_userid, 'code'],
    ['WhatsApp', ticket.whatsapp_phone],
    ['Authenticated', context.authenticated],
    ['Identity', identity?.registered_user ? 'Registered customer' : identity?.saved_contact ? 'Saved contact' : identity?.source === 'google_contact' ? 'Google contact' : identity?.source === 'vcf_contact' ? 'Imported contact (VCF)' : 'Guest']
  ]);

  if (!isStaffChat(ticket)) {
    appendContextCard(container, 'Escalation', [
      ['Reason', ticket.reason],
      ['Requested action', ticket.requested_action],
      ['Tracking reference', ticket.tracking_reference, 'code']
    ]);
    if (ticket.ai_summary) appendContextCard(container, 'AI summary', [['Summary', ticket.ai_summary]], {summary: true});
  }

  if (shipment) {
    appendContextCard(container, 'Shipment', [
      ['Status', shipment.status],
      ['Mode', shipment.mode],
      ['Description', shipment.description],
      ['Order ID', shipment.order_id, 'code'],
      ['Shipment ID', shipment.shipment_id, 'code']
    ]);
  }

  if (isStaffChat(ticket)) {
    appendContextCard(container, 'Chat', [
      ['Status', ticket.active_key ? 'Open' : 'Closed'],
      ['Owner', ticket.current_assignee_name || 'Unassigned'],
      ['WhatsApp line', createSupportNumberLabel(ticket)],
      ['Started', fmt(ticket.created_at)]
    ]);
  } else {
    appendContextCard(container, 'Ticket', [
      ['Status', formatStatus(ticket.status)],
      ['Assignee', ticket.current_assignee_name || 'Unassigned'],
      ['Received on', createSupportNumberLabel(ticket)],
      ['Created', fmt(ticket.created_at)]
    ]);
  }
  $('contextPanel').scrollTop = previousScrollTop;
}

function renderTicketHeader(ticket) {
  const customer = ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'Unknown customer';
  text($('ticketTitle'), isStaffChat(ticket) ? customer : (ticket.ticket_reference || ('Ticket #' + ticket.id)));
  $('ticketSubtitle').replaceChildren(
    document.createTextNode(isStaffChat(ticket) ? ('Chat · ' + ticket.whatsapp_phone + ' · ') : (customer + ' · Received on ')),
    createSupportNumberLabel(ticket)
  );
  text($('ticketAvatar'), initials(customer));
  $('ticketHeadingStatus').replaceChildren(conversationStatusBadge(ticket));
  const meta = $('ticketHeaderMeta');
  meta.replaceChildren();
  if (ticket.current_assignee_name) meta.append(createMetaChip('Assigned to ' + ticket.current_assignee_name, 'user'));
  else meta.append(createMetaChip('Unassigned', 'user'));
  if (ticket.tracking_reference) meta.append(createMetaChip(ticket.tracking_reference, 'truck'));
}

function renderReadOnlyState(data) {
  const banner = $('readOnlyBanner');
  const ticket = data.ticket;
  const permissions = data.permissions;
  banner.replaceChildren();
  if (permissions.is_read_only_observer && ticket.active_key) {
    banner.append(createIcon('eye'), document.createTextNode('Watching only — this conversation is assigned to ' + (ticket.current_assignee_name || 'another staff member') + '. You can follow it live, but only the current assignee can send messages.'));
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
  }
}

function actionButton(label, handler, className = '', iconName = null) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn ' + className;
  button.dataset.ticketAction = 'true';
  button.disabled = state.busy;
  if (iconName) button.append(createIcon(iconName));
  const copy = document.createElement('span');
  copy.className = 'action-label';
  copy.textContent = label;
  button.append(copy);
  button.title = label;
  button.setAttribute('aria-label', label);
  button.onclick = handler;
  return button;
}

function renderActions(data) {
  const container = $('actions');
  // Background refreshes re-render the header every few seconds. Keep the status
  // menu open (and its focused item) across a refresh of the same ticket so it
  // only closes when the agent picks an option, clicks away or switches tickets.
  const ticketKey = String(state.ticketId || '');
  const previousMenu = container.querySelector('.action-menu');
  const keepMenuOpen = Boolean(previousMenu && previousMenu.open && container.dataset.ticketId === ticketKey);
  const focusedMenuItem = keepMenuOpen && previousMenu.contains(document.activeElement)
    ? (document.activeElement.dataset.status || 'summary')
    : '';
  container.dataset.ticketId = ticketKey;
  container.replaceChildren();
  const ticket = data.ticket;
  const permissions = data.permissions;

  if (isStaffChat(ticket)) {
    if (permissions.can_transfer && ticket.active_key) container.append(actionButton('Transfer chat', openTransferModal, '', 'transfer'));
    if (permissions.can_operate && ticket.active_key) {
      container.append(actionButton('Close chat', closeStaffChat, 'danger', 'check-circle'));
    }
    if (permissions.can_delete_chat) container.append(actionButton('Delete chat', deleteStaffChat, 'danger', 'trash'));
    return;
  }

  if (permissions.can_claim) container.append(actionButton('Claim ticket', () => doAction('/claim', 'Ticket claimed.'), 'primary', 'user-check'));
  if (permissions.can_reopen && ticket.status === 'RESOLVED' && !ticket.active_key) {
    container.append(actionButton('Reopen ticket', reopenTicket, 'primary', 'refresh'));
  }
  if (permissions.can_transfer && ticket.active_key) container.append(actionButton('Transfer', openTransferModal, '', 'transfer'));
  if (permissions.can_operate && ticket.active_key) {
    const availableStatuses = ['IN_PROGRESS', 'WAITING_CUSTOMER', 'WAITING_INTERNAL'].filter(status => status !== ticket.status);
    if (availableStatuses.length) {
      const menu = document.createElement('details');
      menu.className = 'action-menu';
      const summary = document.createElement('summary');
      summary.className = 'menu-summary';
      summary.title = 'Change ticket status';
      summary.setAttribute('aria-label', 'Change ticket status');
      summary.append(createIcon('activity'), (() => { const span = document.createElement('span'); span.className = 'action-label'; span.textContent = 'Change status'; return span; })(), createIcon('chevron-down', 'icon icon-sm'));
      const panel = document.createElement('div');
      panel.className = 'action-menu-panel';
      panel.setAttribute('role', 'menu');
      for (const status of availableStatuses) {
        const button = actionButton(formatStatus(status), () => {
          menu.open = false;
          setStatus(status);
        }, 'ghost', status === 'IN_PROGRESS' ? 'activity' : 'clock');
        button.setAttribute('role', 'menuitem');
        button.dataset.status = status;
        panel.append(button);
      }
      menu.append(summary, panel);
      container.append(menu);
      if (keepMenuOpen) {
        menu.open = true;
        const focusTarget = focusedMenuItem === 'summary'
          ? summary
          : Array.from(panel.children).find(button => button.dataset.status === focusedMenuItem);
        if (focusTarget) focusTarget.focus({preventScroll: true});
      }
    }
    container.append(actionButton('Resolve', resolveTicket, 'success-action', 'check-circle'));
  }
  if (permissions.can_delete_history) {
    container.append(actionButton('Delete ticket', deleteHistoryTicket, 'danger', 'trash'));
  }
}

function setWorkspaceBusy(busy) {
  for (const control of document.querySelectorAll('[data-ticket-action="true"]')) control.disabled = busy;
  updateComposerControls();
}

function latestInboundMessageId(data) {
  const messages = Array.isArray(data && data.messages) ? data.messages : [];
  let latest = 0;
  for (const message of messages) {
    if (String(message.direction || '').toUpperCase() !== 'INBOUND') continue;
    const id = Number(message.id || 0);
    if (id > latest) latest = id;
  }
  return latest;
}

function syncConversationSearchControls() {
  const total = state.conversationSearchResults.length;
  const index = state.conversationSearchIndex;
  text($('conversationSearchCount'), total && index >= 0 ? (index + 1) + ' / ' + total : (state.conversationSearchQuery ? '0 / 0' : '—'));
  $('conversationSearchPrev').disabled = !total || index <= 0;
  $('conversationSearchNext').disabled = !total || index < 0 || index >= total - 1;
}

function openConversationSearch() {
  if (!state.ticketId) return;
  state.conversationSearchOpen = true;
  $('conversationSearchToolbar').classList.remove('hidden');
  $('conversationSearchInput').focus();
  $('conversationSearchInput').select();
  syncConversationSearchControls();
}

function closeConversationSearch(restoreFocus = true) {
  state.conversationSearchOpen = false;
  state.conversationSearchQuery = '';
  state.conversationSearchResults = [];
  state.conversationSearchIndex = -1;
  state.searchTargetMessageId = null;
  if (state.conversationSearchTimer) { clearTimeout(state.conversationSearchTimer); state.conversationSearchTimer = null; }
  const toolbar = $('conversationSearchToolbar');
  if (toolbar) toolbar.classList.add('hidden');
  if ($('conversationSearchInput')) $('conversationSearchInput').value = '';
  syncConversationSearchControls();
  const activeTicketId = state.ticketId;
  if (restoreFocus) {
    $('conversationSearchBtn')?.focus();
    if (activeTicketId) openTicket(activeTicketId, {silent: true});
  }
}

async function searchCurrentConversation({focusMessageId = null, preserveQuery = false} = {}) {
  if (!state.ticketId) return;
  const query = preserveQuery ? state.conversationSearchQuery : $('conversationSearchInput').value.trim();
  state.conversationSearchQuery = query;
  if (query.length < 2) {
    state.conversationSearchResults = [];
    state.conversationSearchIndex = -1;
    syncConversationSearchControls();
    if (state.currentTicketData) renderConversation(state.currentTicketData, {forceScroll: false});
    return;
  }
  try {
    const data = await api('/api/support/search/messages?q=' + encodeURIComponent(query) + '&ticket_id=' + encodeURIComponent(state.ticketId) + '&limit=100');
    const chronological = (Array.isArray(data.items) ? data.items : []).slice().reverse();
    state.conversationSearchResults = chronological;
    let index = chronological.length ? chronological.length - 1 : -1;
    if (focusMessageId) {
      const found = chronological.findIndex(item => Number(item.message_id) === Number(focusMessageId));
      if (found >= 0) index = found;
    }
    state.conversationSearchIndex = index;
    syncConversationSearchControls();
    if (index >= 0) await jumpToConversationSearchResult(index);
  } catch (error) {
    showError(error.message);
  }
}

function scheduleConversationSearch() {
  if (state.conversationSearchTimer) clearTimeout(state.conversationSearchTimer);
  state.conversationSearchTimer = setTimeout(() => searchCurrentConversation(), 220);
}

async function jumpToConversationSearchResult(index) {
  const item = state.conversationSearchResults[index];
  if (!item || !state.ticketId) return;
  state.conversationSearchIndex = index;
  state.searchTargetMessageId = Number(item.message_id);
  syncConversationSearchControls();
  await openTicket(state.ticketId, {silent: true, aroundMessageId: state.searchTargetMessageId, preserveSearch: true});
  const target = $('conversation').querySelector('[data-message-id="' + state.searchTargetMessageId + '"]');
  if (target) {
    target.classList.add('search-target');
    target.scrollIntoView({block: 'center', behavior: 'smooth'});
  }
}

function moveConversationSearch(delta) {
  const next = state.conversationSearchIndex + delta;
  if (next < 0 || next >= state.conversationSearchResults.length) return;
  jumpToConversationSearchResult(next);
}

const TICKET_PREFETCH_TTL_MS = 5000;
const TICKET_HOVER_PREFETCH_DELAY_MS = 120;

function ticketOpenPath(id, conversationOrigin) {
  // Mirrors the request openTicket makes for a newly selected conversation.
  const origin = conversationOrigin || (state.view === 'chats' ? 'STAFF' : 'CUSTOMER');
  const base = String(origin).toUpperCase() === 'STAFF' ? '/api/support/chats/' : '/api/support/tickets/';
  return base + id + '?include_previous_history=true';
}

function prefetchTicketDetail(id, conversationOrigin) {
  if (!id || String(id) === String(state.ticketId || '')) return;
  const path = ticketOpenPath(id, conversationOrigin);
  const now = Date.now();
  for (const [key, entry] of state.ticketPrefetch) {
    if (now - entry.at > TICKET_PREFETCH_TTL_MS) state.ticketPrefetch.delete(key);
  }
  if (state.ticketPrefetch.has(path)) return;
  // Settle into a result object so an unused prefetch never raises an
  // unhandled rejection; a failed prefetch is simply retried by openTicket.
  const result = api(path).then(data => ({ok: true, data}), () => ({ok: false}));
  state.ticketPrefetch.set(path, {at: now, result});
}

function takeTicketPrefetch(path) {
  const entry = state.ticketPrefetch.get(path);
  if (!entry) return null;
  state.ticketPrefetch.delete(path);
  if (Date.now() - entry.at > TICKET_PREFETCH_TTL_MS) return null;
  return entry.result.then(outcome => outcome.ok ? outcome.data : api(path));
}

function showTicketOpening(id) {
  // Answer the tap immediately: show the selected ticket's header from its queue
  // row and a conversation placeholder while the full details load.
  const summary = state.view === 'contacts' ? null : (state.queueItems || []).find(item => String(item.id) === String(id));
  if (summary) {
    renderTicketHeader(summary);
  } else {
    text($('ticketTitle'), 'Opening conversation…');
    text($('ticketSubtitle'), '');
    $('ticketHeadingStatus').replaceChildren();
    $('ticketHeaderMeta').replaceChildren();
  }
  $('actions').replaceChildren();
  $('readOnlyBanner').classList.add('hidden');
  const placeholder = document.createElement('div');
  placeholder.className = 'conversation-skeleton';
  placeholder.setAttribute('aria-hidden', 'true');
  for (const variant of ['', 'outbound', 'tall', 'outbound', '']) {
    const bubble = document.createElement('div');
    bubble.className = 'skeleton ' + variant;
    placeholder.append(bubble);
  }
  $('conversation').replaceChildren(placeholder);
}

async function openTicket(id, {silent = false, refreshQueue = false, refreshHistory = false, aroundMessageId = null, preserveSearch = false, conversationOrigin = null, restoring = false} = {}) {
  const isNewSelection = String(state.ticketId || '') !== String(id || '');
  if (!isNewSelection && silent && !refreshHistory && state.previousHistoryPending) return;
  if (isNewSelection) {
    clearMessageSelection();
    state.pendingConversationRender = null;
    state.currentTicketData = null;
    setComposerEnabled(false);
    cancelMessageEdit(false);
    if (state.mediaRecorder) cancelVoiceRecording();
    clearMediaDraft();
    clearReplyTarget();
    $('message').value = '';
    updateComposerCount();
    state.timelineSignature = '';
    state.previousTicketHistory = [];
    state.replyHistoryBoundary = null;
    state.previousTicketHistoryHasMore = false;
    state.previousHistoryExpanded = false;
    state.previousHistoryLoaded = false;
    state.previousHistoryPending = false;
    state.historyJumpActive = false;
    state.historyJumpReachedTop = false;
    updateSharedHistoryShortcut();
    $('scrollToLatestBtn').classList.add('hidden');
    state.cachedCurrentMessages = [];
    state.currentMessagesHasMore = false;
    state.currentMessagesExpanded = false;
    state.historyLoadBusy = false;
    state.historyLoadRequest += 1;
    state.historySelectionEpoch += 1;
    if (!preserveSearch) closeConversationSearch(false);
    showTicketOpening(id);
  }
  state.ticketId = id;
  if (conversationOrigin) state.currentConversationOrigin = conversationOrigin;
  else if (isNewSelection) state.currentConversationOrigin = state.view === 'chats' ? 'STAFF' : 'CUSTOMER';
  setMobileTicketOpen(true);
  // Keep the desktop Customer Details panel in the state chosen by the agent.
  // Background polling reuses openTicket(), so closing it unconditionally here
  // caused the panel to collapse every few seconds. On compact/mobile layouts
  // it remains an overlay, so close it only when switching to another ticket.
  if (isNewSelection && window.innerWidth <= 1100) closeContextPanel(false);
  renderQueue();
  showError('');
  const requestId = ++state.ticketRequest;
  const needPreviousHistory = isNewSelection || refreshHistory || !state.previousHistoryLoaded || state.previousHistoryPending;
  if (needPreviousHistory) state.previousHistoryPending = true;
  if (isNewSelection || !silent) $('workspaceLoading').classList.remove('hidden');
  try {
    const chatRequest = String(state.currentConversationOrigin || '').toUpperCase() === 'STAFF';
    const detailBase = chatRequest ? '/api/support/chats/' : '/api/support/tickets/';
    const detailParams = new URLSearchParams();
    if (aroundMessageId) detailParams.set('around_message_id', String(aroundMessageId));
    if (needPreviousHistory) detailParams.set('include_previous_history', 'true');
    const detailPath = detailBase + id + (detailParams.toString() ? ('?' + detailParams.toString()) : '');
    const data = await (takeTicketPrefetch(detailPath) || api(detailPath));
    if (requestId !== state.ticketRequest || String(state.ticketId) !== String(id)) return;
    if (!silent) api('/api/support/statistics/tickets/' + id + '/view', {
      method: 'POST', body: JSON.stringify({request_id: crypto.randomUUID()})
    }).catch(() => {});
    if (needPreviousHistory && Array.isArray(data.previous_ticket_history)) {
      state.previousTicketHistory = state.previousHistoryExpanded
        ? mergePreviousHistoryThreads(state.previousTicketHistory, data.previous_ticket_history)
        : data.previous_ticket_history;
      if (!state.previousHistoryExpanded) state.previousTicketHistoryHasMore = Boolean(data.previous_ticket_history_has_more);
      state.previousHistoryLoaded = true;
    }
    data.previous_ticket_history = state.previousTicketHistory;
    data.previous_ticket_history_has_more = state.previousTicketHistoryHasMore;
    updateSharedHistoryShortcut();
    if (!aroundMessageId) {
      if (isNewSelection || !state.cachedCurrentMessages.length) {
        state.cachedCurrentMessages = Array.isArray(data.messages) ? data.messages.slice() : [];
        state.currentMessagesHasMore = Boolean(data.messages_has_more);
      } else {
        state.cachedCurrentMessages = mergeTimelineMessages(state.cachedCurrentMessages, data.messages);
        if (!state.currentMessagesExpanded) state.currentMessagesHasMore = Boolean(data.messages_has_more);
      }
      data.messages = state.cachedCurrentMessages.slice();
      data.messages_has_more = state.currentMessagesHasMore;
    }
    mergeLiveMessages(data, id);
    state.currentTicketData = data;
    if (state.replyTarget && (data.messages || []).some(item => Number(item.id) === state.replyTarget.id && item.deleted_for_everyone_at)) {
      clearReplyTarget();
      notify('The replied-to message was deleted.', 'error');
    }
    const ticket = data.ticket;
    state.currentConversationOrigin = ticket?.conversation_origin || state.currentConversationOrigin;
    saveSupportSelection();
    const inboundId = latestInboundMessageId(data);
    if (inboundId) state.lastInboundByTicket[String(id)] = inboundId;
    renderTicketHeader(ticket);
    renderContext(ticket, data.context || {}, data.shipment || null, data.customer_identity || null, data.permissions || null);
    renderReadOnlyState(data);
    renderActions(data);
    const signature = timelineSignature(data);
    if (signature !== state.timelineSignature || isNewSelection || aroundMessageId) {
      renderConversation(data, {forceScroll: isNewSelection && !aroundMessageId, preserveAudio: !isNewSelection});
      state.timelineSignature = signature;
    }
    if (aroundMessageId) {
      const target = $('conversation').querySelector('[data-message-id="' + aroundMessageId + '"]');
      if (target) {
        target.classList.add('search-target');
        target.scrollIntoView({block: 'center', behavior: 'smooth'});
      }
    }
    setComposerEnabled(Boolean(data.permissions.can_reply));
    if (data.permissions.can_reply && isStaffChat(ticket) && !ticket.active_key) {
      text($('composerHelp'), 'Sending will reopen this chat and preserve its full history.');
    }
    if ((isNewSelection && !restoring) || !silent || refreshQueue) await loadQueue({silent: true});
    if (isNewSelection) setAppStatus(isStaffChat(ticket) ? ('Opened chat with ' + (ticket.customer_name || ticket.whatsapp_phone || 'customer') + '.') : ('Opened ' + (ticket.ticket_reference || 'ticket') + ' for ' + (ticket.customer_name || ticket.customer_userid || ticket.whatsapp_phone || 'customer') + '.'));
  } catch (e) {
    if (state.pageLeaving || requestId !== state.ticketRequest) return;
    if (restoring && [403, 404].includes(e.status)) {
      clearTicketWorkspace();
      await loadQueue({silent: true});
      showError('The saved conversation is no longer available. Your queue is still open.');
      return;
    }
    if(e.status===403) {
      state.currentTicketData = null;
      state.ticketId = null;
      state.previousTicketHistory = [];
      state.previousTicketHistoryHasMore = false;
      state.historyJumpActive = false;
      state.historyJumpReachedTop = false;
      updateSharedHistoryShortcut();
      $('scrollToLatestBtn').classList.add('hidden');
      state.timelineSignature = '';
      state.currentConversationOrigin = null;
      saveSupportSelection();
      setComposerEnabled(false);
      clearMediaDraft();
      text($('ticketTitle'), 'Private conversation');
      text($('ticketSubtitle'), 'Conversation access is restricted to participating staff.');
      $('ticketHeadingStatus').replaceChildren();
      $('ticketHeaderMeta').replaceChildren();
      $('conversation').innerHTML = '<div class="conversation-empty"><div class="empty-state-inner"><div class="empty-icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-lock"></use></svg></div><h3>Conversation access restricted</h3><p>You can only open conversations you are allowed to access.</p></div></div>';
      $('context').replaceChildren();
      $('actions').replaceChildren();
      $('readOnlyBanner').classList.add('hidden');
      await loadQueue({silent: true});
    } else if (e.status === 404) {
      clearTicketWorkspace();
      await loadQueue({silent: true});
    } else if (isNewSelection) {
      // Replace the loading placeholder so a failed open does not look like it is still loading.
      $('conversation').replaceChildren(emptyMarkup('Could not open this conversation', 'Check your connection, then select the conversation again.', 'alert').outer);
    }
    showError(e.message);
  } finally {
    if (requestId === state.ticketRequest) {
      state.previousHistoryPending = false;
      $('workspaceLoading').classList.add('hidden');
    }
  }
}

async function doAction(suffix, successMessage = 'Ticket updated.') {
  if (state.busy || !state.ticketId) return;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  let succeeded = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + suffix, {method:'POST'});
    succeeded = true;
    notify(successMessage);
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
  if (!succeeded) return;
  // A claimed ticket now belongs to this agent: follow it into My tickets so the
  // queue shows where it went. openTicket's refreshQueue loads the new view.
  if (suffix === '/claim') setQueueView('mine', {load: false});
  await openTicket(state.ticketId, {silent: true, refreshQueue: true});
}

async function setStatus(status) {
  if (state.busy || !state.ticketId) return;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  let succeeded = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + '/status', {method:'POST', body: JSON.stringify({status})});
    succeeded = true;
    notify('Ticket status changed to ' + formatStatus(status) + '.');
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
  if (succeeded) await openTicket(state.ticketId, {silent: true, refreshQueue: true});
}

async function resolveTicket() {
  if (!(await confirmDialog({title: 'Resolve ticket?', message: 'Resolve this ticket and return the WhatsApp conversation to AI?', confirmLabel: 'Resolve', tone: 'success'}))) return;
  await doAction('/resolve', 'Ticket resolved and returned to AI.');
}

async function reopenTicket() {
  const data = state.currentTicketData;
  if (state.busy || !state.ticketId || !data?.permissions?.can_reopen) return;
  const ticketId = state.ticketId;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  let reopened = null;
  try {
    reopened = await api('/api/support/tickets/' + ticketId + '/reopen', {
      method: 'POST', body: JSON.stringify({resolved_at: data.ticket.resolved_at || null})
    });
    notify('Ticket reopened. Conversation history is preserved.');
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
  if (!reopened || String(state.ticketId) !== String(ticketId)) return;
  setQueueView(String(reopened.ticket.current_assignee_id) === String(state.agent?.id) ? 'mine' : 'active', {load: false});
  // Composer permissions come from the refreshed backend detail only.
  await openTicket(ticketId, {silent: true, refreshQueue: true});
}

async function closeStaffChat() {
  const data = state.currentTicketData;
  if (state.busy || !state.ticketId || !isStaffChat(data?.ticket)) return;
  if (!(await confirmDialog({title: 'Close chat?', message: 'Close this chat? Its message history will stay in Chats and can be reopened later.', confirmLabel: 'Close chat', tone: 'danger'}))) return;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  try {
    await api('/api/support/chats/' + state.ticketId + '/close', {method:'POST'});
    notify('Chat closed. Its history is preserved and will be reused if you start this chat again.');
    await openTicket(state.ticketId, {silent: true, refreshQueue: true, conversationOrigin: 'STAFF'});
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
}

async function deleteHistoryTicket() {
  const data = state.currentTicketData;
  if (state.busy || !state.ticketId || !data?.permissions?.can_delete_history) return;
  const reference = data.ticket?.ticket_reference || ('Ticket #' + state.ticketId);
  if (!(await confirmDialog({title: 'Delete from History?', message: 'Permanently delete ' + reference + ' from History?\n\nThis deletes its saved messages, events, and locally stored attachments. This action cannot be undone.', confirmLabel: 'Delete permanently', tone: 'danger'}))) return;

  const ticketId = state.ticketId;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  try {
    await api('/api/support/tickets/' + ticketId, {method:'DELETE'});
    notify(reference + ' deleted from History.');
    clearTicketWorkspace();
    await loadQueue();
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
}

async function deleteStaffChat() {
  const data = state.currentTicketData;
  if (state.busy || !state.ticketId || !data?.permissions?.can_delete_chat) return;
  const customer = data.ticket?.customer_name || data.ticket?.whatsapp_phone || 'this customer';
  if (!(await confirmDialog({title: 'Delete chat?', message: 'Permanently delete the chat with ' + customer + '?\n\nThis deletes its saved messages, events, and locally stored attachments from the dashboard. Messages already delivered to the customer stay on their WhatsApp. This action cannot be undone.', confirmLabel: 'Delete permanently', tone: 'danger'}))) return;

  const chatId = state.ticketId;
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  try {
    await api('/api/support/chats/' + chatId, {method:'DELETE'});
    notify('Chat deleted.');
    clearTicketWorkspace();
    await loadQueue();
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
  } finally {
    state.busy = false;
    setWorkspaceBusy(false);
  }
}
