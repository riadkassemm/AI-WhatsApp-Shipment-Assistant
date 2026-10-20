import { BridgeError } from './config.js';

// sendMessage resolves after socket relay; it does not itself wait for a server ACK.
export class AckTracker {
  constructor(socket, timeoutMs) {
    this.socket = socket;
    this.timeoutMs = timeoutMs;
    this.waiting = new Map();
    this.onAck = node => {
      const pending = this.waiting.get(node?.attrs?.id);
      if (!pending) return;
      if (node.attrs.error) pending.reject(new BridgeError(502, 'WHATSAPP_REJECTED', 'WhatsApp rejected this message operation.'));
      else pending.resolve();
    };
    socket.ws.on('CB:ack,class:message', this.onAck);
  }
  // With deferred, the timeout starts only when startTimer() is called, so a
  // slow media upload before the relay is not mistaken for a missing ACK.
  expect(id, { deferred = false } = {}) {
    let timer;
    let settled = false;
    const startTimer = () => {
      if (timer || settled) return;
      timer = setTimeout(() => this.waiting.get(id)?.reject(new BridgeError(504, 'CONFIRMATION_UNKNOWN', 'WhatsApp acknowledgment timed out. Retry the same operation to check it.')), this.timeoutMs);
    };
    const promise = new Promise((resolve, reject) => {
      const settle = fn => value => { settled = true; clearTimeout(timer); this.waiting.delete(id); fn(value); };
      this.waiting.set(id, { resolve: settle(resolve), reject: settle(reject) });
    });
    if (!deferred) startTimer();
    // sendMessage can fail before this promise is awaited.
    promise.catch(() => {});
    return { promise, startTimer, cancel: () => this.waiting.get(id)?.reject(new BridgeError(503, 'SEND_FAILED', 'Message relay failed.')) };
  }
  close() {
    this.socket.ws.off('CB:ack,class:message', this.onAck);
    for (const pending of this.waiting.values()) pending.reject(new BridgeError(503, 'DISCONNECTED', 'The WhatsApp device disconnected before acknowledgment.'));
  }
}
