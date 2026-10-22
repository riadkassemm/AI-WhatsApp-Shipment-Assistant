from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiomysql

from app.config import settings
from app.support_auth import SupportAgent


logger = logging.getLogger("shipment-bot")

AIControlMode = Literal["MANUAL", "SCHEDULED"]
AIAvailabilityReason = Literal[
    "MANUAL_ON",
    "MANUAL_OFF",
    "WITHIN_SCHEDULE",
    "OUTSIDE_SCHEDULE",
]

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class AIControlError(Exception):
    pass


class AIControlForbiddenError(AIControlError):
    pass


class AIControlValidationError(AIControlError):
    pass


class AIControlConflictError(AIControlError):
    pass


@dataclass(frozen=True)
class AIControlConfiguration:
    control_mode: AIControlMode
    manual_ai_enabled: bool
    schedule_start_time: str
    schedule_end_time: str
    schedule_timezone: str
    version: int
    updated_at: datetime | None = None
    updated_by_staff_id: int | None = None
    updated_by_staff_name: str | None = None


@dataclass(frozen=True)
class EffectiveAIAvailability:
    configuration: AIControlConfiguration
    effective_ai_enabled: bool
    effective_reason: AIAvailabilityReason
    next_transition_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        updated_at = self.configuration.updated_at
        if updated_at is not None:
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            updated_at_value: str | None = updated_at.astimezone(timezone.utc).isoformat()
        else:
            updated_at_value = None

        next_transition = self.next_transition_at
        next_transition_value = (
            next_transition.astimezone(timezone.utc).isoformat()
            if next_transition is not None
            else None
        )

        updated_by = None
        if self.configuration.updated_by_staff_id is not None:
            updated_by = {
                "id": str(self.configuration.updated_by_staff_id),
                "name": self.configuration.updated_by_staff_name,
            }

        return {
            "control_mode": self.configuration.control_mode,
            "manual_ai_enabled": self.configuration.manual_ai_enabled,
            "effective_ai_enabled": self.effective_ai_enabled,
            "effective_reason": self.effective_reason,
            "schedule": {
                "start_time": self.configuration.schedule_start_time,
                "end_time": self.configuration.schedule_end_time,
                "timezone": self.configuration.schedule_timezone,
            },
            "next_transition_at": next_transition_value,
            "version": self.configuration.version,
            "updated_at": updated_at_value,
            "updated_by": updated_by,
        }


def _parse_time_minutes(value: str) -> int:
    rendered = str(value or "").strip()
    if not _TIME_RE.fullmatch(rendered):
        raise AIControlValidationError("Schedule times must use 24-hour HH:mm format.")
    hour, minute = (int(part) for part in rendered.split(":", 1))
    return hour * 60 + minute


def _validate_timezone(value: str) -> str:
    rendered = str(value or "").strip()
    if not rendered or len(rendered) > 128:
        raise AIControlValidationError("A valid IANA timezone is required.")
    try:
        ZoneInfo(rendered)
    except ZoneInfoNotFoundError as exc:
        raise AIControlValidationError("A valid IANA timezone is required.") from exc
    return rendered


def _normalize_configuration(row: dict[str, Any]) -> AIControlConfiguration:
    mode = str(row.get("control_mode") or "").strip().upper()
    if mode not in {"MANUAL", "SCHEDULED"}:
        raise AIControlError("Stored AI control mode is invalid.")

    start = str(row.get("schedule_start_time") or "").strip()
    end = str(row.get("schedule_end_time") or "").strip()
    try:
        start_minutes = _parse_time_minutes(start)
        end_minutes = _parse_time_minutes(end)
        timezone_name = _validate_timezone(str(row.get("schedule_timezone") or ""))
    except AIControlValidationError as exc:
        raise AIControlError("Stored AI schedule configuration is invalid.") from exc
    if start_minutes == end_minutes:
        raise AIControlError("Stored AI schedule start and end times cannot be equal.")

    try:
        version = int(row.get("version") or 0)
    except (TypeError, ValueError) as exc:
        raise AIControlError("Stored AI settings version is invalid.") from exc
    if version < 1:
        raise AIControlError("Stored AI settings version is invalid.")

    updated_staff_id = row.get("updated_by_staff_id")
    if updated_staff_id is not None:
        try:
            updated_staff_id = int(updated_staff_id)
        except (TypeError, ValueError):
            updated_staff_id = None

    return AIControlConfiguration(
        control_mode=cast(AIControlMode, mode),
        manual_ai_enabled=bool(row.get("manual_ai_enabled")),
        schedule_start_time=start,
        schedule_end_time=end,
        schedule_timezone=timezone_name,
        version=version,
        updated_at=row.get("updated_at") if isinstance(row.get("updated_at"), datetime) else None,
        updated_by_staff_id=updated_staff_id,
        updated_by_staff_name=(
            str(row.get("updated_by_staff_name"))
            if row.get("updated_by_staff_name") is not None
            else None
        ),
    )


def _scheduled_enabled(configuration: AIControlConfiguration, now: datetime) -> bool:
    local_now = now.astimezone(ZoneInfo(configuration.schedule_timezone))
    current_minutes = local_now.hour * 60 + local_now.minute
    start_minutes = _parse_time_minutes(configuration.schedule_start_time)
    end_minutes = _parse_time_minutes(configuration.schedule_end_time)

    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    return current_minutes >= start_minutes or current_minutes < end_minutes


def _next_schedule_transition(
    configuration: AIControlConfiguration,
    now: datetime,
    current_enabled: bool,
) -> datetime | None:
    """Find the next real instant where the wall-clock schedule changes state.

    Scanning minute boundaries in UTC keeps DST spring-forward gaps and repeated
    fall-back hours correct without relying on the server's local timezone.
    """
    cursor = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    cursor += timedelta(minutes=1)
    for _ in range((48 * 60) + 5):
        if _scheduled_enabled(configuration, cursor) != current_enabled:
            return cursor
        cursor += timedelta(minutes=1)
    return None


def evaluate_ai_availability(
    configuration: AIControlConfiguration,
    *,
    now: datetime | None = None,
) -> EffectiveAIAvailability:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)

    if configuration.control_mode == "MANUAL":
        enabled = configuration.manual_ai_enabled
        return EffectiveAIAvailability(
            configuration=configuration,
            effective_ai_enabled=enabled,
            effective_reason="MANUAL_ON" if enabled else "MANUAL_OFF",
            next_transition_at=None,
        )

    enabled = _scheduled_enabled(configuration, instant)
    return EffectiveAIAvailability(
        configuration=configuration,
        effective_ai_enabled=enabled,
        effective_reason="WITHIN_SCHEDULE" if enabled else "OUTSIDE_SCHEDULE",
        next_transition_at=_next_schedule_transition(configuration, instant, enabled),
    )


class AIControlRepository:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await aiomysql.create_pool(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            db=settings.db_name,
            minsize=1,
            maxsize=5,
            autocommit=True,
            charset="utf8mb4",
        )

    async def get(self) -> AIControlConfiguration:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT control_mode, manual_ai_enabled, schedule_start_time,
                           schedule_end_time, schedule_timezone, version, updated_at,
                           updated_by_staff_id, updated_by_staff_name
                    FROM ai_control_settings
                    WHERE id = 1
                    LIMIT 1
                    """
                )
                row = await cur.fetchone()
        if not row:
            raise AIControlError(
                "AI control settings are not initialized. Apply the AI settings migration."
            )
        return _normalize_configuration(row)

    async def update(
        self,
        *,
        expected_version: int,
        control_mode: AIControlMode,
        manual_ai_enabled: bool,
        schedule_start_time: str,
        schedule_end_time: str,
        schedule_timezone: str,
        actor: SupportAgent,
    ) -> AIControlConfiguration:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None

        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT control_mode, manual_ai_enabled, schedule_start_time,
                               schedule_end_time, schedule_timezone, version, updated_at,
                               updated_by_staff_id, updated_by_staff_name
                        FROM ai_control_settings
                        WHERE id = 1
                        FOR UPDATE
                        """
                    )
                    current_row = await cur.fetchone()
                    if not current_row:
                        raise AIControlError(
                            "AI control settings are not initialized. Apply the AI settings migration."
                        )
                    current = _normalize_configuration(current_row)
                    if current.version != int(expected_version):
                        raise AIControlConflictError(
                            "AI settings changed in another session. Refresh and try again."
                        )

                    next_version = current.version + 1
                    await cur.execute(
                        """
                        UPDATE ai_control_settings
                        SET control_mode = %s,
                            manual_ai_enabled = %s,
                            schedule_start_time = %s,
                            schedule_end_time = %s,
                            schedule_timezone = %s,
                            version = %s,
                            updated_by_staff_id = %s,
                            updated_by_staff_name = %s,
                            updated_at = UTC_TIMESTAMP(6)
                        WHERE id = 1
                        """,
                        (
                            control_mode,
                            1 if manual_ai_enabled else 0,
                            schedule_start_time,
                            schedule_end_time,
                            schedule_timezone,
                            next_version,
                            int(actor.id),
                            actor.name,
                        ),
                    )

                    old_payload = {
                        "control_mode": current.control_mode,
                        "manual_ai_enabled": current.manual_ai_enabled,
                        "schedule_start_time": current.schedule_start_time,
                        "schedule_end_time": current.schedule_end_time,
                        "schedule_timezone": current.schedule_timezone,
                        "version": current.version,
                    }
                    new_payload = {
                        "control_mode": control_mode,
                        "manual_ai_enabled": manual_ai_enabled,
                        "schedule_start_time": schedule_start_time,
                        "schedule_end_time": schedule_end_time,
                        "schedule_timezone": schedule_timezone,
                        "version": next_version,
                    }
                    await cur.execute(
                        """
                        INSERT INTO ai_control_settings_audit (
                            settings_id, action, actor_staff_id, actor_staff_name,
                            old_configuration_json, new_configuration_json, created_at
                        ) VALUES (1, 'AI_SETTINGS_UPDATED', %s, %s, %s, %s, UTC_TIMESTAMP(6))
                        """,
                        (
                            int(actor.id),
                            actor.name,
                            json.dumps(old_payload, separators=(",", ":"), ensure_ascii=False),
                            json.dumps(new_payload, separators=(",", ":"), ensure_ascii=False),
                        ),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        return await self.get()


class AIAvailabilityService:
    def __init__(self, repository: AIControlRepository | None = None) -> None:
        self.repository = repository or AIControlRepository()

    @staticmethod
    def _require_manager(actor: SupportAgent) -> None:
        if not actor.is_supervisor:
            raise AIControlForbiddenError(
                "Only a Super Admin or administrator can change AI control settings."
            )

    async def get_effective_ai_availability(
        self,
        *,
        now: datetime | None = None,
    ) -> EffectiveAIAvailability:
        configuration = await self.repository.get()
        return evaluate_ai_availability(configuration, now=now)

    async def get_settings_payload(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return (await self.get_effective_ai_availability(now=now)).to_dict()

    async def update_settings(
        self,
        *,
        actor: SupportAgent,
        expected_version: int,
        control_mode: str,
        manual_ai_enabled: bool,
        schedule_start_time: str,
        schedule_end_time: str,
        schedule_timezone: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        self._require_manager(actor)
        mode = str(control_mode or "").strip().upper()
        if mode not in {"MANUAL", "SCHEDULED"}:
            raise AIControlValidationError("Control mode must be MANUAL or SCHEDULED.")
        try:
            normalized_version = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise AIControlValidationError("A valid settings version is required.") from exc
        if normalized_version < 1:
            raise AIControlValidationError("A valid settings version is required.")

        start = str(schedule_start_time or "").strip()
        end = str(schedule_end_time or "").strip()
        start_minutes = _parse_time_minutes(start)
        end_minutes = _parse_time_minutes(end)
        if start_minutes == end_minutes:
            raise AIControlValidationError(
                "Schedule start and end times must be different."
            )
        timezone_name = _validate_timezone(schedule_timezone)

        updated = await self.repository.update(
            expected_version=normalized_version,
            control_mode=cast(AIControlMode, mode),
            manual_ai_enabled=bool(manual_ai_enabled),
            schedule_start_time=start,
            schedule_end_time=end,
            schedule_timezone=timezone_name,
            actor=actor,
        )
        result = evaluate_ai_availability(updated, now=now)
        logger.info(
            "AI settings changed: actor_staff_id=%s control_mode=%s manual_enabled=%s "
            "schedule=%s-%s timezone=%s effective_enabled=%s reason=%s",
            actor.id,
            updated.control_mode,
            updated.manual_ai_enabled,
            updated.schedule_start_time,
            updated.schedule_end_time,
            updated.schedule_timezone,
            result.effective_ai_enabled,
            result.effective_reason,
        )
        return result.to_dict()


ai_availability_service = AIAvailabilityService()
