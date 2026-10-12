import { normalizeMessageContent, proto } from '@whiskeysockets/baileys';
import { millis, textBody } from './events.js';

// Baileys 7.0.0-rc14 emits the authenticated native upsert before processMessage
// derives its messages.update. Its buffer can later coalesce/absorb that update,
// and processMessage floors edit timestampMs to seconds. Preserve native metadata
// at emit time, while both the actual timestamp and mutation ID are still present.
// Do not fabricate ordering from callback arrival time when that data is missing.
export function preserveNativeMutationMetadata(socket) {
  const emit = socket.ev.emit;
  const latest = new Map();
  const identity = (key, id) => JSON.stringify([key?.remoteJid, Boolean(key?.fromMe), id]);
  socket.ev.emit = function (name, data) {
    if (name === 'messages.upsert' && !data.requestId) {
      for (const message of data.messages ?? []) {
        const protocol = normalizeMessageContent(message.message)?.protocolMessage;
        if (!protocol?.key?.id || !message.key?.id) continue;
        const edit = protocol.type === proto.Message.ProtocolMessage.Type.MESSAGE_EDIT;
        const revoke = protocol.type === proto.Message.ProtocolMessage.Type.REVOKE;
        if (!edit && !revoke) continue;
        const key = identity(message.key, protocol.key.id);
        latest.delete(key);
        latest.set(key, { targetId: protocol.key.id, type: edit ? 'edit' : 'revoke', body: edit ? textBody(protocol.editedMessage) : null,
          timestamp: millis(protocol.timestampMs ?? message.messageTimestamp), sourceId: message.key.id });
        if (latest.size > 4096) latest.delete(latest.keys().next().value);
      }
    } else if (name === 'messages.update') {
      data = data.map(item => {
        const candidate = latest.get(identity(item.key, item.key?.id));
        if (!candidate) return item;
        const edit = Boolean(item.update?.message?.editedMessage);
        const revoke = item.update?.messageStubType === proto.WebMessageInfo.StubType.REVOKE;
        if ((edit && candidate.type === 'edit' && textBody(item.update.message) === candidate.body &&
            Math.floor(millis(item.update.messageTimestamp) / 1000) === Math.floor(candidate.timestamp / 1000)) ||
            (revoke && candidate.type === 'revoke' && item.update?.key?.id === candidate.sourceId)) {
          return { ...item, update: { ...item.update, bridgeMutationTimestampMs: candidate.timestamp,
            bridgeMutationSourceId: candidate.sourceId } };
        }
        return item;
      });
    }
    return emit.call(this, name, data);
  };
  const restore = () => { socket.ev.emit = emit; latest.clear(); };
  restore.purgeTarget = id => { for (const [key, item] of latest) if (item.targetId === id) latest.delete(key); };
  return restore;
}
