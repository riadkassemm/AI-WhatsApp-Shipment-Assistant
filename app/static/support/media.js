function stopMediaTracks() {
  if (!state.mediaStream) return;
  for (const track of state.mediaStream.getTracks()) track.stop();
  state.mediaStream = null;
}

function resetRecordingUi() {
  if (state.recordingTimer) {
    clearInterval(state.recordingTimer);
    state.recordingTimer = null;
  }
  state.mediaRecorder = null;
  state.recordingChunks = [];
  state.recordingStartedAt = 0;
  $('recordingBar').classList.add('hidden');
  text($('recordingTime'), 'Recording 0:00');
  stopMediaTracks();
  updateComposerControls();
}

function cancelVoiceRecording() {
  if (!state.mediaRecorder) return;
  state.discardRecording = true;
  try { state.mediaRecorder.stop(); } catch (_) { resetRecordingUi(); }
}

function clearMediaDraft() {
  for (const draft of state.pendingMediaItems) if (draft.url) URL.revokeObjectURL(draft.url);
  state.pendingMediaItems = [];
  state.pendingMedia = null;
  $('mediaDraft').classList.add('hidden');
  $('mediaDraft').replaceChildren();
  $('imageInput').value = '';
  $('message').maxLength = 4096;
  $('message').placeholder = 'Reply to customer on WhatsApp';
  text($('composerHelp'), 'Messages are sent from the WhatsApp number for this conversation.');
  updateComposerCount();
  updateComposerControls();
}

const MAX_COMPOSER_ATTACHMENTS = 20;
const MAX_COMPOSER_BYTES = 100 * 1024 * 1024;

function removeMediaDraft(id) {
  const draft = state.pendingMediaItems.find(item => item.id === id);
  if (draft?.url) URL.revokeObjectURL(draft.url);
  state.pendingMediaItems = state.pendingMediaItems.filter(item => item.id !== id);
  state.pendingMedia = state.pendingMediaItems[0] || null;
  if (!state.pendingMedia) clearMediaDraft();
  else renderMediaDraft();
}

function renderMediaDraft() {
  const list = $('mediaDraft');
  list.replaceChildren();
  state.pendingMedia = state.pendingMediaItems[0] || null;
  list.className = 'media-draft-list' + (state.pendingMedia ? '' : ' hidden');
  for (const draft of state.pendingMediaItems) {
    const container = document.createElement('div');
    container.className = 'media-draft ' + draft.kind;
    container.dataset.draftId = draft.id;
    const preview = document.createElement('div');
    preview.className = 'media-draft-preview';
    if (draft.kind === 'image' || draft.kind === 'sticker') {
      const img = document.createElement('img');
      img.src = draft.url;
      img.alt = draft.kind === 'sticker' ? 'Selected sticker preview' : 'Selected image preview';
      preview.append(img);
    } else if (draft.kind === 'video') {
      const video = document.createElement('video');
      video.src = draft.url;
      video.muted = true;
      video.preload = 'metadata';
      video.playsInline = true;
      video.setAttribute('aria-label', 'Selected video preview');
      preview.append(video);
    } else if (draft.kind === 'audio') {
      preview.append(createVoiceNotePlayer(draft.url, 'Voice note preview').player);
    } else {
      preview.append(createIcon('file', 'icon icon-lg'));
    }
    const info = document.createElement('div');
    info.className = 'media-draft-info';
    const name = document.createElement('div');
    name.className = 'media-draft-name';
    name.textContent = draft.kind === 'image'
      ? (draft.file.name || 'Selected image')
      : draft.kind === 'video'
        ? (draft.file.name || 'Selected video')
      : draft.kind === 'sticker'
        ? (draft.file.name || 'WhatsApp sticker')
      : draft.kind === 'document'
        ? (draft.file.name || 'PDF document')
        : (draft.file.name || 'Voice note ready');
    const size = document.createElement('div');
    size.className = 'media-draft-size';
    size.textContent = formatBytes(draft.file.size);
    info.append(name, size);
    if (draft.caption) {
      const savedCaption = document.createElement('div'); savedCaption.textContent = draft.caption; info.append(savedCaption);
    }
    const actions = document.createElement('div');
    actions.className = 'media-draft-actions';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn small';
    remove.append(createIcon('trash', 'icon icon-sm'), document.createTextNode('Remove'));
    remove.disabled = state.busy;
    remove.onclick = () => { if (!state.busy) removeMediaDraft(draft.id); };
    if (draft.kind === 'image') {
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'btn small';
      edit.append(createIcon('edit', 'icon icon-sm'), document.createTextNode('Edit'));
      edit.disabled = state.busy || Boolean(draft.submission);
      edit.onclick = () => editPendingImage(draft.id);
      actions.append(edit);
    }
    actions.append(remove);
    container.append(preview, info, actions);

    if (draft.submission) {
      const pending = document.createElement('span');
      pending.textContent = 'Checking previous send on next attempt';
      info.append(pending);
    }
    list.append(container);
  }
  const captionable = state.pendingMediaItems.some(item => ['image', 'document', 'video'].includes(item.kind));
  $('message').maxLength = captionable ? 1024 : 4096;
  $('message').placeholder = captionable ? 'Caption for first image, video or PDF (optional)' : 'Attachments ready to send';
  text($('composerHelp'), state.pendingMediaItems.length + ' attachment(s). Review before sending. Each is sent separately in order.');
  updateComposerCount();
  updateComposerControls();
}

function setPendingMedia(kind, file, {replaceId = null} = {}) {
  if (replaceId) {
    const existing = state.pendingMediaItems.find(item => item.id === replaceId);
    if (!existing || existing.submission) return false;
    URL.revokeObjectURL(existing.url);
    Object.assign(existing, {kind, file, url: URL.createObjectURL(file)});
  } else {
    if (state.pendingMediaItems.length >= MAX_COMPOSER_ATTACHMENTS ||
        state.pendingMediaItems.reduce((sum, item) => sum + item.file.size, 0) + file.size > MAX_COMPOSER_BYTES) {
      showError('Attach up to 20 files, with a combined size of 100 MB.'); return false;
    }
    state.pendingMediaItems.push({kind, file, url: URL.createObjectURL(file), id: crypto.randomUUID()});
  }
  renderMediaDraft();
  return true;
}

function setComposerEnabled(enabled) {
  state.composerEnabled = Boolean(enabled);
  updateComposerControls();
}

function updateComposerCount() {
  const maximum = Number($('message').maxLength > 0 ? $('message').maxLength : 4096);
  const length = $('message').value.length;
  text($('composerCount'), length + ' / ' + maximum);
  $('composerCount').style.color = length >= maximum ? 'var(--danger)' : '';
}

function resizeComposer() {
  const textarea = $('message');
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 154) + 'px';
}

function focusMessageComposer() {
  window.requestAnimationFrame(() => {
    const textarea = $('message');
    if ((!state.composerEnabled && !state.editTarget) || state.busy || !textarea || textarea.disabled) return;
    try { textarea.focus({preventScroll: true}); } catch (_) { textarea.focus(); }
    const end = textarea.value.length;
    try { textarea.setSelectionRange(end, end); } catch (_) {}
  });
}

function updateComposerControls() {
  const enabled = (state.composerEnabled || Boolean(state.editTarget)) && !state.busy;
  const recording = Boolean(state.mediaRecorder);
  const draft = state.pendingMedia;
  const hasBody = Boolean($('message').value.trim());
  const canSend = enabled && !recording && (Boolean(draft) || hasBody);

  $('imageBtn').disabled = !enabled || recording || Boolean(state.editTarget);
  $('voiceBtn').disabled = !enabled || Boolean(state.editTarget) || Boolean(draft);
  $('locationBtn').disabled = !enabled || recording || Boolean(state.editTarget);
  $('voiceBtn').setAttribute('aria-pressed', recording ? 'true' : 'false');
  $('voiceBtn').setAttribute('aria-label', recording ? 'Stop voice recording' : 'Record a voice note');
  $('voiceBtn').setAttribute('data-tooltip', recording ? 'Stop recording' : 'Record voice note');
  setIcon($('voiceBtn'), recording ? 'square' : 'mic');
  $('message').disabled = !enabled || recording || Boolean(draft?.submission) || Boolean(draft && !state.pendingMediaItems.some(item => ['image', 'document', 'video'].includes(item.kind)));
  $('sendBtn').disabled = !canSend;
  for (const replyButton of document.querySelectorAll('.message-reply-btn')) {
    replyButton.disabled = !enabled;
  }
  for (const reactionMenu of document.querySelectorAll('.message-reaction-menu')) {
    reactionMenu.classList.toggle('disabled', !enabled);
    const summary = reactionMenu.querySelector('summary');
    if (summary) summary.setAttribute('aria-disabled', enabled ? 'false' : 'true');
    if (!enabled) reactionMenu.open = false;
  }
  for (const forwardButton of document.querySelectorAll('.message-forward-btn')) {
    // Forwarding is independent from reply ownership. A resolved/history
    // conversation is read-only but its messages can still be forwarded.
    forwardButton.disabled = state.busy || state.forwardBusy;
  }
  const label = $('sendBtn').querySelector('.send-label');
  if (label) label.textContent = state.editTarget ? 'Save' : draft
    ? (state.pendingMediaItems.length > 1 ? 'Send ' + state.pendingMediaItems.length + ' attachments' : draft.kind === 'image' ? 'Send image' : draft.kind === 'video' ? 'Send video' : draft.kind === 'sticker' ? 'Send sticker' : draft.kind === 'document' ? 'Send PDF' : 'Send voice')
    : 'Send';
}

function recordingMimeType() {
  if (typeof MediaRecorder === 'undefined') return '';
  for (const type of ['audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm;codecs=opus', 'audio/webm']) {
    if (!MediaRecorder.isTypeSupported || MediaRecorder.isTypeSupported(type)) return type;
  }
  return '';
}

async function startVoiceRecording() {
  if (!state.composerEnabled || state.busy || state.mediaRecorder || state.pendingMedia) return;
  showError('');
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
    showError('Voice recording is not supported by this browser.');
    return;
  }
  clearMediaDraft();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    state.mediaStream = stream;
    const mime = recordingMimeType();
    const recorder = mime ? new MediaRecorder(stream, {mimeType: mime}) : new MediaRecorder(stream);
    state.recordingChunks = [];
    state.discardRecording = false;
    recorder.ondataavailable = event => {
      if (event.data && event.data.size) state.recordingChunks.push(event.data);
    };
    recorder.onerror = () => {
      showError('The voice recording failed.');
      state.discardRecording = true;
    };
    recorder.onstop = () => {
      const discard = state.discardRecording;
      const chunks = state.recordingChunks.slice();
      const type = recorder.mimeType || mime || 'audio/webm';
      resetRecordingUi();
      state.discardRecording = false;
      if (discard || !chunks.length) return;
      const blob = new Blob(chunks, {type});
      const extension = type.includes('ogg') ? 'ogg' : type.includes('mp4') ? 'm4a' : 'webm';
      const file = new File([blob], 'voice-note.' + extension, {type});
      setPendingMedia('audio', file);
      setAppStatus('Voice note recorded and ready to review.');
    };
    state.mediaRecorder = recorder;
    state.recordingStartedAt = Date.now();
    $('recordingBar').classList.remove('hidden');
    state.recordingTimer = setInterval(() => {
      const seconds = Math.max(0, Math.floor((Date.now() - state.recordingStartedAt) / 1000));
      text($('recordingTime'), 'Recording ' + Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0'));
    }, 250);
    recorder.start(250);
    updateComposerControls();
    setAppStatus('Voice recording started.');
  } catch (error) {
    stopMediaTracks();
    resetRecordingUi();
    showError(error && error.name === 'NotAllowedError' ? 'Microphone permission was denied.' : 'Could not start the microphone.');
  }
}

function toggleVoiceRecording() {
  if (state.mediaRecorder) {
    state.discardRecording = false;
    try { state.mediaRecorder.stop(); } catch (_) { resetRecordingUi(); }
  } else {
    startVoiceRecording();
  }
}

function showLocationError(message) {
  const element = $('locationError');
  element.textContent = message || '';
  element.style.display = message ? 'block' : 'none';
}

function openLocationModal() {
  if (!state.composerEnabled || state.busy || state.mediaRecorder) return;
  showLocationError('');
  $('locationForm').reset();
  openDialog('locationModal', 'useCurrentLocationBtn');
}

function closeLocationModal() {
  if (state.busy) return;
  showLocationError('');
  closeDialog('locationModal');
}

function useCurrentLocation() {
  showLocationError('');
  if (!navigator.geolocation) { showLocationError('Location access is not supported by this browser.'); return; }
  const button = $('useCurrentLocationBtn');
  setButtonBusy(button, true, 'Locating…');
  navigator.geolocation.getCurrentPosition(
    position => {
      $('locationLatitude').value = Number(position.coords.latitude).toFixed(7);
      $('locationLongitude').value = Number(position.coords.longitude).toFixed(7);
      setButtonBusy(button, false);
    },
    error => {
      showLocationError(error && error.code === 1 ? 'Location permission was denied.' : 'Could not determine your current location.');
      setButtonBusy(button, false);
    },
    {enableHighAccuracy: true, timeout: 12000, maximumAge: 30000}
  );
}

async function sendLocation(event) {
  event.preventDefault();
  if (state.busy || !state.ticketId || !$('locationForm').reportValidity()) return;
  const latitude = Number($('locationLatitude').value);
  const longitude = Number($('locationLongitude').value);
  if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90 || !Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
    showLocationError('Enter valid latitude and longitude coordinates.');
    return;
  }
  state.busy = true;
  setButtonBusy($('sendLocationBtn'), true, 'Sending…');
  showLocationError('');
  let succeeded = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + '/location', {method:'POST', body: JSON.stringify({
      latitude, longitude,
      name: $('locationName').value.trim() || null,
      address: $('locationAddress').value.trim() || null,
      client_message_id: crypto.randomUUID(),
      reply_to_message_id: state.replyTarget ? state.replyTarget.id : null
    })});
    clearReplyTarget();
    closeDialog('locationModal');
    notify('Location sent to the customer.');
    succeeded = true;
    await openTicket(state.ticketId, {silent: true, refreshQueue: true});
  } catch (error) {
    showLocationError(error.message);
  } finally {
    state.busy = false;
    setButtonBusy($('sendLocationBtn'), false);
    setWorkspaceBusy(false);
  }
  if (succeeded) focusMessageComposer();
}

function chooseImage() {
  if (state.composerEnabled && !state.busy && !state.mediaRecorder) $('imageInput').click();
}

const VIDEO_MAX_BYTES = Number('__VIDEO_MAX_BYTES__') || 16 * 1024 * 1024;

function canAttachToComposer() {
  return Boolean(state.ticketId && state.composerEnabled && !state.busy && !state.mediaRecorder && !state.editTarget);
}

function attachComposerFile(file) {
  if (!canAttachToComposer()) { showError('Open a conversation you can reply to before attaching a file.'); return false; }
  const types = {'jpg':'image/jpeg', 'jpeg':'image/jpeg', 'png':'image/png', 'webp':'image/webp',
    'pdf':'application/pdf', 'mp4':'video/mp4', '3gp':'video/3gpp',
    'mp3':'audio/mpeg', 'm4a':'audio/mp4', 'aac':'audio/aac', 'amr':'audio/amr', 'ogg':'audio/ogg'};
  if (!file.type || file.type === 'application/octet-stream') {
    const type = types[String(file.name || '').split('.').pop().toLowerCase()];
    if (type) file = new File([file], file.name, {type});
  }
  if (!Object.values(types).includes(file.type)) {
    showError('Use JPEG/PNG, WebP, MP4/3GP, PDF, or supported audio (MP3/M4A/AAC/AMR/OGG).'); return false;
  }
  if (!file.size) { showError('This file is empty.'); return false; }
  const kind = file.type === 'application/pdf' ? 'document' : file.type === 'image/webp' ? 'sticker' : file.type.startsWith('video/') ? 'video' : file.type.startsWith('audio/') ? 'audio' : 'image';
  const limits = {image: Number('__IMAGE_MAX_BYTES__'), document: Number('__DOCUMENT_MAX_BYTES__'), sticker: Number('__STICKER_MAX_BYTES__'), video: VIDEO_MAX_BYTES, audio: Number('__AUDIO_MAX_BYTES__')};
  if (file.size > limits[kind]) { showError('This file exceeds the ' + formatBytes(limits[kind]) + ' limit.'); return false; }
  if (!setPendingMedia(kind, file)) return false;
  $('message').focus();
  setAppStatus('Attachment ready. Review the preview, then click Send.');
  return true;
}

function imageSelected() {
  composerFilesReceived(Array.from($('imageInput').files || []));
  $('imageInput').value = '';
}

function composerFilesReceived(files) {
  if (state.activeModal) return;
  let accepted = 0;
  for (const file of files) if (attachComposerFile(file)) accepted++;
  if (accepted < files.length) setAppStatus(accepted + ' of ' + files.length + ' attachments added. Review the file errors.');
}

function transferHasFiles(transfer) {
  return Boolean(transfer && (Array.from(transfer.types || []).includes('Files') || transfer.files?.length));
}

function installComposerFileInput() {
  const zone = $('composerDropZone');
  let depth = 0;
  const clear = () => { depth = 0; zone.classList.remove('drag-active'); };
  zone.addEventListener('dragenter', event => {
    if (!transferHasFiles(event.dataTransfer)) return;
    event.preventDefault(); depth += 1;
    if (canAttachToComposer() && !state.activeModal) zone.classList.add('drag-active');
  });
  zone.addEventListener('dragover', event => {
    if (!transferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = canAttachToComposer() && !state.activeModal ? 'copy' : 'none';
  });
  zone.addEventListener('dragleave', () => { if (--depth <= 0) clear(); });
  zone.addEventListener('drop', event => {
    clear();
    if (!transferHasFiles(event.dataTransfer)) return;
    event.preventDefault(); event.stopPropagation();
    composerFilesReceived(Array.from(event.dataTransfer.files || []));
  });
  document.addEventListener('dragend', clear);
  // Never navigate away from an in-progress conversation when a file misses the composer.
  for (const name of ['dragover', 'drop']) document.addEventListener(name, event => {
    if (transferHasFiles(event.dataTransfer)) { event.preventDefault(); if (name === 'drop') clear(); }
  });
  $('message').addEventListener('paste', event => {
    const transfer = event.clipboardData;
    const files = Array.from(transfer?.files || []);
    if (!files.length) for (const item of Array.from(transfer?.items || [])) {
      if (item.kind === 'file') { const file = item.getAsFile(); if (file) files.push(file); }
    }
    if (!files.length) return; // Leave ordinary text and keyboard paste untouched.
    event.preventDefault(); composerFilesReceived(files);
  });
}

async function sendMediaMessage() {
  if (!state.pendingMedia || !state.ticketId || state.busy || !state.composerEnabled) return;
  const ticketId = state.ticketId;
  const items = [...state.pendingMediaItems];
  const caption = $('message').value.trim();
  const firstCaption = items.find(item => ['image', 'document', 'video'].includes(item.kind));
  state.busy = true;
  setWorkspaceBusy(true);
  showError('');
  renderMediaDraft();
  let completed = 0;
  let failure = '';
  try {
    for (const draft of items) {
      // Keep an immutable request and key after ambiguous HTTP failures. A
      // repeat checks the existing delivery instead of creating another send.
      draft.submission ||= {caption: draft.caption || (draft === firstCaption ? caption : ''), replyId: state.replyTarget?.id || null};
      const form = new FormData();
      form.append('media_type', draft.kind);
      form.append('client_message_id', draft.id);
      if (['image', 'document', 'video'].includes(draft.kind)) form.append('caption', draft.submission.caption);
      if (draft.submission.replyId) form.append('reply_to_message_id', String(draft.submission.replyId));
      form.append('file', draft.file, draft.file.name || 'attachment');
      setButtonBusy($('sendBtn'), true, 'Sending ' + (completed + 1) + ' of ' + items.length + '…');
      const result = await api('/api/support/tickets/' + ticketId + '/media', {method: 'POST', body: form});
      const message = result.message;
      if (!message?.id) throw new Error('Could not confirm this attachment. Click Send again to check the same attempt.');
      removeMediaDraft(draft.id);
      completed++;
      if (draft === firstCaption && !draft.caption) $('message').value = '';
      if (!['SENT', 'DELIVERED', 'READ', 'PLAYED'].includes(message.send_status)) {
        throw new Error('Sending paused. Check this attachment’s delivery status in the conversation before continuing.');
      }
    }
    clearReplyTarget();
    notify(completed === 1 ? 'Attachment sent.' : completed + ' attachments sent.');
  } catch (error) {
    failure = error.message;
  } finally {
    state.busy = false;
    setButtonBusy($('sendBtn'), false);
    setWorkspaceBusy(false);
    if (state.pendingMedia) renderMediaDraft();
    if (String(state.ticketId) === String(ticketId)) {
      await openTicket(ticketId, {silent: true, refreshQueue: true});
      if (failure) showError(failure);
    }
  }
}

async function sendMessage() {
  if (state.editTarget) {
    const target = state.editTarget;
    const content = $('message').value.trim();
    if (!content || state.busy || !state.ticketId) return;
    const visibleTicketId = state.ticketId;
    state.busy = true; setButtonBusy($('sendBtn'), true, 'Saving…'); showError('');
    try {
      const result = await api('/api/support/tickets/' + target.ticketId + '/messages/' + target.id, {
        method: 'PATCH', body: JSON.stringify({content, version: target.version})
      });
      changeCachedMessage(target.ticketId, target.id, result.message);
      cancelMessageEdit(false);
      refreshMessageSearchAfterChange(target.id);
      if (String(state.ticketId) === String(visibleTicketId)) {
        await openTicket(visibleTicketId, {silent: true, refreshHistory: target.ticketId !== Number(visibleTicketId)});
      }
      notify('Message updated for everyone on WhatsApp.');
    } catch (error) {
      showError(error.message);
      notify(error.message, 'error');
    } finally {
      state.busy = false; setButtonBusy($('sendBtn'), false); updateComposerControls();
    }
    return;
  }
  if (state.pendingMedia) return sendMediaMessage();
  const body = $('message').value.trim();
  if (!body || state.busy || !state.ticketId || !state.composerEnabled) return;
  const ticketId = state.ticketId;
  const reply = state.replyTarget ? {...state.replyTarget} : null;
  const clientMessageId = crypto.randomUUID();
  const optimistic = {
    client_message_id: clientMessageId,
    ticket_id: ticketId,
    sender_type: 'AGENT', sender_id: state.agent?.id,
    sender_name: state.agent?.name || 'Support agent', direction: 'OUTBOUND',
    message_type: 'text', body, send_status: 'PENDING',
    created_at: new Date().toISOString(),
    reply_to: reply ? {id: reply.id, sender_name: reply.actor, body: reply.preview} : null
  };
  state.localMessages.set(clientMessageId, {ticketId, message: optimistic});
  upsertTimelineMessage(optimistic, ticketId);
  $('message').value = '';
  clearReplyTarget();
  resizeComposer();
  updateComposerCount();
  updateComposerControls();
  showError('');
  focusMessageComposer();
  optimistic.reply_to_message_id = reply?.id || null;
  const send = () => deliverTextMessage(optimistic, ticketId);
  state.textSendChain = state.textSendChain.then(send, send);
}

// A request that got no answer (network drop, gateway error page) may or may not
// have reached the server. Retrying with the same client_message_id is safe: the
// server returns the message it already has instead of sending it twice.
