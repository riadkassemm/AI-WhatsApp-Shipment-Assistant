"""Phase 1c: poison messages, stranded queues and abandoned in-flight work."""
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

import pytest  # noqa: E402

from app import conversation_store as store_module  # noqa: E402
from app import main  # noqa: E402
from app.conversation_store import InMemoryConversationStore, inbound_recovery_grace_seconds  # noqa: E402
from app.whatsapp_client import IncomingWhatsAppMessage  # noqa: E402

PHONE = "96170123456"
LINE = "1234567890"
ROUTE = f"{LINE}:{PHONE}"


class Clock:
    """Shifts wall-clock time for both main and the store."""

    def __init__(self, monkeypatch) -> None:
        self.offset = 0.0
        real = time.time
        monkeypatch.setattr(main.time, "time", lambda: real() + self.offset)

    def advance(self, seconds: float) -> None:
        self.offset += seconds


def _message(message_id: str, text: str = "hello", timestamp: str | None = None) -> IncomingWhatsAppMessage:
    return IncomingWhatsAppMessage(
        PHONE, message_id, "text", text, timestamp or str(int(time.time())),
        business_phone_number_id=LINE,
    )


async def _enqueue(store, message: IncomingWhatsAppMessage, **extra) -> None:
    claim = await store.claim_incoming_message(message.message_id or "")
    item = {**main._queued_inbound_item(message, claim), **extra}
    await store.enqueue_incoming_work(ROUTE, item)


class Pipeline:
    """Records how each queued message was processed."""

    def __init__(self, monkeypatch, store, *, healthy: bool = True) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.fail: set[str] = set()
        self.healthy = healthy

        async def process(incoming, **kwargs):
            self.calls.append((incoming.message_id, kwargs.get("safe_mode_reason")))
            if incoming.message_id in self.fail:
                return main._json_response(503, {"status": "support_state_unavailable"})
            return main._json_response(200, {"status": "handled"})

        async def no_ticket(*_args, **_kwargs):
            return None

        async def health():
            return self.healthy

        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main, "_process_incoming_message", process)
        monkeypatch.setattr(main, "_inbound_dependencies_healthy", health)
        monkeypatch.setattr(main.support_service, "get_active_ticket", no_ticket)
        monkeypatch.setattr(main.settings, "whatsapp_message_burst_debounce_seconds", 0)
        monkeypatch.setattr(main.settings, "whatsapp_message_burst_max_wait_seconds", 0)
        monkeypatch.setattr(main, "_inbound_stopping", False)

    @property
    def processed(self) -> list[str]:
        return [message_id for message_id, _reason in self.calls]


def _status(response) -> str:
    return json.loads(response.body)["status"]


# ---------------------------------------------------------------------------
# Item 1: repeated failures, backoff, safe mode, dead letter
# ---------------------------------------------------------------------------

def test_failed_head_backs_off_and_keeps_customer_order(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        pipeline = Pipeline(monkeypatch, store)
        clock = Clock(monkeypatch)
        pipeline.fail = {"m1"}
        await _enqueue(store, _message("m1"))
        await _enqueue(store, _message("m2"))

        first = await main._drive_inbound_queue(ROUTE)
        assert first.status_code == 503
        assert pipeline.processed == ["m1"]
        head = await store.peek_incoming_work(ROUTE)
        assert head["message_id"] == "m1" and head["attempts"] == 1

        # During the backoff nothing behind it overtakes the failed message.
        waiting = await main._drive_inbound_queue(ROUTE)
        assert _status(waiting) == "queued_for_retry"
        assert pipeline.processed == ["m1"]

        pipeline.fail = set()
        clock.advance(10)
        await main._drive_inbound_queue(ROUTE)
        assert pipeline.processed == ["m1", "m1", "m2"]
        assert await store.pending_incoming_work_count(ROUTE) == 0
        assert await store.inflight_incoming_work_count(ROUTE) == 0
    asyncio.run(run())


def test_failures_during_an_outage_are_not_counted(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        pipeline = Pipeline(monkeypatch, store, healthy=False)
        clock = Clock(monkeypatch)
        pipeline.fail = {"m1"}
        await _enqueue(store, _message("m1"))
        for _ in range(30):
            await main._drive_inbound_queue(ROUTE)
            clock.advance(400)
        head = await store.peek_incoming_work(ROUTE)
        assert int(head.get("attempts") or 0) == 0
        assert "first_failed_at" not in head
        assert await store.dead_letter_incoming_work_count(ROUTE) == 0
        # Outage failures never count toward the "repeatedly failing" safe mode.
        # (After hours of outage the message is routed to humans only because our
        # own delay pushed it past the freshness cutoff - never dropped, never AI.)
        reasons = {reason for _id, reason in pipeline.calls}
        assert "INBOUND_PROCESSING_FAILED" not in reasons
        assert reasons <= {None, "INBOUND_DELAYED_PROCESSING"}
    asyncio.run(run())


def test_third_attempt_switches_to_safe_mode(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        pipeline = Pipeline(monkeypatch, store)
        clock = Clock(monkeypatch)
        pipeline.fail = {"m1"}
        await _enqueue(store, _message("m1"))
        for _ in range(3):
            await main._drive_inbound_queue(ROUTE)
            clock.advance(400)
        pipeline.fail = set()
        await main._drive_inbound_queue(ROUTE)
        assert pipeline.calls == [
            ("m1", None), ("m1", None), ("m1", None),
            ("m1", "INBOUND_PROCESSING_FAILED"),
        ]
        assert await store.pending_incoming_work_count(ROUTE) == 0
    asyncio.run(run())


def test_persistent_failure_is_dead_lettered_and_later_messages_continue(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        pipeline = Pipeline(monkeypatch, store)
        clock = Clock(monkeypatch)
        pipeline.fail = {"m1"}
        await _enqueue(store, _message("m1"))
        await _enqueue(store, _message("m2"))
        for _ in range(25):
            await main._drive_inbound_queue(ROUTE)
            clock.advance(400)
            if await store.dead_letter_incoming_work_count(ROUTE):
                break
        assert await store.dead_letter_incoming_work_count(ROUTE) == 1
        assert pipeline.processed.count("m1") == main.settings.inbound_dead_letter_attempts
        assert pipeline.processed[-1] == "m2"
        assert await store.pending_incoming_work_count(ROUTE) == 0
        assert await store.inflight_incoming_work_count(ROUTE) == 0
        # The idempotency claim stays, so a provider redelivery is a duplicate.
        assert await store.claim_incoming_message("m1") is None
    asyncio.run(run())


def test_dead_letter_needs_minimum_age_as_well_as_attempts(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        pipeline = Pipeline(monkeypatch, store)
        pipeline.fail = {"m1"}
        await _enqueue(
            store, _message("m1"), attempts=40, first_failed_at=time.time(),
        )
        await main._drive_inbound_queue(ROUTE)
        assert await store.dead_letter_incoming_work_count(ROUTE) == 0
        assert (await store.peek_incoming_work(ROUTE))["attempts"] == 41
    asyncio.run(run())


def test_items_queued_before_this_release_process_normally(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        pipeline = Pipeline(monkeypatch, store)
        message = _message("legacy")
        claim = await store.claim_incoming_message("legacy")
        item = main._queued_inbound_item(message, claim)
        for field in ("enqueued_at", "contacts"):
            item.pop(field)
        await store.enqueue_incoming_work(ROUTE, item)
        await main._drive_inbound_queue(ROUTE)
        assert pipeline.calls == [("legacy", None)]
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Item 2: sweeper and self-delayed messages
# ---------------------------------------------------------------------------

def test_sweeper_drains_a_stranded_queue(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        pipeline = Pipeline(monkeypatch, store)
        # Queued, but the webhook that queued it never drove the queue.
        await _enqueue(store, _message("stranded"))
        assert await main._sweep_inbound_queues_once() == 1
        assert pipeline.processed == ["stranded"]
        assert await main._sweep_inbound_queues_once() == 0
    asyncio.run(run())


def test_sweeper_skips_while_shutting_down(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        pipeline = Pipeline(monkeypatch, store)
        await _enqueue(store, _message("later"))
        monkeypatch.setattr(main, "_inbound_stopping", True)
        await main._sweep_inbound_queues_once()
        assert pipeline.processed == []
        assert await store.pending_incoming_work_count(ROUTE) == 1
    asyncio.run(run())


def test_delay_caused_by_us_is_detected_but_late_provider_delivery_is_not():
    max_age = main.settings.whatsapp_max_inbound_age_seconds
    sent = int(time.time()) - max_age - 600
    ours = {"timestamp": str(sent), "enqueued_at": sent + 5}
    late = {"timestamp": str(sent), "enqueued_at": int(time.time())}
    fresh = {"timestamp": str(int(time.time())), "enqueued_at": int(time.time())}
    assert main._safe_mode_reason(ours) == "INBOUND_DELAYED_PROCESSING"
    assert main._safe_mode_reason(late) is None
    assert main._safe_mode_reason(fresh) is None
    assert main._safe_mode_reason({"timestamp": str(sent)}) is None  # legacy item
    assert main._safe_mode_reason({"recovered": True}) == "INBOUND_RECOVERED"
    assert main._safe_mode_reason({"attempts": 3}) == "INBOUND_PROCESSING_FAILED"


class Handoffs:
    def __init__(self, monkeypatch) -> None:
        self.reasons: list[str] = []
        self.ai_checks = 0
        store = InMemoryConversationStore()

        async def none(*_args, **_kwargs):
            return None

        async def reconcile(*_args, **_kwargs):
            return None

        async def finalize_handoff(*, handoff, **_kwargs):
            self.reasons.append(handoff.reason)
            return {"id": 9, "ticket_reference": "CS-T", "status": "NEW"}, True

        async def availability(*_args, **_kwargs):
            self.ai_checks += 1
            raise AssertionError("safe mode must not consult AI availability")

        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main.support_service, "get_active_ticket", none)
        monkeypatch.setattr(main.support_service, "reconcile_conversation_mode", reconcile)
        monkeypatch.setattr(main.support_service, "get_message_by_whatsapp_id", none)
        monkeypatch.setattr(main.support_service, "get_ticket_by_source_message", none)
        monkeypatch.setattr(main.support_service, "finalize_handoff", finalize_handoff)
        monkeypatch.setattr(main, "_record_delayed_support_message", none)
        monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", availability)


def test_self_delayed_message_reaches_human_support_instead_of_being_dropped(monkeypatch):
    async def run():
        handoffs = Handoffs(monkeypatch)
        old = str(int(time.time()) - main.settings.whatsapp_max_inbound_age_seconds - 600)
        response = await main._process_incoming_message(
            _message("delayed", timestamp=old), safe_mode_reason="INBOUND_DELAYED_PROCESSING",
        )
        assert _status(response) == "handed_off_safe_mode"
        assert handoffs.reasons == ["INBOUND_DELAYED_PROCESSING"]
        assert handoffs.ai_checks == 0
    asyncio.run(run())


def test_genuinely_late_message_is_still_dropped(monkeypatch):
    async def run():
        handoffs = Handoffs(monkeypatch)
        old = str(int(time.time()) - main.settings.whatsapp_max_inbound_age_seconds - 600)
        response = await main._process_incoming_message(_message("late", timestamp=old))
        assert _status(response) == "stale_message_ignored"
        assert handoffs.reasons == []
    asyncio.run(run())


def test_safe_mode_never_reaches_ai(monkeypatch):
    async def run():
        handoffs = Handoffs(monkeypatch)
        response = await main._process_incoming_message(
            _message("recovered"), safe_mode_reason="INBOUND_RECOVERED",
        )
        assert response.status_code == 200
        assert handoffs.reasons == ["INBOUND_RECOVERED"]
        assert handoffs.ai_checks == 0
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Item 3: abandoned in-flight work
# ---------------------------------------------------------------------------

def _shift_store_clock(monkeypatch, seconds: float) -> None:
    real = time.time
    monkeypatch.setattr(store_module, "_wall_clock", lambda: real() + seconds)


def test_abandoned_work_is_recovered_after_grace_for_human_support(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        pipeline = Pipeline(monkeypatch, store)
        await _enqueue(store, _message("m1"))
        await _enqueue(store, _message("m2"))
        await _enqueue(store, _message("m3"))
        # A worker reserved two messages and then died without acknowledging.
        owner = await store.claim_conversation_processing(ROUTE)
        await store.pop_incoming_work(ROUTE, claim_token=owner)
        await store.pop_incoming_work(ROUTE, claim_token=owner)
        await store.release_conversation_processing(ROUTE, owner)

        # Inside the grace period the work may still be live: keep refusing.
        with pytest.raises(RuntimeError, match="INBOUND_RECOVERY_REQUIRED"):
            await main._drive_inbound_queue(ROUTE)
        assert pipeline.calls == []

        _shift_store_clock(monkeypatch, inbound_recovery_grace_seconds() + 1)
        await main._drive_inbound_queue(ROUTE)
        assert pipeline.calls == [
            ("m1", "INBOUND_RECOVERED"),
            ("m2", "INBOUND_RECOVERED"),
            ("m3", None),
        ]
        assert await store.inflight_incoming_work_count(ROUTE) == 0
        assert await store.pending_incoming_work_count(ROUTE) == 0
    asyncio.run(run())


def test_recovery_requires_the_current_lock_owner():
    async def run():
        store = InMemoryConversationStore()
        await store.enqueue_incoming_work(ROUTE, {"message_id": "x"})
        owner = await store.claim_conversation_processing(ROUTE)
        await store.pop_incoming_work(ROUTE, claim_token=owner)
        with pytest.raises(RuntimeError, match="INBOUND_OWNER_LOST"):
            await store.recover_inflight_incoming_work(ROUTE, "someone-else")
        # Still inside the grace period for the real owner: nothing moves.
        assert await store.recover_inflight_incoming_work(ROUTE, owner) == 0
        assert await store.inflight_incoming_work_count(ROUTE) == 1
    asyncio.run(run())


def test_recovery_grace_never_undercuts_the_lock_ttl(monkeypatch):
    monkeypatch.setattr(main.settings, "inbound_recovery_grace_seconds", 1)
    monkeypatch.setattr(main.settings, "conversation_processing_lock_ttl_seconds", 180)
    assert inbound_recovery_grace_seconds() >= 240


def test_acknowledging_all_work_clears_the_inflight_marker():
    async def run():
        store = InMemoryConversationStore()
        await store.enqueue_incoming_work(ROUTE, {"message_id": None})
        owner = await store.claim_conversation_processing(ROUTE)
        item = await store.pop_incoming_work(ROUTE, claim_token=owner)
        assert ROUTE in store._inflight_since
        await store.acknowledge_incoming_work(ROUTE, item)
        assert ROUTE not in store._inflight_since
    asyncio.run(run())


def test_lifespan_starts_and_stops_the_sweeper(monkeypatch):
    async def run():
        async def nothing(*_args, **_kwargs):
            return None

        monkeypatch.setattr(main.settings, "support_ui_enabled", False)
        monkeypatch.setattr(main.customer_users, "close", nothing)
        monkeypatch.setattr(main.settings, "inbound_sweep_interval_seconds", 30)
        # Shutdown sets this global; let monkeypatch restore it for later tests.
        monkeypatch.setattr(main, "_inbound_stopping", False)
        async with main._lifespan(main.app):
            assert main._inbound_queue_sweeper is not None
            assert not main._inbound_queue_sweeper.done()
        assert main._inbound_queue_sweeper is None
    asyncio.run(run())
