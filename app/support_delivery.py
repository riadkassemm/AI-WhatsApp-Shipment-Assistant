"""One delivery attempt shared by text, location, media and lifecycle notices."""
from __future__ import annotations

import asyncio
import logging

from app.whatsapp_client import WhatsAppSendError


async def deliver(repository, transport, media_store, ticket, message, *, retry=False):
    message_id = int(message['id'])
    token = await repository.claim_delivery(message_id, retry=retry, expected_token=message.get("delivery_token"))
    if token is None:
        return await repository.get_message(message_id)
    try:
        return await _deliver_claimed(repository, transport, media_store, ticket, message, token)
    except asyncio.CancelledError:
        # Cancellation bypasses Exception handlers (e.g. graceful shutdown).
        # Consult the committed state: begin_delivery may have committed before
        # cancellation was observed, even if its caller never received success.
        try:
            await repository.interrupt_delivery(message_id, token)
        except Exception:
            logging.getLogger('shipment-bot').warning(
                'Interrupted support delivery requires operator recovery: message_id=%s', message_id)
        raise


async def _deliver_claimed(repository, transport, media_store, ticket, message, token):
    message_id = int(message['id'])
    started = False
    try:
        kind = message.get('message_type') or 'text'
        body = str(message.get('body') or '')
        common = dict(
            to=str(ticket['whatsapp_phone']),
            phone_number_id=str(ticket.get('business_phone_number_id') or '') or None,
            client_message_id=message['transport_client_id'],
            context_message_id=message.get('reply_to_whatsapp_message_id') or None,
        )
        if kind == 'text':
            send = transport.send_text
            payload = dict(body=body)
        elif kind == 'location':
            send = transport.send_location
            payload = dict(latitude=float(message['location_latitude']), longitude=float(message['location_longitude']),
                           name=message.get('location_name'), address=message.get('location_address'))
        else:
            asset = media_store.get(int(ticket['id']), message_id)
            if asset is None:
                raise ValueError('The stored attachment is unavailable. Restore it before retrying.')
            media_id = await transport.upload_media(content=asset.path.read_bytes(), mime_type=asset.mime_type,
                                                    filename=asset.filename, phone_number_id=common['phone_number_id'])
            defaults = {'image': {'Image'}, 'video': {'Video'}, 'document': {asset.filename, 'Document'}}
            send = transport.send_media
            payload = dict(media_type=kind, media_id=media_id,
                           caption=body if kind in defaults and body not in defaults[kind] else None,
                           voice=kind == 'audio' and body == 'Voice note',
                           filename=asset.filename if kind == 'document' else None)
        # Fence a reclaimed worker before any customer-visible operation.
        if not await repository.begin_delivery(message_id, token):
            return await repository.get_message(message_id)
        started = True
        response = await send(**common, **payload)
        messages = response.get('messages') if isinstance(response, dict) else None
        provider_id = messages[0].get('id') if isinstance(messages, list) and messages and isinstance(messages[0], dict) else None
        if not provider_id:
            raise WhatsAppSendError('Provider acceptance could not be confirmed.', delivery_uncertain=True)
    except Exception as exc:
        safe = not started or (isinstance(exc, WhatsAppSendError) and not exc.delivery_uncertain)
        # Never store raw transport exceptions (which may contain credentials).
        await repository.finish_delivery(message_id, token, status='FAILED' if safe else 'UNCERTAIN',
                                         error='Not sent; retry is available.' if safe else 'Delivery is uncertain; do not resend.',
                                         **({'failure_code': exc.provider_code} if safe and isinstance(exc, WhatsAppSendError) and exc.provider_code else {}))
        return await repository.get_message(message_id)
    # Persistence failure intentionally leaves DISPATCHING; recovery quarantines
    # it. It must never be classified as a provider rejection.
    await repository.finish_delivery(message_id, token, status='SENT', provider_id=str(provider_id))
    return await repository.get_message(message_id)
