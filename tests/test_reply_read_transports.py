import asyncio
import json

import httpx
import pytest

from app.whatsapp_client import WhatsAppClient


def test_cloud_read_receipt_requires_success_without_a_new_message_id(monkeypatch):
    calls = []
    ok = True
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'success': ok})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.whatsapp_client.httpx.AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    client = WhatsAppClient()
    monkeypatch.setattr(client, '_messages_url', lambda *_: 'https://example.test/messages')
    async def run():
        nonlocal ok
        await client.mark_read(message_id='wamid.incoming', phone_number_id='12345', phone='15551234567')
        ok = False
        with pytest.raises(Exception, match='not confirmed'):
            await client.mark_read(message_id='wamid.incoming', phone_number_id='12345', phone='15551234567')
    asyncio.run(run())
    assert calls == [{'messaging_product': 'whatsapp', 'status': 'read', 'message_id': 'wamid.incoming'}] * 2
