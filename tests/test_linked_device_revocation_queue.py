from __future__ import annotations

import asyncio

from app.conversation_store import InMemoryConversationStore
from app.whatsapp_client import IncomingWhatsAppMessage
import app.main as main


def test_revoked_inbound_message_is_removed_before_queue_processing(monkeypatch):
    store = InMemoryConversationStore()
    monkeypatch.setattr(main, "conversation_store", store)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Revoked content entered customer routing")

    monkeypatch.setattr(main, "_process_incoming_message", forbidden)

    async def run():
        incoming = IncomingWhatsAppMessage(
            from_number="96171111111", message_id="ld:90001:REVOKED2",
            message_type="text", text="content since deleted", timestamp="1700000000",
            business_phone_number_id="90001",
        )
        claim = await store.claim_incoming_message(incoming.message_id)
        await store.mark_inbound_revoked(incoming.message_id)
        response = await main._process_inbound_burst(
            "90001:96171111111", [main._queued_inbound_item(incoming, claim)],
        )
        assert response.status_code == 200
        assert await store.is_inbound_revoked(incoming.message_id)
        assert await store.claim_incoming_message(incoming.message_id) is None

    asyncio.run(run())


def test_revoked_inbound_message_cannot_trigger_late_bot_reply(monkeypatch):
    store = InMemoryConversationStore()
    monkeypatch.setattr(main, "conversation_store", store)

    async def forbidden(**_kwargs):
        raise AssertionError("Revoked message triggered a customer reply")

    monkeypatch.setattr(main.whatsapp_client, "send_text", forbidden)

    async def run():
        await store.mark_inbound_revoked("ld:90001:REVOKED3")
        result = await main._send_text_once(
            to="96171111111", body="bot reply",
            source_message_id="ld:90001:REVOKED3", phone_number_id="90001",
        )
        assert result == {"revoked": True}

    asyncio.run(run())
