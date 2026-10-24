const TEXT_SEND_RETRY_DELAYS_MS = [1500, 4000];

function transientSendError(error) {
  return !error.status || ([502, 503, 504].includes(error.status) && !error.detail);
}

async function deliverTextMessage(message, ticketId) {
  const clientMessageId = message.client_message_id;
  state.localMessages.set(clientMessageId, {ticketId, message});
  for (let attempt = 0; ; attempt++) {
    let result;
    try {
      result = await api('/api/support/tickets/' + ticketId + '/messages', {
        method: 'POST',
        body: JSON.stringify({body: message.body, client_message_id: clientMessageId, reply_to_message_id: message.reply_to_message_id || null})
      });
    } catch (error) {
      if (transientSendError(error) && attempt < TEXT_SEND_RETRY_DELAYS_MS.length && !state.pageLeaving) {
        await new Promise(resolve => setTimeout(resolve, TEXT_SEND_RETRY_DELAYS_MS[attempt]));
        continue;
      }
      message.send_status = 'FAILED';
      message.error_message = transientSendError(error)
        ? 'The dashboard could not reach the server. Check your connection, then resend.'
        : (error.message || 'Message could not be sent.');
      if (state.localMessages.has(clientMessageId)) upsertTimelineMessage(message, ticketId);
      if (String(state.ticketId) === String(ticketId)) showError(message.error_message);
      notify(message.error_message, 'error');
      // If the server did store it, the refreshed ticket replaces this local copy.
      if (!transientSendError(error) && String(state.ticketId) === String(ticketId)) openTicket(ticketId, {silent: true}).catch(() => {});
      return;
    }
    state.localMessages.delete(clientMessageId);
    // The send succeeded. A drawing problem must never mark it as failed;
    // the next live update or refresh redraws the timeline.
    try { if (result.message) upsertTimelineMessage(result.message, ticketId); } catch (_) {}
    loadQueue({silent: true}).catch(() => {});
    return;
  }
}

function showMessagePreferencesError(message) {
  const element = $('messagePreferencesError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function updateMessagePreferenceCounts() {
  text($('customGreetingCount'), $('customGreetingMessage').value.length + ' / 2000');
  text($('customResolutionCount'), $('customResolutionMessage').value.length + ' / 2000');
}

async function loadMessagePreferences() {
  const data = await api('/api/support/message-preferences');
  $('customGreetingMessage').value = data.greeting_message || '';
  $('customResolutionMessage').value = data.resolution_message || '';
  updateMessagePreferenceCounts();
  return data;
}

async function openMessagePreferencesModal() {
  if (!state.authenticated || state.messagePreferencesBusy) return;
  showMessagePreferencesError('');
  openDialog('messagePreferencesModal', 'customGreetingMessage');
  state.messagePreferencesBusy = true;
  setButtonBusy($('saveMessagePreferencesBtn'), true, 'Loading…');
  try {
    await loadMessagePreferences();
  } catch (error) {
    showMessagePreferencesError(error.message);
  } finally {
    state.messagePreferencesBusy = false;
    setButtonBusy($('saveMessagePreferencesBtn'), false);
  }
}

function closeMessagePreferencesModal() {
  if (state.messagePreferencesBusy) return;
  showMessagePreferencesError('');
  closeDialog('messagePreferencesModal');
}

async function saveMessagePreferences(event) {
  event.preventDefault();
  if (!state.authenticated || state.messagePreferencesBusy) return;
  const form = $('messagePreferencesForm');
  if (!form.reportValidity()) return;
  state.messagePreferencesBusy = true;
  setButtonBusy($('saveMessagePreferencesBtn'), true, 'Saving…');
  showMessagePreferencesError('');
  try {
    const data = await api('/api/support/message-preferences', {
      method: 'PUT',
      body: JSON.stringify({
        greeting_message: $('customGreetingMessage').value,
        resolution_message: $('customResolutionMessage').value
      })
    });
    $('customGreetingMessage').value = data.greeting_message || '';
    $('customResolutionMessage').value = data.resolution_message || '';
    updateMessagePreferenceCounts();
    notify('Automatic messages saved.');
    closeDialog('messagePreferencesModal');
  } catch (error) {
    showMessagePreferencesError(error.message);
  } finally {
    state.messagePreferencesBusy = false;
    setButtonBusy($('saveMessagePreferencesBtn'), false);
  }
}

// Manager assignments create independent copies; existing replies stay owner-scoped.
const QUICK_REPLIES_LIST_SUBTITLE = 'Copy text, attach a PDF, or preview a saved location. Only you can see your quick replies.';

function quickReplyKind(reply) {
  return reply?.location ? 'location' : reply?.attachment_filename ? 'pdf' : 'text';
}

function quickReplyAssignmentNotice(result) {
  const added = Number(result.saved_count || 0);
  const skipped = Number(result.skipped_count || 0);
  return 'Quick reply added for ' + added + ' staff members.' +
    (skipped ? ' ' + skipped + ' already had it and were skipped.' : '');
}

function canAssignQuickReplies() {
  return ['admin', 'supervisor'].includes(state.agent?.role);
}

function selectQuickReplyTab(kind, focus = false) {
  state.quickReplyTab = kind;
  $('quickRepliesSearch').value = '';
  renderQuickReplies();
  if (focus) $('quickReplyTab' + ({text: 'Text', pdf: 'Pdf', location: 'Location'})[kind]).focus();
}

async function updateQuickReplyAudience() {
  const selected = $('quickReplyAudience').value === 'selected';
  $('quickReplyRecipients').classList.toggle('hidden', !selected);
  if (!canAssignQuickReplies() || !selected || state.quickReplyRecipientsLoading) return;
  const request = state.quickRepliesRequest;
  state.quickReplyRecipientsLoading = true;
  $('quickReplyRecipients').textContent = 'Loading staff…';
  try {
    const data = await api('/api/support/quick-replies/recipients');
    if (!state.authenticated || request !== state.quickRepliesRequest) return;
    state.quickReplyRecipients = data.items || [];
    $('quickReplyRecipients').replaceChildren();
    for (const staff of state.quickReplyRecipients) {
      if (state.quickReplySharing && String(staff.id) === String(state.agent?.id)) continue;
      const label = document.createElement('label');
      label.className = 'quick-reply-recipient';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.value = String(staff.id);
      const name = document.createElement('span');
      name.textContent = staff.name + ' · ' + staffRoleLabel(staff.role);
      label.append(checkbox, name); $('quickReplyRecipients').append(label);
    }
  } catch (error) {
    if (request === state.quickRepliesRequest) {
      $('quickReplyRecipients').textContent = 'Could not load staff. Choose Only me, then Selected staff to retry.';
      showQuickRepliesError(error.message);
    }
  } finally {
    if (request === state.quickRepliesRequest) state.quickReplyRecipientsLoading = false;
  }
}

async function saveLocationQuickReply(message) {
  if (state.activeModal || !state.authenticated) return;
  const agentId = state.agent?.id;
  const request = state.quickRepliesRequest + 1;
  await openQuickRepliesModal();
  if (!state.authenticated || state.agent?.id !== agentId || state.quickRepliesRequest !== request || state.activeModal !== 'quickRepliesModal') return;
  state.quickReplyTab = 'location';
  showQuickReplyForm(null, {title: String(message.location_name || '').slice(0, 80), location: {
    latitude: message.location_latitude, longitude: message.location_longitude, address: message.location_address || ''
  }});
}

function previewQuickReplyLocation(reply) {
  if (state.quickRepliesBusy) return;
  if (!canAttachToComposer()) { showQuickRepliesError('Open a conversation you can reply to first.'); return; }
  if (state.pendingMedia || $('message').value.trim()) { showQuickRepliesError('Send or clear your current draft first.'); return; }
  closeDialog('quickRepliesModal', false);
  openLocationModal();
  $('locationLatitude').value = reply.location.latitude;
  $('locationLongitude').value = reply.location.longitude;
  $('locationName').value = reply.location.name || reply.title;
  $('locationAddress').value = reply.location.address || '';
}

function showQuickRepliesError(message) {
  const element = $('quickRepliesError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function quickReplyLabel(reply) {
  const title = String(reply?.title || '').trim();
  if (title) return title;
  const firstLine = String(reply?.body || '').trim().split('\n')[0] || reply?.attachment_filename || 'Quick reply';
  return firstLine.length > 60 ? firstLine.slice(0, 59).trimEnd() + '…' : firstLine;
}

function resetQuickReplies() {
  state.quickReplies = [];
  state.quickRepliesLoaded = false;
  state.quickRepliesBusy = false;
  state.quickRepliesRequest += 1;
  state.quickReplyEditing = null;
  state.quickReplySharing = null;
  state.quickReplyTab = 'text';
  state.quickReplyKind = 'text';
  state.quickReplyRecipients = [];
  state.quickReplyRecipientsLoading = false;
  $('quickReplyRecipients').replaceChildren();
  $('quickRepliesSearch').value = '';
  $('quickRepliesList').replaceChildren();
  $('quickReplyPdf').value = '';
  setButtonBusy($('saveQuickReplyBtn'), false);
}

function updateQuickReplyCount() {
  const reply = state.quickReplies.find(item => String(item.id) === String(state.quickReplyEditing));
  const pdf = Boolean($('quickReplyPdf').files?.length || reply?.attachment_filename);
  $('quickReplyBody').maxLength = pdf ? 1024 : 4096;
  text($('quickReplyBodyHint'), pdf ? 'Optional PDF caption. Review before sending.' : 'Copied exactly as written.');
  text($('quickReplyBodyCount'), $('quickReplyBody').value.length + ' / ' + (pdf ? 1024 : 4096));
}

function quickReplyRow(reply) {
  const row = document.createElement('div');
  row.className = 'quick-reply-row';
  row.setAttribute('role', 'listitem');
  row.dataset.quickReplyId = String(reply.id);
  const label = quickReplyLabel(reply);
  const location = Boolean(reply.location);

  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'quick-reply-copy';
  copy.setAttribute('aria-label', (location ? 'Preview location: ' : reply.attachment_filename ? 'Attach PDF: ' : 'Copy quick reply: ') + label);
  const copyText = document.createElement('span');
  copyText.className = 'quick-reply-text';
  if (String(reply.title || '').trim()) {
    const heading = document.createElement('strong');
    heading.textContent = label;
    copyText.append(heading);
  } else {
    copyText.classList.add('untitled');
  }
  const preview = document.createElement('span');
  preview.textContent = location ? [reply.location.address, reply.location.latitude + ', ' + reply.location.longitude].filter(Boolean).join(' · ')
    : reply.attachment_filename ? reply.attachment_filename + (reply.body ? ' · ' + reply.body : '') : reply.body;
  copyText.append(preview);
  const hint = document.createElement('span');
  hint.className = 'quick-reply-copy-hint';
  hint.setAttribute('aria-hidden', 'true');
  hint.append(createIcon(location ? 'map-pin' : reply.attachment_filename ? 'paperclip' : 'copy', 'icon icon-sm'), document.createTextNode(location ? 'Preview' : reply.attachment_filename ? 'Attach PDF' : 'Copy'));
  copy.append(copyText, hint);
  copy.addEventListener('click', () => location ? previewQuickReplyLocation(reply) : reply.attachment_filename ? attachQuickReplyPdf(reply) : copyQuickReply(reply));

  const actions = document.createElement('div');
  actions.className = 'quick-reply-actions';
  const edit = document.createElement('button');
  edit.type = 'button';
  edit.className = 'icon-btn';
  edit.setAttribute('aria-label', 'Edit quick reply: ' + label);
  edit.dataset.tooltip = 'Edit';
  edit.append(createIcon('edit', 'icon icon-sm'));
  edit.addEventListener('click', () => showQuickReplyForm(reply));
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'icon-btn danger';
  remove.setAttribute('aria-label', 'Delete quick reply: ' + label);
  remove.dataset.tooltip = 'Delete';
  remove.append(createIcon('trash', 'icon icon-sm'));
  remove.addEventListener('click', () => deleteQuickReply(reply));
  actions.append(edit, remove);
  if (canAssignQuickReplies()) {
    const share = document.createElement('button');
    share.type = 'button'; share.className = 'icon-btn';
    share.setAttribute('aria-label', 'Add to staff: ' + label);
    share.dataset.tooltip = 'Add to staff';
    share.append(createIcon('users', 'icon icon-sm'));
    share.addEventListener('click', () => showQuickReplyAssignment(reply));
    actions.append(share);
  }

  row.append(copy, actions);
  return row;
}

function renderQuickReplies() {
  const list = $('quickRepliesList');
  const query = $('quickRepliesSearch').value.trim().toLocaleLowerCase();
  const tab = state.quickReplyTab || 'text';
  for (const [kind, suffix] of Object.entries({text: 'Text', pdf: 'Pdf', location: 'Location'})) {
    const button = $('quickReplyTab' + suffix);
    button.classList.toggle('active', tab === kind);
    button.setAttribute('aria-selected', String(tab === kind));
    button.tabIndex = tab === kind ? 0 : -1;
    if (tab === kind) $('quickRepliesPanel').setAttribute('aria-labelledby', button.id);
  }
  const inTab = state.quickReplies.filter(reply => quickReplyKind(reply) === tab);
  const replies = inTab.filter(reply => !query
    || String(reply.title || '').toLocaleLowerCase().includes(query)
    || String(reply.body || '').toLocaleLowerCase().includes(query)
    || String(reply.attachment_filename || '').toLocaleLowerCase().includes(query)
    || String(reply.location?.address || '').toLocaleLowerCase().includes(query)
    || String(reply.location ? reply.location.latitude + ', ' + reply.location.longitude : '').includes(query));
  list.replaceChildren();
  if (!state.quickRepliesLoaded) {
    list.append(emptyMarkup('Loading quick replies…', 'Fetching your saved messages.', 'zap').outer);
    return;
  }
  if (!inTab.length) {
    list.append(emptyMarkup('No ' + ({text: 'text replies', pdf: 'PDFs', location: 'locations'})[tab] + ' yet', 'Choose New reply to save one here.', 'zap').outer);
    return;
  }
  if (!replies.length) {
    list.append(emptyMarkup('No matching quick replies', 'Try a different word or clear the search.', 'search').outer);
    return;
  }
  for (const reply of replies) list.append(quickReplyRow(reply));
}

async function loadQuickReplies() {
  const request = ++state.quickRepliesRequest;
  $('quickRepliesList').setAttribute('aria-busy', 'true');
  try {
    const data = await api('/api/support/quick-replies');
    if (request !== state.quickRepliesRequest) return;
    state.quickReplies = Array.isArray(data.items) ? data.items : [];
    state.quickRepliesLoaded = true;
    renderQuickReplies();
  } finally {
    if (request === state.quickRepliesRequest) $('quickRepliesList').setAttribute('aria-busy', 'false');
  }
}

function showQuickReplyList() {
  if (state.quickRepliesBusy) return;
  const editing = state.quickReplyEditing;
  state.quickReplyEditing = null;
  state.quickReplySharing = null;
  showQuickRepliesError('');
  $('quickReplyForm').classList.add('hidden');
  $('quickRepliesListView').classList.remove('hidden');
  text($('quickRepliesTitle'), 'Quick replies');
  text($('quickRepliesSubtitle'), QUICK_REPLIES_LIST_SUBTITLE);
  renderQuickReplies();
  const row = editing && editing !== 'new'
    ? document.querySelector('.quick-reply-row[data-quick-reply-id="' + CSS.escape(String(editing)) + '"] .quick-reply-copy')
    : null;
  (row || (editing ? $('addQuickReplyBtn') : null))?.focus();
}

function showQuickReplyForm(reply, draft = null) {
  if (state.quickRepliesBusy) return;
  state.quickReplySharing = null;
  $('quickReplyContentFields').classList.remove('hidden');
  $('quickReplyShareSummary').classList.add('hidden');
  $('quickReplyAudienceMe').disabled = false;
  text($('quickReplyAudienceAll'), 'All active staff');
  text($('quickReplyAudienceHint'), 'Each recipient gets their own copy to edit or delete. All active staff includes you; new accounts added later are not included.');
  text($('saveQuickReplyBtn'), 'Save reply');
  state.quickReplyEditing = reply ? reply.id : 'new';
  state.quickReplyKind = reply ? quickReplyKind(reply) : state.quickReplyTab || 'text';
  const kind = state.quickReplyKind;
  const source = reply || draft;
  $('quickReplyBodyField').classList.toggle('hidden', kind === 'location');
  $('quickReplyPdfField').classList.toggle('hidden', kind !== 'pdf');
  $('quickReplyLocationFields').classList.toggle('hidden', kind !== 'location');
  text($('quickReplyTitleLabel'), kind === 'location' ? 'Location name' : 'Title (optional)');
  $('quickReplyLatitude').value = source?.location?.latitude ?? '';
  $('quickReplyLongitude').value = source?.location?.longitude ?? '';
  $('quickReplyAddress').value = source?.location?.address || '';
  $('quickReplyMapsUrl').value = '';
  $('quickReplyAssignment').classList.toggle('hidden', Boolean(reply) || !canAssignQuickReplies());
  $('quickReplyAudience').value = 'me';
  $('quickReplyRecipients').classList.add('hidden');
  $('quickReplyRecipients').replaceChildren();
  showQuickRepliesError('');
  $('quickReplyPdf').value = '';
  text($('quickReplyPdfHint'), reply?.attachment_filename ? 'Saved PDF: ' + reply.attachment_filename + '. Choose another PDF to replace it.' : 'Save a PDF to attach it to future replies. A caption is optional.');
  $('quickReplyTitle').value = source ? String(source.title || '') : '';
  $('quickReplyBody').value = reply ? String(reply.body || '') : '';
  updateQuickReplyCount();
  text($('quickRepliesTitle'), reply ? 'Edit quick reply' : 'New quick reply');
  text($('quickRepliesSubtitle'), kind === 'location' ? 'Save a named pin to preview and send to clients later.' : 'Save ' + (kind === 'pdf' ? 'a PDF' : 'text') + ' you send often.');
  $('quickRepliesListView').classList.add('hidden');
  $('quickReplyForm').classList.remove('hidden');
  $(reply && kind !== 'location' ? 'quickReplyBody' : 'quickReplyTitle').focus();
}

async function showQuickReplyAssignment(reply) {
  if (!canAssignQuickReplies() || state.quickRepliesBusy) return;
  showQuickReplyForm(reply);
  state.quickReplySharing = reply.id;
  $('quickReplyContentFields').classList.add('hidden');
  $('quickReplyShareSummary').classList.remove('hidden');
  text($('quickReplyShareSummary'), quickReplyLabel(reply) + ' · ' + ({text: 'Text', pdf: 'PDF', location: 'Location'})[quickReplyKind(reply)]);
  text($('quickRepliesTitle'), 'Add saved reply to staff');
  text($('quickRepliesSubtitle'), 'Choose staff to receive a copy of this saved reply.');
  $('quickReplyAssignment').classList.remove('hidden');
  $('quickReplyAudienceMe').disabled = true;
  $('quickReplyAudience').value = 'selected';
  text($('quickReplyAudienceAll'), 'All other active staff');
  text($('quickReplyAudienceHint'), 'Staff who already have the same title and content are skipped. Your saved reply stays unchanged.');
  text($('saveQuickReplyBtn'), 'Add to staff');
  $('quickReplyAudience').focus();
  await updateQuickReplyAudience();
}

async function shareSavedQuickReply() {
  if (!canAssignQuickReplies() || !state.quickReplySharing || state.quickRepliesBusy) return;
  const all = $('quickReplyAudience').value === 'all';
  const recipients = all ? [] : Array.from($('quickReplyRecipients').querySelectorAll('input:checked'), input => Number(input.value));
  if (!all && (state.quickReplyRecipientsLoading || !recipients.length)) { showQuickRepliesError('Select at least one staff member.'); return; }
  const request = state.quickRepliesRequest;
  const source = state.quickReplySharing;
  state.quickRepliesBusy = true;
  setButtonBusy($('saveQuickReplyBtn'), true, 'Adding…');
  showQuickRepliesError('');
  let succeeded = false;
  try {
    const result = await api('/api/support/quick-replies/' + encodeURIComponent(source) + '/assign', {
      method: 'POST', body: JSON.stringify({recipient_ids: recipients, all_staff: all})
    });
    if (!state.authenticated || request !== state.quickRepliesRequest) return;
    notify(quickReplyAssignmentNotice(result));
    succeeded = true;
  } catch (error) {
    if (request === state.quickRepliesRequest) showQuickRepliesError(error.message);
  } finally {
    if (request === state.quickRepliesRequest) {
      state.quickRepliesBusy = false;
      setButtonBusy($('saveQuickReplyBtn'), false);
    }
  }
  if (succeeded) showQuickReplyList();
}

async function openQuickRepliesModal() {
  if (!state.authenticated || state.activeModal) return;
  state.quickReplyEditing = null;
  showQuickRepliesError('');
  $('quickReplyForm').classList.add('hidden');
  $('quickRepliesListView').classList.remove('hidden');
  text($('quickRepliesTitle'), 'Quick replies');
  text($('quickRepliesSubtitle'), QUICK_REPLIES_LIST_SUBTITLE);
  $('quickRepliesSearch').value = '';
  renderQuickReplies();
  openDialog('quickRepliesModal', state.quickReplies.length ? 'quickRepliesSearch' : 'addQuickReplyBtn');
  try {
    // Always refresh: replies may have changed on another device.
    await loadQuickReplies();
  } catch (error) {
    showQuickRepliesError(error.message);
    if (!state.quickRepliesLoaded) {
      $('quickRepliesList').replaceChildren(emptyMarkup('Could not load quick replies', 'Close this window and try again.', 'alert').outer);
    }
  }
}

function closeQuickRepliesModal() {
  if (state.quickRepliesBusy) return;
  state.quickReplyEditing = null;
  showQuickRepliesError('');
  closeDialog('quickRepliesModal');
}

async function copyQuickReply(reply) {
  if (!await copyMessageText(String(reply.body || ''), 'Quick reply copied. Paste it into the chat.')) return;
  closeDialog('quickRepliesModal', !state.composerEnabled);
  if (state.composerEnabled) focusMessageComposer();
}

async function attachQuickReplyPdf(reply) {
  if (state.quickRepliesBusy) return;
  if (!canAttachToComposer()) { showQuickRepliesError('Open a conversation you can reply to first.'); return; }
  if (state.pendingMedia || $('message').value.trim()) { showQuickRepliesError('Send or clear your current draft before attaching a saved PDF.'); return; }
  const ticketId = state.ticketId;
  const agentId = state.agent?.id;
  const request = state.quickRepliesRequest;
  state.quickRepliesBusy = true;
  showQuickRepliesError('Loading PDF…');
  try {
    const response = await fetch('/api/support/quick-replies/' + encodeURIComponent(reply.id) + '/pdf', {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) {
      if (response.status === 401) showLogin();
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || 'Could not load the saved PDF.');
    }
    const file = new File([await response.blob()], reply.attachment_filename, {type: 'application/pdf'});
    if (!state.authenticated || state.agent?.id !== agentId || request !== state.quickRepliesRequest) return;
    if (state.ticketId !== ticketId || !canAttachToComposer() || state.pendingMedia || $('message').value.trim()) {
      showQuickRepliesError('The conversation or draft changed. Select the PDF again.'); return;
    }
    if (attachComposerFile(file)) {
      $('message').value = reply.body || ''; updateComposerCount(); resizeComposer();
      closeDialog('quickRepliesModal', false); focusMessageComposer();
    }
  } catch (error) {
    if (request === state.quickRepliesRequest) showQuickRepliesError(error.message);
  } finally {
    if (request === state.quickRepliesRequest) state.quickRepliesBusy = false;
  }
}

async function saveQuickReply(event) {
  event.preventDefault();
  if (!state.authenticated || state.quickRepliesBusy || !state.quickReplyEditing) return;
  if (state.quickReplySharing) { await shareSavedQuickReply(); return; }
  const isLocation = state.quickReplyKind === 'location';
  const body = isLocation ? '' : $('quickReplyBody').value;
  const file = $('quickReplyPdf').files?.[0];
  const current = state.quickReplies.find(item => String(item.id) === String(state.quickReplyEditing));
  if ((file || current?.attachment_filename) && body.length > 1024) { showQuickRepliesError('PDF captions must be 1024 characters or fewer.'); return; }
  if (!isLocation && !body.trim() && !file && !current?.attachment_filename) {
    showQuickRepliesError('Enter the quick reply message.');
    $('quickReplyBody').focus();
    return;
  }
  const editing = state.quickReplyEditing;
  if (state.quickReplyKind === 'pdf' && !file && !current?.attachment_filename) { showQuickRepliesError('Choose a PDF file.'); return; }
  if (isLocation && !$('quickReplyTitle').value.trim()) { showQuickRepliesError('Give this saved location a name.'); $('quickReplyTitle').focus(); return; }
  const audience = editing === 'new' && canAssignQuickReplies() ? $('quickReplyAudience').value : 'me';
  const recipients = audience === 'selected' ? Array.from($('quickReplyRecipients').querySelectorAll('input:checked'), input => Number(input.value)) : [];
  if (audience === 'selected' && (state.quickReplyRecipientsLoading || !recipients.length)) { showQuickRepliesError('Select at least one staff member.'); return; }
  const request = state.quickRepliesRequest;
  state.quickRepliesBusy = true;
  setButtonBusy($('saveQuickReplyBtn'), true, 'Saving…');
  showQuickRepliesError('');
  try {
    const fields = {title: $('quickReplyTitle').value, body};
    if (isLocation) fields.location = {
      latitude: $('quickReplyLatitude').value.trim() ? Number($('quickReplyLatitude').value) : null,
      longitude: $('quickReplyLongitude').value.trim() ? Number($('quickReplyLongitude').value) : null,
      name: $('quickReplyTitle').value.trim(), address: $('quickReplyAddress').value.trim(), maps_url: $('quickReplyMapsUrl').value.trim()
    };
    if (audience !== 'me') Object.assign(fields, {recipient_ids: recipients, all_staff: audience === 'all'});
    let payload = JSON.stringify(fields);
    let path = '/api/support/quick-replies' + (editing === 'new' ? '' : '/' + encodeURIComponent(editing));
    if (audience !== 'me') path += '/assign';
    if (file) {
      payload = new FormData(); payload.append('file', file, file.name);
      if (audience !== 'me') payload.append('assignment', JSON.stringify(fields));
      else { payload.append('title', fields.title); payload.append('body', body); }
      path += '/pdf';
    }
    const result = await api(path, {method: editing === 'new' ? 'POST' : 'PUT', body: payload});
    if (!state.authenticated || request !== state.quickRepliesRequest) return;
    const saved = audience === 'me' ? result : result.item;
    if (saved) {
      state.quickReplies = [...state.quickReplies.filter(item => String(item.id) !== String(saved.id)), saved]
        .sort((left, right) => quickReplyLabel(left).localeCompare(quickReplyLabel(right), undefined, {sensitivity: 'base'}) || left.id - right.id);
      state.quickReplyEditing = saved.id;
      state.quickReplyTab = quickReplyKind(saved);
    }
    notify(audience !== 'me' ? quickReplyAssignmentNotice(result) : editing === 'new' ? 'Quick reply saved.' : 'Quick reply updated.');
  } catch (error) {
    if (request === state.quickRepliesRequest) showQuickRepliesError(error.message);
    return;
  } finally {
    if (request === state.quickRepliesRequest) {
      state.quickRepliesBusy = false;
      setButtonBusy($('saveQuickReplyBtn'), false);
    }
  }
  showQuickReplyList();
}

async function deleteQuickReply(reply) {
  if (state.quickRepliesBusy) return;
  const confirmed = await confirmDialog({
    title: 'Delete quick reply?',
    message: '“' + quickReplyLabel(reply) + '” will be removed from your quick replies.',
    confirmLabel: 'Delete',
    tone: 'danger'
  });
  if (!confirmed) return;
  state.quickRepliesBusy = true;
  showQuickRepliesError('');
  try {
    await api('/api/support/quick-replies/' + encodeURIComponent(reply.id), {method: 'DELETE'});
    state.quickReplies = state.quickReplies.filter(item => String(item.id) !== String(reply.id));
    notify('Quick reply deleted.');
  } catch (error) {
    showQuickRepliesError(error.message);
  } finally {
    state.quickRepliesBusy = false;
  }
  renderQuickReplies();
  $('quickRepliesSearch').focus();
}

const AI_REASON_LABELS = {
  MANUAL_ON: 'Manual control is ON',
  MANUAL_OFF: 'Manual control is OFF',
  WITHIN_SCHEDULE: 'Within scheduled AI hours',
  OUTSIDE_SCHEDULE: 'Outside scheduled AI hours'
};

function showAiSettingsError(message) {
  const element = $('aiSettingsError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function selectedAiControlMode() {
  return document.querySelector('input[name="aiControlMode"]:checked')?.value || 'MANUAL';
}

function syncAiSettingsFields() {
  const scheduled = selectedAiControlMode() === 'SCHEDULED';
  $('aiManualFields').classList.toggle('hidden', scheduled);
  $('aiScheduledFields').classList.toggle('hidden', !scheduled);
  $('aiNextChange').classList.toggle('hidden', !scheduled);
}

function validIanaTimezone(value) {
  try {
    new Intl.DateTimeFormat('en-US', {timeZone: value}).format(new Date());
    return true;
  } catch (_) {
    return false;
  }
}

function validateAiSettingsForm() {
  showAiSettingsError('');
  for (const id of ['aiScheduleStart', 'aiScheduleEnd', 'aiScheduleTimezone']) {
    $(id).setAttribute('aria-invalid', 'false');
  }
  if (selectedAiControlMode() !== 'SCHEDULED') return true;

  const start = $('aiScheduleStart').value.trim();
  const end = $('aiScheduleEnd').value.trim();
  const timezone = $('aiScheduleTimezone').value.trim();
  const hhmm = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
  if (!hhmm.test(start)) {
    $('aiScheduleStart').setAttribute('aria-invalid', 'true');
    showAiSettingsError('Choose a valid AI start time in HH:mm format.');
    return false;
  }
  if (!hhmm.test(end)) {
    $('aiScheduleEnd').setAttribute('aria-invalid', 'true');
    showAiSettingsError('Choose a valid AI end time in HH:mm format.');
    return false;
  }
  if (start === end) {
    $('aiScheduleStart').setAttribute('aria-invalid', 'true');
    $('aiScheduleEnd').setAttribute('aria-invalid', 'true');
    showAiSettingsError('AI start and end times must be different.');
    return false;
  }
  if (!timezone || !validIanaTimezone(timezone)) {
    $('aiScheduleTimezone').setAttribute('aria-invalid', 'true');
    showAiSettingsError('Enter a valid IANA timezone, for example Asia/Beirut or UTC.');
    return false;
  }
  return true;
}

function renderAiSettingsUnavailable(message) {
  state.aiSettings = null;
  text($('aiHeaderStatus'), '?');
  $('aiHeaderDot').className = 'ai-status-dot';
  $('aiHeaderBtn').setAttribute('aria-label', 'AI assistant status unavailable');
  $('aiHeaderBtn').setAttribute('title', message || 'AI assistant status unavailable');
}

function syncAiSettingsEditability() {
  const editable = canManageAiSettings();
  for (const id of ['aiModeManual', 'aiModeScheduled', 'aiManualEnabled', 'aiScheduleStart', 'aiScheduleEnd', 'aiScheduleTimezone']) {
    $(id).disabled = !editable;
  }
  $('saveAiSettingsBtn').classList.toggle('hidden', !editable);
  text($('cancelAiSettingsBtn'), editable ? 'Cancel' : 'Close');
  text(
    $('aiSettingsModalSubtitle'),
    editable
      ? 'Control global AI availability for new inbound WhatsApp messages.'
      : 'View the current global AI availability. Only Super Admins and Admins can change it.'
  );
}

function renderAiSettings(settings) {
  state.aiSettings = settings;
  const enabled = Boolean(settings?.effective_ai_enabled);
  text($('aiHeaderStatus'), enabled ? 'ON' : 'OFF');
  $('aiHeaderDot').className = 'ai-status-dot ' + (enabled ? 'on' : 'off');
  $('aiHeaderBtn').setAttribute('aria-label', 'AI assistant is ' + (enabled ? 'on' : 'off'));
  $('aiHeaderBtn').setAttribute('title', 'AI assistant: ' + (enabled ? 'ON' : 'OFF') + '. Select to view settings.');
  const status = $('aiEffectiveStatus');
  status.textContent = enabled ? 'ON' : 'OFF';
  status.classList.toggle('off', !enabled);
  text($('aiEffectiveReason'), AI_REASON_LABELS[settings?.effective_reason] || settings?.effective_reason || 'Current status unavailable');

  const mode = settings?.control_mode === 'SCHEDULED' ? 'SCHEDULED' : 'MANUAL';
  $('aiModeManual').checked = mode === 'MANUAL';
  $('aiModeScheduled').checked = mode === 'SCHEDULED';
  $('aiManualEnabled').value = settings?.manual_ai_enabled ? 'true' : 'false';
  $('aiScheduleStart').value = settings?.schedule?.start_time || '';
  $('aiScheduleEnd').value = settings?.schedule?.end_time || '';
  $('aiScheduleTimezone').value = settings?.schedule?.timezone || '';
  syncAiSettingsFields();
  syncAiSettingsEditability();

  text(
    $('aiNextChange'),
    settings?.next_transition_at
      ? 'Next automatic change: ' + fmtInTimezone(settings.next_transition_at, settings?.schedule?.timezone)
      : 'Next automatic change: —'
  );
  const updater = settings?.updated_by?.name ? ' by ' + settings.updated_by.name : '';
  text(
    $('aiUpdatedMeta'),
    settings?.updated_at
      ? 'Last saved ' + fmt(settings.updated_at) + updater + '. Settings are authoritative across backend instances.'
      : 'Saved configuration is authoritative across backend instances.'
  );
}

async function loadAiSettings() {
  if (!state.authenticated) return;
  const settings = await api('/api/support/ai-settings');
  renderAiSettings(settings);
}

async function openAiSettingsModal() {
  if (!state.authenticated) return;
  showAiSettingsError('');
  syncAiSettingsEditability();
  openDialog('aiSettingsModal', 'aiModeManual');
  state.aiSettingsBusy = true;
  setButtonBusy($('saveAiSettingsBtn'), true, 'Loading…');
  try {
    await loadAiSettings();
  } catch (error) {
    renderAiSettingsUnavailable(error.message);
    showAiSettingsError(error.message);
    syncAiSettingsEditability();
  } finally {
    state.aiSettingsBusy = false;
    setButtonBusy($('saveAiSettingsBtn'), false);
  }
}

function closeAiSettingsModal() {
  if (state.aiSettingsBusy) return;
  showAiSettingsError('');
  closeDialog('aiSettingsModal');
}

async function saveAiSettings(event) {
  event.preventDefault();
  if (state.aiSettingsBusy || !canManageAiSettings() || !state.aiSettings) return;
  if (!validateAiSettingsForm()) return;

  const payload = {
    control_mode: selectedAiControlMode(),
    manual_ai_enabled: $('aiManualEnabled').value === 'true',
    schedule: {
      start_time: $('aiScheduleStart').value.trim(),
      end_time: $('aiScheduleEnd').value.trim(),
      timezone: $('aiScheduleTimezone').value.trim()
    },
    version: Number(state.aiSettings.version)
  };

  state.aiSettingsBusy = true;
  setButtonBusy($('saveAiSettingsBtn'), true, 'Saving…');
  showAiSettingsError('');
  try {
    await api('/api/support/ai-settings', {method:'PUT', body: JSON.stringify(payload)});
    // Re-fetch the authoritative effective state instead of assuming the local form
    // reflects timezone boundaries or a concurrent update correctly.
    await loadAiSettings();
    notify('AI assistant settings saved.');
  } catch (error) {
    showAiSettingsError(error.status === 409 ? error.message + ' Reload the settings and try again.' : error.message);
    if (error.status === 409) {
      try { await loadAiSettings(); } catch (_) {}
    }
  } finally {
    state.aiSettingsBusy = false;
    setButtonBusy($('saveAiSettingsBtn'), false);
  }
}

function showWhatsAppFeedback(id, message) {
  const element = $(id);
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function clearWhatsAppFeedback() {
  showWhatsAppFeedback('whatsappError', '');
  showWhatsAppFeedback('whatsappSuccess', '');
}

function renderWhatsAppRegistry(payload) {
  state.whatsappRegistry = payload || {accounts: [], numbers: []};
  const accounts = Array.isArray(state.whatsappRegistry.accounts) ? state.whatsappRegistry.accounts : [];
  const numbers = Array.isArray(state.whatsappRegistry.numbers) ? state.whatsappRegistry.numbers : [];
  const accountContainer = $('whatsappAccounts');
  const numberContainer = $('whatsappNumbers');
  accountContainer.replaceChildren();
  numberContainer.replaceChildren();

  if (!accounts.length) {
    accountContainer.append(emptyMarkup('No WABA synced yet', 'Enter a WhatsApp Business Account ID above, or receive a signed webhook so the account can be discovered.', 'phone').outer);
  } else {
    for (const account of accounts) {
      const row = document.createElement('div');
      row.className = 'whatsapp-account-row';
      const copy = document.createElement('div');
      const title = document.createElement('div');
      title.className = 'whatsapp-row-title';
      title.textContent = account.name || 'WhatsApp Business Account';
      const subtitle = document.createElement('div');
      subtitle.className = 'whatsapp-row-subtitle';
      subtitle.textContent = 'WABA ' + account.waba_id + (account.last_synced_at ? ' · synced ' + fmt(account.last_synced_at) : ' · not synced yet');
      copy.append(title, subtitle);
      const sync = document.createElement('button');
      sync.type = 'button';
      sync.className = 'btn small';
      sync.append(createIcon('refresh', 'icon icon-sm'), document.createTextNode('Sync'));
      sync.onclick = () => syncWaba(account.waba_id);
      row.append(copy, sync);
      accountContainer.append(row);
    }
  }

  if (!numbers.length) {
    numberContainer.append(emptyMarkup('No phone numbers found', 'Sync a WABA to fetch its registered WhatsApp phone numbers from Meta.', 'phone').outer);
    return;
  }

  for (const item of numbers) {
    const row = document.createElement('div');
    row.className = 'whatsapp-number-row';
    const copy = document.createElement('div');
    const title = document.createElement('div');
    title.className = 'whatsapp-row-title';
    title.append(createSupportNumberLabel(item));
    const subtitle = document.createElement('div');
    subtitle.className = 'whatsapp-row-subtitle';
    subtitle.textContent = (item.verified_name ? item.verified_name + ' · ' : '') + 'ID ' + item.phone_number_id + (item.source === 'LINKED_DEVICE' ? ' · Linked device' : item.waba_id ? ' · WABA ' + item.waba_id : ' · Cloud API');
    const meta = document.createElement('div');
    meta.className = 'whatsapp-row-meta';
    meta.append(createMetaChip(item.is_enabled ? 'Active in app' : 'Not active', item.is_enabled ? 'check-circle' : 'clock'));
    if (item.quality_rating) meta.append(createMetaChip('Quality ' + item.quality_rating, 'activity'));
    if (!item.provider_present) meta.append(createMetaChip(item.source === 'LINKED_DEVICE' ? 'Connection not configured' : 'Missing from latest Meta sync', 'alert'));
    copy.append(title, subtitle, meta);

    const actions = document.createElement('div');
    actions.className = 'whatsapp-number-actions';
    const rename = document.createElement('button');
    rename.type = 'button';
    rename.className = 'btn small';
    rename.disabled = state.whatsappBusy || state.linkedDeviceBusy;
    rename.textContent = item.display_name ? 'Edit name' : 'Rename';
    rename.setAttribute('aria-label', (item.display_name ? 'Edit name for ' : 'Rename ') + supportNumberRaw(item));
    rename.onclick = () => openWhatsAppNumberNameEditor(item, row, rename);
    actions.append(rename);

    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'btn small' + (item.is_enabled ? ' danger' : ' primary');
    action.disabled = state.whatsappBusy || state.linkedDeviceBusy || (!item.provider_present && !item.is_enabled);
    action.textContent = item.is_enabled ? (item.source === 'LINKED_DEVICE' ? 'Deactivate' : 'Remove from app') : 'Add to app';
    action.onclick = () => setWhatsAppNumberState(item.phone_number_id, !item.is_enabled);
    actions.append(action);
    if (item.source === 'LINKED_DEVICE') {
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'btn small danger';
      remove.textContent = 'Remove and unlink';
      remove.disabled = state.whatsappBusy || state.linkedDeviceBusy;
      remove.onclick = () => removeLinkedDeviceNumber(item.phone_number_id);
      actions.append(remove);
    } else if (!item.is_enabled) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn small danger';
      remove.textContent = 'Remove saved number';
      remove.disabled = state.whatsappBusy || state.linkedDeviceBusy;
      remove.onclick = () => removeSavedWhatsAppNumber(item.phone_number_id);
      actions.append(remove);
    }
    row.append(copy, actions);
    numberContainer.append(row);
  }
  if (state.linkedDeviceStatus) renderLinkedDeviceStatus(state.linkedDeviceStatus);
}

function openWhatsAppNumberNameEditor(item, row, button) {
  if (!canManageWhatsApp() || state.whatsappBusy || state.linkedDeviceBusy || row.querySelector('.whatsapp-name-editor')) return;
  const editor = document.createElement('form');
  editor.className = 'whatsapp-name-editor';
  const field = document.createElement('label');
  field.className = 'field';
  const caption = document.createElement('span');
  caption.textContent = 'Custom name for ' + supportNumberRaw(item);
  const input = document.createElement('input');
  input.type = 'text';
  input.maxLength = 80;
  input.placeholder = 'For example, TestNumber';
  input.value = item.display_name || '';
  input.autocomplete = 'off';
  const hint = document.createElement('span');
  hint.className = 'field-hint';
  hint.textContent = 'Leave blank to show the phone number.';
  field.append(caption, input, hint);
  const save = document.createElement('button');
  save.type = 'submit'; save.className = 'btn small primary'; save.textContent = 'Save name';
  const cancel = document.createElement('button');
  cancel.type = 'button'; cancel.className = 'btn small'; cancel.textContent = 'Cancel';
  cancel.onclick = () => { editor.remove(); button.disabled = false; button.focus(); };
  editor.onsubmit = event => { event.preventDefault(); renameWhatsAppNumber(item.phone_number_id, input.value, save); };
  editor.append(field, save, cancel);
  row.append(editor);
  button.disabled = true;
  input.focus();
}

function refreshVisibleSupportNumberName(phoneNumberId, displayName) {
  const matches = ticket => ticket && String(ticket.business_phone_number_id) === String(phoneNumberId);
  for (const ticket of state.queueItems || []) if (matches(ticket)) ticket.display_name = displayName;
  for (const item of state.globalSearchResults || []) if (matches(item)) item.display_name = displayName;
  for (const line of state.chatLines || []) if (String(line.phone_number_id) === String(phoneNumberId)) line.display_name = displayName;
  const scrollTop = $('queue').scrollTop;
  renderQueue();
  $('queue').scrollTop = scrollTop;
  const data = state.currentTicketData;
  if (!data) return;
  const currentChanged = matches(data.ticket);
  if (currentChanged) data.ticket.display_name = displayName;
  let historyChanged = false;
  for (const thread of state.previousTicketHistory || []) {
    if (matches(thread.ticket)) { thread.ticket.display_name = displayName; historyChanged = true; }
  }
  if (currentChanged) {
    renderTicketHeader(data.ticket);
    renderContext(data.ticket, data.context || {}, data.shipment || null,
      data.customer_identity || null, data.permissions || null);
  }
  if (historyChanged) renderConversation(data, {preserveAudio: true});
  if (currentChanged || historyChanged) state.timelineSignature = timelineSignature(data);
}

async function renameWhatsAppNumber(phoneNumberId, value, button) {
  if (!canManageWhatsApp() || state.whatsappBusy || state.linkedDeviceBusy) return;
  const displayName = String(value || '').trim() || null;
  state.whatsappBusy = true;
  clearWhatsAppFeedback();
  setButtonBusy(button, true, 'Saving…');
  let saved = false;
  try {
    const payload = await api('/api/support/whatsapp-numbers/' + encodeURIComponent(phoneNumberId) + '/name',
      {method: 'PUT', body: JSON.stringify({display_name: displayName})});
    renderWhatsAppRegistry(payload);
    refreshVisibleSupportNumberName(phoneNumberId, displayName);
    saved = true;
    showWhatsAppFeedback('whatsappSuccess', displayName ? 'WhatsApp number renamed.' : 'Custom name removed.');
    await Promise.allSettled([
      loadQueue({silent: true}),
      state.ticketId ? openTicket(state.ticketId, {silent: true, refreshHistory: true}) : Promise.resolve()
    ]);
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    if (button.isConnected) setButtonBusy(button, false);
    if (saved && state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
}

function renderLinkedDeviceStatus(payload) {
  state.linkedDeviceStatus = payload;
  const section = $('linkedDeviceSection');
  section.classList.toggle('hidden', !payload?.configured);
  const container = $('linkedDeviceLines');
  container.replaceChildren();
  $('addLinkedDeviceBtn').disabled = state.linkedDeviceBusy || state.whatsappBusy;
  $('addLinkedDeviceBtn').textContent = pendingLinkedDeviceAddition() ? 'Retry linking' : 'Link another number';
  if (!payload?.configured) return;
  for (const line of payload.lines || []) {
    const row = document.createElement('div');
    row.className = 'linked-device-line';
    const heading = document.createElement('strong');
    const registryNumber = (state.whatsappRegistry?.numbers || []).find(item => String(item.phone_number_id) === String(line.line_id));
    heading.append(createSupportNumberLabel({line_id: line.line_id,
      display_phone_number: line.display_phone_number || registryNumber?.display_phone_number,
      display_name: registryNumber?.display_name}));
    const status = document.createElement('div');
    status.className = 'muted';
    const labels = {connected: 'Connected', connecting: 'Connecting…', qr: 'Scan this code with the phone you want to connect', logged_out: 'Not paired', disconnected: 'Disconnected', error: 'Connection failed'};
    status.textContent = labels[line.status] || 'Not paired';
    if (line.error) status.textContent += ' · ' + line.error;
    if (line.pending_callbacks) status.textContent += ' · ' + line.pending_callbacks + ' updates pending';
    if (line.waiting_history_reactions) status.title = line.waiting_history_reactions + ' reactions are waiting for older messages to sync. This does not block new messages.';
    if (line.failed_callbacks) status.textContent += ' · ' + line.failed_callbacks + ' failed updates need administrator attention';
    row.append(heading, status);
    if (line.status === 'qr' && typeof line.qr_data_url === 'string' && line.qr_data_url.startsWith('data:image/png;base64,')) {
      const qr = document.createElement('img');
      qr.className = 'linked-device-qr';
      qr.src = line.qr_data_url;
      qr.alt = 'Scan to pair WhatsApp line ' + line.line_id;
      row.append(qr);
    }
    const connected = line.status === 'connected';
    if (!connected) {
      const pair = document.createElement('button');
      pair.type = 'button'; pair.className = 'btn small';
      pair.textContent = line.status === 'qr' ? 'Refresh pairing' : 'Pair WhatsApp';
      pair.disabled = state.linkedDeviceBusy || state.whatsappBusy || line.status === 'connecting';
      pair.onclick = () => changeLinkedDeviceConnection(line.line_id, 'connect');
      row.append(pair);
    }
    const remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'btn small danger';
    remove.textContent = 'Remove and unlink';
    remove.disabled = state.linkedDeviceBusy || state.whatsappBusy;
    remove.onclick = () => removeLinkedDeviceNumber(line.line_id);
    row.append(remove);
    container.append(row);
  }
  text($('linkedDeviceStatus'), 'Only paired lines added to the app can send or receive messages here.');
}

async function loadLinkedDeviceStatus() {
  if (!canManageWhatsApp() || !state.agent?.linked_device_available || state.linkedDeviceBusy) return;
  const request = ++state.linkedDeviceRequest;
  try {
    const payload = await api('/api/support/whatsapp-linked-device/status');
    if (request === state.linkedDeviceRequest && state.activeModal === 'whatsappModal') renderLinkedDeviceStatus(payload);
  } catch (error) {
    if (request !== state.linkedDeviceRequest || state.activeModal !== 'whatsappModal') return;
    $('linkedDeviceSection').classList.remove('hidden');
    $('linkedDeviceLines').replaceChildren();
    text($('linkedDeviceStatus'), error.message || 'The WhatsApp connection service is unavailable.');
    $('addLinkedDeviceBtn').disabled = state.linkedDeviceBusy || state.whatsappBusy;
  }
}

function linkedDeviceAdditionStorageKey() {
  return 'shipment-support-linked-device-addition:' + (state.agent?.id || '');
}

function pendingLinkedDeviceAddition() {
  if (state.linkedDeviceAdditionId) return state.linkedDeviceAdditionId;
  try {
    const pending = sessionStorage.getItem(linkedDeviceAdditionStorageKey());
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(pending || '')) state.linkedDeviceAdditionId = pending;
  } catch (_) {}
  return state.linkedDeviceAdditionId;
}

function saveLinkedDeviceAddition(requestId) {
  state.linkedDeviceAdditionId = requestId;
  try {
    if (requestId) sessionStorage.setItem(linkedDeviceAdditionStorageKey(), requestId);
    else sessionStorage.removeItem(linkedDeviceAdditionStorageKey());
  } catch (_) {}
}

async function addLinkedDeviceNumber() {
  if (!canManageWhatsApp() || !state.agent?.linked_device_available || state.linkedDeviceBusy || state.whatsappBusy) return;
  // Keep the same request ID after a lost response so retrying resumes its line.
  saveLinkedDeviceAddition(pendingLinkedDeviceAddition() || crypto.randomUUID());
  state.linkedDeviceBusy = true;
  state.linkedDeviceRequest++;
  clearWhatsAppFeedback();
  setButtonBusy($('addLinkedDeviceBtn'), true, 'Linking…');
  try {
    const payload = await api('/api/support/whatsapp-linked-device/lines', {
      method: 'POST', body: JSON.stringify({request_id: state.linkedDeviceAdditionId}),
    });
    saveLinkedDeviceAddition(null);
    if (payload.registry) renderWhatsAppRegistry(payload.registry);
    renderLinkedDeviceStatus(payload);
    const added = (payload.lines || []).find(line => line.line_id === payload.added_line_id);
    showWhatsAppFeedback('whatsappSuccess', added?.status === 'connected'
      ? 'WhatsApp number connected. Choose Add to app to activate it.'
      : 'Scan the new pairing code using the number you want to link, then choose Add to app.');
  } catch (error) {
    if (error.status === 409) {
      saveLinkedDeviceAddition(null);
      showWhatsAppFeedback('whatsappError', error.message + ' Choose Link another number to start a new pairing.');
    } else showWhatsAppFeedback('whatsappError', error.message + ' Retry linking to resume this request.');
  } finally {
    state.linkedDeviceBusy = false;
    setButtonBusy($('addLinkedDeviceBtn'), false);
    if (state.linkedDeviceStatus) renderLinkedDeviceStatus(state.linkedDeviceStatus);
    else {
      $('addLinkedDeviceBtn').disabled = false;
      $('addLinkedDeviceBtn').textContent = state.linkedDeviceAdditionId ? 'Retry linking' : 'Link another number';
    }
    if (state.linkedDeviceAdditionId) {
      try { await loadWhatsAppRegistry(); }
      catch (_) { await loadLinkedDeviceStatus(); }
    } else await loadLinkedDeviceStatus();
  }
}

async function removeLinkedDeviceNumber(lineId) {
  if (!canManageWhatsApp() || state.linkedDeviceBusy || state.whatsappBusy) return;
  if (!(await confirmDialog({title: 'Remove and unlink number?', message: 'Remove and unlink this WhatsApp number completely? It will be signed out and removed from the app. Active conversations on this number will be closed; their chat history will be kept.', confirmLabel: 'Remove and unlink', tone: 'danger'}))) return;
  state.linkedDeviceBusy = true;
  state.linkedDeviceRequest++;
  clearWhatsAppFeedback();
  if (state.linkedDeviceStatus) renderLinkedDeviceStatus(state.linkedDeviceStatus);
  if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  try {
    const payload = await api('/api/support/whatsapp-linked-device/lines/' + encodeURIComponent(lineId), {
      method: 'DELETE', body: JSON.stringify({close_active_tickets: true}),
    });
    saveLinkedDeviceAddition(null);
    if (payload.registry) renderWhatsAppRegistry(payload.registry);
    renderLinkedDeviceStatus(payload);
    showWhatsAppFeedback('whatsappSuccess', 'WhatsApp number removed and unlinked. Its chat history remains available.');
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.linkedDeviceBusy = false;
    try { await loadWhatsAppRegistry(); }
    catch (_) {
      if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
      if (state.linkedDeviceStatus) renderLinkedDeviceStatus(state.linkedDeviceStatus);
    }
  }
}

async function changeLinkedDeviceConnection(lineId, action) {
  if (!canManageWhatsApp() || state.linkedDeviceBusy) return;
  if (action === 'disconnect' && !(await confirmDialog({title: 'Unlink device?', message: 'Unlink this WhatsApp device? You will need to pair it again to use this connection.', confirmLabel: 'Unlink device', tone: 'danger'}))) return;
  state.linkedDeviceBusy = true;
  state.linkedDeviceRequest++;
  clearWhatsAppFeedback();
  try {
    const payload = await api('/api/support/whatsapp-linked-device/lines/' + encodeURIComponent(lineId) + '/' + action, {method: 'POST'});
    renderLinkedDeviceStatus(payload);
    if (action === 'disconnect') showWhatsAppFeedback('whatsappSuccess', 'WhatsApp device unlinked.');
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.linkedDeviceBusy = false;
    if (state.linkedDeviceStatus) renderLinkedDeviceStatus(state.linkedDeviceStatus);
  }
}

async function loadWhatsAppRegistry() {
  if (!canManageWhatsApp()) return;
  const payload = await api('/api/support/whatsapp-numbers');
  renderWhatsAppRegistry(payload);
  await loadLinkedDeviceStatus();
}

async function openWhatsAppModal() {
  if (!canManageWhatsApp() || state.whatsappBusy) return;
  clearWhatsAppFeedback();
  openDialog('whatsappModal', 'wabaIdInput');
  state.whatsappBusy = true;
  $('refreshWhatsAppBtn').disabled = true;
  try {
    await loadWhatsAppRegistry();
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    $('refreshWhatsAppBtn').disabled = false;
    if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
}

function closeWhatsAppModal() {
  if (state.whatsappBusy || state.linkedDeviceBusy) return;
  state.linkedDeviceRequest++;
  $('linkedDeviceLines').replaceChildren();
  state.linkedDeviceStatus = null;
  clearWhatsAppFeedback();
  closeDialog('whatsappModal');
}

async function syncWaba(wabaId) {
  if (!canManageWhatsApp() || state.whatsappBusy) return;
  const value = String(wabaId || '').trim();
  if (!/^[0-9]{5,64}$/.test(value)) {
    showWhatsAppFeedback('whatsappError', 'Enter a valid numeric WABA ID.');
    return;
  }
  state.whatsappBusy = true;
  clearWhatsAppFeedback();
  setButtonBusy($('syncWabaBtn'), true, 'Syncing…');
  try {
    const payload = await api('/api/support/whatsapp-accounts/sync', {method:'POST', body: JSON.stringify({waba_id: value})});
    renderWhatsAppRegistry(payload);
    $('wabaIdInput').value = '';
    showWhatsAppFeedback('whatsappSuccess', 'Meta account synced. New phone numbers remain disabled until you add them to the app.');
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    setButtonBusy($('syncWabaBtn'), false);
    if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
}

async function syncWabaForm(event) {
  event.preventDefault();
  if (!$('wabaSyncForm').reportValidity()) return;
  await syncWaba($('wabaIdInput').value);
}

async function setWhatsAppNumberState(phoneNumberId, enabled) {
  if (!canManageWhatsApp() || state.whatsappBusy || state.linkedDeviceBusy) return;
  if (!enabled && !(await confirmDialog({title: 'Remove number from app?', message: 'Remove this WhatsApp number from the chatbot and support app? Historical tickets will remain available.', confirmLabel: 'Remove from app', tone: 'danger'}))) return;
  state.whatsappBusy = true;
  clearWhatsAppFeedback();
  try {
    const payload = await api('/api/support/whatsapp-numbers/' + encodeURIComponent(phoneNumberId), {method:'PUT', body: JSON.stringify({enabled: Boolean(enabled)})});
    renderWhatsAppRegistry(payload);
    showWhatsAppFeedback('whatsappSuccess', enabled ? 'WhatsApp number added to the app.' : 'WhatsApp number removed from the app.');
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
}

async function removeSavedWhatsAppNumber(phoneNumberId) {
  if (!canManageWhatsApp() || state.whatsappBusy || state.linkedDeviceBusy) return;
  if (!(await confirmDialog({title: 'Remove saved number?', message: 'Permanently remove this inactive Meta number from the app? Existing conversations and their number labels will be kept. Future Meta syncs will not add this entry back. This does not delete the number from your Meta business account.', confirmLabel: 'Remove saved number', tone: 'danger'}))) return;
  state.whatsappBusy = true;
  clearWhatsAppFeedback();
  try {
    const payload = await api('/api/support/whatsapp-numbers/' + encodeURIComponent(phoneNumberId), {method: 'DELETE'});
    renderWhatsAppRegistry(payload);
    showWhatsAppFeedback('whatsappSuccess', 'Saved number removed. Conversation history is preserved.');
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
}

async function refreshWhatsAppRegistry() {
  if (!canManageWhatsApp() || state.whatsappBusy) return;
  state.whatsappBusy = true;
  clearWhatsAppFeedback();
  setButtonBusy($('refreshWhatsAppBtn'), true, 'Refreshing…');
  try {
    await loadWhatsAppRegistry();
  } catch (error) {
    showWhatsAppFeedback('whatsappError', error.message);
  } finally {
    state.whatsappBusy = false;
    setButtonBusy($('refreshWhatsAppBtn'), false);
    if (state.whatsappRegistry) renderWhatsAppRegistry(state.whatsappRegistry);
  }
}

function showStaffFeedback(id, message) {
  const element = $(id);
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function setFieldError(inputId, errorId, message) {
  const input = $(inputId);
  const error = $(errorId);
  input.setAttribute('aria-invalid', message ? 'true' : 'false');
  error.textContent = message || '';
  error.classList.toggle('hidden', !message);
}

function clearStaffFeedback() {
  showStaffFeedback('staffError', '');
  showStaffFeedback('staffSuccess', '');
  $('editConflictStaffBtn').classList.add('hidden');
  state.conflictStaff = null;
  setFieldError('createStaffPasswordConfirm', 'createPasswordMatchError', '');
  setFieldError('editStaffPasswordConfirm', 'editPasswordMatchError', '');
}

function setStaffView(view) {
  state.staffView = view;
  for (const [name, id] of Object.entries({list: 'staffListView', create: 'staffCreateView', edit: 'staffEditView'})) {
    $(id).classList.toggle('hidden', name !== view);
  }
  const copy = {
    list: ['Staff accounts', 'Manage your support team from one place.'],
    create: ['Create staff', 'Create a new support staff account.'],
    edit: ['Edit staff', 'Update an existing support staff account.']
  }[view] || ['Staff accounts', 'Manage your support team from one place.'];
  text($('staffModalTitle'), copy[0]);
  text($('staffModalSubtitle'), copy[1]);
}

function resetCreateStaffForm() {
  $('createStaffForm').reset();
  $('createStaffRole').value = 'agent';
  setFieldError('createStaffPasswordConfirm', 'createPasswordMatchError', '');
}

function resetEditStaffForm() {
  state.editingStaffId = null;
  state.editingStaffOriginal = null;
  $('editStaffForm').reset();
  $('editStaffRole').value = 'agent';
  $('editStaffRole').disabled = false;
  $('editStaffActive').checked = true;
  $('editStaffActive').disabled = false;
  text($('editStaffHeading'), 'Edit staff account');
  setFieldError('editStaffPasswordConfirm', 'editPasswordMatchError', '');
}

function staffFormHasChanges() {
  if (state.staffView === 'create') {
    return Boolean(
      $('createStaffName').value ||
      $('createStaffEmail').value ||
      $('createStaffPassword').value ||
      $('createStaffPasswordConfirm').value ||
      $('createStaffRole').value !== 'agent'
    );
  }
  if (state.staffView === 'edit' && state.editingStaffOriginal) {
    const original = state.editingStaffOriginal;
    return (
      $('editStaffName').value.trim() !== (original.name || '') ||
      $('editStaffEmail').value.trim() !== (original.email || '') ||
      $('editStaffRole').value !== (original.role || 'agent') ||
      $('editStaffActive').checked !== Boolean(original.is_active) ||
      Boolean($('editStaffPassword').value || $('editStaffPasswordConfirm').value)
    );
  }
  return false;
}

async function confirmDiscardStaffChanges() {
  return !staffFormHasChanges() || await confirmDialog({title: 'Discard changes?', message: 'Discard the unsaved staff account changes?', confirmLabel: 'Discard changes', cancelLabel: 'Keep editing', tone: 'danger'});
}

async function closeStaffModal() {
  if (state.staffBusy || !(await confirmDiscardStaffChanges())) return;
  closeDialog('staffModal');
  clearStaffFeedback();
  resetCreateStaffForm();
  resetEditStaffForm();
  setStaffView('list');
}

async function openStaffModal() {
  if (!canManageStaff()) return;
  clearStaffFeedback();
  resetCreateStaffForm();
  resetEditStaffForm();
  setStaffView('list');
  openDialog('staffModal', 'createStaffBtn');
  await loadStaffAccounts();
}

function openCreateStaff() {
  if (!canManageStaff()) return;
  clearStaffFeedback();
  resetCreateStaffForm();
  setStaffView('create');
  $('createStaffName').focus();
}

async function backToStaffList() {
  if (state.staffBusy || !(await confirmDiscardStaffChanges())) return;
  clearStaffFeedback();
  resetCreateStaffForm();
  resetEditStaffForm();
  setStaffView('list');
  $('createStaffBtn').focus();
}

function beginStaffEdit(item) {
  if (!item) return;
  clearStaffFeedback();
  resetEditStaffForm();
  state.editingStaffId = String(item.id);
  state.editingStaffOriginal = {...item};
  $('editStaffName').value = item.name || '';
  $('editStaffEmail').value = item.email || '';
  $('editStaffRole').value = ['agent', 'admin', 'supervisor'].includes(item.role) ? item.role : 'agent';
  $('editStaffActive').checked = Boolean(item.is_active);
  const isSelf = String(item.id) === String(state.agent?.id);
  $('editStaffRole').disabled = isSelf;
  $('editStaffActive').disabled = isSelf;
  $('editStaffPassword').value = '';
  $('editStaffPasswordConfirm').value = '';
  text($('editStaffHeading'), 'Edit ' + (item.name || item.email || 'staff account'));
  setStaffView('edit');
  $('editStaffName').focus();
}

function staffButton(label, handler, className = '', iconName = null) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn ' + className;
  if (iconName) button.append(createIcon(iconName, 'icon icon-sm'));
  button.append(document.createTextNode(label));
  button.onclick = handler;
  return button;
}

function renderStaffAccounts(items) {
  state.staffItems = items || [];
  const container = $('staffList');
  container.replaceChildren();
  if (!state.staffItems.length) {
    container.append(emptyMarkup('No staff accounts found', 'Create an agent or administrator to add them to the support team.', 'users').outer);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of state.staffItems) {
    const row = document.createElement('div');
    row.className = 'staff-list-row';
    row.setAttribute('role', 'row');

    const identity = document.createElement('div');
    identity.setAttribute('role', 'cell');
    const isSelf = String(item.id) === String(state.agent?.id);
    const name = document.createElement('div');
    name.className = 'staff-name';
    const nameText = document.createElement('span');
    nameText.className = 'staff-name-text';
    nameText.textContent = item.name || 'Unnamed';
    nameText.title = item.name || '';
    name.append(nameText);
    if (isSelf) {
      const you = document.createElement('span');
      you.className = 'staff-you';
      you.textContent = 'You';
      name.append(you);
    }
    const created = document.createElement('div');
    created.className = 'staff-created';
    const createdAt = parseServerDate(item.created_at);
    created.textContent = 'Created ' + (Number.isNaN(createdAt.getTime()) ? fmt(item.created_at) : new Intl.DateTimeFormat(undefined, {dateStyle: 'medium'}).format(createdAt));
    created.title = fmt(item.created_at);
    identity.append(name, created);
    if (item.email_conflict) {
      const conflict = document.createElement('span');
      conflict.className = 'status-dot conflict';
      conflict.textContent = 'Duplicate email';
      identity.append(conflict);
    }

    const email = document.createElement('div');
    email.className = 'staff-email';
    email.setAttribute('role', 'cell');
    email.textContent = item.email || '—';
    email.title = item.email || '';

    const role = document.createElement('span');
    role.className = 'role-badge';
    role.setAttribute('role', 'cell');
    role.textContent = staffRoleLabel(item.role);

    const active = document.createElement('span');
    active.className = 'status-dot' + (item.is_active ? '' : ' off');
    active.setAttribute('role', 'cell');
    active.textContent = item.is_active ? 'Active' : 'Deactivated';

    const actions = document.createElement('div');
    actions.className = 'staff-row-actions';
    actions.setAttribute('role', 'cell');
    const label = item.name || item.email || 'staff account';
    const edit = staffButton('Edit', () => beginStaffEdit(item), '', 'edit');
    edit.setAttribute('aria-label', (item.is_active ? 'Edit ' : 'Edit or reactivate ') + label);
    actions.append(edit);
    if (!isSelf) {
      const remove = staffButton('Delete', () => deleteStaffAccount(item), 'danger', 'trash');
      remove.setAttribute('aria-label', 'Delete ' + label);
      actions.append(remove);
    }
    row.append(identity, email, role, active, actions);
    fragment.append(row);
  }
  container.append(fragment);
}

function renderStaffLoading() {
  const container = document.createElement('div');
  container.className = 'loading-list';
  for (let index = 0; index < 4; index += 1) {
    const row = document.createElement('div');
    row.className = 'skeleton-row';
    const first = document.createElement('div');
    first.className = 'skeleton skeleton-line medium';
    const second = document.createElement('div');
    second.className = 'skeleton skeleton-line long';
    row.append(first, second);
    container.append(row);
  }
  $('staffList').replaceChildren(container);
}

async function loadStaffAccounts() {
  if (!canManageStaff()) return;
  renderStaffLoading();
  $('refreshStaffBtn').disabled = true;
  try {
    const data = await api('/api/support/staff');
    renderStaffAccounts(data.items || []);
  } catch (error) {
    $('staffList').replaceChildren(emptyMarkup('Could not load staff accounts', 'Try refreshing the staff directory.', 'alert').outer);
    showStaffFeedback('staffError', error.message);
  } finally {
    $('refreshStaffBtn').disabled = false;
  }
}

function handleStaffConflict(error) {
  const detail = error && error.detail;
  if (detail && detail.code === 'email_conflict' && detail.existing_staff) {
    state.conflictStaff = detail.existing_staff;
    showStaffFeedback('staffError', detail.message || error.message);
    $('editConflictStaffBtn').classList.remove('hidden');
    return true;
  }
  return false;
}

function passwordByteLength(value) {
  return new TextEncoder().encode(value || '').length;
}

function validatePasswordPair(passwordId, confirmationId, errorId, required) {
  const password = $(passwordId).value;
  const confirmation = $(confirmationId).value;
  let message = '';
  if (required && !password) message = 'A password is required.';
  else if (password && password.length < 12) message = 'Use at least 12 characters.';
  else if (password && passwordByteLength(password) > 72) message = 'Use a password no longer than 72 UTF-8 bytes.';
  else if (password !== confirmation) message = 'Passwords do not match.';
  setFieldError(confirmationId, errorId, message);
  return !message;
}

async function createStaffAccount(event) {
  event.preventDefault();
  if (state.staffBusy || !canManageStaff()) return;
  const form = $('createStaffForm');
  clearStaffFeedback();
  if (!form.reportValidity() || !validatePasswordPair('createStaffPassword', 'createStaffPasswordConfirm', 'createPasswordMatchError', true)) return;
  const payload = {
    name: $('createStaffName').value.trim(),
    email: $('createStaffEmail').value.trim(),
    role: $('createStaffRole').value,
    password: $('createStaffPassword').value
  };
  state.staffBusy = true;
  setButtonBusy($('createStaffSubmitBtn'), true, 'Creating…');
  try {
    const data = await api('/api/support/staff', {method:'POST', body: JSON.stringify(payload)});
    resetCreateStaffForm();
    setStaffView('list');
    showStaffFeedback('staffSuccess', 'Created ' + data.staff.name + ' (' + staffRoleLabel(data.staff.role) + ').');
    notify('Staff account created.');
    await loadStaffAccounts();
  } catch (error) {
    if (!handleStaffConflict(error)) showStaffFeedback('staffError', error.message);
  } finally {
    state.staffBusy = false;
    setButtonBusy($('createStaffSubmitBtn'), false);
    $('createStaffPassword').value = '';
    $('createStaffPasswordConfirm').value = '';
  }
}

async function updateStaffAccount(event) {
  event.preventDefault();
  if (state.staffBusy || !canManageStaff() || !state.editingStaffId) return;
  const form = $('editStaffForm');
  clearStaffFeedback();
  if (!form.reportValidity() || !validatePasswordPair('editStaffPassword', 'editStaffPasswordConfirm', 'editPasswordMatchError', false)) return;
  const original = state.editingStaffOriginal || {};
  const name = $('editStaffName').value.trim();
  const email = $('editStaffEmail').value.trim();
  const role = $('editStaffRole').value;
  const active = $('editStaffActive').checked;
  const password = $('editStaffPassword').value;
  const payload = {};
  if (name !== (original.name || '')) payload.name = name;
  if (email !== (original.email || '')) payload.email = email;
  if (role !== (original.role || '')) payload.role = role;
  if (active !== Boolean(original.is_active)) payload.is_active = active;
  if (password) payload.password = password;
  if (!Object.keys(payload).length) {
    showStaffFeedback('staffError', 'No changes to save.');
    return;
  }
  state.staffBusy = true;
  setButtonBusy($('editStaffSubmitBtn'), true, 'Saving…');
  try {
    const data = await api('/api/support/staff/' + encodeURIComponent(state.editingStaffId), {method:'PATCH', body: JSON.stringify(payload)});
    applyStaffProfileName(data.staff.id, data.staff.name);
    const sessionRevoked = Boolean(data.staff.current_session_revoked);
    resetEditStaffForm();
    setStaffView('list');
    showStaffFeedback('staffSuccess', 'Updated ' + data.staff.name + ' (' + staffRoleLabel(data.staff.role) + ').');
    notify('Staff account updated.');
    if (sessionRevoked) {
      await alertDialog({title: 'Sign in again', message: 'Your credentials changed, so this staff session was revoked. Please sign in again.', confirmLabel: 'Go to sign in', tone: 'warning'});
      showLogin();
    } else {
      await loadStaffAccounts();
    }
  } catch (error) {
    if (!handleStaffConflict(error)) showStaffFeedback('staffError', error.message);
  } finally {
    state.staffBusy = false;
    setButtonBusy($('editStaffSubmitBtn'), false);
    $('editStaffPassword').value = '';
    $('editStaffPasswordConfirm').value = '';
  }
}

async function deleteStaffAccount(item) {
  if (state.staffBusy || !item) return;
  if (String(item.id) === String(state.agent && state.agent.id)) {
    showStaffFeedback('staffError', 'You cannot delete your own active account.');
    return;
  }
  if (!(await confirmDialog({title: 'Delete staff account?', message: 'Delete ' + (item.name || item.email || 'this staff account') + '? The account, credentials, private quick replies and activity records are permanently deleted and removed from statistics. Past conversations keep its name and assignment; transfer active work first.', confirmLabel: 'Delete account', tone: 'danger'}))) return;
  state.staffBusy = true;
  try {
    await api('/api/support/staff/' + encodeURIComponent(item.id), {method:'DELETE'});
    showStaffFeedback('staffSuccess', 'Deleted ' + (item.name || item.email) + '.');
    notify('Staff account deleted.');
    clearAgentStatistics();
    await loadStaffAccounts();
  } catch (error) {
    showStaffFeedback('staffError', error.message);
  } finally {
    state.staffBusy = false;
  }
}

function showStartChatError(message) {
  const element = $('startChatError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

async function openStartChatModal(contact = null) {
  if (!state.authenticated || state.chatBusy) return;
  showStartChatError('');
  $('startChatForm').reset();
  if (contact) {
    $('startChatPhone').value = contact.normalized_phone || contact.phone || '';
    $('startChatName').value = contact.display_name || contact.name || '';
  }
  $('startChatLine').replaceChildren(new Option('Loading WhatsApp lines…', ''));
  $('startChatLinePreview').replaceChildren();
  openDialog('startChatModal', contact ? 'startChatBody' : 'startChatPhone');
  try {
    const data = await api('/api/support/chat-lines');
    state.chatLines = Array.isArray(data.items) ? data.items : [];
    const select = $('startChatLine');
    select.replaceChildren();
    if (!state.chatLines.length) {
      select.append(new Option('No enabled WhatsApp lines', ''));
      $('startChatLinePreview').replaceChildren();
      showStartChatError('No WhatsApp line is enabled for outbound chats.');
      $('confirmStartChatBtn').disabled = true;
      return;
    }
    for (const line of state.chatLines) {
      const option = new Option(supportNumberLabel(line), line.phone_number_id);
      option.title = supportNumberRaw(line);
      select.append(option);
    }
    renderStartChatLinePreview();
    $('confirmStartChatBtn').disabled = false;
  } catch (error) {
    showStartChatError(error.message);
    $('confirmStartChatBtn').disabled = true;
  }
}

const CONTACT_SOURCE_NOTES = {
  manual: 'This contact is saved in the dashboard.',
  google: 'From Google Contacts. Your changes are saved in the dashboard; Google Contacts is not changed.',
  vcf: 'Imported from a VCF file. Your changes are saved in the dashboard.'
};

function showContactEditError(message) {
  const element = $('contactEditError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function openContactEditModal(contact) {
  if (!state.authenticated || !contact?.contact_key || state.activeModal) return;
  state.contactEditTarget = contact;
  showContactEditError('');
  $('contactEditName').value = contact.display_name || '';
  // Numbers stored without a country code are shown as-is so staff can add it.
  const digits = String(contact.normalized_phone || '').replace(/\D/g, '');
  $('contactEditPhone').value = digits ? (digits.startsWith('0') ? digits : '+' + digits) : '';
  text($('contactEditSubtitle'), contact.display_name || $('contactEditPhone').value || 'Contact');
  const source = String(contact.contact_key).split(':')[0];
  text($('contactEditNote'), CONTACT_SOURCE_NOTES[source] || CONTACT_SOURCE_NOTES.manual);
  openDialog('contactEditModal', 'contactEditName');
}

function closeContactEditModal() {
  if (state.contactEditBusy) return;
  state.contactEditTarget = null;
  showContactEditError('');
  closeDialog('contactEditModal');
}

async function saveContactEdit(event) {
  event.preventDefault();
  const contact = state.contactEditTarget;
  if (!contact || state.contactEditBusy) return;
  const name = $('contactEditName').value.trim();
  const phone = $('contactEditPhone').value.trim();
  if (!name) { showContactEditError('Enter the contact name.'); $('contactEditName').focus(); return; }
  if (!phone) { showContactEditError('Enter the WhatsApp number.'); $('contactEditPhone').focus(); return; }
  state.contactEditBusy = true;
  setButtonBusy($('saveContactEditBtn'), true, 'Saving…');
  showContactEditError('');
  try {
    await api('/api/support/contacts', {method: 'PUT', body: JSON.stringify({contact_key: contact.contact_key, display_name: name, phone})});
    state.contactEditBusy = false;
    closeContactEditModal();
    notify('Contact saved.');
    if (state.view === 'contacts') await loadQueue();
  } catch (error) {
    showContactEditError(error.message);
  } finally {
    state.contactEditBusy = false;
    setButtonBusy($('saveContactEditBtn'), false);
  }
}

function renderStartChatLinePreview() {
  const select = $('startChatLine');
  const preview = $('startChatLinePreview');
  const line = (state.chatLines || []).find(item => String(item.phone_number_id) === String(select.value));
  if (!line) { preview.replaceChildren(); select.title = ''; return; }
  select.title = supportNumberRaw(line);
  preview.replaceChildren(document.createTextNode('Selected line: '), createSupportNumberLabel(line));
}

function closeStartChatModal() {
  if (state.chatBusy) return;
  showStartChatError('');
  closeDialog('startChatModal');
}

async function submitStartChat(event) {
  event.preventDefault();
  if (state.chatBusy || !$('startChatForm').reportValidity()) return;
  state.chatBusy = true;
  setButtonBusy($('confirmStartChatBtn'), true, 'Starting…');
  showStartChatError('');
  try {
    const data = await api('/api/support/chats', {method:'POST', body: JSON.stringify({
      phone_number: $('startChatPhone').value.trim(),
      business_phone_number_id: $('startChatLine').value,
      customer_name: $('startChatName').value.trim() || null,
      body: $('startChatBody').value.trim()
    })});
    closeDialog('startChatModal');
    setQueueView('chats', {load: false});
    state.page = 1;
    saveSupportSelection();
    await loadQueue();
    const chat = data.chat || data.ticket;
    if (chat && chat.id) await openTicket(chat.id, {conversationOrigin: 'STAFF'});
    notify(data.reopened ? 'Existing WhatsApp chat reopened with its full history.' : 'WhatsApp chat started.');
  } catch (error) {
    showStartChatError(error.message);
  } finally {
    state.chatBusy = false;
    setButtonBusy($('confirmStartChatBtn'), false);
  }
}

function renderGoogleContactsStatus(status) {
  state.googleContactsStatus = status || null;
  const connected = Boolean(status && status.connected);
  const configured = Boolean(status && status.configured);
  const account = status && status.account;
  text($('googleContactsTitle'), connected ? (account?.name || 'Google Contacts') : 'Google Contacts');
  text($('googleContactsStatus'), connected
    ? ((account?.email || 'Connected Google account') + (account?.last_synced_at ? ' · synced ' + relativeTime(account.last_synced_at) : ' · not synced yet'))
    : (configured ? 'Not connected' : 'Google OAuth is not configured on this server'));
  const manager = canManageGoogleContacts();
  $('googleContactsConnectBtn').classList.toggle('hidden', connected || !manager);
  $('googleContactsConnectBtn').disabled = state.googleContactsBusy || !configured;
  // Syncing only refreshes the already-connected shared account, so every
  // authenticated staff member may do it. Account ownership changes remain
  // Supervisor/Admin-only.
  $('googleContactsSyncBtn').classList.toggle('hidden', !connected);
  $('googleContactsDisconnectBtn').classList.toggle('hidden', !connected || !manager);
  $('googleContactsSyncBtn').disabled = state.googleContactsBusy;
  $('googleContactsDisconnectBtn').disabled = state.googleContactsBusy;
}

async function loadGoogleContactsStatus() {
  if (!state.authenticated) return;
  const status = await api('/api/support/google-contacts/status');
  renderGoogleContactsStatus(status);
}

async function connectGoogleContacts() {
  if (state.googleContactsBusy) return;
  state.googleContactsBusy = true;
  renderGoogleContactsStatus(state.googleContactsStatus || {configured: true, connected: false});
  try {
    const data = await api('/api/support/google-contacts/connect', {method:'POST'});
    const popup = window.open(data.authorization_url, 'googleContactsOAuth', 'popup=yes,width=560,height=720,resizable=yes,scrollbars=yes');
    if (!popup) throw new Error('Allow pop-ups for this support site to connect Google Contacts.');
    popup.focus();
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    state.googleContactsBusy = false;
    renderGoogleContactsStatus(state.googleContactsStatus || {configured: true, connected: false});
  }
}

async function syncGoogleContacts() {
  if (state.googleContactsBusy) return;
  state.googleContactsBusy = true;
  renderGoogleContactsStatus(state.googleContactsStatus);
  try {
    const data = await api('/api/support/google-contacts/sync', {method:'POST'});
    notify('Google Contacts synced · ' + Number(data.contact_count || 0) + ' contacts available.');
    await Promise.all([loadGoogleContactsStatus(), refreshAllQueueCounts()]);
    if (state.view === 'contacts') await loadQueue({silent: true});
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    state.googleContactsBusy = false;
    renderGoogleContactsStatus(state.googleContactsStatus);
  }
}

function chooseVcfContactsFile() {
  if (state.vcfImportBusy) return;
  $('vcfContactsInput').click();
}

async function importSelectedVcfContacts() {
  const input = $('vcfContactsInput');
  const file = input.files && input.files[0];
  if (!file || state.vcfImportBusy) return;
  if (!file.name.toLocaleLowerCase().endsWith('.vcf')) {
    input.value = '';
    notify('Choose a .vcf contacts file.', 'error');
    return;
  }
  if (file.size > 10 * 1024 * 1024) {
    input.value = '';
    notify('The VCF file is larger than the 10 MB import limit.', 'error');
    return;
  }

  state.vcfImportBusy = true;
  const button = $('importVcfContactsBtn');
  setButtonBusy(button, true, 'Importing…');
  try {
    const form = new FormData();
    form.append('file', file, file.name);
    const data = await api('/api/support/contacts/import-vcf', {method:'POST', body: form});
    const imported = Number(data.imported || 0);
    const updated = Number(data.updated || 0);
    const duplicates = Number(data.duplicates_removed || 0);
    const skipped = Number(data.skipped_without_phone || 0) + Number(data.skipped_invalid_phone || 0);
    let message = 'VCF imported · ' + imported + ' new contact' + (imported === 1 ? '' : 's');
    if (updated) message += ' · ' + updated + ' updated';
    if (duplicates) message += ' · ' + duplicates + ' duplicate' + (duplicates === 1 ? '' : 's') + ' merged';
    if (skipped) message += ' · ' + skipped + ' skipped';
    notify(message);
    await refreshAllQueueCounts();
    if (state.view === 'contacts') await loadQueue({silent: true});
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    input.value = '';
    state.vcfImportBusy = false;
    setButtonBusy(button, false);
  }
}

async function disconnectGoogleContacts() {
  if (state.googleContactsBusy || !(await confirmDialog({title: 'Disconnect Google Contacts?', message: 'Disconnect Google Contacts and remove its synced contacts from this support account? Manually saved contacts will remain.', confirmLabel: 'Disconnect', tone: 'danger'}))) return;
  state.googleContactsBusy = true;
  renderGoogleContactsStatus(state.googleContactsStatus);
  try {
    await api('/api/support/google-contacts', {method:'DELETE'});
    notify('Google Contacts disconnected.');
    await Promise.all([loadGoogleContactsStatus(), refreshAllQueueCounts()]);
    if (state.view === 'contacts') await loadQueue({silent: true});
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    state.googleContactsBusy = false;
    renderGoogleContactsStatus(state.googleContactsStatus);
  }
}

function forwardChoiceRow({kind, id, name, phone, subtitle, badgeLabel = null}) {
  const button = document.createElement('button');
  button.type = 'button';
  const key = kind + ':' + id;
  button.className = 'contact-picker-row' + (state.forwardSelection?.key === key ? ' selected' : '');
  const copy = document.createElement('div');
  const strong = document.createElement('strong');
  strong.dir = 'auto';
  strong.textContent = name || phone || 'Contact';
  const detail = document.createElement('span');
  detail.textContent = subtitle || phone || '';
  copy.append(strong, detail);
  const badge = document.createElement('span');
  badge.className = 'contact-source' + (badgeLabel === 'Google' ? ' google' : badgeLabel === 'VCF' ? ' vcf' : '');
  badge.textContent = badgeLabel || (kind === 'contact' ? 'Contact' : 'Conversation');
  button.append(copy, badge);
  button.onclick = () => {
    state.forwardSelection = {key, kind, id, name, phone};
    renderForwardChoices($('forwardSearch').value.trim());
    $('confirmForwardBtn').disabled = false;
  };
  return button;
}

function renderForwardChoices(query = '') {
  const normalized = String(query || '').trim().toLocaleLowerCase();
  const contactsContainer = $('forwardContactsList');
  const conversationsContainer = $('forwardConversationsList');
  contactsContainer.replaceChildren();
  conversationsContainer.replaceChildren();
  const contacts = (state.forwardContacts || []).filter(item => {
    if (!normalized) return true;
    return [item.display_name, item.normalized_phone, item.email].filter(Boolean).join(' ').toLocaleLowerCase().includes(normalized);
  });
  const conversations = (state.forwardConversations || []).filter(item => {
    if (!normalized) return true;
    return [item.customer_name, item.whatsapp_phone, item.ticket_reference].filter(Boolean).join(' ').toLocaleLowerCase().includes(normalized);
  });
  if (!contacts.length) contactsContainer.append(emptyMarkup('No matching contacts', 'Save a contact, sync Google Contacts, or import a VCF file.', 'users').outer);
  else for (const item of contacts) contactsContainer.append(forwardChoiceRow({kind:'contact', id:item.contact_key, name:item.display_name, phone:item.normalized_phone, subtitle:(item.email ? item.normalized_phone + ' · ' + item.email : item.normalized_phone), badgeLabel:item.source === 'google' ? 'Google' : item.source === 'vcf' ? 'VCF' : 'Saved'}));
  if (!conversations.length) conversationsContainer.append(emptyMarkup('No matching active conversations', 'You can still forward directly to a contact.', 'message').outer);
  else for (const item of conversations) conversationsContainer.append(forwardChoiceRow({kind:'conversation', id:Number(item.id), name:item.customer_name || item.whatsapp_phone, phone:item.whatsapp_phone, subtitle:item.ticket_reference || ('Conversation #' + item.id)}));
}

async function loadForwardChoices(query = '') {
  const requestId = ++state.forwardChoiceRequest;
  const [contacts, mine, chats] = await Promise.all([
    api('/api/support/contacts?page=1&page_size=100&q=' + encodeURIComponent(query)),
    api('/api/support/tickets?view=mine&page=1&page_size=100'),
    api('/api/support/tickets?view=chats&page=1&page_size=100')
  ]);
  if (requestId !== state.forwardChoiceRequest) return;
  state.forwardContacts = Array.isArray(contacts.items) ? contacts.items : [];
  const all = [...(mine.items || []), ...(chats.items || [])];
  const seen = new Set();
  state.forwardConversations = all.filter(item => {
    if (!item || seen.has(String(item.id))) return false;
    seen.add(String(item.id));
    return String(item.id) !== String(state.ticketId)
      && String(item.current_assignee_id || '') === String(state.agent && state.agent.id || '')
      && !['RESOLVED', 'CLOSED'].includes(String(item.status || ''))
      && Boolean(item.active_key);
  });
  renderForwardChoices(query);
}

function showForwardError(message) {
  const element = $('forwardError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

async function openForwardModal(message) {
  if (!message || !message.id || state.forwardBusy) return;
  state.forwardTarget = {ticketId: Number(state.ticketId), messageId: Number(message.id), preview: messagePreview(message)};
  state.forwardSelection = null;
  state.forwardContacts = [];
  state.forwardConversations = [];
  text($('forwardSubtitle'), 'Forward: ' + state.forwardTarget.preview);
  showForwardError('');
  $('forwardSearch').value = '';
  $('forwardContactsList').replaceChildren(emptyMarkup('Loading contacts', 'Loading saved and Google contacts…', 'users').outer);
  $('forwardConversationsList').replaceChildren(emptyMarkup('Loading conversations', 'Loading active conversations…', 'message').outer);
  $('confirmForwardBtn').disabled = true;
  openDialog('forwardModal', 'forwardSearch');
  try {
    await loadForwardChoices('');
  } catch (error) {
    showForwardError(error.message);
  }
}

function closeForwardModal() {
  if (state.forwardBusy) return;
  state.forwardTarget = null;
  state.forwardSelection = null;
  state.forwardChoiceRequest += 1;
  state.forwardContacts = [];
  state.forwardConversations = [];
  showForwardError('');
  closeDialog('forwardModal');
}

async function confirmForward() {
  if (state.forwardBusy || !state.forwardTarget || !state.forwardSelection) return;
  state.forwardBusy = true;
  setButtonBusy($('confirmForwardBtn'), true, 'Forwarding…');
  showForwardError('');
  try {
    if (state.forwardSelection.kind === 'conversation') {
      await api('/api/support/tickets/' + state.forwardTarget.ticketId + '/messages/' + state.forwardTarget.messageId + '/forward', {
        method:'POST', body: JSON.stringify({destination_ticket_id: Number(state.forwardSelection.id)})
      });
    } else {
      await api('/api/support/tickets/' + state.forwardTarget.ticketId + '/messages/' + state.forwardTarget.messageId + '/forward-contact', {
        method:'POST', body: JSON.stringify({phone_number: state.forwardSelection.phone, customer_name: state.forwardSelection.name || null})
      });
    }
    closeDialog('forwardModal');
    state.forwardTarget = null;
    state.forwardSelection = null;
    notify('Message forwarded.');
    refreshAllQueueCounts().catch(() => {});
  } catch (error) {
    showForwardError(error.message);
  } finally {
    state.forwardBusy = false;
    setButtonBusy($('confirmForwardBtn'), false);
  }
}

function showTransferError(message) {
  const element = $('transferError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function closeTransferModal() {
  if (state.transferBusy) return;
  closeDialog('transferModal');
  showTransferError('');
  $('transferTarget').replaceChildren();
  state.transferTargets = [];
}

async function openTransferModal() {
  const data = state.currentTicketData;
  if (!data || !data.permissions.can_transfer || state.transferBusy) return;
  const chat = isStaffChat(data.ticket);
  text($('transferModalTitle'), chat ? 'Transfer chat' : 'Transfer ticket');
  text($('transferModalSubtitle'), chat ? 'Assign this chat to another staff member.' : 'Assign this conversation to another staff member.');
  $('confirmTransferBtn').lastChild.textContent = chat ? 'Transfer chat' : 'Transfer ticket';
  showTransferError('');
  $('transferTarget').replaceChildren(new Option('Loading staff…', ''));
  $('confirmTransferBtn').disabled = true;
  openDialog('transferModal', 'transferTarget');
  try {
    const directory = await api('/api/support/transfer-targets');
    const currentId = String(data.ticket.current_assignee_id || '');
    state.transferTargets = (directory.items || []).filter(item => String(item.id) !== currentId);
    const select = $('transferTarget');
    select.replaceChildren();
    if (!state.transferTargets.length) {
      select.append(new Option('No other active staff available', ''));
      showTransferError('There is no other active staff member available for transfer.');
      return;
    }
    for (const item of state.transferTargets) select.append(new Option(item.name + ' · ' + staffRoleLabel(item.role), item.id));
    $('confirmTransferBtn').disabled = false;
    select.focus();
  } catch (error) {
    showTransferError(error.message);
  }
}

async function confirmTransfer() {
  if (state.transferBusy || !state.ticketId) return;
  const targetId = Number($('transferTarget').value);
  if (!targetId) {
    showTransferError('Choose a staff member.');
    return;
  }
  const target = state.transferTargets.find(item => Number(item.id) === targetId);
  if (!target) return;
  const chat = isStaffChat(state.currentTicketData?.ticket);
  if (!(await confirmDialog({title: 'Transfer ' + (chat ? 'chat' : 'ticket') + '?', message: 'Transfer this ' + (chat ? 'chat' : 'ticket') + ' to ' + target.name + '? You will become read-only unless it is transferred back to you.', confirmLabel: 'Transfer', tone: 'default'}))) return;
  state.transferBusy = true;
  setButtonBusy($('confirmTransferBtn'), true, 'Transferring…');
  showTransferError('');
  try {
    await api('/api/support/tickets/' + state.ticketId + '/transfer', {method:'POST', body: JSON.stringify({target_staff_id: targetId})});
    closeDialog('transferModal');
    state.transferTargets = [];
    notify((chat ? 'Chat' : 'Ticket') + ' transferred to ' + target.name + '.');
    await openTicket(state.ticketId, {silent: true, refreshQueue: true});
  } catch (error) {
    showTransferError(error.message);
  } finally {
    state.transferBusy = false;
    setButtonBusy($('confirmTransferBtn'), false);
  }
}

$('loginForm').addEventListener('submit', login);
$('sessionRetryBtn').addEventListener('click', bootstrap);
$('loginThemeBtn').addEventListener('click', toggleTheme);
$('themeBtn').addEventListener('click', toggleTheme);
$('notificationSoundBtn').addEventListener('click', toggleNotificationSounds);
window.addEventListener('storage', syncNotificationSoundStorage);
syncNotificationSoundControls();
$('logoutBtn').addEventListener('click', logout);
$('startChatBtn').addEventListener('click', () => openStartChatModal());
$('messagePreferencesBtn').addEventListener('click', openMessagePreferencesModal);
$('aiHeaderBtn').addEventListener('click', openAiSettingsModal);
$('manageStaffBtn').addEventListener('click', openStaffModal);
$('manageWhatsAppBtn').addEventListener('click', openWhatsAppModal);
$('googleContactsConnectBtn').addEventListener('click', connectGoogleContacts);
$('importVcfContactsBtn').addEventListener('click', chooseVcfContactsFile);
$('vcfContactsInput').addEventListener('change', importSelectedVcfContacts);
$('googleContactsSyncBtn').addEventListener('click', syncGoogleContacts);
$('googleContactsDisconnectBtn').addEventListener('click', disconnectGoogleContacts);
$('refreshQueueBtn').addEventListener('click', () => loadQueue());
$('updateReloadBtn').addEventListener('click', () => { saveSupportSelection(); markPageLeaving(); window.location.reload(); });
$('queue').addEventListener('keydown', handleQueueKeydown);
$('queueSearch').addEventListener('input', event => {
  state.query = event.target.value;
  if (state.view === 'contacts') {
    state.page = 1;
    if (state.globalSearchTimer) clearTimeout(state.globalSearchTimer);
    state.globalSearchTimer = setTimeout(() => loadQueue({silent: true}), 220);
    return;
  }
  if (state.query.trim().length >= 2) {
    state.globalSearchBusy = true;
    renderQueue();
    scheduleGlobalMessageSearch();
  } else {
    state.globalSearchResults = [];
    state.globalSearchRequest += 1;
    if (state.globalSearchTimer) { clearTimeout(state.globalSearchTimer); state.globalSearchTimer = null; }
    renderQueue();
  }
});
$('queueSearch').addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.query) {
    event.preventDefault();
    clearQueueSearch();
  }
});
$('clearQueueSearchBtn').addEventListener('click', clearQueueSearch);
$('tabs').addEventListener('click', event => {
  const button = event.target.closest('button[data-view]');
  if (button) setQueueView(button.dataset.view);
});
$('mobileBackBtn').addEventListener('click', closeMobileTicket);
$('contextToggleBtn').addEventListener('click', toggleContextPanel);
$('conversationSearchBtn').addEventListener('click', openConversationSearch);
$('conversationSearchClose').addEventListener('click', () => closeConversationSearch());
$('conversationSearchPrev').addEventListener('click', () => moveConversationSearch(-1));
$('conversationSearchNext').addEventListener('click', () => moveConversationSearch(1));
$('conversationSearchInput').addEventListener('input', scheduleConversationSearch);
$('conversationSearchInput').addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    event.preventDefault();
    closeConversationSearch();
  } else if (event.key === 'Enter') {
    event.preventDefault();
    if (event.shiftKey) moveConversationSearch(-1);
    else moveConversationSearch(1);
  }
});
$('closeContextBtn').addEventListener('click', () => closeContextPanel());
$('contextBackdrop').addEventListener('click', () => closeContextPanel());
$('dismissErrorBtn').addEventListener('click', () => showError(''));
$('viewSharedHistoryBtn').addEventListener('click', jumpToSharedHistory);
$('scrollToLatestBtn').addEventListener('click', jumpToCurrentConversation);
$('conversation').addEventListener('scroll', updateScrollLatestButton, {passive: true});

$('imageBtn').addEventListener('click', chooseImage);
$('voiceBtn').addEventListener('click', toggleVoiceRecording);
$('locationBtn').addEventListener('click', openLocationModal);
$('cancelRecordingBtn').addEventListener('click', cancelVoiceRecording);
$('cancelReplyBtn').addEventListener('click', clearReplyTarget);
$('cancelEditBtn').addEventListener('click', () => cancelMessageEdit());
$('cancelMessageDeleteBtn').addEventListener('click', closeMessageDelete);
$('cancelMessageDeleteActionBtn').addEventListener('click', closeMessageDelete);
$('deleteMeBtn').addEventListener('click', () => deleteMessage('me'));
$('deleteEveryoneBtn').addEventListener('click', () => deleteMessage('everyone'));
$('imageInput').addEventListener('change', imageSelected);
installComposerFileInput();
$('sendBtn').addEventListener('click', sendMessage);
$('message').addEventListener('input', () => {
  updateComposerCount();
  resizeComposer();
  updateComposerControls();
});
$('message').addEventListener('keydown', event => {
  if (event.key === 'Escape' && state.editTarget) { event.preventDefault(); cancelMessageEdit(); return; }
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    sendMessage();
  }
});

$('closeMessagePreferencesBtn').addEventListener('click', closeMessagePreferencesModal);
$('cancelMessagePreferencesBtn').addEventListener('click', closeMessagePreferencesModal);
$('messagePreferencesForm').addEventListener('submit', saveMessagePreferences);
$('quickRepliesBtn').addEventListener('click', openQuickRepliesModal);
$('closeContactEditBtn').addEventListener('click', closeContactEditModal);
$('cancelContactEditBtn').addEventListener('click', closeContactEditModal);
$('contactEditForm').addEventListener('submit', saveContactEdit);
$('closeQuickRepliesBtn').addEventListener('click', closeQuickRepliesModal);
$('addQuickReplyBtn').addEventListener('click', () => showQuickReplyForm(null));
$('cancelQuickReplyBtn').addEventListener('click', showQuickReplyList);
$('quickReplyForm').addEventListener('submit', saveQuickReply);
$('quickRepliesSearch').addEventListener('input', renderQuickReplies);
$('quickReplyAudience').addEventListener('change', updateQuickReplyAudience);
for (const [index, [kind, suffix]] of Object.entries({text: 'Text', pdf: 'Pdf', location: 'Location'}).entries()) {
  const button = $('quickReplyTab' + suffix);
  button.addEventListener('click', () => selectQuickReplyTab(kind));
  button.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (index + (event.key === 'ArrowRight' ? 1 : 2)) % 3;
    selectQuickReplyTab(['text', 'pdf', 'location'][target], true);
  });
}
$('quickReplyBody').addEventListener('input', updateQuickReplyCount);
$('quickReplyPdf').addEventListener('change', updateQuickReplyCount);
$('customGreetingMessage').addEventListener('input', updateMessagePreferenceCounts);
$('customResolutionMessage').addEventListener('input', updateMessagePreferenceCounts);

$('closeAiSettingsBtn').addEventListener('click', closeAiSettingsModal);
$('cancelAiSettingsBtn').addEventListener('click', closeAiSettingsModal);
$('aiSettingsForm').addEventListener('submit', saveAiSettings);
$('aiModeManual').addEventListener('change', syncAiSettingsFields);
$('aiModeScheduled').addEventListener('change', syncAiSettingsFields);

$('closeWhatsAppBtn').addEventListener('click', closeWhatsAppModal);
$('addLinkedDeviceBtn').addEventListener('click', addLinkedDeviceNumber);
$('refreshWhatsAppBtn').addEventListener('click', refreshWhatsAppRegistry);
$('wabaSyncForm').addEventListener('submit', syncWabaForm);
$('startChatLine').addEventListener('change', renderStartChatLinePreview);
$('closeLocationBtn').addEventListener('click', closeLocationModal);
$('cancelLocationBtn').addEventListener('click', closeLocationModal);
$('useCurrentLocationBtn').addEventListener('click', useCurrentLocation);
$('locationForm').addEventListener('submit', sendLocation);
$('closeMediaViewerBtn').addEventListener('click', closeMediaViewer);
$('editMediaBtn').addEventListener('click', enterImageEditor);
$('editorSendBtn').addEventListener('click', sendEditedImage);
$('copyMediaBtn').addEventListener('click', () => copyImageToClipboard(state.mediaSource));
$('editorSelectBtn').addEventListener('click', () => setImageEditorMode('select'));
$('editorDrawBtn').addEventListener('click', () => setImageEditorMode('draw'));
$('editorTextBtn').addEventListener('click', () => setImageEditorMode('text'));
$('editorCropBtn').addEventListener('click', () => setImageEditorMode('crop'));
$('editorRotateBtn').addEventListener('click', () => state.imageEditor?.rotate());
$('editorApplyCropBtn').addEventListener('click', () => state.imageEditor?.applyCrop());
$('editorCancelCropBtn').addEventListener('click', () => state.imageEditor?.cancelCrop());
$('editorCopyBtn').addEventListener('click', () => {
  const editor = state.imageEditor;
  if (editor) copyImageToClipboard(() => editor.pngBlob());
});
$('editorUndoBtn').addEventListener('click', () => state.imageEditor?.undo());
$('editorRedoBtn').addEventListener('click', () => state.imageEditor?.redo());
$('editorEditTextBtn').addEventListener('click', () => state.imageEditor?.editText());
$('editorDeleteBtn').addEventListener('click', () => state.imageEditor?.removeSelected());
$('editorColor').addEventListener('change', () => state.imageEditor?.setStyle());
$('editorSize').addEventListener('change', () => state.imageEditor?.setStyle());
$('zoomOutBtn').addEventListener('click', () => changeMediaZoom(-0.25));
$('zoomInBtn').addEventListener('click', () => changeMediaZoom(0.25));
$('zoomResetBtn').addEventListener('click', resetMediaZoom);
$('mediaViewerModal').addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing || event.repeat ||
      !state.imageEditor || event.target.closest('button, textarea, select')) return;
  event.preventDefault();
  sendEditedImage();
});
document.addEventListener('keydown', event => {
  if (event.code !== 'Space' || !state.imageEditor ||
      event.target.closest('input, textarea, button, select')) return;
  window.mediaEditorSpaceDown = true;
  event.preventDefault();
});
window.addEventListener('keyup', event => {
  if (event.code === 'Space') window.mediaEditorSpaceDown = false;
});
window.addEventListener('blur', () => { window.mediaEditorSpaceDown = false; });
$('mediaViewerBody').addEventListener('wheel', event => {
  if (state.activeModal !== 'mediaViewerModal' || $('mediaViewerTools').classList.contains('hidden')) return;
  event.preventDefault();
  zoomMediaAt(state.mediaZoom * Math.exp(-event.deltaY * 0.0015), event.clientX, event.clientY);
}, {passive: false});
const mediaPointers = new Map();
let lastPinch = null;
const viewerBody = $('mediaViewerBody');
viewerBody.addEventListener('pointerdown', event => {
  if (!state.mediaSource) return;
  const editor = state.imageEditor;
  const navigating = editor && (event.button === 1 || window.mediaEditorSpaceDown ||
    (editor.mode === 'select' && !editor.pointer && state.mediaZoom > 1));
  if (editor && !navigating && event.pointerType !== 'touch') return;
  mediaPointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
  if (mediaPointers.size === 2 && editor) {
    editor.cancelActiveStroke();
    for (const pointerId of mediaPointers.keys()) viewerBody.setPointerCapture(pointerId);
  } else if (navigating || !editor) viewerBody.setPointerCapture(event.pointerId);
  if (mediaPointers.size === 2) lastPinch = null;
  if (state.mediaZoom > 1) viewerBody.classList.add('panning');
  if (navigating || mediaPointers.size > 1 || !editor) event.preventDefault();
});
viewerBody.addEventListener('pointermove', event => {
  if (!mediaPointers.has(event.pointerId)) return;
  if (state.imageEditor?.pointer && mediaPointers.size === 1) return;
  const previous = mediaPointers.get(event.pointerId);
  mediaPointers.set(event.pointerId, {x: event.clientX, y: event.clientY});
  if (mediaPointers.size === 1 && state.mediaZoom > 1) {
    state.mediaTranslateX += event.clientX - previous.x;
    state.mediaTranslateY += event.clientY - previous.y;
    applyMediaZoom();
  } else if (mediaPointers.size === 2) {
    const [a, b] = [...mediaPointers.values()];
    const now = {distance: Math.hypot(a.x - b.x, a.y - b.y), x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
    if (lastPinch) {
      zoomMediaAt(state.mediaZoom * now.distance / Math.max(1, lastPinch.distance), lastPinch.x, lastPinch.y);
      state.mediaTranslateX += now.x - lastPinch.x;
      state.mediaTranslateY += now.y - lastPinch.y;
      applyMediaZoom();
    }
    lastPinch = now;
  }
  event.preventDefault();
});
for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) viewerBody.addEventListener(name, event => {
  mediaPointers.delete(event.pointerId);
  lastPinch = null;
  viewerBody.classList.remove('panning');
});
viewerBody.addEventListener('dblclick', event => {
  if (!state.mediaSource || state.imageEditor?.selected >= 0) return;
  zoomMediaAt(state.mediaZoom > 1 ? 1 : 2, event.clientX, event.clientY);
});
window.addEventListener('resize', () => {
  state.imageEditor?.resize();
  applyMediaZoom();
});
$('closeStaffBtn').addEventListener('click', closeStaffModal);
$('createStaffBtn').addEventListener('click', openCreateStaff);
$('refreshStaffBtn').addEventListener('click', loadStaffAccounts);
$('backFromCreateBtn').addEventListener('click', backToStaffList);
$('backFromEditBtn').addEventListener('click', backToStaffList);
$('cancelCreateStaffBtn').addEventListener('click', backToStaffList);
$('cancelEditStaffBtn').addEventListener('click', backToStaffList);
$('editConflictStaffBtn').addEventListener('click', () => {
  if (state.conflictStaff) beginStaffEdit(state.conflictStaff);
});
$('createStaffForm').addEventListener('submit', createStaffAccount);
$('editStaffForm').addEventListener('submit', updateStaffAccount);
$('createStaffPassword').addEventListener('input', () => setFieldError('createStaffPasswordConfirm', 'createPasswordMatchError', ''));
$('createStaffPasswordConfirm').addEventListener('input', () => validatePasswordPair('createStaffPassword', 'createStaffPasswordConfirm', 'createPasswordMatchError', true));
$('editStaffPassword').addEventListener('input', () => setFieldError('editStaffPasswordConfirm', 'editPasswordMatchError', ''));
$('editStaffPasswordConfirm').addEventListener('input', () => validatePasswordPair('editStaffPassword', 'editStaffPasswordConfirm', 'editPasswordMatchError', false));

$('closeStartChatBtn').addEventListener('click', closeStartChatModal);
$('cancelStartChatBtn').addEventListener('click', closeStartChatModal);
$('startChatForm').addEventListener('submit', submitStartChat);
$('closeForwardBtn').addEventListener('click', closeForwardModal);
$('cancelForwardBtn').addEventListener('click', closeForwardModal);
$('confirmForwardBtn').addEventListener('click', confirmForward);
$('forwardSearch').addEventListener('input', event => {
  const query = event.target.value.trim();
  if (state.forwardSearchTimer) clearTimeout(state.forwardSearchTimer);
  renderForwardChoices(query);
  state.forwardSearchTimer = setTimeout(() => {
    loadForwardChoices(query).catch(error => showForwardError(error.message));
  }, 220);
});

$('closeTransferBtn').addEventListener('click', closeTransferModal);
$('cancelTransferBtn').addEventListener('click', closeTransferModal);
$('confirmTransferBtn').addEventListener('click', confirmTransfer);
$('confirmForm').addEventListener('submit', event => { event.preventDefault(); settleConfirmDialog(true); });
$('confirmCancelBtn').addEventListener('click', () => settleConfirmDialog(false));
$('shortcutsHelpBtn').addEventListener('click', openShortcutsHelp);
$('closeShortcutsBtn').addEventListener('click', closeShortcutsHelp);
$('passwordToggleBtn').addEventListener('click', () => {
  const input = $('password');
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  const button = $('passwordToggleBtn');
  const label = show ? 'Hide password' : 'Show password';
  button.setAttribute('aria-pressed', show ? 'true' : 'false');
  button.setAttribute('aria-label', label);
  button.dataset.tooltip = label;
  input.focus();
});

document.addEventListener('keydown', handleGlobalKeydown);
document.addEventListener('copy', handleConversationCopy);
document.addEventListener('selectionchange', flushPendingConversationRender);
$('conversation').addEventListener('pointerdown', beginConversationSelection);
window.addEventListener('pointerup', finishConversationSelection);
window.addEventListener('pointercancel', finishConversationSelection);
window.addEventListener('blur', () => {
  state.conversationSelectionPointer = null;
  flushPendingConversationRender();
});
document.addEventListener('pointerdown', unlockNotificationAudio);
document.addEventListener('click', event => {
  for (const menu of document.querySelectorAll('.action-menu[open]')) {
    if (!menu.contains(event.target)) menu.open = false;
  }
});
window.addEventListener('message', event => {
  if (event.origin !== window.location.origin || !event.data || typeof event.data !== 'object') return;
  if (event.data.type === 'google_contacts_connected') {
    notify(event.data.message || 'Google Contacts connected.');
    Promise.all([loadGoogleContactsStatus(), refreshAllQueueCounts()]).then(() => {
      if (state.view === 'contacts') loadQueue({silent: true});
    }).catch(() => {});
  } else if (event.data.type === 'google_contacts_error') {
    notify(event.data.message || 'Google Contacts connection failed.', 'error');
  }
});
window.addEventListener('resize', syncContextLayout);
window.addEventListener('beforeunload', () => {
  markPageLeaving();
  stopMediaTracks();
});
window.addEventListener('pagehide', markPageLeaving);
window.addEventListener('pageshow', event => {
  state.pageLeaving = false;
  if (event.persisted && state.authenticated) {
    if (state.ticketId) openTicket(state.ticketId, {silent: true}).catch(() => {});
    else loadQueue({silent: true}).catch(() => {});
  }
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) clearAttentionIndicator();
  if (!document.hidden && state.authenticated && !state.busy && !state.activeModal) {
    if (state.ticketId && !state.conversationSearchOpen) openTicket(state.ticketId, {silent: true});
    else if (!state.ticketId) loadQueue({silent: true});
  }
});

restoreLayoutPreferences();
installColumnResizer({id: 'queueResizer', variableName: '--queue-width', storageKey: LAYOUT_STORAGE.queueWidth, minimum: 260, maximum: 520, direction: 1, enabled: () => window.innerWidth > 800});
installColumnResizer({id: 'contextResizer', variableName: '--context-width', storageKey: LAYOUT_STORAGE.contextWidth, minimum: 260, maximum: 520, direction: -1, enabled: () => window.innerWidth > 1100 && state.contextOpen});
updateComposerCount();
updateMessagePreferenceCounts();
updateComposerControls();
updateQueueSummary();
bootstrap();
setInterval(() => {
  if (!state.authenticated || state.busy || state.mediaRecorder || state.activeModal || state.conversationSearchOpen) return;
  const audioPlaying = [...document.querySelectorAll('#conversation audio')].some(audio => !audio.paused && !audio.ended);
  if (state.ticketId && !audioPlaying) openTicket(state.ticketId, {silent: true});
  else if (!state.ticketId) loadQueue({silent: true});
}, Math.max(3000, __POLL_MS__));
setInterval(() => {
  if (state.authenticated && state.activeModal === 'whatsappModal' && !state.whatsappBusy && !state.linkedDeviceBusy) loadLinkedDeviceStatus();
}, 2000);
setInterval(() => {
  if (!state.authenticated || state.busy || state.activeModal) return;
  pollUnassignedActivity();
}, Math.max(2000, __POLL_MS__));
setInterval(() => {
  if (!state.authenticated) return;
  refreshAllQueueCounts();
}, Math.max(5000, __POLL_MS__));
setInterval(() => {
  if (!state.authenticated || state.aiSettingsBusy || document.hidden) return;
  loadAiSettings().catch(error => renderAiSettingsUnavailable(error.message));
}, Math.max(30000, __POLL_MS__));
