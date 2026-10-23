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
      refreshAllQueueCounts().catch(() => {});
      loadQueue({silent: true}).catch(() => {});
      if (state.query.trim().length >= 2) runGlobalMessageSearch().catch(() => {});
      if (state.ticketId) openTicket(state.ticketId, {silent: true, refreshHistory: true}).catch(() => {});
      return;
    }
    if (type === 'incoming_read') {
      if (payload.authorized !== true) return;
      state.ticketPrefetch.delete(String(payload.ticket_id));
      loadQueue({silent: true}).catch(() => {});
      if (String(state.ticketId) === String(payload.ticket_id)) openTicket(state.ticketId, {silent: true}).catch(() => {});
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
        openTicket(state.ticketId, {silent: true}).catch(() => {});
      } else if (type === 'message_media_ready' && state.previousTicketHistory.some(thread => String(thread.ticket?.id) === String(payload.ticket_id))) {
        if (payload.message) {
          changeCachedMessage(payload.ticket_id, payload.message.id, payload.message);
          if (state.currentTicketData) {
            state.timelineSignature = timelineSignature(state.currentTicketData);
            renderConversation(state.currentTicketData);
          }
        } else if (state.ticketId) openTicket(state.ticketId, {silent: true, refreshHistory: true}).catch(() => {});
      }
      if (type === 'outbound_message') {
        // Messages sent from the linked WhatsApp app can change a ticket's
        // preview and place in the queue even when another ticket is open.
        // Refreshing only the queue leaves any playing voice note untouched.
        refreshAllQueueCounts().catch(() => {});
        if (state.view !== 'contacts') loadQueue({silent: true}).catch(() => {});
        return;
      }
      if (type !== 'customer_message') return;
    }
    refreshAllQueueCounts().catch(() => {});
    if (type === 'ticket_updated') {
      if (payload.authorized !== true) return;
      if (state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) {
        openTicket(state.ticketId, {silent: true}).catch(() => {});
      }
      loadQueue({silent: true}).catch(() => {});
      return;
    }
    if (type === 'contacts_synced' || type === 'contact_saved') {
      if (!payload.staff_id || String(payload.staff_id) === String(state.agent?.id || '')) {
        if (state.view === 'contacts') loadQueue({silent: true}).catch(() => {});
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
          } else openTicket(state.ticketId, {silent: true}).catch(() => {});
        } else if (state.previousTicketHistory.some(thread => String(thread.ticket?.id) === String(payload.ticket_id))) {
          if (payload.message && type !== 'message_deleted_for_me') changeCachedMessage(payload.ticket_id, changedId, payload.message);
          openTicket(state.ticketId, {silent: true, refreshHistory: true}).catch(() => {});
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
      if (state.view !== 'contacts') loadQueue({silent: true}).catch(() => {});
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
    if (state.view === 'unassigned') loadQueue({silent: true}).catch(() => {});
    else pollUnassignedActivity().catch(() => {});
    if (type !== 'customer_message' && state.ticketId && String(state.ticketId) === String(payload.ticket_id || '')) openTicket(state.ticketId, {silent: true}).catch(() => {});
  };
  source.onerror = () => {
    // EventSource retries automatically. Polling remains active as a fallback.
  };
}
