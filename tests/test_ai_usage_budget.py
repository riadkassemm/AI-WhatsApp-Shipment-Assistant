"""Step 2c: usage metering, verified pricing and the global AI budget.

Spending is reserved before every billable call, recorded durably as pending
at the full estimate, and only real provider usage may lower it. Unknown or
unverified pricing blocks the call. Real Redis/MariaDB behaviour (atomicity,
crash recovery, rebuilds) is covered in tests/integration/test_ai_usage_budget.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

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

from app import ai_usage, main, openai_service  # noqa: E402
from app.ai_usage import (  # noqa: E402
    AIUsageBlocked,
    AIUsageForbiddenError,
    AIUsageService,
    AIUsageValidationError,
    ModelPrice,
    audio_cost_micros,
    estimate_input_tokens,
    token_cost_micros,
)
from app.conversation_store import Conversation  # noqa: E402
from app.support_auth import SupportAgent  # noqa: E402

AGENT = SupportAgent(id="7", name="Agent", role="agent")
ADMIN = SupportAgent(id="6", name="Admin", role="admin")
NOW = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
SOL = ModelPrice(1, "gpt-6.2-sol", Decimal("2"), Decimal("0.1"), Decimal("10"), None, Decimal("0.5"))
LUNA_ROW = {"id": 2, "model_prefix": "gpt-6-luna", "input_per_million": "0.1", "cached_input_per_million": "0.01",
            "output_per_million": "0.5", "audio_per_minute": None, "batch_factor": "0.5"}
SOL_ROW = {"id": 1, "model_prefix": "gpt-6.2-sol", "input_per_million": "2", "cached_input_per_million": "0.1",
           "output_per_million": "10", "audio_per_minute": None, "batch_factor": "0.5"}


class FakeRepository:
    def __init__(self, *, prices=None, monthly_usd=300, daily_usd=None, enforce=True, thresholds="50,80,95") -> None:
        self.prices = prices if prices is not None else [SOL_ROW, LUNA_ROW]
        self.settings = {"monthly_limit_micros": int(monthly_usd * 1_000_000),
                         "daily_limit_micros": int(daily_usd * 1_000_000) if daily_usd else None,
                         "warning_thresholds": thresholds, "enforcement_enabled": 1 if enforce else 0,
                         "budget_timezone": "Asia/Beirut", "override_until": None, "version": 4}
        self.rows: dict[str, dict] = {}
        self.audits: list[tuple[str, dict]] = []
        self.fail_insert = False
        self.fail_settle = False
        self.saved_settings = None

    async def verified_prices(self):
        return sorted(self.prices, key=lambda r: -len(r["model_prefix"]))

    async def get_settings(self):
        return dict(self.settings)

    async def insert_pending(self, row):
        if self.fail_insert:
            raise ConnectionError("database down")
        self.rows[row["reservation_id"]] = {**row, "outcome": "pending", "cost_micros": row["estimate_micros"]}

    async def settle(self, reservation_id, values):
        if self.fail_settle:
            raise ConnectionError("database down")
        row = self.rows[reservation_id]
        if row["outcome"] != "pending":
            return False
        row.update(values)
        return True

    async def period_totals(self, *, month=None, day=None):
        key = "budget_month" if month else "budget_day"
        value = month or day
        settled = sum(r["cost_micros"] for r in self.rows.values() if r[key] == value and r["outcome"] != "pending")
        pending = sum(r["cost_micros"] for r in self.rows.values() if r[key] == value and r["outcome"] == "pending")
        return settled, pending

    async def update_settings(self, *, expected_version, values, actor):
        self.saved_settings = (expected_version, values, actor.id)

    async def audit(self, action, details):
        self.audits.append((action, details))


class FakeCounters:
    """Python model of the Redis Lua scripts (atomic in real Redis)."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, int]] = {}
        self.alerts: set[str] = set()
        self.blocked: set[str] = set()
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionError("redis down")

    month_key = staticmethod(lambda month: f"ai:budget:{month}")
    day_key = staticmethod(lambda day: f"ai:budget:{day}")

    async def exists(self, key):
        self._check()
        return key in self.data

    async def ensure(self, key, settled, pending):
        self._check()
        if key not in self.data:
            self.data[key] = {"spent": settled, "reserved": pending}
            return
        total = self.data[key]["spent"] + self.data[key]["reserved"]
        if settled + pending > total:
            self.data[key]["spent"] += settled + pending - total

    def _used(self, key):
        entry = self.data.get(key, {"spent": 0, "reserved": 0})
        return entry["spent"] + entry["reserved"]

    async def reserve(self, month, day, estimate, month_limit, day_limit, enforce):
        self._check()
        mk, dk = self.month_key(month), self.day_key(day)
        if enforce:
            if self._used(mk) + estimate > month_limit:
                return 1
            if day_limit and self._used(dk) + estimate > day_limit:
                return 2
        for key in (mk, dk):
            self.data.setdefault(key, {"spent": 0, "reserved": 0})["reserved"] += estimate
        return 0

    async def settle(self, month, day, estimate, cost):
        self._check()
        for key in (self.month_key(month), self.day_key(day)):
            entry = self.data.setdefault(key, {"spent": 0, "reserved": 0})
            entry["reserved"] = max(0, entry["reserved"] - estimate)
            entry["spent"] += cost

    async def used(self, key):
        self._check()
        return self._used(key)

    async def claim_alert(self, month, threshold):
        key = f"{month}:{threshold}"
        if key in self.alerts:
            return False
        self.alerts.add(key)
        return True

    async def block_model(self, model):
        self.blocked.add(model)

    async def unblock_model(self, model):
        self.blocked.discard(model)

    async def blocked_models(self):
        self._check()
        return set(self.blocked)


def _service(**repo_kwargs):
    repository = FakeRepository(**repo_kwargs)
    counters = FakeCounters()
    return AIUsageService(repository, counters, clock=lambda: NOW), repository, counters


def _response(model="gpt-6.2-sol-2026-11-01", *, input_tokens=1000, cached=400, output=200, reasoning=50):
    return SimpleNamespace(model=model, _request_id="req_123", usage=SimpleNamespace(
        input_tokens=input_tokens, input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens=output, output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning)))


MONTH = "ai:budget:2026-11"


# ---------------------------------------------------------------------------
# Cost arithmetic
# ---------------------------------------------------------------------------

def test_cost_uses_actual_tokens_and_the_model_price():
    # (10,000 - 4,000) x $2 + 4,000 x $0.10 + 500 x $10 per 1M tokens = $0.0174
    assert token_cost_micros(SOL, input_tokens=10_000, cached_input_tokens=4_000, output_tokens=500) == 17_400
    assert token_cost_micros(SOL, input_tokens=10_000, cached_input_tokens=4_000, output_tokens=500, batch=True) == 8_700
    transcribe = ModelPrice(9, "gpt-transcribe", Decimal(0), Decimal(0), Decimal(0), Decimal("0.0045"), Decimal("0.5"))
    assert audio_cost_micros(transcribe, 30) == 2_250  # half a minute at $0.0045/min


def test_input_estimate_is_conservative_for_arabic_text():
    arabic = "وين صارت الشحنة تبعي؟ " * 20
    assert estimate_input_tokens(arabic) >= len(arabic) // 2
    assert estimate_input_tokens("hi", images=2) >= 4000


# ---------------------------------------------------------------------------
# Metered calls
# ---------------------------------------------------------------------------

def test_successful_call_reserves_records_pending_then_settles_actual_cost():
    service, repository, counters = _service()
    order: list[str] = []
    real_insert = repository.insert_pending

    async def insert(row):
        order.append("pending_row")
        assert counters.data[MONTH]["reserved"] == row["estimate_micros"]  # reserved first
        await real_insert(row)

    repository.insert_pending = insert

    async def call():
        order.append("openai")
        return _response()

    asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call,
                                input_payload="hello", max_output_tokens=500))
    assert order == ["pending_row", "openai"]
    row = next(iter(repository.rows.values()))
    expected = token_cost_micros(SOL, input_tokens=1000, cached_input_tokens=400, output_tokens=200)
    assert row["outcome"] == "success" and row["cost_basis"] == "computed" and row["cost_micros"] == expected
    assert row["model"] == "gpt-6.2-sol-2026-11-01" and row["reasoning_tokens"] == 50
    assert counters.data[MONTH] == {"spent": expected, "reserved": 0}
    assert row["estimate_micros"] >= expected


def test_failed_or_timed_out_call_keeps_the_full_estimate():
    for error, outcome in ((ConnectionError("reset"), "failure"), (asyncio.TimeoutError(), "timeout")):
        service, repository, counters = _service()

        async def call():
            raise error

        with pytest.raises(type(error)):
            asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call,
                                        input_payload="x", max_output_tokens=400))
        row = next(iter(repository.rows.values()))
        assert row["outcome"] == outcome and row["cost_basis"] == "reserved_estimate"
        assert row["cost_micros"] == row["estimate_micros"]
        assert counters.data[MONTH] == {"spent": row["estimate_micros"], "reserved": 0}


def test_unverified_or_unknown_pricing_blocks_before_any_reservation_or_call():
    service, repository, counters = _service(prices=[LUNA_ROW])
    called = []

    async def call():
        called.append(1)

    with pytest.raises(AIUsageBlocked) as blocked:
        asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call))
    assert blocked.value.reason == "AI_MODEL_PRICING_UNCONFIGURED"
    assert called == [] and repository.rows == {} and counters.data == {}


def test_unpriced_returned_model_charges_the_estimate_and_blocks_the_model():
    service, repository, counters = _service()

    async def call():
        return _response(model="gpt-7-surprise")

    asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call,
                                input_payload="x", max_output_tokens=300))
    row = next(iter(repository.rows.values()))
    assert row["cost_basis"] == "unpriced" and row["cost_micros"] == row["estimate_micros"]
    assert "gpt-6.2-sol" in counters.blocked
    with pytest.raises(AIUsageBlocked, match="unpriced"):
        asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call))


def test_pending_row_failure_releases_the_reservation_and_skips_the_call():
    service, repository, counters = _service()
    repository.fail_insert = True
    called = []

    async def call():
        called.append(1)

    with pytest.raises(AIUsageBlocked) as blocked:
        asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call,
                                    input_payload="x", max_output_tokens=100))
    assert blocked.value.reason == "AI_BUDGET_UNAVAILABLE"
    assert called == [] and counters.data[MONTH] == {"spent": 0, "reserved": 0}


def test_failed_settlement_keeps_spend_fully_counted():
    service, repository, counters = _service()
    repository.fail_settle = True

    async def call():
        return _response()

    asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call,
                                input_payload="x", max_output_tokens=100))
    row = next(iter(repository.rows.values()))
    assert row["outcome"] == "pending" and row["cost_micros"] == row["estimate_micros"]
    assert counters.data[MONTH]["reserved"] == row["estimate_micros"]  # still counted


def test_redis_outage_fails_closed():
    service, _repository, counters = _service()
    counters.down = True

    async def call():
        raise AssertionError("must not be called")

    with pytest.raises(AIUsageBlocked) as blocked:
        asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call))
    assert blocked.value.reason == "AI_BUDGET_UNAVAILABLE"
    assert asyncio.run(service.budget_state()) == (False, "BUDGET_UNAVAILABLE")


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------

def _spend(service, counters, micros):
    counters.data[MONTH] = {"spent": micros, "reserved": 0}
    service._reconciled[MONTH] = time.monotonic()
    service._reconciled["ai:budget:2026-11-22"] = time.monotonic()
    counters.data.setdefault("ai:budget:2026-11-22", {"spent": 0, "reserved": 0})


def test_monthly_limit_refuses_new_calls_and_pauses_ai():
    service, repository, counters = _service(monthly_usd=1)
    _spend(service, counters, 999_990)
    called = []

    async def call():
        called.append(1)

    with pytest.raises(AIUsageBlocked) as blocked:
        asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call,
                                    input_payload="x" * 100, max_output_tokens=500))
    assert blocked.value.reason == "AI_BUDGET_EXCEEDED" and called == []
    _spend(service, counters, 1_000_000)
    assert asyncio.run(service.budget_state()) == (False, "BUDGET_EXCEEDED")


def test_daily_limit_and_enforcement_switch_and_override():
    service, repository, counters = _service(monthly_usd=100, daily_usd=1)
    _spend(service, counters, 0)
    counters.data["ai:budget:2026-11-22"] = {"spent": 1_000_000, "reserved": 0}
    assert asyncio.run(service.budget_state()) == (False, "BUDGET_EXCEEDED")

    repository.settings["enforcement_enabled"] = 0
    service._settings_cache = None
    assert asyncio.run(service.budget_state()) == (True, None)

    repository.settings["enforcement_enabled"] = 1
    repository.settings["override_until"] = NOW + timedelta(hours=1)
    service._settings_cache = None
    assert asyncio.run(service.budget_state()) == (True, None)


def test_missing_counters_are_rebuilt_from_durable_rows_including_pending():
    service, repository, counters = _service(monthly_usd=1)
    repository.rows = {
        "a": {"budget_month": "2026-11", "budget_day": "2026-11-21", "outcome": "success", "cost_micros": 600_000},
        "b": {"budget_month": "2026-11", "budget_day": "2026-11-22", "outcome": "pending", "cost_micros": 450_000},
    }
    # Redis was flushed: nothing in counters. The rebuild must still see $1.05.
    assert asyncio.run(service.budget_state()) == (False, "BUDGET_EXCEEDED")
    assert counters.data[MONTH] == {"spent": 600_000, "reserved": 450_000}


def test_reconciliation_only_ever_raises_the_counters():
    service, repository, counters = _service()
    counters.data[MONTH] = {"spent": 900_000, "reserved": 0}
    counters.data["ai:budget:2026-11-22"] = {"spent": 0, "reserved": 0}
    repository.rows = {"a": {"budget_month": "2026-11", "budget_day": "2026-11-22", "outcome": "success",
                             "cost_micros": 100_000}}
    asyncio.run(service.budget_state())
    assert counters.data[MONTH]["spent"] == 900_000  # never lowered to the smaller DB total
    repository.rows["b"] = {"budget_month": "2026-11", "budget_day": "2026-11-22", "outcome": "pending",
                            "cost_micros": 1_500_000}
    service._reconciled.clear()
    asyncio.run(service.budget_state())
    assert counters.data[MONTH]["spent"] + counters.data[MONTH]["reserved"] == 1_600_000


def test_thresholds_alert_once_each():
    service, repository, counters = _service(monthly_usd=1)
    _spend(service, counters, 0)
    published = []

    async def publish(event_type, **payload):
        published.append((event_type, payload["threshold"]))

    import app.support_events as events
    original = events.publish_support_event
    events.publish_support_event = publish
    try:
        async def call():
            return _response(input_tokens=0, cached=0, output=55_000)  # $0.55 on sol

        asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call,
                                    input_payload="x", max_output_tokens=60_000))
        asyncio.run(service._check_thresholds("2026-11"))
    finally:
        events.publish_support_event = original
    assert [a for a, _ in repository.audits] == ["AI_BUDGET_THRESHOLD"]
    assert published == [("ai_budget_threshold", 50)]


# ---------------------------------------------------------------------------
# Settings, routing, events
# ---------------------------------------------------------------------------

def test_only_admins_change_the_budget_and_values_are_validated():
    service, repository, _ = _service()
    good = {"monthly_limit_usd": 300, "daily_limit_usd": None, "warning_thresholds": [50, 80, 95],
            "enforcement_enabled": True, "version": 4}
    with pytest.raises(AIUsageForbiddenError):
        asyncio.run(service.update_settings(AGENT, good))
    for bad in ({"monthly_limit_usd": 0}, {"monthly_limit_usd": "abc"}, {"daily_limit_usd": 500},
                {"warning_thresholds": [0]}, {"warning_thresholds": []}, {"override_until": "not-a-date"}):
        with pytest.raises(AIUsageValidationError):
            asyncio.run(service.update_settings(ADMIN, {**good, **bad}))
    assert repository.saved_settings is None
    asyncio.run(service.update_settings(ADMIN, {**good, "monthly_limit_usd": "250.50", "daily_limit_usd": "20"}))
    version, values, actor = repository.saved_settings
    assert version == 4 and actor == "6"
    assert values["monthly_limit_micros"] == 250_500_000 and values["daily_limit_micros"] == 20_000_000
    assert values["warning_thresholds"] == "50,80,95"


def test_default_settings_are_300_dollars_with_50_80_95_warnings():
    from pathlib import Path
    sql = Path("migrations/20261122_ai_usage_budget.sql").read_text()
    assert "monthly_limit_micros BIGINT UNSIGNED NOT NULL DEFAULT 300000000" in sql
    assert "warning_thresholds VARCHAR(64) NOT NULL DEFAULT '50,80,95'" in sql
    assert "enforcement_enabled TINYINT(1) NOT NULL DEFAULT 1" in sql
    # Seeded prices come from the pricing page but start unverified.
    assert "verified_at" not in sql.split("INSERT INTO ai_model_prices")[1].split(";")[0]


def test_blocked_call_during_a_turn_hands_off_with_the_reason(monkeypatch):
    async def blocked(**_kwargs):
        raise AIUsageBlocked("AI_BUDGET_EXCEEDED", "budget used up")

    class Client:
        class responses:
            @staticmethod
            async def create(**_kwargs):
                raise AssertionError("never called")

    monkeypatch.setattr(openai_service, "_get_client", lambda: Client())
    monkeypatch.setattr(openai_service.ai_usage_service, "metered", blocked)
    result = asyncio.run(openai_service.get_ai_reply(Conversation(phone_number="96170000001"), "hello"))
    assert result.reply_text == "" and result.handoff.reason == "AI_BUDGET_EXCEEDED"


def test_routing_hands_off_when_the_budget_pauses_ai(monkeypatch):
    from app.ai_availability import AIControlConfiguration, evaluate_ai_availability
    from app.conversation_store import InMemoryConversationStore
    from app.whatsapp_client import IncomingWhatsAppMessage

    handoffs = []
    base = evaluate_ai_availability(AIControlConfiguration("MANUAL", True, "08:00", "18:00", "UTC", 1))

    async def availability(**_kwargs):
        return type(base)(base.configuration, False, "BUDGET_EXCEEDED", None)

    async def none(*_args, **_kwargs):
        return None

    async def finalize(*, handoff, **_kwargs):
        handoffs.append(handoff.reason)
        return {"id": 3, "ticket_reference": "CS-T", "status": "NEW"}, True

    monkeypatch.setattr(main, "conversation_store", InMemoryConversationStore())
    monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", availability)
    for name in ("get_active_ticket", "reconcile_conversation_mode", "get_message_by_whatsapp_id",
                 "get_ticket_by_source_message"):
        monkeypatch.setattr(main.support_service, name, none)
    monkeypatch.setattr(main.support_service, "finalize_handoff", finalize)
    incoming = IncomingWhatsAppMessage("96170000001", "wamid.b", "text", "hi", str(int(time.time())),
                                       business_phone_number_id="90052")
    asyncio.run(main._process_incoming_message(incoming))
    assert handoffs == ["AI_BUDGET_EXCEEDED"]


def test_budget_events_reach_admins_only():
    from app.support_service import SupportService

    event = {"type": "ai_budget_threshold", "threshold": 80, "month": "2026-11"}
    assert asyncio.run(SupportService().can_receive_support_event(dict(event), AGENT)) is False
    assert asyncio.run(SupportService().can_receive_support_event(dict(event), ADMIN)) is True


def test_usage_rows_never_store_message_text():
    service, repository, _ = _service()

    async def call():
        return _response()

    asyncio.run(service.metered(purpose="turn", tier="primary", model="gpt-6.2-sol", call=call,
                                input_payload="My password is hunter2 and my order is ORD-QZX-9081",
                                max_output_tokens=100))
    stored = json.dumps(list(repository.rows.values()), default=str)
    assert "hunter2" not in stored and "ORD-QZX-9081" not in stored


@pytest.mark.parametrize('audio_price', [None, '0'])
def test_audio_without_verified_minute_price_is_blocked(audio_price):
    service, repository, counters = _service(prices=[{**LUNA_ROW, 'model_prefix': 'gpt-transcribe', 'audio_per_minute': audio_price}])
    async def forbidden():
        raise AssertionError('Provider must not be called')
    with pytest.raises(AIUsageBlocked, match='verified price'):
        asyncio.run(service.metered(purpose='transcription', tier='primary', model='gpt-transcribe',
                                   audio_seconds=30, call=forbidden))
    assert repository.rows == {}
