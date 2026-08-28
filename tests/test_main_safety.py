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


def test_stale_and_out_of_order_message_detection() -> None:
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
    ) == "out_of_order"
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
