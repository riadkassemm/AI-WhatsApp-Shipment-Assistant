"""Dashboard-initiated restart of the WhatsApp linked-device bridge.

The API runs unprivileged (NoNewPrivileges=true), so it never restarts a
service itself. It writes a small request file into the control directory; a
root-owned systemd path unit (deployment/production/bridge-restart/) notices
it, removes it, enforces the rate limit again and runs exactly one action:
`systemctl restart --no-block whatsapp-bridge`. It then writes a result file
this module reads back. Nothing in the request can change what gets run.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from app.config import settings

REQUEST_NAME = "bridge-restart.request"
RESULT_NAME = "bridge-restart.result"
MIN_INTERVAL_SECONDS = 300


class BridgeRestartUnavailable(Exception):
    pass


class BridgeRestartTooSoon(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"The WhatsApp service was restarted recently. Try again in {max(1, retry_after // 60 + (retry_after % 60 > 0))} min.")
        self.retry_after = retry_after


def _directory() -> Path:
    return Path(settings.whatsapp_bridge_control_directory)


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 4096:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def available() -> bool:
    return bool(settings.whatsapp_bridge_restart_enabled)


def status(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    result = _read_json(_directory() / RESULT_NAME)
    restarted_at = result.get("restarted_at") if isinstance(result.get("restarted_at"), (int, float)) else None
    pending = (_directory() / REQUEST_NAME).exists()
    retry_after = 0
    if restarted_at is not None:
        retry_after = max(0, int(MIN_INTERVAL_SECONDS - (now - restarted_at)))
    return {
        "available": available(),
        "pending": pending,
        "last_restarted_at": restarted_at,
        "last_status": result.get("status") if isinstance(result.get("status"), str) else None,
        "retry_after": retry_after,
    }


def request_restart(staff_id: int, now: float | None = None) -> dict:
    if not available():
        raise BridgeRestartUnavailable("Restarting the WhatsApp service from the dashboard is not set up on this server.")
    now = time.time() if now is None else now
    current = status(now)
    if current["pending"]:
        return {**current, "requested": False}
    if current["retry_after"]:
        raise BridgeRestartTooSoon(current["retry_after"])
    directory = _directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    request = {"version": 1, "request_id": str(uuid.uuid4()), "staff_id": int(staff_id), "requested_at": now}
    temporary = directory / f".{REQUEST_NAME}.{request['request_id']}.tmp"
    with open(temporary, "x", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(request, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, directory / REQUEST_NAME)
    return {**status(now), "requested": True, "request_id": request["request_id"]}
