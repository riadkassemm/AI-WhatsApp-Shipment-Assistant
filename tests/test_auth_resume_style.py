from __future__ import annotations

import asyncio
import re
import sys
import types

# Lightweight import-time stubs for production-only dependencies unavailable in the
# artifact validation runtime. Production uses the real packages.
fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {})
)
sys.modules["aiomysql"] = fake_aiomysql

fake_bcrypt = sys.modules.get("bcrypt") or types.ModuleType("bcrypt")
fake_bcrypt.checkpw = getattr(fake_bcrypt, "checkpw", lambda *_args, **_kwargs: False)
fake_bcrypt.hashpw = getattr(fake_bcrypt, "hashpw", lambda *_args, **_kwargs: b"hash")
fake_bcrypt.gensalt = getattr(fake_bcrypt, "gensalt", lambda *_args, **_kwargs: b"salt")
sys.modules["bcrypt"] = fake_bcrypt

fake_openai = sys.modules.get("openai") or types.ModuleType("openai")


class _AsyncOpenAI:
    def __init__(self, *_args, **_kwargs) -> None:
        pass


fake_openai.AsyncOpenAI = getattr(fake_openai, "AsyncOpenAI", _AsyncOpenAI)
sys.modules["openai"] = fake_openai

from app import main
from app.conversation_store import Conversation
from app.language import detect_communication_style
from app.openai_service import _input_for_api
from app.support_models import AIReplyResult
from app.whatsapp_client import IncomingWhatsAppMessage


_ARABIC_SCRIPT_RE = re.compile(r"[\u0600-\u06FF]")


class _Store:
    def __init__(self) -> None:
        self.saved: list[str] = []

    async def save(self, conversation: Conversation) -> None:
        self.saved.append(conversation.to_json())

    async def claim_outbound_reply(self, _key: str) -> str:
        return "token"

    async def complete_outbound_reply(self, _key: str, _token: str) -> None:
        return None

    async def release_outbound_reply(self, _key: str, _token: str) -> None:
        return None


class _WhatsApp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to: str, body: str) -> dict:
        self.sent.append((to, body))
        return {"messages": [{"id": "wamid.out"}]}


def test_short_lebanese_order_requests_are_detected_as_arabizi() -> None:
    assert detect_communication_style("bade tshefle order").style == "leb_arabizi"
    assert detect_communication_style("shefle el order").style == "leb_arabizi"
    assert detect_communication_style("bade check my order").style == "leb_arabizi"
    assert detect_communication_style("check my order").style == "en"


def test_pending_request_is_kept_when_model_asks_for_userid_without_auth_flag() -> None:
    async def run() -> None:
        original_store = main.conversation_store
        original_client = main.whatsapp_client
        original_ai = main._get_ai_reply_with_deadline
        store = _Store()
        client = _WhatsApp()
        conversation = Conversation(
            phone_number="96170123456",
            current_language="ar-LB",
            communication_style="leb_arabizi",
        )
        incoming = IncomingWhatsAppMessage(
            from_number=conversation.phone_number,
            message_id=None,
            message_type="text",
            text="bade tshefle order",
            timestamp=None,
        )

        async def fake_ai(
            conv: Conversation,
            user_message: str,
            **_kwargs,
        ) -> AIReplyResult:
            conv.messages.append({"role": "user", "content": user_message})
            mixed_reply = "أكيد، 3tine الـ User ID."
            conv.messages.append({"role": "assistant", "content": mixed_reply})
            return AIReplyResult(
                reply_text=mixed_reply,
                auth_required=False,
            )

        main.conversation_store = store  # type: ignore[assignment]
        main.whatsapp_client = client  # type: ignore[assignment]
        main._get_ai_reply_with_deadline = fake_ai  # type: ignore[assignment]
        try:
            ok, outcome = await main._handle_ai_result(
                conversation=conversation,
                incoming=incoming,
                user_message_for_ticket=incoming.text,
            )
        finally:
            main.conversation_store = original_store
            main.whatsapp_client = original_client
            main._get_ai_reply_with_deadline = original_ai

        assert ok is True
        assert outcome == "authentication_required"
        assert conversation.auth_state == "awaiting_credentials"
        assert conversation.pending_request == "bade tshefle order"
        assert conversation.pending_tool_name is None
        assert client.sent == [(conversation.phone_number, "B3atle l user ID taba3ak.")]
        assert not _ARABIC_SCRIPT_RE.search(client.sent[0][1])
        assert conversation.messages[-1] == {
            "role": "assistant",
            "content": "B3atle l user ID taba3ak.",
        }

    asyncio.run(run())


def test_blocked_order_tool_is_replayed_and_rendered_without_a_second_model_pass() -> None:
    async def run() -> None:
        original_store = main.conversation_store
        original_client = main.whatsapp_client
        original_dispatch = main.dispatch_tool_call
        original_ai = main._get_ai_reply_with_deadline
        store = _Store()
        client = _WhatsApp()
        dispatch_calls: list[tuple[str, dict, str | None]] = []
        conversation = Conversation(
            phone_number="96170123456",
            verified_customer_id="42",
            authenticated_userid="10002",
            auth_state="authenticated",
            pending_request="bade tshefle order",
            pending_tool_name="get_customer_shipments",
            pending_tool_arguments={},
            current_language="ar-LB",
            communication_style="leb_arabizi",
        )
        conversation.messages.extend(
            [
                {"role": "user", "content": "bade tshefle order"},
                {"role": "assistant", "content": "B3atle l user ID taba3ak."},
                {"role": "user", "content": "10002"},
                {"role": "assistant", "content": "Tamem, b3atle l password la nkammel."},
                {"role": "user", "content": "********"},
            ]
        )

        async def fake_dispatch(
            name: str,
            arguments: dict,
            verified_customer_id: str | None,
        ) -> dict:
            dispatch_calls.append((name, arguments, verified_customer_id))
            return {
                "found": True,
                "count": 2,
                "shipments": [
                    {
                        "order_id": "ORD-77",
                        "tracking_number": "TRK-77",
                        "shipment_id": "SHP-77",
                        "status": "IN_TRANSIT",
                    },
                    {
                        "order_id": "ORD-88",
                        "tracking_number": "TRK-88",
                        "shipment_id": "SHP-88",
                        "status": "RECEIVED",
                    },
                ],
            }

        async def fake_ai(*_args, **_kwargs) -> AIReplyResult:
            raise AssertionError("order-list replay must not require a model renderer")

        main.conversation_store = store  # type: ignore[assignment]
        main.whatsapp_client = client  # type: ignore[assignment]
        main.dispatch_tool_call = fake_dispatch  # type: ignore[assignment]
        main._get_ai_reply_with_deadline = fake_ai  # type: ignore[assignment]
        try:
            ok, outcome = await main._resume_after_auth(
                conversation,
                source_message_id=None,
            )
        finally:
            main.conversation_store = original_store
            main.whatsapp_client = original_client
            main.dispatch_tool_call = original_dispatch
            main._get_ai_reply_with_deadline = original_ai

        assert ok is True
        assert outcome == "authenticated_and_handled"
        assert dispatch_calls == [("get_customer_shipments", {}, "42")]
        assert conversation.pending_request is None
        assert conversation.pending_tool_name is None
        assert conversation.pending_tool_arguments is None
        assert len(client.sent) == 1
        reply = client.sent[0][1]
        assert "ORD-77" in reply
        assert "TRK-88" in reply
        assert "SHP-88" in reply
        assert "Wait Arabic forbidden" not in reply
        assert not _ARABIC_SCRIPT_RE.search(reply)

    asyncio.run(run())


def test_pending_request_is_last_ephemeral_user_turn_after_password_redaction() -> None:
    conversation = Conversation(
        phone_number="96170123456",
        verified_customer_id="42",
        auth_state="authenticated",
        current_language="ar-LB",
        communication_style="leb_arabizi",
    )
    conversation.messages.extend(
        [
            {"role": "user", "content": "bade tshefle order"},
            {"role": "assistant", "content": "B3atle l user ID taba3ak."},
            {"role": "user", "content": "********"},
        ]
    )
    items = _input_for_api(
        conversation,
        transient_system_context="AUTH EVENT: verified",
        transient_user_message="bade tshefle order",
    )
    assert items[-1] == {"role": "user", "content": "bade tshefle order"}
