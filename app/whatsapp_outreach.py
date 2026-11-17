"""Outreach guard: limits on chats that staff start on a WhatsApp number.

WhatsApp restricts numbers that write first to many people, especially with
the same text and from a new number (Tripoli, 2026-11-17). Every staff-started
chat (Start chat, Forward to a new contact) reserves a slot here BEFORE the
number is looked up on WhatsApp, so a refused attempt never reaches WhatsApp.

Two limits apply per number and per local day (WHATSAPP_OUTREACH_TIMEZONE):
- distinct customers contacted (lower while the number is new);
- distinct customers who received the same text.
Contacting the same customer again that day uses no extra slot. There is no
per-message override; Admins change the limits per number instead. When the
counter store is unavailable the guard fails closed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import settings


logger = logging.getLogger("shipment-bot")

LIMIT_FIELDS = ("daily_limit", "new_number_daily_limit", "same_text_limit")
LIMIT_COLUMNS = {
    "daily_limit": "outreach_daily_limit",
    "new_number_daily_limit": "outreach_new_number_daily_limit",
    "same_text_limit": "outreach_same_text_limit",
}
MAX_LIMIT = 500
_KEY_TTL_SECONDS = 2 * 24 * 60 * 60


class OutreachLimitError(Exception):
    """A staff-started chat would exceed this number's limits."""


class OutreachUnavailableError(Exception):
    """The counters cannot be read, so new chats are refused (fail closed)."""


def default_limits() -> dict[str, int]:
    return {
        "daily_limit": int(settings.whatsapp_outreach_daily_limit),
        "new_number_daily_limit": int(settings.whatsapp_outreach_new_number_daily_limit),
        "same_text_limit": int(settings.whatsapp_outreach_same_text_limit),
    }


@dataclass(frozen=True)
class OutreachLimits:
    daily_limit: int
    new_number_daily_limit: int
    same_text_limit: int
    new_number_until: datetime | None = None
    new_number: bool = False
    custom: dict[str, int | None] = field(default_factory=dict)

    @property
    def limit_today(self) -> int:
        return self.new_number_daily_limit if self.new_number else self.daily_limit


def _as_utc(value: datetime) -> datetime:
    # DATETIME columns come back naive in the database host's local time.
    return value.astimezone(timezone.utc) if value.tzinfo else value.astimezone().astimezone(timezone.utc)


def resolve_limits(row: dict[str, Any] | None, *, now: datetime | None = None) -> OutreachLimits:
    """Effective limits for one whatsapp_phone_numbers row (NULL = default)."""
    row = row or {}
    defaults = default_limits()
    custom: dict[str, int | None] = {}
    values: dict[str, int] = {}
    for name in LIMIT_FIELDS:
        raw = row.get(LIMIT_COLUMNS[name])
        custom[name] = None if raw is None else max(0, min(MAX_LIMIT, int(raw)))
        values[name] = defaults[name] if custom[name] is None else int(custom[name])
    now = now or datetime.now(timezone.utc)
    until = None
    created = row.get("created_at")
    days = int(settings.whatsapp_outreach_new_number_days)
    if isinstance(created, datetime) and days > 0:
        until = _as_utc(created) + timedelta(days=days)
    return OutreachLimits(
        **values,
        new_number_until=until,
        new_number=bool(until and now < until),
        custom=custom,
    )


def text_fingerprint(body: str) -> str:
    """Same text, ignoring case and spacing."""
    normalized = re.sub(r"\s+", " ", str(body or "")).strip().casefold()
    return "text:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


class WhatsAppOutreachGuard:
    # KEYS[1] = customers contacted today, KEYS[2] = customers sent this text
    # today. A customer already in a set uses no new slot in it.
    _RESERVE_SCRIPT = """
    local recipient = ARGV[1]
    local daily = tonumber(ARGV[2])
    local same = tonumber(ARGV[3])
    local ttl = tonumber(ARGV[4])
    local used = redis.call('SCARD', KEYS[1])
    if redis.call('SISMEMBER', KEYS[1], recipient) == 0 and used >= daily then
        return {0, 1, used}
    end
    local same_used = redis.call('SCARD', KEYS[2])
    if redis.call('SISMEMBER', KEYS[2], recipient) == 0 and same_used >= same then
        return {0, 2, same_used}
    end
    redis.call('SADD', KEYS[1], recipient)
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('SADD', KEYS[2], recipient)
    redis.call('EXPIRE', KEYS[2], ttl)
    return {1, 0, redis.call('SCARD', KEYS[1])}
    """

    def __init__(
        self,
        *,
        redis_url: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.redis_url = str(settings.redis_url if redis_url is None else redis_url).strip()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._redis_client = None

    def _get_redis(self):
        if self._redis_client is None:
            if not self.redis_url:
                raise OutreachUnavailableError("No counter store is configured.")
            import redis.asyncio as redis

            self._redis_client = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis_client

    def local_day(self) -> str:
        try:
            zone = ZoneInfo(str(settings.whatsapp_outreach_timezone or "UTC"))
        except Exception:
            zone = timezone.utc
        return self._clock().astimezone(zone).date().isoformat()

    def _day_key(self, line_id: str, day: str) -> str:
        return f"whatsapp:outreach:{line_id}:{day}:customers"

    def _text_key(self, line_id: str, day: str, fingerprint: str) -> str:
        return f"whatsapp:outreach:{line_id}:{day}:text:{_digest(fingerprint)}"

    async def reserve(
        self,
        *,
        line_id: str,
        recipient_phone: str,
        fingerprint: str,
        limits: OutreachLimits,
    ) -> int:
        """Reserve a slot for one staff-started chat; return today's count."""
        line_id = str(line_id)
        day = self.local_day()
        try:
            result = await self._get_redis().eval(
                self._RESERVE_SCRIPT,
                2,
                self._day_key(line_id, day),
                self._text_key(line_id, day, fingerprint),
                _digest(recipient_phone),
                str(limits.limit_today),
                str(limits.same_text_limit),
                str(_KEY_TTL_SECONDS),
            )
            allowed, reason, count = (int(item) for item in result)
        except OutreachUnavailableError:
            raise
        except Exception as exc:
            logger.error("WhatsApp outreach guard unavailable: line_id=%s error_type=%s", line_id, type(exc).__name__)
            raise OutreachUnavailableError(
                "New chats are paused because the chat limit counter is unavailable. Try again in a minute."
            ) from exc
        if allowed:
            return count
        logger.warning(
            "WhatsApp outreach refused: line_id=%s reason=%s count=%s limit=%s",
            line_id,
            "daily_limit" if reason == 1 else "same_text_limit",
            count,
            limits.limit_today if reason == 1 else limits.same_text_limit,
        )
        raise OutreachLimitError(self._refusal(reason, limits))

    @staticmethod
    def _refusal(reason: int, limits: OutreachLimits) -> str:
        if reason == 1:
            limit = limits.limit_today
            if limit == 0:
                return "Starting new chats is turned off for this WhatsApp number. Customers who write in can still be answered."
            if limits.new_number and limits.new_number_until:
                return (
                    f"This WhatsApp number is new and can start {limit} new chats a day until "
                    f"{limits.new_number_until.date().isoformat()}. Today's limit is reached. "
                    "Customers who write in can still be answered."
                )
            return (
                f"This WhatsApp number has started its {limit} new chats for today. "
                "Customers who write in can still be answered; new chats can start again tomorrow."
            )
        return (
            f"This message was already sent to {limits.same_text_limit} customers today from this number. "
            "Sending the same text to many people gets WhatsApp numbers banned. Ask IT about announcements."
        )

    async def used_today(self, line_ids: list[str]) -> dict[str, int | None]:
        """Customers contacted today per number; None when unknown."""
        if not line_ids:
            return {}
        day = self.local_day()
        try:
            client = self._get_redis()
            counts = [await client.scard(self._day_key(str(line_id), day)) for line_id in line_ids]
        except Exception as exc:
            logger.warning("WhatsApp outreach usage unavailable: error_type=%s", type(exc).__name__)
            return {str(line_id): None for line_id in line_ids}
        return {str(line_id): int(count or 0) for line_id, count in zip(line_ids, counts)}


whatsapp_outreach_guard = WhatsAppOutreachGuard()
