from __future__ import annotations

import asyncio
import json
import sys
import time
import types

# Keep imports lightweight in artifact validation environments.
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

from app.ai_rate_limit import AIQuotaDecision, AIUsageRateLimiter
from app.conversation_store import InMemoryConversationStore
from app import main
from app.shipping_quote_service import ShippingQuoteOutcome
from app.whatsapp_client import IncomingWhatsAppMessage


def test_memory_ai_limiter_enforces_rolling_customer_minute_quota() -> None:
    async def run() -> None:
        now = [1000.0]
        limiter = AIUsageRateLimiter(
            redis_url="",
            per_minute=2,
            per_hour=0,
            per_day=0,
            global_per_day=0,
            clock=lambda: now[0],
        )
        assert (await limiter.consume("96170111111")).allowed is True
        assert (await limiter.consume("96170111111")).allowed is True
        blocked = await limiter.consume("96170111111")
        assert blocked == AIQuotaDecision(False, "customer_minute")

        now[0] += 61
        assert (await limiter.consume("96170111111")).allowed is True

    asyncio.run(run())


def test_memory_ai_limiter_global_daily_circuit_breaker_spans_customers() -> None:
    async def run() -> None:
        limiter = AIUsageRateLimiter(
            redis_url="",
            per_minute=0,
            per_hour=0,
            per_day=0,
            global_per_day=2,
        )
        assert (await limiter.consume("customer-a")).allowed is True
        assert (await limiter.consume("customer-b")).allowed is True
        blocked = await limiter.consume("customer-c")
        assert blocked == AIQuotaDecision(False, "global_day")

    asyncio.run(run())


def test_ai_limit_notice_is_throttled_separately_from_quota() -> None:
    async def run() -> None:
        now = [5000.0]
        limiter = AIUsageRateLimiter(
            redis_url="",
            per_minute=1,
            per_hour=0,
            per_day=0,
            global_per_day=0,
            notice_cooldown_seconds=60,
            clock=lambda: now[0],
        )
        assert await limiter.claim_notice("customer-a") is True
        assert await limiter.claim_notice("customer-a") is False
        now[0] += 61
        assert await limiter.claim_notice("customer-a") is True

    asyncio.run(run())


def test_quota_block_returns_200_and_never_calls_ai(monkeypatch) -> None:
    async def ai_on():
        return types.SimpleNamespace(effective_ai_enabled=True)

    monkeypatch.setattr(
        main.ai_availability_service,
        "get_effective_ai_availability",
        ai_on,
    )
    class Blocker:
        async def consume(self, _key: str) -> AIQuotaDecision:
            return AIQuotaDecision(False, "customer_minute")

        async def claim_notice(self, _key: str) -> bool:
            return True

    async def run() -> None:
        store = InMemoryConversationStore()
        sent: list[str] = []
        ai_called = False

        async def no_ticket(*_args, **_kwargs):
            return None

        async def reconcile(*_args, **_kwargs):
            return None

        async def no_support_message(*_args, **_kwargs):
            return None

        async def no_auth(*_args, **_kwargs):
            return None

        async def no_shipping(*_args, **_kwargs):
            return None

        async def fake_send_text_once(*, body: str, **_kwargs):
            sent.append(body)
            return {"messages": [{"id": "wamid.out"}]}

        async def fail_ai(*_args, **_kwargs):
            nonlocal ai_called
            ai_called = True
            raise AssertionError("AI path must not run after quota denial")

        originals = (
            main.conversation_store,
            main.ai_rate_limiter,
            main.support_service.get_active_ticket,
            main.support_service.reconcile_conversation_mode,
            main.support_service.get_message_by_whatsapp_id,
            main.support_service.get_ticket_by_source_message,
            main._handle_conversational_auth,
            main.maybe_handle_shipping_quote,
            main._send_text_once,
            main._handle_ai_result,
        )
        main.conversation_store = store  # type: ignore[assignment]
        main.ai_rate_limiter = Blocker()  # type: ignore[assignment]
        main.support_service.get_active_ticket = no_ticket  # type: ignore[method-assign]
        main.support_service.reconcile_conversation_mode = reconcile  # type: ignore[method-assign]
        main.support_service.get_message_by_whatsapp_id = no_support_message  # type: ignore[method-assign]
        main.support_service.get_ticket_by_source_message = no_support_message  # type: ignore[method-assign]
        main._handle_conversational_auth = no_auth  # type: ignore[assignment]
        main.maybe_handle_shipping_quote = no_shipping  # type: ignore[assignment]
        main._send_text_once = fake_send_text_once  # type: ignore[assignment]
        main._handle_ai_result = fail_ai  # type: ignore[assignment]
        try:
            response = await main._process_incoming_message(
                IncomingWhatsAppMessage(
                    "96170123456", "wamid.limit", "text", "hello there", str(int(time.time()))
                )
            )
        finally:
            (
                main.conversation_store,
                main.ai_rate_limiter,
                main.support_service.get_active_ticket,
                main.support_service.reconcile_conversation_mode,
                main.support_service.get_message_by_whatsapp_id,
                main.support_service.get_ticket_by_source_message,
                main._handle_conversational_auth,
                main.maybe_handle_shipping_quote,
                main._send_text_once,
                main._handle_ai_result,
            ) = originals

        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "ai_rate_limited"
        assert ai_called is False
        assert len(sent) == 1

    asyncio.run(run())


def test_deterministic_shipping_answer_does_not_consume_ai_quota(monkeypatch) -> None:
    async def ai_on():
        return types.SimpleNamespace(effective_ai_enabled=True)

    monkeypatch.setattr(
        main.ai_availability_service,
        "get_effective_ai_availability",
        ai_on,
    )
    class MustNotConsume:
        async def consume(self, _key: str) -> AIQuotaDecision:
            raise AssertionError("deterministic shipping should bypass AI quota")

    async def run() -> None:
        store = InMemoryConversationStore()
        semantic_flags: list[bool] = []
        sent: list[str] = []

        async def no_ticket(*_args, **_kwargs):
            return None

        async def reconcile(*_args, **_kwargs):
            return None

        async def no_support_message(*_args, **_kwargs):
            return None

        async def no_auth(*_args, **_kwargs):
            return None

        async def deterministic_shipping(
            _conversation,
            _text: str,
            *,
            semantic_normalizer_enabled: bool = True,
        ):
            semantic_flags.append(semantic_normalizer_enabled)
            return ShippingQuoteOutcome("$10", "shipping_catalog_price")

        async def fake_send_and_record(_conversation, body: str, **_kwargs):
            sent.append(body)
            return None

        originals = (
            main.conversation_store,
            main.ai_rate_limiter,
            main.support_service.get_active_ticket,
            main.support_service.reconcile_conversation_mode,
            main.support_service.get_message_by_whatsapp_id,
            main.support_service.get_ticket_by_source_message,
            main._handle_conversational_auth,
            main.maybe_handle_shipping_quote,
            main._send_and_record,
        )
        main.conversation_store = store  # type: ignore[assignment]
        main.ai_rate_limiter = MustNotConsume()  # type: ignore[assignment]
        main.support_service.get_active_ticket = no_ticket  # type: ignore[method-assign]
        main.support_service.reconcile_conversation_mode = reconcile  # type: ignore[method-assign]
        main.support_service.get_message_by_whatsapp_id = no_support_message  # type: ignore[method-assign]
        main.support_service.get_ticket_by_source_message = no_support_message  # type: ignore[method-assign]
        main._handle_conversational_auth = no_auth  # type: ignore[assignment]
        main.maybe_handle_shipping_quote = deterministic_shipping  # type: ignore[assignment]
        main._send_and_record = fake_send_and_record  # type: ignore[assignment]
        try:
            response = await main._process_incoming_message(
                IncomingWhatsAppMessage(
                    "96170123456", "wamid.quote", "text", "5kg to KSA", str(int(time.time()))
                )
            )
        finally:
            (
                main.conversation_store,
                main.ai_rate_limiter,
                main.support_service.get_active_ticket,
                main.support_service.reconcile_conversation_mode,
                main.support_service.get_message_by_whatsapp_id,
                main.support_service.get_ticket_by_source_message,
                main._handle_conversational_auth,
                main.maybe_handle_shipping_quote,
                main._send_and_record,
            ) = originals

        assert response.status_code == 200
        assert semantic_flags == [False]
        assert sent == ["$10"]

    asyncio.run(run())


def test_redis_limiter_failure_is_fail_closed_for_wallet_protection() -> None:
    class BrokenRedis:
        async def eval(self, *_args, **_kwargs):
            raise ConnectionError("redis unavailable")

    async def run() -> None:
        limiter = AIUsageRateLimiter(
            redis_url="redis://example.invalid:6379/0",
            per_minute=1,
            per_hour=0,
            per_day=0,
            global_per_day=0,
            fail_closed=True,
        )
        limiter._redis_client = BrokenRedis()  # type: ignore[assignment]
        decision = await limiter.consume("customer-a")
        assert decision == AIQuotaDecision(False, "limiter_unavailable")

    asyncio.run(run())
