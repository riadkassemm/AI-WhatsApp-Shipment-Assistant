"""Phase 1b: an automated turn that has started is never replayed.

Once a turn may have replied, consumed quota, attempted a login or called OpenAI,
any failure hands the message to human support. A redelivered message whose turn
already started goes straight to that handoff instead of running the turn again.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types

os.environ["REDIS_URL"] = ""

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
if not hasattr(fake_openai, "AsyncOpenAI"):
    class _AsyncOpenAI:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    fake_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules["openai"] = fake_openai

from app import main  # noqa: E402
from app.ai_availability import AIControlConfiguration, evaluate_ai_availability  # noqa: E402
from app.ai_rate_limit import AIQuotaDecision  # noqa: E402
from app.conversation_store import InMemoryConversationStore  # noqa: E402
from app.support_models import AIReplyResult  # noqa: E402
from app.whatsapp_client import IncomingWhatsAppMessage, WhatsAppSendError  # noqa: E402

PHONE = "96170123456"
LINE = "1234567890"


def _incoming(message_id: str = "wamid.turn", text: str = "Hello") -> IncomingWhatsAppMessage:
    return IncomingWhatsAppMessage(
        PHONE, message_id, "text", text, str(int(time.time())),
        business_phone_number_id=LINE,
    )


class Quota:
    def __init__(self) -> None:
        self.calls = 0

    async def consume(self, _customer: str) -> AIQuotaDecision:
        self.calls += 1
        return AIQuotaDecision(True)

    async def claim_notice(self, _customer: str) -> bool:
        return False


class Harness:
    """Real routing, store, guard and send-once code; fake providers and DB."""

    def __init__(self, monkeypatch) -> None:
        self.store = InMemoryConversationStore()
        self.quota = Quota()
        self.ai_calls = 0
        self.handoffs: list[str] = []
        self.provider_sends = 0
        self.send_error: Exception | None = None
        self.handoff_error: Exception | None = None
        self.ticket: dict | None = None
        configuration = AIControlConfiguration(
            control_mode="MANUAL", manual_ai_enabled=True, schedule_start_time="08:00",
            schedule_end_time="18:00", schedule_timezone="UTC", version=1,
        )
        availability = evaluate_ai_availability(configuration)

        async def get_ai_reply(conversation, user_message, **_kwargs):
            self.ai_calls += 1
            conversation.messages.append({"role": "user", "content": user_message})
            conversation.messages.append({"role": "assistant", "content": "Your parcel is on its way."})
            return AIReplyResult(reply_text="Your parcel is on its way.")

        async def send_text(**_kwargs):
            self.provider_sends += 1
            if self.send_error is not None:
                raise self.send_error
            return {"messages": [{"id": "wamid.out"}]}

        async def finalize_handoff(*, handoff, conversation, **_kwargs):
            if self.handoff_error is not None:
                raise self.handoff_error
            self.handoffs.append(handoff.reason)
            self.ticket = {"id": 77, "ticket_reference": "CS-TEST", "status": "NEW"}
            return self.ticket, len(self.handoffs) == 1

        async def none(*_args, **_kwargs):
            return None

        async def reconcile(conversation, active_ticket):
            conversation.mode = "human" if active_ticket else "ai"

        async def available(*_args, **_kwargs):
            return availability

        async def no_shipping(*_args, **_kwargs):
            return None

        monkeypatch.setattr(main, "conversation_store", self.store)
        monkeypatch.setattr(main, "ai_rate_limiter", self.quota)
        monkeypatch.setattr(main, "get_ai_reply", get_ai_reply)
        monkeypatch.setattr(main.whatsapp_client, "send_text", send_text)
        monkeypatch.setattr(main.support_service, "get_active_ticket", none)
        monkeypatch.setattr(main.support_service, "reconcile_conversation_mode", reconcile)
        monkeypatch.setattr(main.support_service, "get_message_by_whatsapp_id", none)
        monkeypatch.setattr(main.support_service, "get_ticket_by_source_message", none)
        monkeypatch.setattr(main.support_service, "finalize_handoff", finalize_handoff)
        monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", available)
        monkeypatch.setattr(main, "maybe_handle_shipping_quote", no_shipping)

    async def process(self, incoming: IncomingWhatsAppMessage, **kwargs):
        return await main._process_incoming_message(incoming, **kwargs)


def _status(response) -> str:
    return json.loads(response.body)["status"]


def test_successful_turn_is_unchanged(monkeypatch):
    async def run():
        h = Harness(monkeypatch)
        response = await h.process(_incoming())
        assert response.status_code == 200 and _status(response) == "handled"
        assert (h.ai_calls, h.quota.calls, h.provider_sends, h.handoffs) == (1, 1, 1, [])
    asyncio.run(run())


def test_definite_send_failure_hands_off_once_without_replaying_the_turn(monkeypatch):
    async def run():
        h = Harness(monkeypatch)
        h.send_error = WhatsAppSendError("rejected", delivery_uncertain=False, provider_code=131047)
        response = await h.process(_incoming())
        assert response.status_code == 200
        assert _status(response) == "handed_off_after_failed_turn"
        assert h.handoffs == ["AI_REPLY_SEND_FAILED"]
        assert (h.ai_calls, h.quota.calls) == (1, 1)
        conversation = await h.store.get_or_create(PHONE, conversation_key=f"{LINE}:{PHONE}")
        assistant = [m for m in conversation.messages if m.get("role") == "assistant"]
        assert len(assistant) == 1  # no duplicated history
    asyncio.run(run())


def test_uncertain_send_failure_also_hands_off(monkeypatch):
    async def run():
        h = Harness(monkeypatch)
        h.send_error = WhatsAppSendError("timeout", delivery_uncertain=True)
        response = await h.process(_incoming())
        assert response.status_code == 200
        assert h.handoffs == ["AI_REPLY_SEND_FAILED"]
        assert h.ai_calls == 1
    asyncio.run(run())


def test_unexpected_error_after_start_hands_off(monkeypatch):
    async def run():
        h = Harness(monkeypatch)
        h.send_error = OSError("connection reset")
        response = await h.process(_incoming())
        assert response.status_code == 200
        assert h.handoffs == ["AI_TURN_FAILED"]
        assert h.ai_calls == 1
    asyncio.run(run())


def test_redelivery_after_failed_handoff_never_reruns_the_turn(monkeypatch):
    async def run():
        h = Harness(monkeypatch)
        h.send_error = WhatsAppSendError("rejected", delivery_uncertain=False)
        h.handoff_error = RuntimeError("database unavailable")
        first = await h.process(_incoming())
        # The handoff could not be stored, so the message stays retryable...
        assert first.status_code == 503
        assert (h.ai_calls, h.quota.calls, h.provider_sends) == (1, 1, 1)

        # ...and the retry goes straight to human support: no second AI call,
        # no second quota use, no second customer send.
        h.handoff_error = None
        h.send_error = None
        second = await h.process(_incoming())
        assert second.status_code == 200
        assert _status(second) == "handed_off_after_failed_turn"
        assert h.handoffs == ["AI_TURN_FAILED"]
        assert (h.ai_calls, h.quota.calls, h.provider_sends) == (1, 1, 1)
    asyncio.run(run())


def test_retryable_response_from_inside_the_turn_hands_off(monkeypatch):
    async def run():
        h = Harness(monkeypatch)

        async def failing_auth(*_args, **_kwargs):
            raise RuntimeError("auth store down")

        monkeypatch.setattr(main, "_handle_conversational_auth", failing_auth)
        response = await h.process(_incoming())
        # The auth handler's own 503 is converted into a durable human handoff.
        assert response.status_code == 200
        assert h.handoffs == ["AI_TURN_FAILED"]
        assert h.ai_calls == 0
    asyncio.run(run())


def test_marker_unavailable_starts_nothing_and_stays_retryable(monkeypatch):
    async def run():
        h = Harness(monkeypatch)

        async def broken(_ids):
            raise ConnectionError("redis down")

        monkeypatch.setattr(h.store, "inbound_turn_started", broken)
        response = await h.process(_incoming())
        assert response.status_code == 503
        assert _status(response) == "turn_state_unavailable"
        assert (h.ai_calls, h.quota.calls, h.provider_sends, h.handoffs) == (0, 0, 0, [])
    asyncio.run(run())


def test_burst_marks_every_message_so_a_regrouped_retry_is_caught(monkeypatch):
    async def run():
        h = Harness(monkeypatch)
        h.send_error = WhatsAppSendError("rejected", delivery_uncertain=False)
        h.handoff_error = RuntimeError("database unavailable")
        combined = _incoming("wamid.b2", "first\nsecond")
        first = await h.process(combined, burst_message_ids=("wamid.b1", "wamid.b2"))
        assert first.status_code == 503 and h.ai_calls == 1

        # The retry regroups the earlier message with a newer one; its latest ID
        # differs, but the started turn is still recognised.
        h.handoff_error = None
        regrouped = _incoming("wamid.b3", "first\nsecond\nthird")
        second = await h.process(regrouped, burst_message_ids=("wamid.b1", "wamid.b2", "wamid.b3"))
        assert second.status_code == 200
        assert h.ai_calls == 1
        assert h.handoffs == ["AI_TURN_FAILED"]
    asyncio.run(run())


def test_ai_disabled_path_is_untouched_by_the_marker(monkeypatch):
    async def run():
        h = Harness(monkeypatch)
        off = evaluate_ai_availability(AIControlConfiguration(
            control_mode="MANUAL", manual_ai_enabled=False, schedule_start_time="08:00",
            schedule_end_time="18:00", schedule_timezone="UTC", version=1,
        ))

        async def unavailable(*_args, **_kwargs):
            return off

        monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", unavailable)
        response = await h.process(_incoming("wamid.off"))
        assert _status(response) == "handed_off_ai_disabled"
        assert h.handoffs == ["AI_DISABLED_GLOBAL"]
        assert not await h.store.inbound_turn_started(["wamid.off"])
    asyncio.run(run())


def test_in_memory_marker_contract():
    async def run():
        store = InMemoryConversationStore()
        assert not await store.inbound_turn_started(["a", "b"])
        await store.mark_inbound_turn_started(["a", ""])
        assert await store.inbound_turn_started(["b", "a"])
        assert not await store.inbound_turn_started(["b"])
        assert not await store.inbound_turn_started([])
    asyncio.run(run())


import pytest  # noqa: E402
# Turn-logic tests: automated turns get an in-memory AI ticket (see conftest).
pytestmark = pytest.mark.usefixtures("fake_ai_ticket")
