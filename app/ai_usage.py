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
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import aiomysql

from app.config import settings
from app.security import safe_log_identifier
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
                phone_number_id, ticket_id, batch_id, budget_day, budget_month, agent_turn_id
            ) VALUES (%s, UTC_TIMESTAMP(6), %s, %s, %s, %s, %s, %s, %s, 'reserved_estimate', 'pending',
                      %s, %s, %s, %s, %s, %s)
            """,
            (row["reservation_id"], row["purpose"], row["tier"], row["requested_model"], row["modalities"],
             row["estimate_micros"], row["estimate_micros"], row["price_id"],
             row["phone_number_id"], row["ticket_id"], row["batch_id"], row["budget_day"], row["budget_month"], row.get("agent_turn_id")),
        )

    async def settle(self, reservation_id: str, values: dict[str, Any]) -> bool:
        """Settle a pending row once; returns False if it was already settled."""
        changed = await self._execute(
            """
            UPDATE ai_usage_events
            SET settled_at = UTC_TIMESTAMP(6), duration_ms = %s, model = %s,
                input_tokens = %s, cached_input_tokens = %s, output_tokens = %s, reasoning_tokens = %s,
                audio_seconds = %s, cost_micros = %s, cost_basis = %s, outcome = %s, openai_request_id = %s,
                price_id = COALESCE(%s, price_id)
            WHERE reservation_id = %s AND outcome = 'pending'
            """,
            (values.get("duration_ms"), values.get("model"), values.get("input_tokens"),
             values.get("cached_input_tokens"), values.get("output_tokens"), values.get("reasoning_tokens"),
             values.get("audio_seconds"), int(values["cost_micros"]), values["cost_basis"], values["outcome"],
             values.get("openai_request_id"), values.get("price_id"), reservation_id),
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

    @staticmethod
    def _filters(start: str, end: str, line: str | None, model: str | None) -> tuple[str, tuple]:
        clauses, args = ["budget_day BETWEEN %s AND %s"], [start, end]
        if line:
            clauses.append("phone_number_id = %s")
            args.append(line)
        if model:
            clauses.append("COALESCE(model, requested_model) = %s")
            args.append(model)
        return " AND ".join(clauses), tuple(args)

    async def usage_by_day(self, start: str, end: str, line: str | None, model: str | None) -> list[dict[str, Any]]:
        where, args = self._filters(start, end, line, model)
        return await self._fetch(
            f"""
            SELECT budget_day AS day, COUNT(*) AS requests, COALESCE(SUM(cost_micros), 0) AS cost_micros,
                   COALESCE(SUM(CASE WHEN outcome = 'pending' THEN cost_micros END), 0) AS pending_micros
            FROM ai_usage_events WHERE {where}
            GROUP BY budget_day ORDER BY budget_day
            """, args,
        )

    async def usage_by(self, column: str, start: str, end: str, line: str | None, model: str | None) -> list[dict[str, Any]]:
        expression = {"model": "COALESCE(model, requested_model)", "purpose": "purpose", "tier": "tier"}[column]
        where, args = self._filters(start, end, line, model)
        return await self._fetch(
            f"""
            SELECT {expression} AS name, COUNT(*) AS requests,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                   COALESCE(SUM(cost_micros), 0) AS cost_micros,
                   SUM(outcome = 'success') AS successes, SUM(outcome IN ('failure', 'timeout')) AS failures
            FROM ai_usage_events WHERE {where}
            GROUP BY {expression} ORDER BY cost_micros DESC
            """, args,
        )

    async def ticket_cost(self, start: str, end: str, line: str | None, model: str | None) -> dict[str, Any]:
        where, args = self._filters(start, end, line, model)
        return await self._fetch(
            f"""
            SELECT COUNT(DISTINCT ticket_id) AS tickets,
                   COALESCE(SUM(CASE WHEN ticket_id IS NOT NULL THEN cost_micros END), 0) AS cost_micros
            FROM ai_usage_events WHERE {where}
              AND purpose NOT IN ('kb_build', 'eval', 'test')
            """, args, one=True,
        ) or {}

    async def ticket_usage(self, ticket_id: int) -> dict[str, Any]:
        return await self._fetch(
            """
            SELECT COUNT(*) AS requests, COALESCE(SUM(cost_micros), 0) AS cost_micros,
                   COALESCE(SUM(outcome = 'pending'), 0) AS pending
            FROM ai_usage_events WHERE ticket_id = %s
            """, (ticket_id,), one=True,
        ) or {}

    async def ai_ticket_outcomes(self, start_utc: datetime, end_utc: datetime, line: str | None) -> dict[str, Any]:
        """Outcomes of tickets opened as AI tickets in [start_utc, end_utc)."""
        line_clause, args = ("AND t.business_phone_number_id = %s", (line,)) if line else ("", ())
        return await self._fetch(
            f"""
            SELECT COUNT(*) AS tickets,
                   COALESCE(SUM(EXISTS (SELECT 1 FROM support_ticket_events e WHERE e.ticket_id = t.id
                        AND e.event_type = 'TICKET_RESOLVED' AND e.actor_type = 'AI')), 0) AS resolved_by_ai,
                   COALESCE(SUM(EXISTS (SELECT 1 FROM support_ticket_events e WHERE e.ticket_id = t.id
                        AND e.event_type IN ('AI_ESCALATED', 'AI_TAKEN_OVER'))), 0) AS handed_to_staff,
                   COALESCE(SUM(t.handling_mode = 'AI' AND t.active_key IS NOT NULL), 0) AS still_open
            FROM support_tickets t
            WHERE t.created_at >= %s AND t.created_at < %s {line_clause}
              AND EXISTS (SELECT 1 FROM support_ticket_events c WHERE c.ticket_id = t.id
                    AND c.event_type = 'TICKET_CREATED'
                    AND JSON_VALID(c.details_json)
                    AND JSON_UNQUOTE(JSON_EXTRACT(c.details_json, '$.handling_mode')) = 'AI')
            """,
            (start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None), *args), one=True,
        ) or {}

    async def list_prices(self) -> list[dict[str, Any]]:
        return await self._fetch(
            """
            SELECT id, model_prefix, input_per_million, cached_input_per_million, output_per_million,
                   audio_per_minute, batch_factor, effective_from, source, verified_by_staff_id,
                   verified_by_staff_name, verified_at, retired_at
            FROM ai_model_prices ORDER BY retired_at IS NOT NULL, model_prefix, effective_from DESC, id DESC
            """
        )

    async def insert_price(self, values: dict[str, Any], *, actor: SupportAgent) -> int:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO ai_model_prices (model_prefix, input_per_million, cached_input_per_million,
                            output_per_million, audio_per_minute, batch_factor, effective_from, source)
                        VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6), %s)
                        """,
                        (values["model_prefix"], values["input_per_million"], values["cached_input_per_million"],
                         values["output_per_million"], values["audio_per_minute"], values["batch_factor"],
                         values["source"]),
                    )
                    price_id = int(cur.lastrowid)
                    await self._price_audit(cur, "AI_PRICE_ADDED", actor, {"id": price_id, **values})
                await conn.commit()
                return price_id
            except Exception:
                await conn.rollback()
                raise

    async def mark_price(self, price_id: int, *, verify: bool, actor: SupportAgent) -> dict[str, Any]:
        # Prices are immutable revisions: amounts are never edited in place.
        # Lock state transitions so verification cannot race retirement.
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT * FROM ai_model_prices WHERE id = %s FOR UPDATE", (int(price_id),))
                    row = await cur.fetchone()
                    if not row or row.get("retired_at"):
                        raise AIUsageConflictError("This price is no longer available. Refresh and try again.")
                    if verify and row.get("verified_at"):
                        raise AIUsageConflictError("This price is already verified. Refresh and try again.")
                    if verify:
                        await cur.execute(
                            "UPDATE ai_model_prices SET verified_at = UTC_TIMESTAMP(6), "
                            "verified_by_staff_id = %s, verified_by_staff_name = %s WHERE id = %s",
                            (int(actor.id), actor.name, int(price_id)),
                        )
                    else:
                        await cur.execute("UPDATE ai_model_prices SET retired_at = UTC_TIMESTAMP(6) WHERE id = %s",
                                          (int(price_id),))
                    await self._price_audit(cur, "AI_PRICE_VERIFIED" if verify else "AI_PRICE_RETIRED", actor,
                                            {"id": int(price_id), "model_prefix": row["model_prefix"]})
                await conn.commit()
                return row
            except Exception:
                await conn.rollback()
                raise

    @staticmethod
    async def _price_audit(cur: Any, action: str, actor: SupportAgent, details: dict[str, Any]) -> None:
        await cur.execute(
            """
            INSERT INTO ai_control_settings_audit (
                settings_id, action, actor_staff_id, actor_staff_name,
                old_configuration_json, new_configuration_json, created_at
            ) VALUES (1, %s, %s, %s, '{}', %s, UTC_TIMESTAMP(6))
            """,
            (action, int(actor.id), actor.name, json.dumps(details, default=str)),
        )

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
        if not monthly.is_finite() or not (Decimal("0") < monthly <= Decimal("100000")):
            raise AIUsageValidationError("The monthly limit must be more than $0 and at most $100,000.")
        if daily is not None and (not daily.is_finite() or not (Decimal("0") < daily <= monthly)):
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
        logger.info("AI budget changed: actor=%s", safe_log_identifier(agent.id, "staff"))
        self._settings_cache = None
        return (await self.budget_settings(fresh=True)).to_dict()

    # -- dashboard -------------------------------------------------------------------
    @staticmethod
    def _require_manager(agent: SupportAgent) -> None:
        if not agent.is_supervisor:
            raise AIUsageForbiddenError("Only Super Admins and Admins can view AI costs or change AI spending.")

    async def usage_report(
        self, agent: SupportAgent, *, start: str | None = None, end: str | None = None,
        line: str | None = None, model: str | None = None,
    ) -> dict[str, Any]:
        """Usage & Costs dashboard data. Amounts are application estimates."""
        self._require_manager(agent)
        budget = await self.budget_settings(fresh=True)
        now = self._clock()
        today, month = periods(now, budget.budget_timezone)
        start = start or f"{month}-01"
        end = end or today
        try:
            first = datetime.strptime(start, "%Y-%m-%d").date()
            last = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError as exc:
            raise AIUsageValidationError("Dates must be YYYY-MM-DD.") from exc
        if last < first or (last - first).days > 366:
            raise AIUsageValidationError("Choose a date range of at most one year, ending after it starts.")
        start, end = first.isoformat(), last.isoformat()
        line = (str(line).strip()[:64] or None) if line else None
        model = (str(model).strip()[:100] or None) if model else None

        today_settled, today_pending = await self.repository.period_totals(day=today)
        month_settled, month_pending = await self.repository.period_totals(month=month)
        month_cost = month_settled + month_pending
        state_ok, state_reason = await self.budget_state()
        days = await self.repository.usage_by_day(start, end, line, model)
        day_rows = {str(row["day"]): row for row in days}
        days = [day_rows.get((first + timedelta(days=offset)).isoformat(), {
            "day": (first + timedelta(days=offset)).isoformat(), "requests": 0,
            "cost_micros": 0, "pending_micros": 0,
        }) for offset in range((last - first).days + 1)]
        by_model = await self.repository.usage_by("model", start, end, line, model)
        by_purpose = await self.repository.usage_by("purpose", start, end, line, model)
        by_tier = await self.repository.usage_by("tier", start, end, line, model)
        tickets = await self.repository.ticket_cost(start, end, line, model)
        zone = ZoneInfo(budget.budget_timezone)
        outcomes = await self.repository.ai_ticket_outcomes(
            datetime.combine(first, datetime.min.time(), zone).astimezone(timezone.utc),
            datetime.combine(last + timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc),
            line,
        )
        opened = int(outcomes.get("tickets") or 0)
        offline_purposes = {"kb_build", "eval"}
        range_cost = sum(int(row["cost_micros"]) for row in days)
        offline_cost = sum(int(row["cost_micros"]) for row in by_purpose if row["name"] in offline_purposes)
        ticket_count = int(tickets.get("tickets") or 0)

        def usage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{
                "name": row["name"], "requests": int(row["requests"] or 0),
                "input_tokens": int(row["input_tokens"] or 0), "cached_input_tokens": int(row["cached_input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0), "reasoning_tokens": int(row["reasoning_tokens"] or 0),
                "cost_usd": micros_to_usd(row["cost_micros"]), "failures": int(row["failures"] or 0),
            } for row in rows]

        return {
            "estimated": True,
            "note": "Estimated by the application from API usage. The OpenAI invoice is final and may differ.",
            "filters": {"from": start, "to": end, "line": line, "model": model},
            "budget": budget.to_dict(),
            "status": {"within_budget": state_ok, "reason": state_reason},
            "summary": {
                "today_cost_usd": micros_to_usd(today_settled + today_pending),
                "month_cost_usd": micros_to_usd(month_cost),
                "monthly_budget_usd": micros_to_usd(budget.monthly_limit_micros),
                "budget_remaining_usd": micros_to_usd(max(0, budget.monthly_limit_micros - month_cost)),
                "budget_used_percent": round(month_cost * 100 / budget.monthly_limit_micros, 1)
                if budget.monthly_limit_micros else None,
                "range_cost_usd": micros_to_usd(range_cost),
                "offline_cost_usd": micros_to_usd(offline_cost),
                "api_requests": sum(int(row["requests"] or 0) for row in days),
                "ai_tickets_handled": ticket_count or None,
                "average_cost_per_ticket_usd": (
                    micros_to_usd(int(tickets.get("cost_micros") or 0) // ticket_count) if ticket_count else None
                ),
                # Of the AI tickets opened in the period (not filtered by model):
                # handed to staff (escalated or taken over) and resolved by the AI.
                "ai_tickets_opened": opened,
                "ai_tickets_open": int(outcomes.get("still_open") or 0),
                "escalation_rate": (
                    round(int(outcomes.get("handed_to_staff") or 0) * 100 / opened, 1) if opened else None
                ),
                "resolution_rate": (
                    round(int(outcomes.get("resolved_by_ai") or 0) * 100 / opened, 1) if opened else None
                ),
            },
            "daily": [{"day": str(row["day"]), "requests": int(row["requests"] or 0),
                       "cost_usd": micros_to_usd(row["cost_micros"]),
                       "pending_usd": micros_to_usd(row["pending_micros"])} for row in days],
            "by_model": usage_rows(by_model),
            "by_purpose": usage_rows(by_purpose),
            "by_tier": usage_rows(by_tier),
        }

    async def ticket_usage(self, agent: SupportAgent, ticket_id: int) -> dict[str, Any] | None:
        """Estimated AI cost of one ticket, for Admins and Super Admins only."""
        if not agent.is_supervisor:
            return None
        row = await self.repository.ticket_usage(ticket_id)
        requests = int(row.get("requests") or 0)
        return {
            "estimated": True,
            "requests": requests,
            "pending": int(row.get("pending") or 0),
            "cost_usd": micros_to_usd(row.get("cost_micros") or 0),
        }

    async def price_list(self, agent: SupportAgent) -> dict[str, Any]:
        self._require_manager(agent)
        rows = await self.repository.list_prices()
        try:
            blocked = sorted(await self.counters.blocked_models())
        except Exception:
            blocked = []

        def money(value: Any) -> str | None:
            return None if value is None else format(Decimal(str(value)).normalize(), "f")

        return {
            "prices": [{
                "id": int(row["id"]), "model_prefix": row["model_prefix"],
                "input_per_million": money(row["input_per_million"]),
                "cached_input_per_million": money(row["cached_input_per_million"]),
                "output_per_million": money(row["output_per_million"]),
                "audio_per_minute": money(row.get("audio_per_minute")),
                "batch_factor": money(row.get("batch_factor")),
                "effective_from": _iso(row.get("effective_from")), "source": row.get("source"),
                "verified": row.get("verified_at") is not None, "verified_at": _iso(row.get("verified_at")),
                "verified_by": row.get("verified_by_staff_name"),
                "retired": row.get("retired_at") is not None,
            } for row in rows],
            "blocked_models": blocked,
        }

    async def add_price(self, agent: SupportAgent, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_manager(agent)
        prefix = str(payload.get("model_prefix") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,99}", prefix):
            raise AIUsageValidationError("Enter the OpenAI model name, for example gpt-6-luna.")

        def amount(name: str, *, optional: bool = False) -> Decimal | None:
            raw = payload.get(name)
            if optional and raw in (None, ""):
                return None
            try:
                value = Decimal(str(raw))
            except (ArithmeticError, ValueError) as exc:
                raise AIUsageValidationError(f"Enter a valid amount for {name.replace('_', ' ')}.") from exc
            if not value.is_finite() or not (Decimal(0) <= value <= Decimal(1000)):
                raise AIUsageValidationError("Prices must be between $0 and $1,000.")
            if value != value.quantize(Decimal("0.000001")):
                raise AIUsageValidationError("Prices support at most six decimal places.")
            return value

        values = {
            "model_prefix": prefix,
            "input_per_million": amount("input_per_million"),
            "cached_input_per_million": amount("cached_input_per_million"),
            "output_per_million": amount("output_per_million"),
            "audio_per_minute": amount("audio_per_minute", optional=True),
            "batch_factor": amount("batch_factor", optional=True),
            "source": str(payload.get("source") or "Entered in the dashboard")[:255],
        }
        factor = values["batch_factor"] if values["batch_factor"] is not None else Decimal("0.5")
        if not 0 < factor <= 1 or factor != factor.quantize(Decimal("0.0001")):
            raise AIUsageValidationError("Batch factor must be above 0 and at most 1, with up to four decimal places.")
        values["batch_factor"] = factor
        await self.repository.insert_price(values, actor=agent)
        logger.info("AI model price added: actor=%s", safe_log_identifier(agent.id, "staff"))
        return await self.price_list(agent)

    async def verify_price(self, agent: SupportAgent, price_id: int) -> dict[str, Any]:
        self._require_manager(agent)
        row = await self.repository.mark_price(price_id, verify=True, actor=agent)
        try:
            for model in await self.counters.blocked_models():
                if model.startswith(str(row["model_prefix"])) and await self.price_for(model) is not None:
                    await self.counters.unblock_model(model)
        except Exception:
            logger.warning("Blocked AI models could not be refreshed after price verification")
        logger.info("AI model price verified: actor=%s", safe_log_identifier(agent.id, "staff"))
        return await self.price_list(agent)

    async def retire_price(self, agent: SupportAgent, price_id: int) -> dict[str, Any]:
        self._require_manager(agent)
        await self.repository.mark_price(price_id, verify=False, actor=agent)
        return await self.price_list(agent)

    async def budget_payload(self, agent: SupportAgent) -> dict[str, Any]:
        self._require_manager(agent)
        return (await self.budget_settings(fresh=True)).to_dict()

    # -- prices --------------------------------------------------------------------
    async def price_for(self, model: str) -> ModelPrice | None:
        return self._matching_price(model, await self.repository.verified_prices())

    @staticmethod
    def _matching_price(model: str, rows: list[dict[str, Any]]) -> ModelPrice | None:
        model = str(model or "")
        for row in rows:
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
        # Price edits affect new requests only. Settlement uses this same
        # verified snapshot, including when the provider returns a dated model.
        price_rows = await self.repository.verified_prices()
        price = self._matching_price(model, price_rows)
        if price is None or (audio_seconds is not None and (price.audio_per_minute is None or price.audio_per_minute <= 0)):
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
                "agent_turn_id": scope.get("agent_turn_id"),
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
        values = await self._actual_values(response, price, estimate, model, audio_seconds, batch, price_rows)
        values["duration_ms"] = int((time.monotonic() - started) * 1000)
        await self._settle(reservation_id, month, day, estimate, values)
        return response

    async def _actual_values(self, response: Any, price: ModelPrice, estimate: int, model: str,
                             audio_seconds: float | None, batch: bool,
                             price_rows: list[dict[str, Any]]) -> dict[str, Any]:
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
        actual_price = self._matching_price(returned_model, price_rows)
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
        return {"model": returned_model, **tokens, "price_id": actual_price.id,
                "cost_micros": cost, "cost_basis": "computed",
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
