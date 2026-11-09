import asyncio
import shutil
import subprocess
import types

import pytest
from fastapi import HTTPException

from app import linked_device_api
from app.support_ui import _HTML

STAFF = types.SimpleNamespace(id=3, role="agent", is_supervisor=False, is_staff=True)
ADMIN = types.SimpleNamespace(id=4, role="admin", is_supervisor=True, is_staff=True)
OUTSIDER = types.SimpleNamespace(id=5, role="viewer", is_supervisor=False, is_staff=False)


@pytest.fixture
def bridge(monkeypatch):
    calls = []

    async def record(name, line_id):
        calls.append((name, line_id))
        return {"line_id": line_id}

    for name in ("reconnect", "reset_session", "force_remove"):
        monkeypatch.setattr(linked_device_api.linked_device_client, name,
                            lambda line_id, name=name: record(name, line_id))
    monkeypatch.setattr(linked_device_api, "configured_linked_device_lines", lambda: {"90052"})
    monkeypatch.setattr(linked_device_api, "removed_linked_device_line_ids", lambda: set())

    async def status(agent):
        return {"configured": True, "lines": []}
    monkeypatch.setattr(linked_device_api, "linked_device_status", status)
    return calls


def test_reset_and_force_remove_stay_with_admins_while_reconnect_is_for_all_staff(bridge) -> None:
    with pytest.raises(HTTPException) as denied:
        asyncio.run(linked_device_api.reset_linked_device_session("90052", STAFF))
    assert denied.value.status_code == 403
    body = linked_device_api.ForceRemoveLinkedDeviceLineRequest(close_active_tickets=True, confirm="REMOVE")
    with pytest.raises(HTTPException) as denied:
        asyncio.run(linked_device_api.force_remove_linked_device_line("90052", body, STAFF))
    assert denied.value.status_code == 403
    assert bridge == []
    asyncio.run(linked_device_api.reconnect_linked_device("90052", STAFF))
    assert bridge == [("reconnect", "90052")], "every staff role may reconnect a number"
    with pytest.raises(HTTPException) as outsider:
        asyncio.run(linked_device_api.reconnect_linked_device("90052", OUTSIDER))
    assert outsider.value.status_code == 403


def test_reconnect_and_reset_reach_only_the_requested_number(bridge) -> None:
    asyncio.run(linked_device_api.reconnect_linked_device("90052", ADMIN))
    asyncio.run(linked_device_api.reset_linked_device_session("90052", ADMIN))
    assert bridge == [("reconnect", "90052"), ("reset_session", "90052")]
    with pytest.raises(HTTPException) as unknown:
        asyncio.run(linked_device_api.reconnect_linked_device("90099", ADMIN))
    assert unknown.value.status_code == 404


def test_force_remove_needs_typed_confirmation_then_finishes_the_normal_removal(bridge, monkeypatch) -> None:
    finished = []

    async def normal_removal(line_id, body, agent):
        finished.append((line_id, body.close_active_tickets, agent.id))
        return {"removed_line_id": line_id, "unlinked": True}
    monkeypatch.setattr(linked_device_api, "remove_linked_device_line", normal_removal)
    wrong = linked_device_api.ForceRemoveLinkedDeviceLineRequest(close_active_tickets=True, confirm="yes")
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(linked_device_api.force_remove_linked_device_line("90052", wrong, ADMIN))
    assert rejected.value.status_code == 422 and bridge == []
    body = linked_device_api.ForceRemoveLinkedDeviceLineRequest(close_active_tickets=True, confirm="remove")
    result = asyncio.run(linked_device_api.force_remove_linked_device_line("90052", body, ADMIN))
    assert bridge == [("force_remove", "90052")]
    assert finished == [("90052", True, 4)]
    assert result["forced"] is True and result["removed_line_id"] == "90052"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_each_number_state_offers_only_the_matching_recovery_buttons() -> None:
    code = _HTML[_HTML.index("const BLOCKED_CATEGORIES"): _HTML.index("async function loadLinkedDeviceStatus(")]
    script = r"""
const assert = require('node:assert/strict');
""" + code + r"""
const labels = line => linkedDeviceRecoveryActions(line).map(action => action.label);
assert.deepEqual(labels({status: 'connected', is_paired: true}), ['Reconnect']);
assert.deepEqual(labels({status: 'error', is_paired: false}), ['Reset session', 'Force remove']);
assert.deepEqual(labels({status: 'disconnected', is_paired: true, recovery_category: 'storage_failure'}), ['Reconnect', 'Reset session']);
assert.deepEqual(labels({status: 'qr', is_paired: false}), []);
assert.deepEqual(labels({status: 'connected', is_paired: true, recovery_category: 'storage_failure'}), ['Reconnect'], 'a connected number is never reset');
assert.equal(linkedDeviceRecoveryActions({status: 'error'}).find(action => action.label === 'Force remove').danger, true);
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_resume_is_for_all_staff_and_keeps_the_bridge_guidance_as_409(bridge, monkeypatch) -> None:
    from app.whatsapp_client import WhatsAppSendError
    with pytest.raises(HTTPException) as denied:
        asyncio.run(linked_device_api.resume_linked_device_session("90052", OUTSIDER))
    assert denied.value.status_code == 403
    calls = []

    async def resumed(line_id):
        calls.append(line_id)
        return {"line_id": line_id}
    monkeypatch.setattr(linked_device_api.linked_device_client, "resume_session", resumed)
    asyncio.run(linked_device_api.resume_linked_device_session("90052", STAFF))
    asyncio.run(linked_device_api.resume_linked_device_session("90052", ADMIN))
    assert calls == ["90052", "90052"], "Regular Staff and Admins may both resume"


    async def refused(line_id):
        error = WhatsAppSendError("This session stopped on a disk error (EIO).", delivery_uncertain=False)
        error.http_status = 409
        raise error
    monkeypatch.setattr(linked_device_api.linked_device_client, "resume_session", refused)
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(linked_device_api.resume_linked_device_session("90052", ADMIN))
    assert conflict.value.status_code == 409 and "disk error" in conflict.value.detail


def test_status_tells_the_dashboard_which_sessions_are_blocked(monkeypatch) -> None:
    monkeypatch.setattr(linked_device_api.settings, "whatsapp_linked_device_secret", "s" * 40)
    monkeypatch.setattr(linked_device_api, "configured_linked_device_lines", lambda: {"90054": "+90"})
    monkeypatch.setattr(linked_device_api, "get_transport", lambda line_id: "linked_device")

    async def bridge_status():
        return {"lines": [{"line_id": "90054", "status": "error", "is_paired": False,
                           "session_blocked": True, "saved_login": True, "recovery_category": "storage_failure"}]}
    monkeypatch.setattr(linked_device_api.linked_device_client, "get_status", bridge_status)
    line = asyncio.run(linked_device_api.linked_device_status(ADMIN))["lines"][0]
    assert line["session_blocked"] is True and line["saved_login"] is True
    assert line["recovery_category"] == "storage_failure"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_a_blocked_session_offers_resume_reset_and_remove_but_never_reconnect_or_pair() -> None:
    code = _HTML[_HTML.index("const BLOCKED_CATEGORIES"): _HTML.index("async function loadLinkedDeviceStatus(")]
    script = r"""
const assert = require('node:assert/strict');
""" + code + r"""
const labels = line => linkedDeviceRecoveryActions(line).map(action => action.label);
assert.deepEqual(labels({status: 'error', is_paired: true, session_blocked: true, saved_login: true}), ['Resume session', 'Reset session', 'Force remove']);
assert.deepEqual(labels({status: 'error', is_paired: false, session_blocked: true, saved_login: false}), ['Reset session', 'Force remove'], 'no saved login: resume cannot help');
assert.deepEqual(labels({status: 'connected', is_paired: true}), ['Reconnect']);
assert.equal(linkedDeviceRecoveryActions({status: 'error', session_blocked: true, saved_login: true})[0].primary, true);
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    render = _HTML[_HTML.index("function renderLinkedDeviceStatus("): _HTML.index("async function loadLinkedDeviceStatus(")]
    assert "!line.session_blocked && line.recovery_category !== 'storage_failure'" in render, "Pair is never offered for a blocked session"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_regular_staff_see_reconnect_and_resume_but_not_reset_or_force_remove() -> None:
    code = _HTML[_HTML.index("const BLOCKED_CATEGORIES"): _HTML.index("async function loadLinkedDeviceStatus(")]
    script = r"""
const assert = require('node:assert/strict');
""" + code + r"""
const forStaff = line => linkedDeviceRecoveryActions(line).filter(action => !action.managersOnly).map(action => action.label);
assert.deepEqual(forStaff({status: 'error', is_paired: true, session_blocked: true, saved_login: true}), ['Resume session']);
assert.deepEqual(forStaff({status: 'connected', is_paired: true}), ['Reconnect']);
assert.deepEqual(forStaff({status: 'error', session_blocked: true, saved_login: false}), [], 'without a saved login an Admin must reset');
const managersOnly = linkedDeviceRecoveryActions({status: 'error', session_blocked: true, saved_login: true}).filter(action => action.managersOnly).map(action => action.label);
assert.deepEqual(managersOnly, ['Reset session', 'Force remove']);
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    render = _HTML[_HTML.index("function renderLinkedDeviceStatus("): _HTML.index("async function loadLinkedDeviceStatus(")]
    assert "action.managersOnly ? !canManageWhatsApp() : !canLinkWhatsApp()" in render
