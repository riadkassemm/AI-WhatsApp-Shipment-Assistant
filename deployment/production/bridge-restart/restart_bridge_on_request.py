#!/usr/bin/python3
"""Restart the WhatsApp bridge when an Admin requests it from the dashboard.

Runs as root, only from whatsapp-bridge-restart.service (triggered by the
whatsapp-bridge-restart.path unit). The request file is consumed first, so the
path unit cannot loop. Its contents are only logged: the one action this
helper can take is `systemctl restart --no-block whatsapp-bridge.service`, at
most once every 5 minutes. Install to /usr/local/libexec/whatsapp-support/.
"""
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time

CONTROL = os.environ.get("BRIDGE_CONTROL_DIRECTORY", "/var/lib/whatsapp-support/control")
OWNER = os.environ.get("BRIDGE_CONTROL_OWNER", "whatsapp-support")
SYSTEMCTL = os.environ.get("BRIDGE_SYSTEMCTL", "/usr/bin/systemctl")  # overridden only by tests
MIN_INTERVAL_SECONDS = 300
REQUEST = "bridge-restart.request"
RESULT = "bridge-restart.result"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def log(event, **fields):
    print(json.dumps({"component": "bridge_restart", "event": event, **fields}), flush=True)


def read_json(path, uid):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_size > 4096:
            return None
        try:
            value = json.load(handle)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def write_result(uid, gid, value):
    path = os.path.join(CONTROL, RESULT)
    temporary = path + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.chown(temporary, uid, gid)
    os.replace(temporary, path)


def main():
    owner = pwd.getpwnam(OWNER)
    uid, gid = owner.pw_uid, owner.pw_gid
    path = os.path.join(CONTROL, REQUEST)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return 0
    request = read_json(path, uid)
    os.unlink(path)  # consume first: a bad request must never retrigger the path unit
    if request is None:
        log("request_rejected", reason="invalid_request")
        return 0
    staff_id = request.get("staff_id") if isinstance(request.get("staff_id"), int) else None
    request_id = request.get("request_id") if isinstance(request.get("request_id"), str) and UUID.match(request.get("request_id")) else None
    previous = read_json(os.path.join(CONTROL, RESULT), uid) or {}
    last = previous.get("restarted_at") if isinstance(previous.get("restarted_at"), (int, float)) else None
    now = time.time()
    if last is not None and now - last < MIN_INTERVAL_SECONDS:
        write_result(uid, gid, {"version": 1, "status": "refused_too_soon", "restarted_at": last,
                                "staff_id": staff_id, "request_id": request_id})
        log("request_rejected", reason="too_soon", staff_id=staff_id, request_id=request_id)
        return 0
    code = subprocess.run([SYSTEMCTL, "restart", "--no-block", "whatsapp-bridge.service"], check=False).returncode
    status = "restarting" if code == 0 else "failed"
    write_result(uid, gid, {"version": 1, "status": status, "restarted_at": now if code == 0 else last,
                            "staff_id": staff_id, "request_id": request_id})
    log("bridge_restart", status=status, staff_id=staff_id, request_id=request_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
