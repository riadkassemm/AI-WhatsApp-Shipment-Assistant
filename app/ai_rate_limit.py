from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable

from app.config import settings


logger = logging.getLogger("shipment-bot")


@dataclass(frozen=True)
class AIQuotaDecision:
    allowed: bool
    scope: str | None = None


class AIUsageRateLimiter:
    """Rolling AI-turn quotas with Redis coordination and an in-memory fallback.

    Quotas are intentionally keyed by the customer's WhatsApp sender number rather
    than the receiving business line. A customer therefore cannot bypass the AI budget
    by messaging another support number owned by the same deployment.
    """

    _CONSUME_SCRIPT = """
    local now = tonumber(ARGV[1])
    local minute_ms = tonumber(ARGV[2])
    local hour_ms = tonumber(ARGV[3])
    local day_ms = tonumber(ARGV[4])
    local minute_limit = tonumber(ARGV[5])
    local hour_limit = tonumber(ARGV[6])
    local day_limit = tonumber(ARGV[7])
    local global_day_limit = tonumber(ARGV[8])
    local member = ARGV[9]

    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - day_ms)
    if global_day_limit > 0 then
        redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - day_ms)
    end

    if minute_limit > 0 and redis.call('ZCOUNT', KEYS[1], now - minute_ms, '+inf') >= minute_limit then
        return {0, 1}
    end
    if hour_limit > 0 and redis.call('ZCOUNT', KEYS[1], now - hour_ms, '+inf') >= hour_limit then
        return {0, 2}
    end
    if day_limit > 0 and redis.call('ZCARD', KEYS[1]) >= day_limit then
        return {0, 3}
    end
    if global_day_limit > 0 and redis.call('ZCARD', KEYS[2]) >= global_day_limit then
        return {0, 4}
    end

    redis.call('ZADD', KEYS[1], now, member)
    redis.call('PEXPIRE', KEYS[1], day_ms + 60000)
    if global_day_limit > 0 then
        redis.call('ZADD', KEYS[2], now, member)
        redis.call('PEXPIRE', KEYS[2], day_ms + 60000)
    end
    return {1, 0}
    """

    _SCOPES = {
        1: "customer_minute",
        2: "customer_hour",
        3: "customer_day",
        4: "global_day",
    }

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        per_minute: int | None = None,
        per_hour: int | None = None,
        per_day: int | None = None,
        global_per_day: int | None = None,
        notice_cooldown_seconds: int | None = None,
        fail_closed: bool | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.redis_url = str(settings.redis_url if redis_url is None else redis_url).strip()
        self.per_minute = max(
            0,
            int(
                settings.ai_rate_limit_turns_per_minute
                if per_minute is None
                else per_minute
            ),
        )
        self.per_hour = max(
            0,
            int(settings.ai_rate_limit_turns_per_hour if per_hour is None else per_hour),
        )
        self.per_day = max(
            0,
            int(settings.ai_rate_limit_turns_per_day if per_day is None else per_day),
        )
        self.global_per_day = max(
            0,
            int(
                settings.ai_rate_limit_global_turns_per_day
                if global_per_day is None
                else global_per_day
            ),
        )
        self.notice_cooldown_seconds = max(
            1,
            int(
                settings.ai_rate_limit_notice_cooldown_seconds
                if notice_cooldown_seconds is None
                else notice_cooldown_seconds
            ),
        )
        self.fail_closed = bool(
            settings.ai_rate_limit_fail_closed if fail_closed is None else fail_closed
        )
        self._clock = clock
        self._redis_client = None
        self._lock = asyncio.Lock()
        self._customer_events: dict[str, deque[float]] = defaultdict(deque)
        self._global_events: deque[float] = deque()
        self._notice_until: dict[str, float] = {}

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    def _customer_key(self, customer_key: str) -> str:
        return f"security:ai-turns:customer:{self._digest(customer_key)}"

    @staticmethod
    def _global_key() -> str:
        return "security:ai-turns:global"

    def _notice_key(self, customer_key: str) -> str:
        return f"security:ai-turns:notice:{self._digest(customer_key)}"

    def _get_redis(self):
        if not self.redis_url:
            return None
        if self._redis_client is None:
            import redis.asyncio as redis

            self._redis_client = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis_client

    def _enabled(self) -> bool:
        return any(
            limit > 0
            for limit in (
                self.per_minute,
                self.per_hour,
                self.per_day,
                self.global_per_day,
            )
        )

    async def consume(self, customer_key: str) -> AIQuotaDecision:
        if not bool(settings.ai_rate_limit_enabled) or not self._enabled():
            return AIQuotaDecision(True)

        redis_client = self._get_redis()
        if redis_client is None:
            return await self._consume_memory(customer_key)

        now_ms = int(self._clock() * 1000)
        member = f"{now_ms}:{secrets.token_urlsafe(12)}"
        try:
            result = await redis_client.eval(
                self._CONSUME_SCRIPT,
                2,
                self._customer_key(customer_key),
                self._global_key(),
                str(now_ms),
                str(60 * 1000),
                str(60 * 60 * 1000),
                str(24 * 60 * 60 * 1000),
                str(self.per_minute),
                str(self.per_hour),
                str(self.per_day),
                str(self.global_per_day),
                member,
            )
        except Exception as exc:
            logger.error(
                "AI rate limiter unavailable: error_type=%s fail_closed=%s",
                type(exc).__name__,
                self.fail_closed,
            )
            return AIQuotaDecision(not self.fail_closed, "limiter_unavailable")

        try:
            allowed = bool(int(result[0]))
            code = int(result[1])
        except (TypeError, ValueError, IndexError):
            logger.error("AI rate limiter returned an invalid Redis response")
            return AIQuotaDecision(not self.fail_closed, "limiter_unavailable")
        return AIQuotaDecision(allowed, None if allowed else self._SCOPES.get(code, "unknown"))

    async def _consume_memory(self, customer_key: str) -> AIQuotaDecision:
        now = self._clock()
        async with self._lock:
            customer = self._customer_events[customer_key]
            while customer and customer[0] <= now - 24 * 60 * 60:
                customer.popleft()
            while self._global_events and self._global_events[0] <= now - 24 * 60 * 60:
                self._global_events.popleft()

            if self.per_minute > 0:
                count = sum(1 for item in customer if item > now - 60)
                if count >= self.per_minute:
                    return AIQuotaDecision(False, "customer_minute")
            if self.per_hour > 0:
                count = sum(1 for item in customer if item > now - 60 * 60)
                if count >= self.per_hour:
                    return AIQuotaDecision(False, "customer_hour")
            if self.per_day > 0 and len(customer) >= self.per_day:
                return AIQuotaDecision(False, "customer_day")
            if self.global_per_day > 0 and len(self._global_events) >= self.global_per_day:
                return AIQuotaDecision(False, "global_day")

            customer.append(now)
            if self.global_per_day > 0:
                self._global_events.append(now)
            return AIQuotaDecision(True)

    async def claim_notice(self, customer_key: str) -> bool:
        """Allow at most one customer-facing quota notice per cooldown period."""
        redis_client = self._get_redis()
        if redis_client is not None:
            try:
                claimed = await redis_client.set(
                    self._notice_key(customer_key),
                    "1",
                    nx=True,
                    ex=self.notice_cooldown_seconds,
                )
                return bool(claimed)
            except Exception as exc:
                logger.warning(
                    "AI rate-limit notice guard unavailable: error_type=%s",
                    type(exc).__name__,
                )
                # A failed notice guard must not disable the main quota. Prefer one
                # extra static notice over accidentally allowing an OpenAI request.
                return True

        now = self._clock()
        async with self._lock:
            until = self._notice_until.get(customer_key, 0.0)
            if until > now:
                return False
            self._notice_until[customer_key] = now + self.notice_cooldown_seconds
            return True


ai_rate_limiter = AIUsageRateLimiter()
