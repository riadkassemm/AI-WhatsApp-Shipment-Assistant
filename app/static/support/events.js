// Update display metadata only: message delivery state, drafts and ownership stay intact.
function updateCachedStaffName(value, staffId, name) {
  if (!value || typeof value !== 'object') return;
  const same = id => id != null && String(id) === String(staffId);
  for (const prefix of ['current_assignee', 'initiated_by_staff']) {
    if (same(value[prefix + '_id'])) value[prefix + '_name'] = name;
  }
  for (const prefix of ['sender', 'actor', 'reactor']) {
    if (value[prefix + '_type'] === 'AGENT' && same(value[prefix + '_id'])) value[prefix + '_name'] = name;
  }
  if (value.event_type === 'TICKET_TRANSFERRED' && value.details_json) {
    try {
      const details = JSON.parse(value.details_json);
      for (const prefix of ['from_agent', 'to_agent']) if (same(details[prefix + '_id'])) details[prefix + '_name'] = name;
      value.details_json = JSON.stringify(details);
    } catch (_) {}
  }
  for (const child of Object.values(value)) if (child && typeof child === 'object') updateCachedStaffName(child, staffId, name);
}

function applyStaffProfileName(staffId, name) {
  if (!state.authenticated || !staffId || typeof name !== 'string') return;
  state.ticketPrefetch.clear();
  if (String(state.agent?.id) === String(staffId)) {
    state.agent.name = name;
    setAgentIdentity(state.agent);
  }
  for (const value of [state.currentTicketData, state.queueItems, state.previousTicketHistory,
    state.cachedCurrentMessages, state.globalSearchResults, state.conversationSearchResults,
    state.forwardConversations, ...state.realtimeMessages.values(), ...state.localMessages.values()]) {
    updateCachedStaffName(value, staffId, name);
  }
  for (const item of [...state.staffItems, ...state.transferTargets]) if (String(item.id) === String(staffId)) item.name = name;
  for (const option of $('transferTarget').options) {
    const target = state.transferTargets.find(item => String(item.id) === option.value);
    if (target) option.textContent = target.name + ' · ' + staffRoleLabel(target.role);
  }
  if (canManageStaff()) renderStaffAccounts(state.staffItems);
  renderQueue();
  const data = state.currentTicketData;
  if (data) {
    renderTicketHeader(data.ticket);
    renderContext(data.ticket, data.context || {}, data.shipment || null, data.customer_identity || null, data.permissions || {});
    renderReadOnlyState(data);
    renderConversation(data, {preserveAudio: true});
    state.timelineSignature = timelineSignature(data);
    const reply = (data.messages || []).find(message => Number(message.id) === state.replyTarget?.id);
    if (reply) { state.replyTarget.actor = messageActor(reply); renderReplyDraft(); }
  }
  if (state.activeModal === 'agentStatisticsModal') loadAgentStatistics();
}

function disconnectSupportEvents() {
  if (state.supportEventSource) {
    state.supportEventSource.close();
    state.supportEventSource = null;
  }
}

function connectSupportEvents() {
  disconnectSupportEvents();
  if (!state.authenticated || typeof EventSource === 'undefined') return;
  const source = new EventSource('/api/support/events', {withCredentials: true});
  state.supportEventSource = source;
  source.onmessage = event => {
    if (state.supportEventSource !== source || !state.authenticated) return;
    let payload = {};
    try { payload = JSON.parse(event.data || '{}'); } catch (_) { return; }
    const type = String(payload.type || '');
    if (type === 'staff_profile_updated') {
      if (payload.authorized === true) applyStaffProfileName(payload.staff_id, payload.name);
      return;
    }
    if (type === 'ticket_queues_changed') {
      if (payload.authorized !== true) return;
      state.ticketPrefetch.clear();
      state.previousTicketHistory = [];
      state.previousHistoryExpanded = false;
      state.previousHistoryLoaded = false;
      requestDashboardRefresh('counts');
      requestDashboardRefresh('queue');
      if (state.ticketId) requestDashboardRefresh('history');
      return;
    }
    if (type === 'incoming_read' || type === 'dashboard_read') {
      if (payload.authorized !== true) return;
      state.ticketPrefetch.delete(String(payload.ticket_id));
      requestDashboardRefresh('queue');
      if (String(state.ticketId) === String(payload.ticket_id)) requestDashboardRefresh('conversation');
      return;
    }
    if (type === 'message_status') {
      if (payload.authorized === true && payload.message && state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) {
        upsertTimelineMessage(payload.message, payload.ticket_id);
      }
      return;
    }
    if (type === 'outbound_message' || type === 'customer_message' || type === 'message_media_ready') {
      if (payload.authorized !== true) return;
      if (payload.message && state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) {
        upsertTimelineMessage(payload.message, payload.ticket_id);
      } else if (state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) {
        requestDashboardRefresh('conversation');
      } else if (type === 'message_media_ready' && state.previousTicketHistory.some(thread => String(thread.ticket?.id) === String(payload.ticket_id))) {
        if (payload.message) {
          changeCachedMessage(payload.ticket_id, payload.message.id, payload.message);
          if (state.currentTicketData) {
            state.timelineSignature = timelineSignature(state.currentTicketData);
            renderConversation(state.currentTicketData);
          }
        } else if (state.ticketId) requestDashboardRefresh('history');
      }
      if (type === 'outbound_message') {
        // Messages sent from the linked WhatsApp app can change a ticket's
        // preview and place in the queue even when another ticket is open.
        // Refreshing only the queue leaves any playing voice note untouched.
        requestDashboardRefresh('counts');
        if (state.view !== 'contacts') requestDashboardRefresh('queue');
        return;
      }
      if (type !== 'customer_message') return;
      requestDashboardRefresh('queue');
      if (payload.conversation_origin === 'STAFF') {
        handleAssignedChatNotification(payload);
        return;
      }
    }
    requestDashboardRefresh('counts');
    if (type === 'ticket_updated') {
      if (payload.authorized !== true) return;
      if (state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) {
        requestDashboardRefresh('conversation');
      }
      requestDashboardRefresh('queue');
      return;
    }
    if (type === 'contacts_synced' || type === 'contact_saved') {
      if (!payload.staff_id || String(payload.staff_id) === String(state.agent?.id || '')) {
        if (state.view === 'contacts') requestDashboardRefresh('queue');
        loadGoogleContactsStatus().catch(() => {});
      }
      return;
    }
    if (['message_updated', 'message_deleted_for_everyone', 'message_deleted_for_me'].includes(type)) {
      if (payload.authorized === true) {
        const changedId = Number(payload.message_id);
        state.realtimeMessages.delete(String(payload.ticket_id) + ':' + changedId);
        if (type === 'message_deleted_for_everyone' && state.editTarget?.id === changedId && String(state.editTarget.ticketId) === String(payload.ticket_id)) {
          cancelMessageEdit();
          notify('The message you were editing was deleted on WhatsApp.', 'error');
        }
        if (type === 'message_deleted_for_me') {
          state.realtimeMessages.delete(String(payload.ticket_id) + ':' + changedId);
          changeCachedMessage(payload.ticket_id, changedId, null, true);
        }
        if (type !== 'message_updated' && state.mediaSource && state.mediaSource.includes('/messages/' + changedId + '/media')) {
          if (state.imageEditor) notify('The original image was deleted. Your edits remain open, but it cannot be sent.', 'error');
          else closeMediaViewer();
        }
        refreshMessageSearchAfterChange(changedId);
        if (state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) {
          if (payload.message && type !== 'message_deleted_for_me') {
            upsertTimelineMessage(payload.message, payload.ticket_id);
            if (type === 'message_deleted_for_everyone' && state.replyTarget?.id === changedId) clearReplyTarget();
          } else requestDashboardRefresh('conversation');
        } else if (state.previousTicketHistory.some(thread => String(thread.ticket?.id) === String(payload.ticket_id))) {
          if (payload.message && type !== 'message_deleted_for_me') changeCachedMessage(payload.ticket_id, changedId, payload.message);
          requestDashboardRefresh('history');
        }
      }
      return;
    }
    if (!['ticket_created', 'customer_message', 'message_reaction'].includes(type)) return;
    // Ticket/chat events are only actionable when the per-session backend SSE
    // authorizer approved them. This prevents future event types from accidentally
    // reintroducing client-side-only notification filtering.
    if (payload.authorized !== true) return;
    if (type === 'ticket_created' && payload.phone_initiated === true) {
      // Started in the WhatsApp app, so someone is already talking to the
      // customer. It goes to Active without an unassigned alert.
      if (state.view !== 'contacts') requestDashboardRefresh('queue');
      return;
    }
    const customer = payload.customer_name || payload.customer_phone || 'customer';
    const notificationText = type === 'ticket_created'
      ? 'New unassigned conversation from ' + customer + '.'
      : type === 'message_reaction'
        ? 'New WhatsApp reaction from ' + customer + '.'
        : 'New customer message from ' + customer + '.';
    notifyNewCustomerActivity(notificationText);
    state.unassignedSnapshot = null;
    if (state.view === 'unassigned') requestDashboardRefresh('queue');
    else requestDashboardRefresh('counts');
    if (type !== 'customer_message' && state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) requestDashboardRefresh('conversation');
  };
  source.onopen = () => requestDashboardRefresh('queue', 'conversation', 'counts');
  source.onerror = () => {
    // EventSource retries automatically. Polling remains active as a fallback.
  };
}

function handleAssignedChatNotification(payload) {
  if (payload.authorized !== true || payload.chat_notification !== true ||
      String(payload.current_assignee_id || '') !== String(state.agent?.id || '') ||
      payload.message?.direction !== 'INBOUND' || !payload.message_id) return;
  const key = String(state.agent.id) + ':' + payload.ticket_id + ':' + payload.message_id;
  if (state.chatNotificationIds.has(key)) return;
  state.chatNotificationIds.add(key);
  // Preserve duplicate suppression across page refreshes, without storing bodies.
  try {
    const storageKey = 'support-chat-notifications:' + state.agent.id;
    const seen = JSON.parse(sessionStorage.getItem(storageKey) || '[]');
    if (Array.isArray(seen) && seen.includes(key)) return;
    sessionStorage.setItem(storageKey, JSON.stringify([...(Array.isArray(seen) ? seen : []), key].slice(-500)));
  } catch (_) {}
  if (state.chatNotificationIds.size > 1000) state.chatNotificationIds.delete(state.chatNotificationIds.values().next().value);
  const label = payload.customer_name || payload.customer_phone || 'customer';
  const message = 'New chat message from ' + label + '.';
  notifyNewCustomerActivity(message);
  notify(message, 'success', () => {
    setQueueView('chats');
    openTicket(payload.ticket_id, {conversationOrigin: 'STAFF'}).catch(() => {});
  });
}

let dashboardReadTimer = null;
function scheduleDashboardRead() {
  if (dashboardReadTimer) clearTimeout(dashboardReadTimer);
  dashboardReadTimer = setTimeout(markVisibleDashboardMessagesRead, 150);
}

async function markVisibleDashboardMessagesRead() {
  if (!state.authenticated || document.visibilityState !== 'visible' || !document.hasFocus() ||
      state.activeModal || state.historyJumpActive || !state.ticketId) return;
  const container = $('conversation');
  const bounds = container.getBoundingClientRect();
  if (!bounds.width || !bounds.height || bounds.right <= 0 || bounds.left >= window.innerWidth ||
      bounds.bottom <= 0 || bounds.top >= window.innerHeight) return;
  const data = state.currentTicketData;
  const ticketId = state.ticketId;
  if (String(data?.ticket?.id) !== String(ticketId)) return;
  const incoming = new Set((data.messages || []).filter(m => m.direction === 'INBOUND' && !m.deleted_for_everyone_at).map(m => String(m.id)));
  let boundary = 0;
  for (const node of container.querySelectorAll('[data-message-id]')) {
    if (!incoming.has(node.dataset.messageId)) continue;
    const rect = node.getBoundingClientRect();
    if (rect.height && rect.bottom > bounds.top && rect.top < bounds.bottom) boundary = Math.max(boundary, Number(node.dataset.messageId));
  }
  const key = String(state.agent.id) + ':' + ticketId;
  if (!boundary || boundary <= (state.dashboardReadBoundaries.get(key) || 0)) return;
  try {
    const result = await api('/api/support/tickets/' + ticketId + '/dashboard-read', {
      method: 'POST', body: JSON.stringify({message_id: boundary})
    });
    if (key !== String(state.agent?.id) + ':' + ticketId) return;
    state.dashboardReadBoundaries.set(key, Math.max(boundary, state.dashboardReadBoundaries.get(key) || 0));
    // Fetch rather than clearing optimistically: a newer arrival may remain unread.
    if (result && state.authenticated) requestDashboardRefresh('queue');
  } catch (_) {
    // Failed requests do not clear the dot; the next visible refresh retries.
  }
}
