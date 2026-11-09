import asyncio
import getpass
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import bridge_restart, config
from app import linked_device_api
from app.support_ui import _HTML

HELPER = Path(__file__).resolve().parents[1] / "deployment/production/bridge-restart/restart_bridge_on_request.py"


@pytest.fixture
def control(tmp_path, monkeypatch):
    directory = tmp_path / "control"
    monkeypatch.setattr(config.settings, "whatsapp_bridge_control_directory", str(directory))
    monkeypatch.setattr(config.settings, "whatsapp_bridge_restart_enabled", True)
    return directory


def test_restart_is_off_until_the_server_helper_is_installed(control, monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "whatsapp_bridge_restart_enabled", False)
    with pytest.raises(bridge_restart.BridgeRestartUnavailable):
        bridge_restart.request_restart(7)
    assert not control.exists()


def test_a_request_is_written_privately_once_and_rate_limited(control) -> None:
    result = bridge_restart.request_restart(7, now=1000.0)
    request = control / bridge_restart.REQUEST_NAME
    assert result["requested"] is True and result["pending"] is True
    assert stat.S_IMODE(request.stat().st_mode) == 0o600
    payload = json.loads(request.read_text())
    assert payload["staff_id"] == 7 and payload["version"] == 1
    # A second click while the server has not consumed it does not stack requests.
    assert bridge_restart.request_restart(8, now=1001.0)["requested"] is False
    request.unlink()
    (control / bridge_restart.RESULT_NAME).write_text(json.dumps({"status": "restarting", "restarted_at": 1000.0}))
    with pytest.raises(bridge_restart.BridgeRestartTooSoon) as too_soon:
        bridge_restart.request_restart(7, now=1100.0)
    assert too_soon.value.retry_after == 200
    assert bridge_restart.request_restart(7, now=1000.0 + bridge_restart.MIN_INTERVAL_SECONDS)["requested"] is True


def test_only_admins_and_super_admins_can_restart_and_it_never_contacts_the_bridge(control, monkeypatch) -> None:
    async def unreachable(*_args, **_kwargs):
        raise AssertionError("the restart request must not depend on the bridge")
    monkeypatch.setattr(linked_device_api.linked_device_client, "get_status", unreachable)
    staff = types.SimpleNamespace(id=3, is_supervisor=False, is_staff=True)
    admin = types.SimpleNamespace(id=4, is_supervisor=True, is_staff=True)
    with pytest.raises(HTTPException) as denied:
        asyncio.run(linked_device_api.restart_bridge(staff))
    assert denied.value.status_code == 403
    result = asyncio.run(linked_device_api.restart_bridge(admin))
    assert result["requested"] is True
    assert asyncio.run(linked_device_api.bridge_restart_status(admin))["pending"] is True
    with pytest.raises(HTTPException) as busy:
        (control / bridge_restart.REQUEST_NAME).unlink()
        (control / bridge_restart.RESULT_NAME).write_text(json.dumps({"restarted_at": time.time()}))
        asyncio.run(linked_device_api.restart_bridge(admin))
    assert busy.value.status_code == 429


def _run_helper(control: Path, systemctl: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "BRIDGE_CONTROL_DIRECTORY": str(control), "BRIDGE_CONTROL_OWNER": getpass.getuser(),
           "BRIDGE_SYSTEMCTL": str(systemctl)}
    return subprocess.run([sys.executable, str(HELPER)], env=env, capture_output=True, text=True, timeout=20)


@pytest.fixture
def fake_systemctl(tmp_path):
    calls = tmp_path / "systemctl-calls"
    script = tmp_path / "systemctl"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n")
    script.chmod(0o755)
    return script, calls


def test_root_helper_runs_only_the_fixed_restart_and_consumes_the_request(control, fake_systemctl) -> None:
    script, calls = fake_systemctl
    bridge_restart.request_restart(11)
    completed = _run_helper(control, script)
    assert completed.returncode == 0, completed.stderr
    assert calls.read_text().splitlines() == ["restart --no-block whatsapp-bridge.service"]
    assert not (control / bridge_restart.REQUEST_NAME).exists()
    result = json.loads((control / bridge_restart.RESULT_NAME).read_text())
    assert result["status"] == "restarting" and result["staff_id"] == 11
    assert '"event": "bridge_restart"' in completed.stdout
    # A second request inside 5 minutes is consumed but refused by the helper too.
    (control / bridge_restart.REQUEST_NAME).write_text(json.dumps({"version": 1, "staff_id": 12}))
    completed = _run_helper(control, script)
    assert calls.read_text().splitlines() == ["restart --no-block whatsapp-bridge.service"]
    assert json.loads((control / bridge_restart.RESULT_NAME).read_text())["status"] == "refused_too_soon"
    assert not (control / bridge_restart.REQUEST_NAME).exists()


def test_root_helper_rejects_symlinks_and_garbage_without_restarting(control, fake_systemctl, tmp_path) -> None:
    script, calls = fake_systemctl
    control.mkdir()
    target = tmp_path / "elsewhere"
    target.write_text(json.dumps({"version": 1, "staff_id": 1}))
    (control / bridge_restart.REQUEST_NAME).symlink_to(target)
    assert _run_helper(control, script).returncode == 0
    assert not (control / bridge_restart.REQUEST_NAME).exists(), "the symlink is consumed"
    assert target.exists(), "its target is never touched"
    (control / bridge_restart.REQUEST_NAME).write_text("{not json")
    completed = _run_helper(control, script)
    assert '"reason": "invalid_request"' in completed.stdout
    assert not calls.exists(), "nothing was restarted"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_dashboard_button_is_for_admins_and_waits_until_numbers_reconnect() -> None:
    code = _HTML[_HTML.index("function syncBridgeRestartButton("): _HTML.index("async function loadLinkedDeviceStatus(")]
    script = r"""
const assert = require('node:assert/strict');
const button = {hidden: true, disabled: false, textContent: '', classList: {toggle(name, value) { if (name === 'hidden') button.hidden = value; }}};
const $ = id => id === 'restartBridgeBtn' ? button : null;
const window = {setTimeout(fn) { fn(); }};
const shown = [], calls = [];
const notify = (message, type = 'success') => shown.push([type, message]);
let confirmed = true;
const confirmDialog = async () => confirmed;
let statusCalls = 0;
const api = async (path, options = {}) => {
  calls.push([options.method || 'GET', path]);
  if (path.endsWith('/restart')) return {requested: true};
  statusCalls++;
  if (statusCalls === 1) throw new Error('The WhatsApp connection service is unavailable.');
  return {lines: [{line_id: '90047', is_paired: true, status: statusCalls < 3 ? 'connecting' : 'connected'}]};
};
const renderLinkedDeviceStatus = () => {};
const state = {agent: {bridge_restart_available: false}, activeModal: 'whatsappModal'};
""" + code + r"""
(async () => {
  syncBridgeRestartButton();
  assert.equal(button.hidden, true, 'hidden from Regular Staff and when not installed');
  state.agent.bridge_restart_available = true;
  syncBridgeRestartButton();
  assert.equal(button.hidden, false);
  confirmed = false;
  await restartWhatsAppService();
  assert.equal(calls.length, 0, 'cancelling the confirmation sends nothing');
  confirmed = true;
  await restartWhatsAppService();
  assert.deepEqual(calls[0], ['POST', '/api/support/whatsapp-linked-device/restart']);
  assert.match(shown[0][1], /Restarting the WhatsApp service/);
  assert.deepEqual(shown.at(-1), ['success', 'WhatsApp service restarted. All numbers are connected.']);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, 'Restart WhatsApp service');
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
