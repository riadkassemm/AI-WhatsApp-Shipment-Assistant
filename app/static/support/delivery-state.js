// Delivery evidence is independent of content edits. A stale HTTP response may
// contain an earlier edit but a newer receipt; merge those dimensions separately.
function supportMessageTime(value) {
  const text = String(value || '').replace(' ', 'T');
  const millis = Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(text) ? text : text + 'Z');
  const fraction = (text.match(/\.(\d+)/)?.[1] || '').padEnd(6, '0').slice(0, 6);
  return Number.isFinite(millis) ? millis * 1000 + Number(fraction.slice(3)) : 0;
}

function mergeSupportMessage(previous, incoming) {
  if (!previous) return incoming;
  if (!incoming) return previous;
  if (previous.id && !incoming.id) return previous;
  const oldVersion = Number(previous.version || 0), newVersion = Number(incoming.version || 0);
  const older = newVersion < oldVersion || (newVersion === oldVersion &&
    supportMessageTime(incoming.updated_at) < supportMessageTime(previous.updated_at));
  let result = older ? {...incoming, ...previous} : {...previous, ...incoming};
  // Deletion is terminal. Never resurrect body/media from an earlier snapshot.
  if (previous.deleted_for_everyone_at && !incoming.deleted_for_everyone_at) result = {...incoming, ...previous};
  if (incoming.deleted_for_everyone_at && !previous.deleted_for_everyone_at) result = {...previous, ...incoming};
  const rank = {sent: 1, delivered: 2, read: 3, played: 4};
  const receipt = (rank[previous.delivery_status] || 0) > (rank[incoming.delivery_status] || 0)
    ? previous.delivery_status : incoming.delivery_status;
  if (receipt) result.delivery_status = receipt;
  const confirmed = message => ['SENT', 'DELIVERED', 'READ', 'PLAYED'].includes(message.send_status) ||
    (rank[message.delivery_status] || 0) > 0;
  if (confirmed(previous) || confirmed(incoming)) {
    if (!['SENT', 'DELIVERED', 'READ', 'PLAYED'].includes(result.send_status)) result.send_status = 'SENT';
    result.error_message = null;
    result.held_until = null;
    result.delivery_retry_safe = false;
  }
  if (result.deleted_for_everyone_at) {
    for (const field of ['body', 'location_latitude', 'location_longitude', 'location_name', 'location_address',
        'provider_media_id', 'provider_media_mime_type', 'provider_media_filename', 'error_message']) result[field] = null;
    for (const field of ['media_url', 'media_filename', 'media_mime_type', 'media_size_bytes', 'media_available', 'media_cached',
        'media_too_large', 'media_limit_label']) delete result[field];
    result.reactions = [];
  }
  if (result.id) { delete result.local_submission; delete result.confirmation_required; }
  return result;
}

function supportMessageHidden(message) {
  return Boolean(message?.id && state.hiddenSupportMessages?.has(String(message.id)));
}

function sameClientMessageScope(left, right) {
  return Boolean(left.client_message_id && left.client_message_id === right.client_message_id &&
    String(left.sender_id || '') === String(right.sender_id || '') && left.sender_type === right.sender_type);
}

function observePersistedSupportMessage(message, ticketId) {
  if (!message?.id || !message.client_message_id) return;
  const local = state.localMessages.get(String(message.client_message_id));
  if (local && String(local.ticketId) === String(ticketId) && sameClientMessageScope(local.message, message)) {
    supportSendQueue.release(supportSendGroup(ticketId, local.message.business_phone_number_id), message.client_message_id);
    state.localMessages.delete(String(message.client_message_id));
  }
}

class ScopedSupportSendQueue {
  constructor({maxActive = 3, maxPending = 64, perConversation = 16} = {}) {
    Object.assign(this, {maxActive, maxPending, perConversation});
    this.entries = new Map(); this.waiting = []; this.activeGroups = new Set(); this.heldGroups = new Map();
  }
  enqueue({id, conversation, group}, run) {
    if (this.entries.has(id)) return this.entries.get(id).promise;
    if (this.entries.size >= this.maxPending || [...this.entries.values()].filter(item =>
      item.conversation === conversation).length >= this.perConversation) {
      throw new Error('The sending queue is full. Your draft is still here; wait for pending messages before sending more.');
    }
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    const item = {id, conversation, group, run, promise, resolve, reject};
    this.entries.set(id, item); this.waiting.push(item); this.pump();
    return promise;
  }
  pump() {
    if (this.paused) return;
    while (this.activeGroups.size < this.maxActive) {
      const index = this.waiting.findIndex(item => !this.activeGroups.has(item.group) &&
        (!this.heldGroups.has(item.group) || this.heldGroups.get(item.group) === item.id));
      if (index < 0) return;
      const [item] = this.waiting.splice(index, 1);
      this.activeGroups.add(item.group);
      Promise.resolve().then(item.run).then(item.resolve, item.reject).finally(() => {
        this.entries.delete(item.id); this.activeGroups.delete(item.group); this.pump();
      });
    }
  }
  pause() { this.paused = true; }
  resume() { this.paused = false; this.pump(); }
  hold(group, id) {
    // A later request must not overtake an original whose persistence is unknown.
    // Keep this identity blocked without consuming a global concurrency slot.
    if (!this.heldGroups.has(group)) this.heldGroups.set(group, id);
  }
  release(group, id) {
    if (this.heldGroups.get(group) !== id) return;
    this.heldGroups.delete(group); this.pump();
  }
  async waitForGroup(group) {
    // Check between jobs: a timed-out head request may hold its successors.
    // Attachments/quick replies keep their drafts instead of waiting indefinitely.
    for (;;) {
      if (this.heldGroups.has(group)) throw new Error('An earlier request for this WhatsApp number needs confirmation. Check its status before sending more.');
      const next = [...this.entries.values()].find(item => item.group === group);
      if (!next) return;
      await Promise.allSettled([next.promise]);
    }
  }
  cancelWaiting() {
    for (const item of this.waiting.splice(0)) { this.entries.delete(item.id); item.resolve(false); }
    this.heldGroups.clear();
  }
}

const supportSendQueue = new ScopedSupportSendQueue();
function supportSendGroup(ticketId, lineId = null) {
  const line = lineId || (String(state.ticketId) === String(ticketId) ? state.currentTicketData?.ticket?.business_phone_number_id : null);
  return line ? 'line:' + line : 'ticket:' + ticketId;
}
function hasUnpersistedSupportMessages() {
  return [...state.localMessages.values()].some(item => !item.message.id);
}
function warnUnpersistedSupportMessages(event) {
  if (!hasUnpersistedSupportMessages()) return;
  event.preventDefault(); event.returnValue = '';
}
