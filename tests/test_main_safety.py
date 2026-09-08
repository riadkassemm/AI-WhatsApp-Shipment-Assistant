from __future__ import annotations

import asyncio
import sys
import time
import types

# Lightweight import-time stubs for production-only dependencies unavailable in the
# artifact validation runtime.
fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {})
)
sys.modules["aiomysql"] = fake_aiomysql

fake_bcrypt = types.ModuleType("bcrypt")
fake_bcrypt.checkpw = lambda *_args, **_kwargs: False
fake_bcrypt.hashpw = lambda *_args, **_kwargs: b"hash"
fake_bcrypt.gensalt = lambda *_args, **_kwargs: b"salt"
sys.modules.setdefault("bcrypt", fake_bcrypt)

fake_openai = types.ModuleType("openai")


class _AsyncOpenAI:
    def __init__(self, *_args, **_kwargs) -> None:
        pass


fake_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules.setdefault("openai", fake_openai)

from app import main
from app.conversation_store import Conversation
from app.whatsapp_client import IncomingWhatsAppMessage


def test_stale_detection_keeps_unique_late_messages() -> None:
    now = int(time.time())
    conversation = Conversation(
        phone_number="96170123456",
        last_inbound_timestamp=now - 2,
        recent_inbound_message_ids=["wamid.old"],
    )
    assert main._stale_inbound_reason(
        conversation,
        IncomingWhatsAppMessage(
            "96170123456", "wamid.old", "text", "hello", str(now)
        ),
    ) == "recent_duplicate"
    assert main._stale_inbound_reason(
        conversation,
        IncomingWhatsAppMessage(
            "96170123456", "wamid.older", "text", "hello", str(now - 3)
        ),
    ) is None
    assert main._stale_inbound_reason(
        conversation,
        IncomingWhatsAppMessage(
            "96170123456",
            "wamid.expired",
            "text",
            "hello",
            str(now - int(main.settings.whatsapp_max_inbound_age_seconds) - 1),
        ),
    ) == "expired"


def test_message_burst_combines_adjacent_text_messages_in_provider_order() -> None:
    items = [
        {
            "from_number": "96170123456",
            "message_id": "wamid.second",
            "message_type": "text",
            "text": "and it is 5 kg",
            "timestamp": "200",
            "claim_token": "two",
        },
        {
            "from_number": "96170123456",
            "message_id": "wamid.first",
            "message_type": "text",
            "text": "How much to ship to KSA?",
            "timestamp": "199",
            "claim_token": "one",
        },
    ]
    ordered = main._sort_inbound_burst(items)
    combined = main._combine_inbound_burst(ordered)
    assert combined is not None
    assert combined.text == "How much to ship to KSA?\nand it is 5 kg"
    assert combined.message_id == "wamid.second"
    assert combined.timestamp == "200"


def test_sender_queue_processes_adjacent_messages_as_one_turn() -> None:
    from app.conversation_store import InMemoryConversationStore

    async def run() -> None:
        store = InMemoryConversationStore()
        phone = "96170123456"
        first = IncomingWhatsAppMessage(
            phone, "wamid.1", "text", "How much to ship to KSA?", "100"
        )
        second = IncomingWhatsAppMessage(
            phone, "wamid.2", "text", "It is 5 kg", "101"
        )
        first_claim = await store.claim_incoming_message(first.message_id or "")
        second_claim = await store.claim_incoming_message(second.message_id or "")
        assert first_claim and second_claim
        await store.enqueue_incoming_work(
            phone, main._queued_inbound_item(first, first_claim)
        )
        await store.enqueue_incoming_work(
            phone, main._queued_inbound_item(second, second_claim)
        )

        seen: list[str | None] = []

        async def fake_process(incoming: IncomingWhatsAppMessage):
            seen.append(incoming.text)
            return main._json_response(200, {"status": "handled"})

        async def no_ticket(_phone: str):
            return None

        original_store = main.conversation_store
        original_process = main._process_incoming_message
        original_get_ticket = main.support_service.get_active_ticket
        original_debounce = main.settings.whatsapp_message_burst_debounce_seconds
        original_max_wait = main.settings.whatsapp_message_burst_max_wait_seconds
        main.conversation_store = store  # type: ignore[assignment]
        main._process_incoming_message = fake_process  # type: ignore[assignment]
        main.support_service.get_active_ticket = no_ticket  # type: ignore[method-assign]
        main.settings.whatsapp_message_burst_debounce_seconds = 0
        main.settings.whatsapp_message_burst_max_wait_seconds = 0
        try:
            response = await main._drive_inbound_queue(phone)
        finally:
            main.conversation_store = original_store
            main._process_incoming_message = original_process  # type: ignore[assignment]
            main.support_service.get_active_ticket = original_get_ticket  # type: ignore[method-assign]
            main.settings.whatsapp_message_burst_debounce_seconds = original_debounce
            main.settings.whatsapp_message_burst_max_wait_seconds = original_max_wait

        assert response is not None and response.status_code == 200
        assert seen == ["How much to ship to KSA?\nIt is 5 kg"]
        assert await store.pending_incoming_work_count(phone) == 0
        assert await store.claim_incoming_message("wamid.1") is None
        assert await store.claim_incoming_message("wamid.2") is None

    asyncio.run(run())


def test_primary_reply_is_sent_only_once_for_an_inbound_id() -> None:
    class Store:
        def __init__(self) -> None:
            self.claimed: set[str] = set()

        async def claim_outbound_reply(self, key: str) -> str | None:
            if key in self.claimed:
                return None
            self.claimed.add(key)
            return "token"

        async def complete_outbound_reply(self, key: str, token: str) -> None:
            return None

        async def release_outbound_reply(self, key: str, token: str) -> None:
            self.claimed.discard(key)

    class WhatsApp:
        def __init__(self) -> None:
            self.sent = 0

        async def send_text(self, to: str, body: str) -> dict:
            self.sent += 1
            return {"messages": [{"id": "wamid.out"}]}

    async def run() -> None:
        original_store = main.conversation_store
        original_client = main.whatsapp_client
        store = Store()
        client = WhatsApp()
        main.conversation_store = store  # type: ignore[assignment]
        main.whatsapp_client = client  # type: ignore[assignment]
        try:
            first = await main._send_text_once(
                to="96170123456",
                body="hello",
                source_message_id="wamid.in",
            )
            second = await main._send_text_once(
                to="96170123456",
                body="hello again",
                source_message_id="wamid.in",
            )
        finally:
            main.conversation_store = original_store
            main.whatsapp_client = original_client

        assert client.sent == 1
        assert first["messages"][0]["id"] == "wamid.out"
        assert second == {"deduplicated": True}

    asyncio.run(run())


def test_human_support_burst_keeps_each_message_separate() -> None:
    from app.conversation_store import InMemoryConversationStore

    async def run() -> None:
        store = InMemoryConversationStore()
        phone = "96170123456"
        messages = [
            IncomingWhatsAppMessage(phone, "wamid.h1", "text", "first", "100"),
            IncomingWhatsAppMessage(phone, "wamid.h2", "text", "second", "101"),
        ]
        for message in messages:
            claim = await store.claim_incoming_message(message.message_id or "")
            assert claim
            await store.enqueue_incoming_work(
                phone, main._queued_inbound_item(message, claim)
            )

        seen: list[str | None] = []

        async def fake_process(incoming: IncomingWhatsAppMessage):
            seen.append(incoming.text)
            return main._json_response(200, {"status": "held_for_human"})

        async def active_ticket(_phone: str):
            return {"id": "ticket-1"}

        original_store = main.conversation_store
        original_process = main._process_incoming_message
        original_get_ticket = main.support_service.get_active_ticket
        original_debounce = main.settings.whatsapp_message_burst_debounce_seconds
        original_max_wait = main.settings.whatsapp_message_burst_max_wait_seconds
        main.conversation_store = store  # type: ignore[assignment]
        main._process_incoming_message = fake_process  # type: ignore[assignment]
        main.support_service.get_active_ticket = active_ticket  # type: ignore[method-assign]
        main.settings.whatsapp_message_burst_debounce_seconds = 0
        main.settings.whatsapp_message_burst_max_wait_seconds = 0
        try:
            response = await main._drive_inbound_queue(phone)
        finally:
            main.conversation_store = original_store
            main._process_incoming_message = original_process  # type: ignore[assignment]
            main.support_service.get_active_ticket = original_get_ticket  # type: ignore[method-assign]
            main.settings.whatsapp_message_burst_debounce_seconds = original_debounce
            main.settings.whatsapp_message_burst_max_wait_seconds = original_max_wait

        assert response is not None and response.status_code == 200
        assert seen == ["first", "second"]

    asyncio.run(run())


def test_retryable_burst_is_requeued_without_losing_claim() -> None:
    from app.conversation_store import InMemoryConversationStore

    async def run() -> None:
        store = InMemoryConversationStore()
        phone = "96170123456"
        message = IncomingWhatsAppMessage(phone, "wamid.retry", "text", "hello", "100")
        claim = await store.claim_incoming_message(message.message_id or "")
        assert claim
        await store.enqueue_incoming_work(phone, main._queued_inbound_item(message, claim))

        calls = 0

        async def fake_process(_incoming: IncomingWhatsAppMessage):
            nonlocal calls
            calls += 1
            status = 503 if calls == 1 else 200
            return main._json_response(status, {"status": "test"})

        async def no_ticket(_phone: str):
            return None

        original_store = main.conversation_store
        original_process = main._process_incoming_message
        original_get_ticket = main.support_service.get_active_ticket
        original_debounce = main.settings.whatsapp_message_burst_debounce_seconds
        original_max_wait = main.settings.whatsapp_message_burst_max_wait_seconds
        main.conversation_store = store  # type: ignore[assignment]
        main._process_incoming_message = fake_process  # type: ignore[assignment]
        main.support_service.get_active_ticket = no_ticket  # type: ignore[method-assign]
        main.settings.whatsapp_message_burst_debounce_seconds = 0
        main.settings.whatsapp_message_burst_max_wait_seconds = 0
        try:
            first = await main._drive_inbound_queue(phone)
            assert first is not None and first.status_code == 503
            assert await store.pending_incoming_work_count(phone) == 1
            # The original idempotency claim is intentionally retained while queued.
            assert await store.claim_incoming_message("wamid.retry") is None

            second = await main._drive_inbound_queue(phone)
        finally:
            main.conversation_store = original_store
            main._process_incoming_message = original_process  # type: ignore[assignment]
            main.support_service.get_active_ticket = original_get_ticket  # type: ignore[method-assign]
            main.settings.whatsapp_message_burst_debounce_seconds = original_debounce
            main.settings.whatsapp_message_burst_max_wait_seconds = original_max_wait

        assert second is not None and second.status_code == 200
        assert await store.pending_incoming_work_count(phone) == 0
        assert calls == 2

    asyncio.run(run())


def test_inbound_queue_preserves_media_metadata() -> None:
    incoming = IncomingWhatsAppMessage(
        "96170123456",
        "wamid.media",
        "audio",
        None,
        "500",
        media_id="media-123",
        media_mime_type="audio/ogg; codecs=opus",
        media_sha256="sha",
        media_caption=None,
        is_voice=True,
    )
    item = main._queued_inbound_item(incoming, "claim-token")
    restored = main._incoming_from_queued_item(item)
    assert restored is not None
    assert restored.message_id == incoming.message_id
    assert restored.message_type == "audio"
    assert restored.media_id == "media-123"
    assert restored.media_mime_type == "audio/ogg; codecs=opus"
    assert restored.media_sha256 == "sha"
    assert restored.is_voice is True
