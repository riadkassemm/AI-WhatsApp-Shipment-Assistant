import asyncio
import shutil
import subprocess
import types

import pytest
from fastapi import HTTPException

from app import linked_device_api
from app.support_ui import _HTML

STAFF = types.SimpleNamespace(id=3, is_supervisor=False, is_staff=True)
ADMIN = types.SimpleNamespace(id=4, is_supervisor=True, is_staff=True)


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


def test_recovery_actions_are_for_admins_and_super_admins_only(bridge) -> None:
    for action in (linked_device_api.reconnect_linked_device, linked_device_api.reset_linked_device_session):
        with pytest.raises(HTTPException) as denied:
            asyncio.run(action("90052", STAFF))
        assert denied.value.status_code == 403
    body = linked_device_api.ForceRemoveLinkedDeviceLineRequest(close_active_tickets=True, confirm="REMOVE")
    with pytest.raises(HTTPException) as denied:
        asyncio.run(linked_device_api.force_remove_linked_device_line("90052", body, STAFF))
    assert denied.value.status_code == 403
    assert bridge == []


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
