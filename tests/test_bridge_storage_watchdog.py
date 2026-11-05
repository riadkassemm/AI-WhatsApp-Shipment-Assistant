from __future__ import annotations

import configparser
import json
import os
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts import check_bridge_storage as watchdog


def disk(available=20 * 1024 ** 3, free_inodes=900):
    return types.SimpleNamespace(f_frsize=1, f_bsize=4096, f_blocks=100 * 1024 ** 3,
                                 f_bavail=available, f_files=1000, f_favail=free_inodes)


def healthy(_env):
    return {"level": "ok", "issues": []}


def test_watchdog_alerts_disk_and_bridge_startup_failure_without_reading_session_data():
    touched = []

    def filesystem(directory):
        touched.append(directory)
        return disk(10, 1)

    def unavailable(_env):
        raise OSError("private/credentials-and-secret-must-not-escape")

    result = watchdog.collect({"WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY": "/private/state"},
                              filesystem=filesystem, probe=unavailable, now=lambda: 1234)
    assert touched == ["/private/state"]
    assert result["level"] == "critical" and result["bridge_available"] is False
    assert {issue["code"] for issue in result["issues"]} == {"DISK_SPACE_LOW", "INODES_LOW", "BRIDGE_UNAVAILABLE"}
    assert "private" not in json.dumps(result) and "secret" not in json.dumps(result)


def test_watchdog_detects_disk_probe_failure_even_with_healthy_bridge():
    def unavailable(_directory):
        raise PermissionError("private")

    result = watchdog.collect({}, filesystem=unavailable, probe=healthy)
    assert result["bridge_available"] is True
    assert result["issues"] == [{"code": "MONITOR_UNAVAILABLE", "level": "critical"}]


def test_watchdog_uses_configured_thresholds_and_sanitizes_provider_status():
    env = {watchdog.PREFIX + "DISK_WARN_BYTES": "10", watchdog.PREFIX + "DISK_CRITICAL_BYTES": "5"}
    status = {"level": "warning", "issues": [
        {"code": "COMPACTION_FAILED", "level": "warning", "line_id": "90011", "path": "/private"},
        {"code": "INODES_LOW", "level": "warning", "line_id": "private"},
        {"code": "secret", "level": "critical"},
    ], "secret": "credentials"}
    result = watchdog.collect(env, filesystem=lambda _: disk(), probe=lambda _: status)
    assert result["level"] == "warning"
    assert result["issues"] == [{"code": "COMPACTION_FAILED", "level": "warning", "line_id": "90011"},
                                {"code": "INODES_LOW", "level": "warning"}]
    assert "private" not in json.dumps(result) and "credentials" not in json.dumps(result)
    assert watchdog.collect({}, filesystem=lambda _: disk(), probe=lambda _: {"level": "critical", "issues": []})["level"] == "critical"
    invalid = watchdog.collect({watchdog.PREFIX + "DISK_WARN_BYTES": "invalid"}, filesystem=lambda _: disk(), probe=healthy)
    assert invalid["issues"] == [{"code": "WATCHDOG_CONFIG_INVALID", "level": "critical"}]


@pytest.mark.parametrize("origin", ["https://example.com", "http://127.0.0.1/secret", "http://user:pass@localhost", "http://localhost?token=secret"])
def test_watchdog_never_sends_secret_to_remote_or_non_origin_url(origin):
    with pytest.raises(ValueError, match="LOOPBACK_REQUIRED"):
        watchdog.bridge_probe({"WHATSAPP_LINKED_DEVICE_URL": origin, "WHATSAPP_LINKED_DEVICE_SECRET": "s" * 32})


def test_watchdog_refuses_redirects_and_ignores_proxy_environment(monkeypatch):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/must-not-follow")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("http_proxy", "http://invalid.invalid:9")
    monkeypatch.setenv("no_proxy", "")
    try:
        with pytest.raises(ValueError, match="REDIRECT_REJECTED"):
            watchdog.bridge_probe({"WHATSAPP_LINKED_DEVICE_URL": f"http://127.0.0.1:{server.server_port}",
                                   "WHATSAPP_LINKED_DEVICE_SECRET": "s" * 32})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert calls == [("/v1/storage", "Bearer " + "s" * 32)]


def test_watchdog_survives_rollback_to_deployed_bridge_without_storage_endpoint():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(404 if self.path == "/v1/storage" else 200)
            self.end_headers()
            if self.path == "/v1/status":
                self.wfile.write(b'{"lines":[{"line_id":"90011","status":"connected","private":"omit"}]}')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status = watchdog.bridge_probe({"WHATSAPP_LINKED_DEVICE_URL": f"http://127.0.0.1:{server.server_port}",
                                        "WHATSAPP_LINKED_DEVICE_SECRET": "s" * 32})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert calls == ["/v1/storage", "/v1/status"]
    assert status == {"level": "warning", "issues": [{"code": "MONITOR_UNAVAILABLE", "level": "warning"}]}
    assert "private" not in json.dumps(status)


def test_runtime_report_is_atomic_private_and_failed_publication_cleans_only_owned_temporary(tmp_path, monkeypatch):
    report = tmp_path / "status.json"
    authoritative = tmp_path / "auth.json"
    authoritative.write_bytes(b"do not modify")
    first = watchdog.collect({}, filesystem=lambda _: disk(), probe=healthy, now=lambda: 1)
    watchdog.write_report(str(report), first)
    assert json.loads(report.read_bytes()) == first
    assert report.stat().st_mode & 0o777 == 0o600
    original = report.read_bytes()

    def failed_replace(*_args):
        raise OSError("ENOSPC")

    monkeypatch.setattr(os, "replace", failed_replace)
    for _ in range(3):
        with pytest.raises(OSError, match="ENOSPC"):
            watchdog.write_report(str(report), {**first, "checked_at": 2})
    assert report.read_bytes() == original
    assert authoritative.read_bytes() == b"do not modify"
    assert sorted(path.name for path in tmp_path.iterdir()) == [".status.json.lock", "auth.json", "status.json"]


def test_interrupted_runtime_report_has_one_reusable_pending_file_and_preserves_other_files(tmp_path):
    pending = tmp_path / ".status.json.pending"
    unrelated = tmp_path / ".other.pending"
    unrelated.write_bytes(b"retain")
    for index in range(3):
        pending.write_bytes(b"interrupted report")
        watchdog.write_report(str(tmp_path / "status.json"), {"iteration": index})
        assert not pending.exists()
        assert json.loads((tmp_path / "status.json").read_bytes()) == {"iteration": index}
        assert unrelated.read_bytes() == b"retain"


def test_watchdog_emits_alert_and_failure_exit_even_when_runtime_report_cannot_be_written(monkeypatch, capsys):
    result = watchdog.collect({}, filesystem=lambda _: disk(), probe=healthy, now=lambda: 123)
    monkeypatch.setattr(watchdog, "collect", lambda _env: result)

    def fail(*_args):
        raise OSError("private runtime pathname")

    monkeypatch.setattr(watchdog, "write_report", fail)
    assert watchdog.main() == 2
    logged = capsys.readouterr().out
    assert "private" not in logged
    assert json.loads(logged)["issues"] == [{"code": "WATCHDOG_REPORT_FAILED", "level": "critical"}]


def test_watchdog_service_is_independent_read_only_bounded_and_timer_enabled_separately():
    service = configparser.ConfigParser(interpolation=None, strict=False)
    service.read("deployment/dev-vps/whatsapp-storage-watchdog.service")
    assert "Requires" not in service["Unit"] and "PartOf" not in service["Unit"]
    assert service["Service"]["ProtectSystem"] == "strict"
    assert service["Service"]["ReadWritePaths"] == "/run/whatsapp-storage-watchdog"
    assert service["Service"]["TimeoutStartSec"] == "10"
    assert service["Service"]["RuntimeDirectoryPreserve"] == "yes"
    assert service["Service"]["ExecStart"] == "/usr/bin/python3 /usr/local/libexec/whatsapp-support/check_bridge_storage.py"
    timer = configparser.ConfigParser(interpolation=None)
    timer.read("deployment/dev-vps/whatsapp-storage-watchdog.timer")
    assert timer["Timer"]["OnUnitActiveSec"] == "60s"
    assert timer["Install"]["WantedBy"] == "timers.target"
