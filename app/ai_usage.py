"""Metering, pricing and the global AI budget for every billable OpenAI call.

Every billable call goes through ``ai_usage_service.metered(...)``:

1. **Price check.** The requested model must have a price row verified by an
   Admin/Super Admin. Unknown or unverified pricing blocks the call (no fallback
   price is ever assumed).
2. **Atomic reservation.** A Redis Lua script checks ``spent + reserved +
   estimate`` against the monthly (and optional daily) limit and reserves the
   worst-case estimate. All WhatsApp numbers share one global budget.
3. **Durable pending row.** An ``ai_usage_events`` row is committed with
   ``outcome='pending'`` and ``cost_micros`` equal to the full estimate BEFORE the
   call is made.
4. **The call.**
5. **Settlement.** Only real provider ``usage`` may lower the row below its
   estimate. Timeouts, failures and crashes keep the full estimate.

Redis counters are only an enforcement cache. MariaDB is the durable record:
counters are rebuilt from it whenever they are missing, and a periodic
reconciliation raises them to the database total, never lowering them. Figures
are application estimates, not the provider's invoice.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import aiomysql

from app.config import settings
from app.support_auth import SupportAgent

logger = logging.getLogger("shipment-bot")

MICROS_PER_USD = 1_000_000
_COUNTER_TTL_SECONDS = 40 * 24 * 60 * 60
_RECONCILE_INTERVAL_SECONDS = 60.0
_IMAGE_TOKEN_ESTIMATE = 2000
_SETTINGS_TTL_SECONDS = 15.0
_BLOCKED_MODELS_KEY = "ai:pricing:blocked"

# The receiving number / ticket for usage attribution, set by the turn runner.
usage_scope: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ai_usage_scope", default=None)


class AIUsageError(Exception):
    reason = "AI_USAGE_UNAVAILABLE"


class AIUsageBlocked(AIUsageError):
    """A billable call was refused before reaching OpenAI."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class AIUsageForbiddenError(AIUsageError):
    pass


class AIUsageValidationError(AIUsageError):
    pass


class AIUsageConflictError(AIUsageError):
    pass


@dataclass(frozen=True)
class ModelPrice:
    id: int
    model_prefix: str
    input_per_million: Decimal
    cached_input_per_million: Decimal
    output_per_million: Decimal
    audio_per_minute: Decimal | None
    batch_factor: Decimal


@dataclass(frozen=True)
class BudgetSettings:
    monthly_limit_micros: int
    daily_limit_micros: int | None
    warning_thresholds: tuple[int, ...]
    enforcement_enabled: bool
    budget_timezone: str
    override_until: datetime | None
    version: int
    updated_at: datetime | None = None
    updated_by_staff_id: int | None = None
    updated_by_staff_name: str | None = None

    def override_active(self, now: datetime) -> bool:
        until = self.override_until
        if until is None:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > now

    def to_dict(self) -> dict[str, Any]:
        return {
            "monthly_limit_usd": micros_to_usd(self.monthly_limit_micros),
            "daily_limit_usd": micros_to_usd(self.daily_limit_micros) if self.daily_limit_micros else None,
            "warning_thresholds": list(self.warning_thresholds),
            "enforcement_enabled": self.enforcement_enabled,
            "budget_timezone": self.budget_timezone,
            "override_until": _iso(self.override_until),
            "version": self.version,
            "updated_at": _iso(self.updated_at),
            "updated_by": (
                {"id": str(self.updated_by_staff_id), "name": self.updated_by_staff_name}
                if self.updated_by_staff_id else None
            ),
        }


def micros_to_usd(value: int | None) -> float:
    return round(int(value or 0) / MICROS_PER_USD, 6)


def usd_to_micros(value: Any) -> int:
    return int((Decimal(str(value)) * MICROS_PER_USD).to_integral_value(rounding=ROUND_CEILING))


def _ceil_micros(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def token_cost_micros(price: ModelPrice, *, input_tokens: int, cached_input_tokens: int,
                      output_tokens: int, batch: bool = False) -> int:
    """Price per 1M tokens in USD equals micro-USD per token."""
    uncached = max(0, int(input_tokens) - int(cached_input_tokens))
    cost = (
        Decimal(uncached) * price.input_per_million
        + Decimal(max(0, int(cached_input_tokens))) * price.cached_input_per_million
        + Decimal(max(0, int(output_tokens))) * price.output_per_million
    )
    if batch:
        cost *= price.batch_factor
    return _ceil_micros(cost)


def audio_cost_micros(price: ModelPrice, seconds: float) -> int:
    per_minute = price.audio_per_minute or Decimal(0)
    return _ceil_micros(Decimal(str(max(0.0, seconds))) / Decimal(60) * per_minute * MICROS_PER_USD)


def estimate_input_tokens(payload: Any, *, images: int = 0) -> int:
    """Deliberately high input estimate: every 2 characters count as one token.

    Arabic and Arabizi tokenize less efficiently than English; overestimating
    only makes the reservation more conservative.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    return math.ceil(len(text) / 2) + 64 + images * _IMAGE_TOKEN_ESTIMATE


def periods(now: datetime, tz_name: str) -> tuple[str, str]:
    local = now.astimezone(ZoneInfo(tz_name))
    return local.strftime("%Y-%m-%d"), local.strftime("%Y-%m")


class AIUsageRepository:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await aiomysql.create_pool(
            host=settings.db_host, port=settings.db_port, user=settings.db_user,
            password=settings.db_password, db=settings.db_name, minsize=1, maxsize=5,
            autocommit=True, charset="utf8mb4",
        )

    async def _fetch(self, query: str, args: tuple = (), *, one: bool = False):
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, args)
                return await (cur.fetchone() if one else cur.fetchall())

    async def _execute(self, query: str, args: tuple = ()) -> int:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, args)
                return int(cur.rowcount or 0)

    async def verified_prices(self) -> list[dict[str, Any]]:
        return await self._fetch(
            """
            SELECT id, model_prefix, input_per_million, cached_input_per_million, output_per_million,
                   audio_per_minute, batch_factor
            FROM ai_model_prices
            WHERE verified_at IS NOT NULL AND retired_at IS NULL AND effective_from <= UTC_TIMESTAMP(6)
            ORDER BY CHAR_LENGTH(model_prefix) DESC, effective_from DESC, id DESC
            """
        )

    async def get_settings(self) -> dict[str, Any] | None:
        return await self._fetch("SELECT * FROM ai_budget_settings WHERE id = 1", one=True)

    async def insert_pending(self, row: dict[str, Any]) -> None:
        await self._execute(
            """
            INSERT INTO ai_usage_events (
                reservation_id, created_at, purpose, tier, requested_model, modalities,
                estimate_micros, cost_micros, price_id, cost_basis, outcome,
                phone_number_id, ticket_id, batch_id, budget_day, budget_month
            ) VALUES (%s, UTC_TIMESTAMP(6), %s, %s, %s, %s, %s, %s, %s, 'reserved_estimate', 'pending',
                      %s, %s, %s, %s, %s)
            """,
            (row["reservation_id"], row["purpose"], row["tier"], row["requested_model"], row["modalities"],
             row["estimate_micros"], row["estimate_micros"], row["price_id"],
             row["phone_number_id"], row["ticket_id"], row["batch_id"], row["budget_day"], row["budget_month"]),
        )

    async def settle(self, reservation_id: str, values: dict[str, Any]) -> bool:
        """Settle a pending row once; returns False if it was already settled."""
        changed = await self._execute(
            """
            UPDATE ai_usage_events
            SET settled_at = UTC_TIMESTAMP(6), duration_ms = %s, model = %s,
                input_tokens = %s, cached_input_tokens = %s, output_tokens = %s, reasoning_tokens = %s,
                audio_seconds = %s, cost_micros = %s, cost_basis = %s, outcome = %s, openai_request_id = %s
            WHERE reservation_id = %s AND outcome = 'pending'
            """,
            (values.get("duration_ms"), values.get("model"), values.get("input_tokens"),
             values.get("cached_input_tokens"), values.get("output_tokens"), values.get("reasoning_tokens"),
             values.get("audio_seconds"), int(values["cost_micros"]), values["cost_basis"], values["outcome"],
             values.get("openai_request_id"), reservation_id),
        )
        return changed == 1

    async def period_totals(self, *, month: str | None = None, day: str | None = None) -> tuple[int, int]:
        """(settled micros, pending micros) for one budget period - pending at full estimate."""
        column, value = ("budget_month", month) if month else ("budget_day", day)
        row = await self._fetch(
            f"""
            SELECT COALESCE(SUM(CASE WHEN outcome <> 'pending' THEN cost_micros END), 0) AS settled,
                   COALESCE(SUM(CASE WHEN outcome = 'pending' THEN cost_micros END), 0) AS pending
            FROM ai_usage_events WHERE {column} = %s
            """,
            (value,), one=True,
        )
        return int((row or {}).get("settled") or 0), int((row or {}).get("pending") or 0)

    async def update_settings(self, *, expected_version: int, values: dict[str, Any], actor: SupportAgent) -> None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM ai_budget_settings WHERE id = 1 FOR UPDATE")
                    current = await cur.fetchone()
                    if not current:
                        raise AIUsageError("AI budget settings are not initialized. Apply the migration.")
                    if int(current["version"]) != int(expected_version):
                        raise AIUsageConflictError("Budget settings changed in another session. Refresh and try again.")
                    await cur.execute(
                        """
                        UPDATE ai_budget_settings
                        SET monthly_limit_micros = %s, daily_limit_micros = %s, warning_thresholds = %s,
                            enforcement_enabled = %s, override_until = %s, version = version + 1,
                            updated_by_staff_id = %s, updated_by_staff_name = %s, updated_at = UTC_TIMESTAMP(6)
                        WHERE id = 1
                        """,
                        (values["monthly_limit_micros"], values["daily_limit_micros"], values["warning_thresholds"],
                         1 if values["enforcement_enabled"] else 0, values["override_until"],
                         int(actor.id), actor.name),
                    )
                    old = {k: current.get(k) for k in values}
                    await cur.execute(
                        """
                        INSERT INTO ai_control_settings_audit (
                            settings_id, action, actor_staff_id, actor_staff_name,
                            old_configuration_json, new_configuration_json, created_at
                        ) VALUES (1, 'AI_BUDGET_UPDATED', %s, %s, %s, %s, UTC_TIMESTAMP(6))
                        """,
                        (int(actor.id), actor.name, json.dumps(old, default=str), json.dumps(values, default=str)),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def audit(self, action: str, details: dict[str, Any]) -> None:
        await self._execute(
            """
            INSERT INTO ai_control_settings_audit (
                settings_id, action, actor_staff_id, actor_staff_name,
                old_configuration_json, new_configuration_json, created_at
            ) VALUES (1, %s, 0, 'System', '{}', %s, UTC_TIMESTAMP(6))
            """,
            (action, json.dumps(details, default=str)),
        )


class BudgetCounters:
    """Global Redis enforcement counters (micro-USD), keyed by budget period."""

    _ENSURE_SCRIPT = """
    -- KEYS[1] period hash. ARGV: settled, pending, ttl.
    if redis.call('EXISTS', KEYS[1]) == 0 then
      redis.call('HSET', KEYS[1], 'spent', ARGV[1], 'reserved', ARGV[2])
      redis.call('EXPIRE', KEYS[1], ARGV[3])
      return 1
    end
    -- Never lower: raise to the database total if Redis is behind.
    local total = tonumber(redis.call('HGET', KEYS[1], 'spent') or '0') + tonumber(redis.call('HGET', KEYS[1], 'reserved') or '0')
    local durable = tonumber(ARGV[1]) + tonumber(ARGV[2])
    if durable > total then redis.call('HINCRBY', KEYS[1], 'spent', durable - total) end
    return 0
    """

    _RESERVE_SCRIPT = """
    -- KEYS[1] month, KEYS[2] day. ARGV: estimate, month_limit, day_limit (0 = none), enforce (1/0).
    local estimate = tonumber(ARGV[1])
    local function used(key)
      return tonumber(redis.call('HGET', key, 'spent') or '0') + tonumber(redis.call('HGET', key, 'reserved') or '0')
    end
    if ARGV[4] == '1' then
      if used(KEYS[1]) + estimate > tonumber(ARGV[2]) then return {0, 1} end
      if tonumber(ARGV[3]) > 0 and used(KEYS[2]) + estimate > tonumber(ARGV[3]) then return {0, 2} end
    end
    redis.call('HINCRBY', KEYS[1], 'reserved', estimate)
    redis.call('HINCRBY', KEYS[2], 'reserved', estimate)
    return {1, 0}
    """

    _SETTLE_SCRIPT = """
    -- Release the reservation and add the settled cost. ARGV: estimate, cost.
    for _, key in ipairs(KEYS) do
      redis.call('HINCRBY', key, 'reserved', -tonumber(ARGV[1]))
      redis.call('HINCRBY', key, 'spent', tonumber(ARGV[2]))
      if tonumber(redis.call('HGET', key, 'reserved') or '0') < 0 then redis.call('HSET', key, 'reserved', 0) end
    end
    return 1
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = str(settings.redis_url if redis_url is None else redis_url).strip()
        self._client = None

    def client(self):
        if not self.redis_url:
            raise AIUsageBlocked("AI_BUDGET_UNAVAILABLE", "Budget counters need Redis; AI is paused.")
        if self._client is None:
            import redis.asyncio as redis

            self._client = redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    @staticmethod
    def month_key(month: str) -> str:
        return f"ai:budget:{month}"

    @staticmethod
    def day_key(day: str) -> str:
        return f"ai:budget:{day}"

    async def ensure(self, key: str, settled: int, pending: int) -> None:
        await self.client().eval(self._ENSURE_SCRIPT, 1, key, settled, pending, _COUNTER_TTL_SECONDS)

    async def exists(self, key: str) -> bool:
        return bool(await self.client().exists(key))

    async def reserve(self, month: str, day: str, estimate: int, month_limit: int,
                      day_limit: int | None, enforce: bool) -> int:
        allowed, scope = await self.client().eval(
            self._RESERVE_SCRIPT, 2, self.month_key(month), self.day_key(day),
            estimate, month_limit, day_limit or 0, "1" if enforce else "0",
        )
        return 0 if int(allowed) == 1 else int(scope)

    async def settle(self, month: str, day: str, estimate: int, cost: int) -> None:
        await self.client().eval(self._SETTLE_SCRIPT, 2, self.month_key(month), self.day_key(day), estimate, cost)

    async def used(self, key: str) -> int:
        values = await self.client().hmget(key, "spent", "reserved")
        return sum(int(value or 0) for value in values)

    async def claim_alert(self, month: str, threshold: int) -> bool:
        return bool(await self.client().set(
            f"ai:budget:alert:{month}:{threshold}", "1", nx=True, ex=_COUNTER_TTL_SECONDS,
        ))

    async def block_model(self, model: str) -> None:
        await self.client().sadd(_BLOCKED_MODELS_KEY, model)

    async def unblock_model(self, model: str) -> None:
        await self.client().srem(_BLOCKED_MODELS_KEY, model)

    async def blocked_models(self) -> set[str]:
        return set(await self.client().smembers(_BLOCKED_MODELS_KEY))


def _normalize_settings(row: dict[str, Any]) -> BudgetSettings:
    thresholds = tuple(sorted({
        int(part) for part in str(row.get("warning_thresholds") or "").split(",")
        if part.strip().isdigit() and 0 < int(part) <= 100
    }))
    return BudgetSettings(
        monthly_limit_micros=int(row.get("monthly_limit_micros") or 0),
        daily_limit_micros=int(row["daily_limit_micros"]) if row.get("daily_limit_micros") else None,
        warning_thresholds=thresholds,
        enforcement_enabled=bool(row.get("enforcement_enabled")),
        budget_timezone=str(row.get("budget_timezone") or "Asia/Beirut"),
        override_until=row.get("override_until"),
        version=int(row.get("version") or 1),
        updated_at=row.get("updated_at"),
        updated_by_staff_id=int(row["updated_by_staff_id"]) if row.get("updated_by_staff_id") else None,
        updated_by_staff_name=row.get("updated_by_staff_name"),
    )


def _price_from_row(row: dict[str, Any]) -> ModelPrice:
    return ModelPrice(
        id=int(row["id"]),
        model_prefix=str(row["model_prefix"]),
        input_per_million=Decimal(str(row["input_per_million"])),
        cached_input_per_million=Decimal(str(row["cached_input_per_million"])),
        output_per_million=Decimal(str(row["output_per_million"])),
        audio_per_minute=Decimal(str(row["audio_per_minute"])) if row.get("audio_per_minute") is not None else None,
        batch_factor=Decimal(str(row.get("batch_factor") or "0.5")),
    )


def _usage_field(usage: Any, *path: str) -> int:
    value: Any = usage
    for name in path:
        value = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if value is None:
            return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class AIUsageService:
    def __init__(self, repository: AIUsageRepository | None = None, counters: BudgetCounters | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.repository = repository or AIUsageRepository()
        self.counters = counters or BudgetCounters()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._settings_cache: tuple[float, BudgetSettings] | None = None
        self._reconciled: dict[str, float] = {}

    # -- settings ----------------------------------------------------------------
    async def budget_settings(self, *, fresh: bool = False) -> BudgetSettings:
        cached = self._settings_cache
        if not fresh and cached and time.monotonic() - cached[0] < _SETTINGS_TTL_SECONDS:
            return cached[1]
        row = await self.repository.get_settings()
        if not row:
            raise AIUsageBlocked("AI_BUDGET_UNAVAILABLE", "AI budget settings are not initialized.")
        value = _normalize_settings(row)
        self._settings_cache = (time.monotonic(), value)
        return value

    async def update_settings(self, agent: SupportAgent, payload: dict[str, Any]) -> dict[str, Any]:
        if not agent.is_supervisor:
            raise AIUsageForbiddenError("Only Super Admins and Admins can change the AI budget.")
        try:
            monthly = Decimal(str(payload["monthly_limit_usd"]))
            daily_raw = payload.get("daily_limit_usd")
            daily = Decimal(str(daily_raw)) if daily_raw not in (None, "") else None
            thresholds = sorted({int(value) for value in payload.get("warning_thresholds") or []})
            version = int(payload["version"])
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise AIUsageValidationError("Enter valid budget amounts.") from exc
        if not (Decimal("0") < monthly <= Decimal("100000")):
            raise AIUsageValidationError("The monthly limit must be more than $0 and at most $100,000.")
        if daily is not None and not (Decimal("0") < daily <= monthly):
            raise AIUsageValidationError("The daily limit must be more than $0 and not above the monthly limit.")
        if not thresholds or any(not 1 <= value <= 100 for value in thresholds) or len(thresholds) > 6:
            raise AIUsageValidationError("Warnings must be 1 to 6 percentages between 1 and 100.")
        override_until = payload.get("override_until")
        if override_until:
            try:
                override_until = datetime.fromisoformat(str(override_until).replace("Z", "+00:00"))
            except ValueError as exc:
                raise AIUsageValidationError("Enter a valid re-enable-until time.") from exc
            if override_until.tzinfo is None:
                override_until = override_until.replace(tzinfo=timezone.utc)
            override_until = override_until.astimezone(timezone.utc).replace(tzinfo=None)
        await self.repository.update_settings(
            expected_version=version,
            values={
                "monthly_limit_micros": usd_to_micros(monthly),
                "daily_limit_micros": usd_to_micros(daily) if daily is not None else None,
                "warning_thresholds": ",".join(str(value) for value in thresholds),
                "enforcement_enabled": bool(payload.get("enforcement_enabled", True)),
                "override_until": override_until or None,
            },
            actor=agent,
        )
        logger.info("AI budget changed: actor_staff_id=%s", agent.id)
        self._settings_cache = None
        return (await self.budget_settings(fresh=True)).to_dict()

    # -- prices --------------------------------------------------------------------
    async def price_for(self, model: str) -> ModelPrice | None:
        model = str(model or "")
        for row in await self.repository.verified_prices():
            if model.startswith(str(row["model_prefix"])):
                return _price_from_row(row)
        return None

    # -- counters ------------------------------------------------------------------
    async def _ensure_counters(self, month: str, day: str) -> None:
        now = time.monotonic()
        for key, totals in (
            (self.counters.month_key(month), lambda: self.repository.period_totals(month=month)),
            (self.counters.day_key(day), lambda: self.repository.period_totals(day=day)),
        ):
            missing = not await self.counters.exists(key)
            if missing or now - self._reconciled.get(key, 0.0) >= _RECONCILE_INTERVAL_SECONDS:
                settled, pending = await totals()
                await self.counters.ensure(key, settled, pending)
                self._reconciled[key] = now

    async def budget_state(self) -> tuple[bool, str | None]:
        """(within budget, reason) for the effective AI availability check."""
        try:
            budget = await self.budget_settings()
            now = self._clock()
            if not budget.enforcement_enabled or budget.override_active(now):
                return True, None
            day, month = periods(now, budget.budget_timezone)
            await self._ensure_counters(month, day)
            if await self.counters.used(self.counters.month_key(month)) >= budget.monthly_limit_micros:
                return False, "BUDGET_EXCEEDED"
            if budget.daily_limit_micros and await self.counters.used(self.counters.day_key(day)) >= budget.daily_limit_micros:
                return False, "BUDGET_EXCEEDED"
            return True, None
        except Exception:
            logger.warning("AI budget state unavailable; AI paused")
            return False, "BUDGET_UNAVAILABLE"

    # -- metered call ----------------------------------------------------------------
    async def metered(
        self,
        *,
        purpose: str,
        tier: str,
        model: str,
        call: Callable[[], Awaitable[Any]],
        input_payload: Any = None,
        max_output_tokens: int = 0,
        images: int = 0,
        audio_seconds: float | None = None,
        modalities: str = "text",
        batch: bool = False,
    ) -> Any:
        price = await self.price_for(model)
        if price is None:
            raise AIUsageBlocked(
                "AI_MODEL_PRICING_UNCONFIGURED",
                f"No verified price is configured for model {model}; AI calls to it are blocked.",
            )
        try:
            blocked = await self.counters.blocked_models()
        except Exception as exc:
            raise AIUsageBlocked("AI_BUDGET_UNAVAILABLE", "Budget counters are unavailable; AI is paused.") from exc
        if model in blocked:
            raise AIUsageBlocked(
                "AI_MODEL_PRICING_UNCONFIGURED",
                f"Model {model} returned an unpriced model ID; blocked until its price is verified.",
            )
        if audio_seconds is not None:
            estimate = audio_cost_micros(price, audio_seconds) + 1
        else:
            estimate = token_cost_micros(
                price, input_tokens=estimate_input_tokens(input_payload, images=images),
                cached_input_tokens=0, output_tokens=max(1, int(max_output_tokens)), batch=batch,
            ) + 1
        budget = await self.budget_settings()
        now = self._clock()
        day, month = periods(now, budget.budget_timezone)
        enforce = budget.enforcement_enabled and not budget.override_active(now)
        try:
            await self._ensure_counters(month, day)
            refused = await self.counters.reserve(
                month, day, estimate, budget.monthly_limit_micros, budget.daily_limit_micros, enforce,
            )
        except AIUsageBlocked:
            raise
        except Exception as exc:
            raise AIUsageBlocked("AI_BUDGET_UNAVAILABLE", "Budget counters are unavailable; AI is paused.") from exc
        if refused:
            raise AIUsageBlocked(
                "AI_BUDGET_EXCEEDED",
                "The monthly AI budget is used up." if refused == 1 else "The daily AI budget is used up.",
            )
        scope = usage_scope.get() or {}
        reservation_id = secrets.token_hex(16)
        try:
            await self.repository.insert_pending({
                "reservation_id": reservation_id, "purpose": purpose[:32], "tier": tier[:16],
                "requested_model": model[:100], "modalities": modalities[:32], "estimate_micros": estimate,
                "price_id": price.id, "phone_number_id": scope.get("phone_number_id"),
                "ticket_id": scope.get("ticket_id"), "batch_id": scope.get("batch_id"),
                "budget_day": day, "budget_month": month,
            })
        except Exception as exc:
            # Nothing durable records this reservation and no call was made: release it.
            await self.counters.settle(month, day, estimate, 0)
            raise AIUsageBlocked("AI_BUDGET_UNAVAILABLE", "Usage could not be recorded; AI is paused.") from exc

        started = time.monotonic()
        try:
            response = await call()
        except BaseException as exc:
            outcome = "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError)) else "failure"
            # No provider usage: keep the full estimate (OpenAI may still bill it).
            await self._settle(reservation_id, month, day, estimate, {
                "duration_ms": int((time.monotonic() - started) * 1000), "cost_micros": estimate,
                "cost_basis": "reserved_estimate", "outcome": outcome,
            })
            raise
        values = await self._actual_values(response, price, estimate, model, audio_seconds, batch)
        values["duration_ms"] = int((time.monotonic() - started) * 1000)
        await self._settle(reservation_id, month, day, estimate, values)
        return response

    async def _actual_values(self, response: Any, price: ModelPrice, estimate: int, model: str,
                             audio_seconds: float | None, batch: bool) -> dict[str, Any]:
        returned_model = str(getattr(response, "model", "") or model)
        request_id = getattr(response, "_request_id", None) or getattr(response, "id", None)
        if audio_seconds is not None:
            return {"model": returned_model, "audio_seconds": round(float(audio_seconds), 2),
                    "cost_micros": audio_cost_micros(price, audio_seconds), "cost_basis": "computed",
                    "outcome": "success", "openai_request_id": str(request_id)[:128] if request_id else None}
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
        tokens = {
            "input_tokens": _usage_field(usage, "input_tokens"),
            "cached_input_tokens": _usage_field(usage, "input_tokens_details", "cached_tokens"),
            "output_tokens": _usage_field(usage, "output_tokens"),
            "reasoning_tokens": _usage_field(usage, "output_tokens_details", "reasoning_tokens"),
        }
        actual_price = await self.price_for(returned_model)
        if usage is None or actual_price is None:
            if actual_price is None:
                # The provider answered with a model ID that has no verified price:
                # charge the full reservation and block the requested model.
                await self.counters.block_model(model)
                logger.error("OpenAI returned an unpriced model; blocking requested model until verified")
            return {"model": returned_model, **tokens, "cost_micros": estimate,
                    "cost_basis": "unpriced" if actual_price is None else "reserved_estimate",
                    "outcome": "success", "openai_request_id": str(request_id)[:128] if request_id else None}
        cost = token_cost_micros(actual_price, input_tokens=tokens["input_tokens"],
                                 cached_input_tokens=tokens["cached_input_tokens"],
                                 output_tokens=tokens["output_tokens"], batch=batch)
        return {"model": returned_model, **tokens, "cost_micros": cost, "cost_basis": "computed",
                "outcome": "success", "openai_request_id": str(request_id)[:128] if request_id else None}

    async def _settle(self, reservation_id: str, month: str, day: str, estimate: int, values: dict[str, Any]) -> None:
        try:
            if await self.repository.settle(reservation_id, values):
                await self.counters.settle(month, day, estimate, int(values["cost_micros"]))
                await self._check_thresholds(month)
        except Exception:
            # The pending row (full estimate) and the Redis reservation both remain,
            # so spending is still fully counted.
            logger.warning("AI usage settlement failed; the full estimate stays counted")

    async def _check_thresholds(self, month: str) -> None:
        budget = await self.budget_settings()
        if not budget.monthly_limit_micros:
            return
        used = await self.counters.used(self.counters.month_key(month))
        percent = used * 100 / budget.monthly_limit_micros
        for threshold in budget.warning_thresholds:
            if percent >= threshold and await self.counters.claim_alert(month, threshold):
                await self.repository.audit("AI_BUDGET_THRESHOLD", {"month": month, "threshold": threshold})
                logger.warning("AI budget threshold reached: month=%s threshold=%s", month, threshold)
                try:
                    from app.support_events import publish_support_event

                    await publish_support_event("ai_budget_threshold", threshold=threshold, month=month)
                except Exception:
                    logger.warning("AI budget threshold event could not be published")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


ai_usage_service = AIUsageService()
