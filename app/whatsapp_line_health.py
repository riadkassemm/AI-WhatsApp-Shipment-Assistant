"""Connection health of enabled WhatsApp lines, for every signed-in staff member.

Feeds the dashboard's outage banner and the composer notice. Exposes no QR
codes, pairing state details or session data, only whether each line can send.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

from app.config import configured_linked_device_lines, settings
from app.support_auth import SupportAgent, get_current_agent
from app.whatsapp_client import linked_device_client

router = APIRouter(tags=["whatsapp-line-health"])

# Reconnects normally finish within seconds; alert only when one does not.
ALERT_AFTER_SECONDS = 120
_CACHE_SECONDS = 5
_STATES = {"connected": "connected", "logged_out": "logged_out", "error": "offline",
           "connecting": "reconnecting", "disconnected": "reconnecting", "qr": "reconnecting"}
_cache: dict[str, Any] = {"at": 0.0, "payload": None}
_first_seen_down: dict[str, float] = {}
_lock = asyncio.Lock()
_STORAGE_CODES = {"MONITOR_UNAVAILABLE", "DISK_SPACE_LOW", "INODES_LOW", "STORE_UNAVAILABLE",
                  "JOURNAL_LIMIT", "SNAPSHOT_LIMIT", "COMPACTION_FAILED", "RECOVERY_ATTENTION",
                  "STORAGE_HEALTH_UNAVAILABLE", "BRIDGE_UNAVAILABLE", "WATCHDOG_CONFIG_INVALID",
                  "WATCHDOG_REPORT_FAILED"}
_WATCHDOG_REPORT = "/run/whatsapp-storage-watchdog/status.json"
_WATCHDOG_MAX_AGE_SECONDS = 180


def _iso(epoch_seconds: float | None) -> str | None:
    if not epoch_seconds:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def summarize(registry: list[dict[str, Any]], bridge_lines: list[dict[str, Any]] | None, now: float,
              storage: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Merge enabled lines with bridge status. ``bridge_lines=None`` means the bridge is unreachable."""
    by_id = {str(line.get("line_id")): line for line in bridge_lines or [] if isinstance(line, dict)}
    lines = []
    for row in registry:
        line_id = str(row["phone_number_id"])
        bridge = by_id.get(line_id)
        state = "offline" if bridge is None else _STATES.get(str(bridge.get("status") or ""), "offline")
        if state == "connected":
            _first_seen_down.pop(line_id, None)
            since = None
        else:
            # The logout time survives bridge restarts; the disconnect time does not.
            reported = ((bridge.get("logged_out_at") if state == "logged_out" else None) or bridge.get("disconnected_since")) if bridge else None
            since = reported / 1000 if isinstance(reported, (int, float)) and reported > 0 else _first_seen_down.setdefault(line_id, now)
        reason = bridge.get("logout_reason") if bridge and state == "logged_out" else None
        category = (bridge or {}).get("recovery_category")
        storage_blocked = category == "storage_failure" or any(
            issue.get("level") == "critical" and issue.get("code") not in {"BRIDGE_UNAVAILABLE", "MONITOR_UNAVAILABLE"}
            and (not issue.get("line_id") or str(issue["line_id"]) == line_id)
            for issue in (storage or {}).get("issues", [])
        )
        if storage_blocked:
            connection_state, recovery_action = "storage_recovery_required", "administrator"
        elif state == "logged_out" or (bridge or {}).get("status") == "qr":
            connection_state, recovery_action = "relink_required", "relink"
        elif state == "connected":
            connection_state, recovery_action = "connected", "none"
        elif state == "reconnecting" and category not in {"authentication_failure", "session_conflict", "protocol_failure"}:
            connection_state, recovery_action = "reconnecting", "automatic"
        else:
            connection_state, recovery_action = "temporarily_unavailable", "administrator"
        administration = (bridge or {}).get('administration')
        administration = administration if isinstance(administration, dict) else {}
        paused = administration.get('paused') if type(administration.get('paused')) is bool else None
        lines.append({
            "line_id": line_id, "name": row.get("name") or line_id, "status": state, "since": _iso(since),
            **({'administratively_paused':paused} if settings.whatsapp_admin_recovery_enabled or paused is True else {}),
            "logout_reason": reason if isinstance(reason, str) else None,
            "connection_state": connection_state, "recovery_action": recovery_action,
            "alert": connection_state in {"relink_required", "storage_recovery_required"} or
                     (state != "connected" and since is not None and now - since >= ALERT_AFTER_SECONDS),
        })
    return lines


def summarize_storage(value: Any, registry: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Expose only operational severity/codes, never bridge paths or session state."""
    if not isinstance(value, dict) or not isinstance(value.get("level"), str) or value["level"] not in {"ok", "warning", "critical"}:
        return None
    by_id = {str(row["phone_number_id"]): row.get("name") or str(row["phone_number_id"]) for row in registry}
    issues = []
    for issue in value.get("issues", []) if isinstance(value.get("issues"), list) else []:
        if (not isinstance(issue, dict) or not isinstance(issue.get("code"), str) or issue["code"] not in _STORAGE_CODES
                or not isinstance(issue.get("level"), str) or issue["level"] not in {"warning", "critical"}):
            continue
        line_id = issue.get("line_id")
        if line_id is not None and str(line_id) not in by_id:
            continue
        safe = {"code": issue["code"], "level": issue["level"],
                "line_id": str(line_id) if line_id is not None else None,
                "name": by_id.get(str(line_id)) if line_id is not None else None}
        # Store failure is not evidence of exhausted disk space. Retain only
        # the fixed store kind so the dashboard can explain session recovery.
        if isinstance(issue.get("kind"), str) and issue["kind"] in {"auth", "state", "outbox"}:
            safe["kind"] = issue["kind"]
        issues.append(safe)
    level = "critical" if any(issue["level"] == "critical" for issue in issues) else "warning" if issues else "ok"
    return {"level": level, "issues": issues}


def watchdog_storage(registry: list[dict[str, Any]], now: float) -> dict[str, Any]:
    """Read a bounded, fresh operational report when bridge startup/status fails.

    Runtime telemetry is never evidence that the currently unreachable bridge
    can send. Missing/stale reports explicitly expose the monitoring gap.
    """
    issues = [{"code": "BRIDGE_UNAVAILABLE", "level": "critical"}]
    try:
        filename = os.environ.get("WHATSAPP_LINKED_DEVICE_STORAGE_WATCHDOG_REPORT", _WATCHDOG_REPORT)
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 65536 or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValueError("INVALID_WATCHDOG_REPORT")
            contents = handle.read(65537)
        if len(contents) > 65536:
            raise ValueError("INVALID_WATCHDOG_REPORT")
        report = json.loads(contents)
        checked = report.get("checked_at") if isinstance(report, dict) else None
        if (not isinstance(checked, (int, float)) or isinstance(checked, bool) or not math.isfinite(checked)
                or not 0 <= now - checked <= _WATCHDOG_MAX_AGE_SECONDS or report.get("version") != 1):
            raise ValueError("STALE_WATCHDOG_REPORT")
        summary = summarize_storage(report, registry)
        if summary is None:
            raise ValueError("INVALID_WATCHDOG_REPORT")
        issues.extend(issue for issue in summary["issues"] if issue["code"] != "BRIDGE_UNAVAILABLE")
    except (OSError, ValueError, TypeError):
        issues.append({"code": "MONITOR_UNAVAILABLE", "level": "critical"})
    return summarize_storage({"level": "critical", "issues": issues}, registry)


async def line_health() -> dict[str, Any]:
    async with _lock:
        now = time.time()
        if _cache["payload"] is not None and 0 <= time.monotonic() - _cache["at"] < _CACHE_SECONDS:
            return _cache["payload"]
        from app.whatsapp_registry import whatsapp_registry_repository

        configured = configured_linked_device_lines()
        registry = [row for row in await whatsapp_registry_repository.fetch_all(
            """SELECT phone_number_id, COALESCE(NULLIF(display_name, ''), NULLIF(display_phone_number, ''), phone_number_id) AS name
               FROM whatsapp_phone_numbers
               WHERE source = 'LINKED_DEVICE' AND is_enabled = 1 AND removed_at IS NULL
               ORDER BY name, phone_number_id""") if str(row["phone_number_id"]) in configured]
        bridge_lines = None
        storage = None
        if registry and len(settings.whatsapp_linked_device_secret) >= 32:
            try:
                bridge_status = await linked_device_client.get_status()
                bridge_lines = bridge_status.get("lines", [])
                storage = summarize_storage(bridge_status.get("storage"), registry)
            except Exception:
                bridge_lines = None
                storage = await asyncio.to_thread(watchdog_storage, registry, now)
        completed_at = time.time()
        payload = {"available": not registry or bridge_lines is not None,
                   "lines": summarize(registry, bridge_lines, completed_at, storage), "storage": storage, "checked_at": _iso(completed_at)}
        if settings.whatsapp_admin_recovery_enabled:
            from app.bridge_recovery_repository import bridge_recovery_repository
            try:
                controls = {row['line_id']:row for row in await bridge_recovery_repository.controls()}
                for line in payload['lines']:
                    if controls.get(line['line_id'], {}).get('paused'): line['administratively_paused'] = True
            except Exception:
                for line in payload['lines']:
                    if not line['administratively_paused']: line['administratively_paused'] = None
        # Cache from completion: a slow failed probe must not let every queued
        # staff poll launch another full timeout immediately. Use monotonic time
        # so clock corrections cannot prolong or repeatedly invalidate health.
        _cache.update(at=time.monotonic(), payload=payload)
        return payload


@router.get("/api/support/whatsapp-lines/health")
async def whatsapp_line_health(agent: SupportAgent = Depends(get_current_agent)) -> dict:
    return await line_health()
