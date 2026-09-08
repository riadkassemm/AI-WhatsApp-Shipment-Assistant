from __future__ import annotations

import asyncio
import json
import sys
import time
import types

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
from app.client_chat_auth import is_auth_cancel_intent, is_auth_topic_switch_intent
from app.conversation_store import Conversation, InMemoryConversationStore
from app.shipping_quote_service import ShippingQuoteOutcome
from app.whatsapp_client import IncomingWhatsAppMessage


def test_auth_cancel_intent_matches_customer_examples_without_stealing_business_cancel() -> None:
    for text in (
        "cancel",
        "Abort.",
        "ensa ma bade tshefle order",
        "ya khaye ma bade sewe login, ensa el order",
        "khaye ensa el order ma bade",
        "never mind the order check",
        "I don't want to login",
        "خلاص ما بدي تسجيل الدخول",
    ):
        assert is_auth_cancel_intent(text), text

    # This can be a fresh protected business request, so it must not be confused with
    # cancelling the authentication flow itself.
    assert not is_auth_cancel_intent("cancel my shipment")
    assert not is_auth_cancel_intent("cancel my order")


def test_auth_topic_switch_requires_high_confidence_noncredential_shape() -> None:
    assert is_auth_topic_switch_intent("bade esh7an bda3a 3a lebnen")
    assert is_auth_topic_switch_intent("what are your shipping rates?")
    assert is_auth_topic_switch_intent("shu fine esh7an w mn wen")
    assert is_auth_topic_switch_intent("mashkour")

    # Explicit credential/login wording wins and remains in the credential path.
    assert not is_auth_topic_switch_intent("password is hello world")
    assert not is_auth_topic_switch_intent("bade fout")
    # Weak prose should not be enough to reinterpret a possible passphrase as a topic.
    assert not is_auth_topic_switch_intent("hello world 123")


def test_abandon_pending_auth_clears_all_partial_state_and_one_time_password() -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        conversation = Conversation(
            phone_number="96170123456",
            auth_state="awaiting_username",
            pending_request="check my order",
            pending_tool_name="get_customer_shipments",
            pending_tool_arguments={},
        )
        await store.save(conversation)
        await store.stash_pending_auth_password(conversation.storage_key, "secret-pass")

        original_store = main.conversation_store
        main.conversation_store = store  # type: ignore[assignment]
        try:
            await main._abandon_pending_auth(conversation, reason="cancel")
        finally:
            main.conversation_store = original_store

        assert conversation.auth_state == "guest"
        assert conversation.pending_auth_userid is None
        assert conversation.pending_request is None
        assert conversation.pending_tool_name is None
        assert conversation.pending_tool_arguments is None
        assert await store.pop_pending_auth_password(conversation.storage_key) is None
        assert conversation.messages[-1]["role"] == "system"
        assert conversation.messages[-1]["content"].startswith("AUTH EVENT:")

    asyncio.run(run())


def test_explicit_cancel_stops_login_instead_of_becoming_a_password() -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        phone = "96170123456"
        conversation = Conversation(
            phone_number=phone,
            auth_state="awaiting_password",
            pending_auth_userid="10002",
            pending_request="bade tshefle order",
            pending_tool_name="get_customer_shipments",
            pending_tool_arguments={},
            current_language="ar-LB",
            communication_style="leb_arabizi",
        )
        await store.save(conversation)
        sent: list[str] = []

        async def no_ticket(*_args, **_kwargs):
            return None

        async def reconcile(*_args, **_kwargs):
            return None

        async def no_support_message(*_args, **_kwargs):
            return None

        async def fake_send_text_once(*, body: str, **_kwargs):
            sent.append(body)
            return {"messages": [{"id": "wamid.out"}]}

        originals = (
            main.conversation_store,
            main.support_service.get_active_ticket,
            main.support_service.reconcile_conversation_mode,
            main.support_service.get_message_by_whatsapp_id,
            main.support_service.get_ticket_by_source_message,
            main._send_text_once,
        )
        main.conversation_store = store  # type: ignore[assignment]
        main.support_service.get_active_ticket = no_ticket  # type: ignore[method-assign]
        main.support_service.reconcile_conversation_mode = reconcile  # type: ignore[method-assign]
        main.support_service.get_message_by_whatsapp_id = no_support_message  # type: ignore[method-assign]
        main.support_service.get_ticket_by_source_message = no_support_message  # type: ignore[method-assign]
        main._send_text_once = fake_send_text_once  # type: ignore[assignment]
        try:
            response = await main._process_incoming_message(
                IncomingWhatsAppMessage(
                    phone, "wamid.cancel", "text", "Abort.", str(int(time.time()))
                )
            )
        finally:
            (
                main.conversation_store,
                main.support_service.get_active_ticket,
                main.support_service.reconcile_conversation_mode,
                main.support_service.get_message_by_whatsapp_id,
                main.support_service.get_ticket_by_source_message,
                main._send_text_once,
            ) = originals

        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "authentication_cancelled"
        assert conversation.auth_state == "guest"
        assert conversation.pending_auth_userid is None
        assert conversation.pending_request is None
        assert len(sent) == 1
        assert "password" not in sent[0].casefold()

    asyncio.run(run())


def test_shipping_topic_switch_clears_stale_login_and_processes_new_request() -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        phone = "96170123456"
        conversation = Conversation(
            phone_number=phone,
            auth_state="awaiting_password",
            pending_auth_userid="10002",
            pending_request="bade tshefle order",
            pending_tool_name="get_customer_shipments",
            pending_tool_arguments={},
            current_language="ar-LB",
            communication_style="leb_arabizi",
        )
        await store.save(conversation)
        sent: list[str] = []
        semantic_flags: list[bool] = []

        async def no_ticket(*_args, **_kwargs):
            return None

        async def reconcile(*_args, **_kwargs):
            return None

        async def no_support_message(*_args, **_kwargs):
            return None

        async def shipping(
            _conversation,
            text: str,
            *,
            semantic_normalizer_enabled: bool = True,
        ):
            semantic_flags.append(semantic_normalizer_enabled)
            assert text == "bade esh7an bda3a 3a lebnen"
            return ShippingQuoteOutcome("Hayde l shipping options.", "shipping_catalog_options")

        async def fake_send_text_once(*, body: str, **_kwargs):
            sent.append(body)
            return {"messages": [{"id": "wamid.out"}]}

        originals = (
            main.conversation_store,
            main.support_service.get_active_ticket,
            main.support_service.reconcile_conversation_mode,
            main.support_service.get_message_by_whatsapp_id,
            main.support_service.get_ticket_by_source_message,
            main.maybe_handle_shipping_quote,
            main._send_text_once,
        )
        main.conversation_store = store  # type: ignore[assignment]
        main.support_service.get_active_ticket = no_ticket  # type: ignore[method-assign]
        main.support_service.reconcile_conversation_mode = reconcile  # type: ignore[method-assign]
        main.support_service.get_message_by_whatsapp_id = no_support_message  # type: ignore[method-assign]
        main.support_service.get_ticket_by_source_message = no_support_message  # type: ignore[method-assign]
        main.maybe_handle_shipping_quote = shipping  # type: ignore[assignment]
        main._send_text_once = fake_send_text_once  # type: ignore[assignment]
        try:
            response = await main._process_incoming_message(
                IncomingWhatsAppMessage(
                    phone,
                    "wamid.switch",
                    "text",
                    "bade esh7an bda3a 3a lebnen",
                    str(int(time.time())),
                )
            )
        finally:
            (
                main.conversation_store,
                main.support_service.get_active_ticket,
                main.support_service.reconcile_conversation_mode,
                main.support_service.get_message_by_whatsapp_id,
                main.support_service.get_ticket_by_source_message,
                main.maybe_handle_shipping_quote,
                main._send_text_once,
            ) = originals

        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "shipping_catalog_options"
        assert conversation.auth_state == "guest"
        assert conversation.pending_auth_userid is None
        assert conversation.pending_request is None
        assert conversation.pending_tool_name is None
        assert semantic_flags == [False]
        assert sent == ["Hayde l shipping options."]

    asyncio.run(run())
