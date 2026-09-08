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


def test_sender_processing_lock_can_be_refreshed_only_by_owner() -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        token = await store.claim_conversation_processing("96170123456")
        assert token is not None
        assert await store.refresh_conversation_processing("96170123456", token) is True
        assert (
            await store.refresh_conversation_processing("96170123456", "wrong-token")
            is False
        )

    asyncio.run(run())


def test_pending_inbound_queue_is_fifo_and_supports_retry_prepend() -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        phone = "96170123456"
        await store.enqueue_incoming_work(phone, {"message_id": "two"})
        await store.enqueue_incoming_work(phone, {"message_id": "three"})
        await store.enqueue_incoming_work(phone, {"message_id": "one"}, front=True)
        assert await store.pending_incoming_work_count(phone) == 3
        assert (await store.pop_incoming_work(phone) or {})["message_id"] == "one"
        assert (await store.pop_incoming_work(phone) or {})["message_id"] == "two"
        assert (await store.pop_incoming_work(phone) or {})["message_id"] == "three"
        assert await store.pop_incoming_work(phone) is None

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
