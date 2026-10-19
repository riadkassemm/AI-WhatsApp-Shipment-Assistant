import crypto from 'node:crypto';
import { normalizeMessageContent, proto } from '@whiskeysockets/baileys';
import { wrapId } from './config.js';

export const millis = value => {
  const numeric = Number(value?.toNumber ? value.toNumber() : value);
  return Number.isFinite(numeric) && numeric > 0 ? (numeric < 100_000_000_000 ? numeric * 1000 : numeric) : Date.now();
};
// WhatsApp media message kinds the dashboard stores and plays.
export const MEDIA_TYPES = ['image', 'audio', 'document', 'sticker', 'video'];

export function textBody(content) {
  const normalized = normalizeMessageContent(content);
  return normalized?.conversation ?? normalized?.extendedTextMessage?.text ?? null;
}
export function eventId(type, lineId, id, timestamp, body = '') {
  return crypto.createHash('sha256').update(JSON.stringify([type, lineId, id, timestamp, body])).digest('hex');
}

export function mutationEvent(line, message, senderPhone) {
  const content = normalizeMessageContent(message.message);
  const protocol = content?.protocolMessage;
  if (!protocol?.key?.id) return null;
  let type;
  let body;
  if (protocol.type === proto.Message.ProtocolMessage.Type.REVOKE) type = 'message_deleted_for_everyone';
  else if (protocol.type === proto.Message.ProtocolMessage.Type.MESSAGE_EDIT) {
    type = 'message_updated';
    body = textBody(protocol.editedMessage);
    if (body === null) return null;
  } else return null;
  const timestamp = millis(protocol.timestampMs ?? message.messageTimestamp);
  return {
    event_id: eventId(type, line.line_id, protocol.key.id, timestamp, body), type,
    line_id: line.line_id, display_phone_number: line.display_phone_number,
    message_id: wrapId(line.line_id, protocol.key.id), ...(body !== undefined ? { body } : {}),
    timestamp, from_me: Boolean(message.key?.fromMe), sender_phone: senderPhone,
  };
}

function chatMessageEvent(line, message, senderPhone, mediaId, type) {
  if (Boolean(message.key?.fromMe) !== (type === 'outgoing_message') || !message.key?.id || !senderPhone) return null;
  const content = normalizeMessageContent(message.message);
  if (!content || content.protocolMessage) return null;
  const timestamp = millis(message.messageTimestamp);
  const node = { id: wrapId(line.line_id, message.key.id), from: senderPhone, timestamp: String(Math.floor(timestamp / 1000)) };
  const body = textBody(content);
  let context;
  if (body !== null) { node.type = 'text'; node.text = { body }; context = content.extendedTextMessage?.contextInfo; }
  else if (content.reactionMessage?.key?.id) {
    node.type = 'reaction'; node.reaction = { message_id: wrapId(line.line_id, content.reactionMessage.key.id), emoji: content.reactionMessage.text ?? '' };
  } else if (content.locationMessage) {
    const location = content.locationMessage;
    node.type = 'location'; node.location = { latitude: location.degreesLatitude, longitude: location.degreesLongitude, name: location.name, address: location.address };
    context = location.contextInfo;
  } else {
    const candidate = MEDIA_TYPES.find(type => content[`${type}Message`]);
    if (candidate && mediaId) {
      const media = content[`${candidate}Message`];
      node.type = candidate;
      node[candidate] = {
        id: mediaId, mime_type: media.mimetype, caption: media.caption, filename: media.fileName,
        ...(media.fileSha256 ? { sha256: Buffer.from(media.fileSha256).toString('hex') } : {}),
        ...(candidate === 'audio' ? { voice: Boolean(media.ptt) } : {}),
      };
      context = media.contextInfo;
    } else if (type === 'outgoing_message') {
      // Preserve a visible timeline marker for a message type the dashboard
      // cannot render. Keep its private native payload in bridge storage only.
      const kind = Object.keys(content).find(key => !['messageContextInfo', 'senderKeyDistributionMessage'].includes(key));
      if (!kind) return null;
      node.type = 'unsupported';
      node.unsupported = { kind: kind.slice(0, 60) };
    } else return null;
  }
  if (context?.stanzaId) node.context = { id: wrapId(line.line_id, context.stanzaId) };
  const value = {
    messaging_product: 'whatsapp',
    metadata: { phone_number_id: line.line_id, display_phone_number: line.display_phone_number },
    contacts: [{ wa_id: senderPhone, profile: { name: message.pushName ?? senderPhone } }], messages: [node],
  };
  return {
    event_id: eventId(type, line.line_id, message.key.id, 0), type, line_id: line.line_id,
    display_phone_number: line.display_phone_number, message_id: node.id,
    timestamp, from_me: type === 'outgoing_message', sender_phone: senderPhone,
    payload: { object: 'whatsapp_business_account', entry: [{ id: line.line_id, changes: [{ field: 'messages', value }] }] },
  };
}

export function inboundEvent(line, message, senderPhone, mediaId = null) {
  return chatMessageEvent(line, message, senderPhone, mediaId, 'message');
}

export function outgoingEvent(line, message, senderPhone, mediaId = null) {
  return chatMessageEvent(line, message, senderPhone, mediaId, 'outgoing_message');
}

export function outgoingConfirmationEvent(line, nativeId, senderPhone, clientMessageId, timestamp) {
  return {
    event_id: eventId('outgoing_confirmation', line.line_id, nativeId, 0),
    type: 'outgoing_confirmation', line_id: line.line_id,
    message_id: wrapId(line.line_id, nativeId), client_message_id: clientMessageId,
    sender_phone: senderPhone, timestamp, from_me: true,
  };
}

export function messageTombstoneEvent(line, nativeId, senderPhone, timestamp, deletedTimestamp) {
  return {
    event_id: eventId('message_tombstone', line.line_id, nativeId, 0),
    type: 'message_tombstone', line_id: line.line_id,
    display_phone_number: line.display_phone_number,
    message_id: wrapId(line.line_id, nativeId),
    sender_phone: senderPhone, timestamp, deleted_timestamp: deletedTimestamp, from_me: false,
  };
}
