"""Connection health of enabled WhatsApp lines, for every signed-in staff member.

Feeds the dashboard's outage banner and the composer notice. Exposes no QR
codes, pairing state details or session data, only whether each line can send.
"""
from __future__ import annotations

import asyncio
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
_CACHE_SECONDS = 10
_STATES = {"connected": "connected", "logged_out": "logged_out", "error": "offline",
           "connecting": "reconnecting", "disconnected": "reconnecting", "qr": "reconnecting"}
_cache: dict[str, Any] = {"at": 0.0, "payload": None}
_first_seen_down: dict[str, float] = {}
_lock = asyncio.Lock()


def _iso(epoch_seconds: float | None) -> str | None:
    if not epoch_seconds:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def summarize(registry: list[dict[str, Any]], bridge_lines: list[dict[str, Any]] | None, now: float) -> list[dict[str, Any]]:
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
        lines.append({
            "line_id": line_id, "name": row.get("name") or line_id, "status": state, "since": _iso(since),
            "logout_reason": reason if isinstance(reason, str) else None,
            "alert": state == "logged_out" or (state != "connected" and since is not None and now - since >= ALERT_AFTER_SECONDS),
        })
    return lines


async def line_health() -> dict[str, Any]:
    async with _lock:
        now = time.time()
        if _cache["payload"] is not None and now - _cache["at"] < _CACHE_SECONDS:
            return _cache["payload"]
        from app.whatsapp_registry import whatsapp_registry_repository

        configured = configured_linked_device_lines()
        registry = [row for row in await whatsapp_registry_repository.fetch_all(
            """SELECT phone_number_id, COALESCE(NULLIF(display_name, ''), NULLIF(display_phone_number, ''), phone_number_id) AS name
               FROM whatsapp_phone_numbers
               WHERE source = 'LINKED_DEVICE' AND is_enabled = 1 AND removed_at IS NULL
               ORDER BY name, phone_number_id""") if str(row["phone_number_id"]) in configured]
        bridge_lines = None
        if registry and len(settings.whatsapp_linked_device_secret) >= 32:
            try:
                bridge_lines = (await linked_device_client.get_status()).get("lines", [])
            except Exception:
                bridge_lines = None
        payload = {"available": not registry or bridge_lines is not None,
                   "lines": summarize(registry, bridge_lines, now), "checked_at": _iso(now)}
        _cache.update(at=now, payload=payload)
        return payload


@router.get("/api/support/whatsapp-lines/health")
async def whatsapp_line_health(agent: SupportAgent = Depends(get_current_agent)) -> dict:
    return await line_health()
