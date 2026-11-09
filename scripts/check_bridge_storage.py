#!/usr/bin/env python3
"""Independent, read-only bridge/disk probe; never opens session contents.

The systemd timer runs this even when bridge initialization fails. Its only
write is a redacted runtime report on /run, outside authoritative storage.
Alerts also reach journald and the service exit status if that write fails.
"""
from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

PREFIX = "WHATSAPP_LINKED_DEVICE_STORAGE_"
DEFAULT_REPORT = "/run/whatsapp-storage-watchdog/status.json"
ISSUE_CODES = {"MONITOR_UNAVAILABLE", "DISK_SPACE_LOW", "INODES_LOW", "STORE_UNAVAILABLE",
               "JOURNAL_LIMIT", "SNAPSHOT_LIMIT", "COMPACTION_FAILED", "RECOVERY_ATTENTION",
               "STORAGE_HEALTH_UNAVAILABLE"}
RANK = {"ok": 0, "warning": 1, "critical": 2}


def thresholds(env: dict[str, str]) -> dict[str, int]:
    defaults = {"DISK_WARN_BYTES": 2 * 1024 ** 3, "DISK_CRITICAL_BYTES": 512 * 1024 ** 2,
                "DISK_WARN_PERCENT": 5, "DISK_CRITICAL_PERCENT": 1,
                "INODE_WARN_PERCENT": 10, "INODE_CRITICAL_PERCENT": 5}
    values = {key: int(env.get(PREFIX + key, value)) for key, value in defaults.items()}
    if (any(value <= 0 for value in values.values())
            or values["DISK_CRITICAL_BYTES"] >= values["DISK_WARN_BYTES"]
            or not values["DISK_CRITICAL_PERCENT"] < values["DISK_WARN_PERCENT"] <= 100
            or not values["INODE_CRITICAL_PERCENT"] < values["INODE_WARN_PERCENT"] <= 100):
        raise ValueError("INVALID_WATCHDOG_THRESHOLDS")
    return values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("WATCHDOG_REDIRECT_REJECTED")


def bridge_probe(env: dict[str, str]) -> dict:
    origin = urllib.parse.urlsplit(env.get("WHATSAPP_LINKED_DEVICE_URL", "http://127.0.0.1:3001"))
    if (origin.scheme not in {"http", "https"} or origin.hostname not in {"127.0.0.1", "::1", "localhost"}
            or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment):
        raise ValueError("WATCHDOG_LOOPBACK_REQUIRED")
    secret = env.get("WHATSAPP_LINKED_DEVICE_SECRET", "")
    if len(secret) < 32:
        raise ValueError("WATCHDOG_AUTH_REQUIRED")
    # Never follow redirects or inherit proxy settings with the bridge secret.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def read(endpoint):
        url = urllib.parse.urlunsplit((origin.scheme, origin.netloc, endpoint, "", ""))
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + secret})
        with opener.open(request, timeout=3) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise ValueError("INVALID_BRIDGE_HEALTH")
        return json.loads(body)

    try:
        value = read("/v1/storage")
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code != 404:
            raise
        # Rollback to 2a6a225 retains this independent probe but has no storage
        # endpoint. Prove that bridge status works, retain disk monitoring, and
        # explicitly warn that detailed lifecycle telemetry is unavailable.
        legacy = read("/v1/status")
        if not isinstance(legacy, dict) or not isinstance(legacy.get("lines"), list):
            raise ValueError("INVALID_BRIDGE_HEALTH")
        return {"level": "warning", "issues": [{"code": "MONITOR_UNAVAILABLE", "level": "warning"}]}
    if not isinstance(value, dict) or value.get("level") not in RANK or not isinstance(value.get("issues"), list):
        raise ValueError("INVALID_BRIDGE_HEALTH")
    return value


def collect(env: dict[str, str], *, filesystem=os.statvfs, probe=bridge_probe, now=time.time) -> dict:
    result = {"version": 1, "checked_at": now(), "level": "ok", "bridge_available": False,
              "disk": None, "issues": []}

    def issue(code: str, level: str, line_id=None):
        item = {"code": code, "level": level}
        if isinstance(line_id, str) and line_id.isascii() and line_id.isdigit() and 5 <= len(line_id) <= 32:
            item["line_id"] = line_id
        if item not in result["issues"]:
            result["issues"].append(item)
        if RANK[level] > RANK[result["level"]]:
            result["level"] = level

    try:
        limits = thresholds(env)
    except (TypeError, ValueError, OverflowError):
        limits = thresholds({})
        issue("WATCHDOG_CONFIG_INVALID", "critical")
    try:
        # A shared environment setting uses the same path as the bridge. The
        # systemd working directory is the project root for relative paths.
        disk = filesystem(env.get("WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY", "whatsapp_bridge/data"))
        block_size = disk.f_frsize or disk.f_bsize
        if block_size <= 0 or disk.f_blocks <= 0 or disk.f_bavail < 0 or disk.f_bavail > disk.f_blocks:
            raise ValueError("INVALID_DISK_METRICS")
        available = disk.f_bavail * block_size
        percent = disk.f_bavail / disk.f_blocks * 100
        inode_percent = disk.f_favail / disk.f_files * 100 if disk.f_files > 0 and 0 <= disk.f_favail <= disk.f_files else None
        result["disk"] = {"available_bytes": available, "available_percent": percent,
                          "available_inodes": disk.f_favail if inode_percent is not None else None,
                          "available_inode_percent": inode_percent}
        if available <= limits["DISK_CRITICAL_BYTES"] or percent <= limits["DISK_CRITICAL_PERCENT"]:
            issue("DISK_SPACE_LOW", "critical")
        elif available <= limits["DISK_WARN_BYTES"] or percent <= limits["DISK_WARN_PERCENT"]:
            issue("DISK_SPACE_LOW", "warning")
        if inode_percent is not None and inode_percent <= limits["INODE_CRITICAL_PERCENT"]:
            issue("INODES_LOW", "critical")
        elif inode_percent is not None and inode_percent <= limits["INODE_WARN_PERCENT"]:
            issue("INODES_LOW", "warning")
    except (OSError, ValueError, OverflowError):
        issue("MONITOR_UNAVAILABLE", "critical")
    try:
        status = probe(env)
        if not isinstance(status, dict) or status.get("level") not in RANK or not isinstance(status.get("issues"), list):
            raise ValueError("INVALID_BRIDGE_HEALTH")
        result["bridge_available"] = True
        accepted = False
        for item in status["issues"]:
            if isinstance(item, dict) and item.get("code") in ISSUE_CODES and item.get("level") in {"warning", "critical"}:
                issue(item["code"], item["level"], item.get("line_id"))
                accepted = True
        if status["level"] != "ok" and not accepted:
            issue("MONITOR_UNAVAILABLE", "critical")
    except Exception:
        # Exception strings can contain URLs or credentials; emit only a code.
        issue("BRIDGE_UNAVAILABLE", "critical")
    return result


def write_report(filename: str, result: dict) -> None:
    target = Path(filename)
    # Fixed nonauthoritative pending path bounds retained telemetry after a
    # process kill. A separate flock excludes timer/manual overlap. Neither
    # pathname is inside authoritative storage in the supplied systemd unit.
    lockname = target.with_name("." + target.name + ".lock")
    temporary = target.with_name("." + target.name + ".pending")
    lock = os.open(lockname, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    locked = False
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("INVALID_WATCHDOG_LOCK")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        temporary.unlink(missing_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(result, handle, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            if locked:
                temporary.unlink(missing_ok=True)
        finally:
            os.close(lock)


def main() -> int:
    result = collect(dict(os.environ))
    try:
        write_report(os.environ.get(PREFIX + "WATCHDOG_REPORT", DEFAULT_REPORT), result)
    except Exception:
        result["level"] = "critical"
        result["issues"].append({"code": "WATCHDOG_REPORT_FAILED", "level": "critical"})
    print(json.dumps({"component": "whatsapp_storage_watchdog", "event": "storage_watchdog", **result}), flush=True)
    return 2 if result["level"] == "critical" else 0


if __name__ == "__main__":
    raise SystemExit(main())
