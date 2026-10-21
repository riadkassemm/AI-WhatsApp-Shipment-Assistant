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
  if (state.pendingMedia && state.pendingMedia.url) URL.revokeObjectURL(state.pendingMedia.url);
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

function renderMediaDraft() {
  const container = $('mediaDraft');
  container.replaceChildren();
  const draft = state.pendingMedia;
  if (!draft) {
    container.classList.add('hidden');
    return;
  }
  container.className = 'media-draft ' + draft.kind;
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
      : 'Voice note ready';
  const size = document.createElement('div');
  size.className = 'media-draft-size';
  size.textContent = formatBytes(draft.file.size);
  info.append(name, size);
  const actions = document.createElement('div');
  actions.className = 'media-draft-actions';
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'btn small';
  remove.append(createIcon('trash', 'icon icon-sm'), document.createTextNode('Remove'));
  remove.onclick = clearMediaDraft;
  if (draft.kind === 'image') {
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'btn small';
    edit.append(createIcon('edit', 'icon icon-sm'), document.createTextNode('Edit'));
    edit.onclick = editPendingImage;
    actions.append(edit);
  }
  actions.append(remove);
  container.append(preview, info, actions);

  if (draft.kind === 'image' || draft.kind === 'document' || draft.kind === 'video') {
    const label = draft.kind === 'image' ? 'image' : draft.kind === 'video' ? 'video' : 'PDF';
    $('message').maxLength = 1024;
    $('message').placeholder = 'Add a' + (label === 'image' ? 'n ' : ' ') + label + ' caption (optional)';
    text($('composerHelp'), 'The ' + label + ' and optional caption will be sent as a public WhatsApp reply.');
  } else if (draft.kind === 'audio') {
    $('message').value = '';
    $('message').placeholder = 'Voice note ready to send';
    text($('composerHelp'), 'Review the recording, then send it as a public WhatsApp voice note.');
  } else {
    $('message').value = '';
    $('message').placeholder = 'Sticker ready to send';
    text($('composerHelp'), 'The WebP sticker will be sent as a public WhatsApp reply.');
  }
  updateComposerCount();
  updateComposerControls();
}

function setPendingMedia(kind, file) {
  clearMediaDraft();
  const url = URL.createObjectURL(file);
  state.pendingMedia = {kind, file, url};
  renderMediaDraft();
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
  $('voiceBtn').disabled = !enabled || Boolean(state.editTarget);
  $('locationBtn').disabled = !enabled || recording || Boolean(state.editTarget);
  $('voiceBtn').setAttribute('aria-pressed', recording ? 'true' : 'false');
  $('voiceBtn').setAttribute('aria-label', recording ? 'Stop voice recording' : 'Record a voice note');
  $('voiceBtn').setAttribute('data-tooltip', recording ? 'Stop recording' : 'Record voice note');
  setIcon($('voiceBtn'), recording ? 'square' : 'mic');
  $('message').disabled = !enabled || recording || Boolean(draft && draft.kind === 'audio');
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
    ? (draft.kind === 'image' ? 'Send image' : draft.kind === 'video' ? 'Send video' : draft.kind === 'sticker' ? 'Send sticker' : draft.kind === 'document' ? 'Send PDF' : 'Send voice')
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
  if (!state.composerEnabled || state.busy || state.mediaRecorder) return;
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

function imageSelected() {
  const file = $('imageInput').files && $('imageInput').files[0];
  if (!file) return;
  if (!['image/jpeg', 'image/png', 'image/webp', 'application/pdf', 'video/mp4', 'video/3gpp'].includes(file.type)) {
    showError('Use a JPEG/PNG image, MP4 video, WebP sticker, or PDF document.');
    $('imageInput').value = '';
    return;
  }
  const kind = file.type === 'application/pdf' ? 'document' : file.type === 'image/webp' ? 'sticker' : file.type.startsWith('video/') ? 'video' : 'image';
  if (kind === 'video' && file.size > VIDEO_MAX_BYTES) {
    showError('This video is ' + formatBytes(file.size) + '. WhatsApp videos must be ' + formatBytes(VIDEO_MAX_BYTES) + ' or smaller.');
    $('imageInput').value = '';
    return;
  }
  setPendingMedia(kind, file);
  $('message').focus();
  setAppStatus(kind === 'document' ? 'PDF attached. You may add a caption before sending.' : kind === 'sticker' ? 'Sticker attached and ready to send.' : kind === 'video' ? 'Video attached. You may add a caption before sending.' : 'Image attached. You may add a caption before sending.');
}

async function sendMediaMessage() {
  const draft = state.pendingMedia;
  if (!draft || !state.ticketId || state.busy) return;
  state.busy = true;
  setWorkspaceBusy(true);
  setButtonBusy($('sendBtn'), true, draft.kind === 'image' ? 'Sending image…' : draft.kind === 'video' ? 'Sending video…' : draft.kind === 'sticker' ? 'Sending sticker…' : draft.kind === 'document' ? 'Sending PDF…' : 'Sending voice…');
  showError('');
  const form = new FormData();
  form.append('media_type', draft.kind);
  form.append('client_message_id', crypto.randomUUID());
  if (['image', 'document', 'video'].includes(draft.kind)) form.append('caption', $('message').value.trim());
  if (state.replyTarget) form.append('reply_to_message_id', String(state.replyTarget.id));
  form.append('file', draft.file, draft.file.name || (draft.kind === 'image' ? 'image' : draft.kind === 'video' ? 'video.mp4' : draft.kind === 'sticker' ? 'sticker.webp' : draft.kind === 'document' ? 'document.pdf' : 'voice-note'));
  let refresh = false;
  let succeeded = false;
  try {
    await api('/api/support/tickets/' + state.ticketId + '/media', {method:'POST', body: form});
    $('message').value = '';
    clearMediaDraft();
    clearReplyTarget();
    notify(draft.kind === 'image' ? 'Image sent to the customer.' : draft.kind === 'video' ? 'Video sent to the customer.' : draft.kind === 'sticker' ? 'Sticker sent to the customer.' : draft.kind === 'document' ? 'PDF sent to the customer.' : 'Voice note sent to the customer.');
    refresh = true;
    succeeded = true;
  } catch (error) {
    showError(error.message);
    notify(error.message, 'error');
    refresh = true;
  } finally {
    state.busy = false;
    setButtonBusy($('sendBtn'), false);
    setWorkspaceBusy(false);
    updateComposerCount();
  }
  if (refresh) await openTicket(state.ticketId, {silent: true, refreshQueue: true});
  if (succeeded) focusMessageComposer();
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
