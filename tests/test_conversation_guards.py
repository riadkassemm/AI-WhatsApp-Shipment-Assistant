from __future__ import annotations

import asyncio

from app.conversation_store import Conversation, InMemoryConversationStore


def test_sender_processing_lock_serializes_different_message_ids() -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        first = await store.claim_conversation_processing("96170123456")
        assert first is not None
        assert await store.claim_conversation_processing("96170123456") is None
        await store.release_conversation_processing("96170123456", first)
        assert await store.claim_conversation_processing("96170123456") is not None

    asyncio.run(run())


def test_outbound_reply_guard_is_at_most_once() -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        key = "primary-customer-reply:wamid.example"
        token = await store.claim_outbound_reply(key)
        assert token is not None
        await store.complete_outbound_reply(key, token)
        assert await store.claim_outbound_reply(key) is None

    asyncio.run(run())


def test_old_conversation_json_gets_new_watermark_defaults() -> None:
    old = Conversation(phone_number="96170123456").to_json()
    restored = Conversation.from_json(old)
    assert restored.last_inbound_timestamp is None
    assert restored.recent_inbound_message_ids == []
