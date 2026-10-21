from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.linked_device_api import router
import app.linked_device_api as api_module
from app.support_auth import SupportAgent, get_current_agent, require_support_csrf


@pytest.fixture
def linked_config(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "whatsapp_transport", "cloud")
    monkeypatch.setattr(settings, "whatsapp_linked_device_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_linked_device_secret", "s" * 48)
    monkeypatch.setattr(settings, "whatsapp_linked_device_lines", {"90001": ""})
    monkeypatch.setattr(settings, "whatsapp_linked_device_data_directory", str(tmp_path))
    monkeypatch.setattr(api_module, "_line_addition_lock", asyncio.Lock())


def request(method, path, *, payload=None, token="s" * 48, role="admin", content=None):
    async def run():
        app = FastAPI()
        app.include_router(router)

        async def agent():
            return SupportAgent(id="7", name="Staff", role=role)

        app.dependency_overrides[get_current_agent] = agent
        app.dependency_overrides[require_support_csrf] = agent
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            kwargs = {"headers": {"Authorization": "Bearer " + token}}
            if content is not None:
                kwargs["content"] = content
            else:
                kwargs["json"] = payload
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def message_event():
    return {
        "event_id": "event-1", "type": "message", "line_id": "90001", "from_me": False,
        "payload": {"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "90001", "display_phone_number": "96170000000"},
            "messages": [{"id": "ld:90001:MESSAGE1", "from": "96171111111", "timestamp": "123", "type": "text", "text": {"body": "hello"}}],
        }}]}]},
    }


def test_linked_callback_authentication_and_route_validation(linked_config, monkeypatch):
    import app.main as main_module

    calls = []

    async def process(payload, *, source):
        calls.append((payload, source))
        return JSONResponse({"status": "queued"})

    monkeypatch.setattr(main_module, "process_whatsapp_payload", process)
    endpoint = "/api/whatsapp/linked-device/events"
    assert request("POST", endpoint, payload=message_event(), token="wrong").status_code == 403
    event = message_event()
    event["line_id"] = "90002"
    assert request("POST", endpoint, payload=event).status_code == 404
    event = message_event()
    event["payload"]["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.cloud"
    assert request("POST", endpoint, payload=event).status_code == 400
    assert calls == []
    response = request("POST", endpoint, payload=message_event())
    assert response.status_code == 200 and response.json()["status"] == "queued"
    assert calls[0][1] == "linked_device"


def test_linked_callback_does_not_process_outgoing_as_customer(linked_config):
    event = message_event()
    event["from_me"] = True
    assert request("POST", "/api/whatsapp/linked-device/events", payload=event).status_code == 400


def test_linked_outgoing_callback_uses_support_sync_without_customer_ai(linked_config, monkeypatch):
    import app.main as main_module

    event = message_event()
    event.update(
        event_id="outgoing-1", type="outgoing_message", from_me=True,
        sender_phone="96171111111", message_id="ld:90001:MESSAGE1",
        timestamp=1_700_000_000_123,
    )
    event["payload"]["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = "1700000000"
    observed = []

    async def enabled(**kwargs):
        observed.append(("registry", kwargs))
        return True

    async def sync(incoming):
        observed.append(("sync", incoming))
        return True

    async def forbidden_ai(*_args, **_kwargs):
        raise AssertionError("Phone-origin outgoing messages must not enter customer AI routing")

    monkeypatch.setattr(api_module.whatsapp_registry_service, "observe_inbound", enabled)
    monkeypatch.setattr(api_module.support_service, "sync_linked_device_outbound", sync)
    monkeypatch.setattr(main_module, "process_whatsapp_payload", forbidden_ai)
    response = request("POST", "/api/whatsapp/linked-device/events", payload=event)
    assert response.status_code == 200 and response.json() == {"status": "applied"}
    assert observed[0][1]["phone_number_id"] == "90001"
    assert observed[1][1].message_id == event["message_id"]

    mismatched = json.loads(json.dumps(event))
    mismatched["sender_phone"] = "96172222222"
    assert request("POST", "/api/whatsapp/linked-device/events", payload=mismatched).status_code == 400
    assert len(observed) == 2

    async def pending(_incoming):
        return False

    monkeypatch.setattr(api_module.support_service, "sync_linked_device_outbound", pending)
    assert request("POST", "/api/whatsapp/linked-device/events", payload=event).status_code == 503


def test_inbound_tombstone_bypasses_customer_ai_and_rejects_content(linked_config, monkeypatch):
    import app.main as main_module

    event = {
        "event_id": "tombstone-1", "type": "message_tombstone",
        "line_id": "90001", "message_id": "ld:90001:REVOKED1",
        "sender_phone": "96171111111", "from_me": False,
        "timestamp": 1_700_000_000_000,
        "deleted_timestamp": 1_700_000_001_000,
    }
    observed = []

    async def enabled(**kwargs):
        observed.append(("registry", kwargs))
        return True

    async def sync(payload):
        observed.append(("tombstone", payload))
        return True

    async def forbidden_ai(*_args, **_kwargs):
        raise AssertionError("A deleted customer message must not enter the AI flow")

    monkeypatch.setattr(api_module.whatsapp_registry_service, "observe_inbound", enabled)
    monkeypatch.setattr(api_module.support_service, "sync_linked_device_tombstone", sync)
    monkeypatch.setattr(main_module, "process_whatsapp_payload", forbidden_ai)
    response = request("POST", "/api/whatsapp/linked-device/events", payload=event)
    assert response.status_code == 200 and response.json() == {"status": "applied"}
    assert observed[0][1]["phone_number_id"] == "90001"
    assert observed[1][1]["deleted_timestamp"] == event["deleted_timestamp"]
    assert request("POST", "/api/whatsapp/linked-device/events", payload={**event, "body": "secret"}).status_code == 400
    assert len(observed) == 2


def test_inbound_delete_without_support_row_acks_after_tombstone(linked_config, monkeypatch):
    event = {
        "event_id": "revoke-1", "type": "message_deleted_for_everyone",
        "line_id": "90001", "message_id": "ld:90001:REVOKED1",
        "sender_phone": "96171111111", "from_me": False,
        "timestamp": 1_700_000_001_000,
    }

    async def missing(_payload):
        return False

    monkeypatch.setattr(api_module.support_service, "apply_linked_device_change", missing)
    response = request("POST", "/api/whatsapp/linked-device/events", payload=event)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "message_not_in_support"}
    assert request("POST", "/api/whatsapp/linked-device/events", payload={**event, "from_me": True}).status_code == 503


def test_linked_outgoing_confirmation_reconciles_original_dashboard_row(linked_config, monkeypatch):
    event = {
        "event_id": "confirmation-1", "type": "outgoing_confirmation",
        "line_id": "90001", "from_me": True,
        "message_id": "ld:90001:SENT1", "client_message_id": "chat-start:request-1",
        "sender_phone": "96171111111", "timestamp": 1_700_000_000_123,
    }
    seen = []

    async def confirm(payload):
        seen.append(payload)
        return True

    monkeypatch.setattr(api_module.support_service, "confirm_linked_device_outgoing", confirm)
    response = request("POST", "/api/whatsapp/linked-device/events", payload=event)
    assert response.status_code == 200 and response.json() == {"status": "applied"}
    assert seen[0]["client_message_id"] == "chat-start:request-1"

    invalid = dict(event, sender_phone="not-a-phone")
    assert request("POST", "/api/whatsapp/linked-device/events", payload=invalid).status_code == 400

    async def pending(_payload):
        return False

    monkeypatch.setattr(api_module.support_service, "confirm_linked_device_outgoing", pending)
    assert request("POST", "/api/whatsapp/linked-device/events", payload=event).status_code == 503


def test_linked_inbound_does_not_create_a_synthetic_meta_account(linked_config, monkeypatch):
    import app.main as main_module

    observed = []

    async def disabled(**kwargs):
        observed.append(kwargs)
        return False

    monkeypatch.setattr(main_module.whatsapp_registry_service, "observe_inbound", disabled)
    payload = message_event()["payload"]
    payload["entry"][0]["id"] = "90001"
    response = asyncio.run(main_module.process_whatsapp_payload(payload, source="linked_device"))
    assert response.status_code == 200
    assert observed[0]["phone_number_id"] == "90001"
    assert observed[0]["waba_id"] is None


def test_linked_callback_disabled_route_and_size_bound(linked_config, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_linked_device_enabled", False)
    response = request("POST", "/api/whatsapp/linked-device/events", payload=message_event())
    assert response.json() == {"status": "ignored", "reason": "inactive_transport"}
    assert request("POST", "/api/whatsapp/linked-device/events", content=b"x" * (129 * 1024)).status_code == 413


def test_linked_mutation_callback_waits_for_original_and_preserves_event(linked_config, monkeypatch):
    event = {
        "event_id": "changed-1", "type": "message_updated", "line_id": "90001",
        "message_id": "ld:90001:MESSAGE1", "body": "corrected", "timestamp": 1_800_000_000_123,
        "from_me": False, "sender_phone": "96171111111",
    }
    calls = []

    async def missing(payload):
        calls.append(payload)
        return False

    monkeypatch.setattr(api_module.support_service, "apply_linked_device_change", missing)
    response = request("POST", "/api/whatsapp/linked-device/events", payload=event)
    assert response.status_code == 503
    assert calls[0]["body"] == "corrected" and calls[0]["timestamp"] == event["timestamp"]

    async def applied(payload):
        return True

    monkeypatch.setattr(api_module.support_service, "apply_linked_device_change", applied)
    assert request("POST", "/api/whatsapp/linked-device/events", payload=event).json() == {"status": "applied"}


def test_linked_status_is_manager_only_and_filters_accounts(linked_config, monkeypatch):
    calls = []

    async def status():
        calls.append(True)
        return {"lines": [
            {"line_id": "90001", "status": "qr", "qr_data_url": "data:image/png;base64,test"},
            {"line_id": "90002", "status": "qr", "qr_data_url": "private-other-account"},
        ]}

    monkeypatch.setattr(api_module.linked_device_client, "get_status", status)
    path = "/api/support/whatsapp-linked-device/status"
    assert request("GET", path, role="agent").status_code == 403
    assert calls == []
    payload = request("GET", path).json()
    assert len(payload["lines"]) == 1 and payload["lines"][0]["line_id"] == "90001"
    assert payload["lines"][0]["active_transport"] == "linked_device"
    assert "private-other-account" not in str(payload)


def test_linked_disconnect_requires_line_removed_from_app(linked_config, monkeypatch):
    from app.whatsapp_registry import whatsapp_registry_service

    async def enabled(_line):
        return True

    async def must_not_disconnect(_line):
        raise AssertionError("An enabled line cannot be unlinked")

    monkeypatch.setattr(whatsapp_registry_service, "is_number_enabled", enabled)
    monkeypatch.setattr(api_module.linked_device_client, "disconnect", must_not_disconnect)
    assert request("POST", "/api/support/whatsapp-linked-device/lines/90001/disconnect").status_code == 409


@pytest.fixture
def allocation_bridge(linked_config, monkeypatch):
    from app.whatsapp_registry import whatsapp_registry_service

    state = {"calls": [], "audit": [], "registry": [], "lines": {"90001": "connected"}, "requests": {}, "fail": None}
    manifest_path = Path(settings.whatsapp_linked_device_data_directory) / "lines.json"

    async def registry(agent):
        assert agent.is_supervisor
        configured = api_module.configured_linked_device_lines()
        state["registry"] = [{"phone_number_id": "90002", "source": "LEGACY_ENV", "is_enabled": True}, *[
            {"phone_number_id": line, "source": "LINKED_DEVICE", "is_enabled": line == "90001"} for line in configured
        ]]
        return {"accounts": [], "numbers": list(state["registry"])}

    async def add(request_id, expected_phone="", *, reserved_line_ids=None):
        state["calls"].append(("add", request_id, expected_phone, reserved_line_ids))
        assert reserved_line_ids == ["90001", "90002"] or reserved_line_ids == ["90001", "90002", "90003"]
        created = request_id not in state["requests"]
        if created:
            state["requests"][request_id] = {"line_id": str(90003 + len(state["requests"])), "expected_phone": expected_phone}
        line_id = state["requests"][request_id]["line_id"]
        state["lines"].setdefault(line_id, "disconnected")
        manifest_path.write_text(json.dumps({"version": 1, "lines": {entry["line_id"]: entry["expected_phone"] for entry in state["requests"].values()}, "requests": state["requests"]}))
        if state["fail"] == "add_uncertain":
            state["fail"] = None
            raise api_module.WhatsAppSendError("registration response lost", delivery_uncertain=True)
        return {"line_id": line_id, "status": state["lines"][line_id], "created": created}

    async def connect(line):
        state["calls"].append(("connect", line))
        if state["fail"] == "connect":
            state["fail"] = None
            raise api_module.WhatsAppSendError("pairing unavailable", delivery_uncertain=False)
        state["lines"][line] = "qr"
        return {"line_id": line, "status": "qr"}

    async def status():
        return {"lines": [{"line_id": line, "status": status, "qr_data_url": "data:image/png;base64,test" if status == "qr" else None} for line, status in state["lines"].items()]}

    async def audit(**kwargs):
        state["audit"].append(kwargs)

    monkeypatch.setattr(whatsapp_registry_service, "list_payload", registry)
    monkeypatch.setattr(whatsapp_registry_service, "_audit", audit)
    monkeypatch.setattr(api_module.linked_device_client, "add_line", add)
    monkeypatch.setattr(api_module.linked_device_client, "connect", connect)
    monkeypatch.setattr(api_module.linked_device_client, "get_status", status)
    return state


def test_add_number_uses_durable_uuid_reserves_cloud_ids_and_stays_disabled(allocation_bridge):
    state = allocation_bridge
    request_id = str(uuid4())
    response = request("POST", "/api/support/whatsapp-linked-device/lines", payload={"request_id": request_id, "expected_phone": "+961 (70) 123-456"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["added_line_id"] == "90003"
    assert state["calls"] == [("add", request_id, "96170123456", ["90001", "90002"]), ("connect", "90003")]
    assert any(line["line_id"] == "90003" and line["status"] == "qr" for line in payload["lines"])
    assert state["lines"]["90001"] == "connected"
    numbers = {row["phone_number_id"]: row for row in payload["registry"]["numbers"]}
    assert numbers["90001"]["is_enabled"] and numbers["90002"]["is_enabled"]
    assert not numbers["90003"]["is_enabled"]
    assert state["audit"] == [{"action": "LINKED_DEVICE_LINE_CREATED", "actor_staff_id": 7, "phone_number_id": "90003", "details": {"request_id": request_id}}]


def test_add_number_retry_preserves_uuid_and_does_not_duplicate_audit(allocation_bridge):
    state = allocation_bridge
    body = {"request_id": str(uuid4())}
    for _attempt in range(2):
        response = request("POST", "/api/support/whatsapp-linked-device/lines", payload=body)
        assert response.status_code == 200 and response.json()["added_line_id"] == "90003"
    assert len(state["audit"]) == 1 and len(state["requests"]) == 1
    assert [call[1] for call in state["calls"] if call[0] == "add"] == [body["request_id"], body["request_id"]]


def test_lost_allocation_response_reuses_existing_registration_on_retry(allocation_bridge):
    state = allocation_bridge
    state["fail"] = "add_uncertain"
    body = {"request_id": str(uuid4())}
    failed = request("POST", "/api/support/whatsapp-linked-device/lines", payload=body)
    assert failed.status_code == 503 and not any(call[0] == "connect" for call in state["calls"])
    assert "90003" in api_module.configured_linked_device_lines()
    retry = request("POST", "/api/support/whatsapp-linked-device/lines", payload=body)
    assert retry.status_code == 200 and retry.json()["added_line_id"] == "90003"
    assert len(state["requests"]) == 1


def test_pairing_failure_preserves_registered_disabled_row_for_existing_pair(allocation_bridge):
    state = allocation_bridge
    state["fail"] = "connect"
    body = {"request_id": str(uuid4()), "expected_phone": "96170123456"}
    failed = request("POST", "/api/support/whatsapp-linked-device/lines", payload=body)
    assert failed.status_code == 503 and "use Pair to retry" in failed.json()["detail"]
    registered = next(row for row in state["registry"] if row["phone_number_id"] == "90003")
    assert not registered["is_enabled"] and registered["source"] == "LINKED_DEVICE"
    retry = request("POST", "/api/support/whatsapp-linked-device/lines/90003/connect")
    assert retry.status_code == 200
    assert len([call for call in state["calls"] if call[0] == "add"]) == 1
    assert state["lines"]["90001"] == "connected"


@pytest.mark.parametrize("role", ["agent", "driver"])
def test_add_number_is_manager_only(allocation_bridge, role):
    response = request("POST", "/api/support/whatsapp-linked-device/lines", payload={"request_id": str(uuid4())}, role=role)
    assert response.status_code == 403 and allocation_bridge["calls"] == []


@pytest.mark.parametrize("body", [{}, {"request_id": "invalid"}, {"request_id": str(uuid4()), "expected_phone": "not-a-phone"}, {"request_id": str(uuid4()), "expected_phone": "123"}, {"request_id": str(uuid4()), "line_id": "90003"}])
def test_add_number_validates_uuid_phone_and_prevents_client_chosen_ids(allocation_bridge, body):
    assert request("POST", "/api/support/whatsapp-linked-device/lines", payload=body).status_code == 422
    assert allocation_bridge["calls"] == []


def test_add_number_enforces_actual_csrf_dependency(allocation_bridge):
    async def run():
        app = FastAPI()
        app.include_router(router)

        async def agent():
            return SupportAgent(id="7", name="Admin", role="admin", csrf_hash=hashlib.sha256(b"correct-csrf").hexdigest())

        app.dependency_overrides[get_current_agent] = agent
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = "/api/support/whatsapp-linked-device/lines"
            body = {"request_id": str(uuid4())}
            for headers in ({}, {"X-CSRF-Token": "wrong"}):
                response = await client.post(path, json=body, headers=headers)
                assert response.status_code == 403
            assert allocation_bridge["calls"] == []
            response = await client.post(path, json=body, headers={"X-CSRF-Token": "correct-csrf"})
            assert response.status_code == 200
    asyncio.run(run())


def test_status_allows_adding_first_number_before_any_routes_exist(linked_config, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_linked_device_lines", {})

    async def status():
        return {"lines": []}

    monkeypatch.setattr(api_module.linked_device_client, "get_status", status)
    response = request("GET", "/api/support/whatsapp-linked-device/status")
    assert response.status_code == 200 and response.json()["configured"] is True and response.json()["lines"] == []


def test_concurrent_additions_serialize_registry_snapshots_without_touching_existing_accounts(allocation_bridge, monkeypatch):
    async def run():
        original_add = api_module.linked_device_client.add_line
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def delayed_add(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return await original_add(*args, **kwargs)

        monkeypatch.setattr(api_module.linked_device_client, "add_line", delayed_add)
        app = FastAPI()
        app.include_router(router)

        async def agent():
            return SupportAgent(id="7", name="Admin", role="admin")

        app.dependency_overrides[require_support_csrf] = agent
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = "/api/support/whatsapp-linked-device/lines"
            first = asyncio.create_task(client.post(path, json={"request_id": str(uuid4())}))
            await entered.wait()
            second = asyncio.create_task(client.post(path, json={"request_id": str(uuid4())}))
            await asyncio.sleep(0)
            assert calls == 1
            release.set()
            responses = await asyncio.gather(first, second)
        assert all(response.status_code == 200 for response in responses)
        assert {response.json()["added_line_id"] for response in responses} == {"90003", "90004"}
        additions = [call for call in allocation_bridge["calls"] if call[0] == "add"]
        assert additions[0][3] == ["90001", "90002"]
        assert additions[1][3] == ["90001", "90002", "90003"]
        assert allocation_bridge["lines"]["90001"] == "connected"
    asyncio.run(run())


def test_registry_failure_stops_allocation_before_any_bridge_changes(allocation_bridge, monkeypatch):
    from app.whatsapp_registry import WhatsAppRegistryError, whatsapp_registry_service

    async def broken_registry(_agent):
        raise WhatsAppRegistryError("The number registry is unavailable.")

    monkeypatch.setattr(whatsapp_registry_service, "list_payload", broken_registry)
    response = request("POST", "/api/support/whatsapp-linked-device/lines", payload={"request_id": str(uuid4())})
    assert response.status_code == 503 and allocation_bridge["calls"] == []


@pytest.fixture
def removal_bridge(linked_config, monkeypatch):
    from app.whatsapp_registry import whatsapp_registry_service

    state = {"calls": [], "enabled": True, "tickets": [23], "fail": None}

    async def prepare(agent, line_id, *, close_active_tickets):
        from app.whatsapp_registry import WhatsAppRegistryConflictError
        state["calls"].append(("prepare", line_id, close_active_tickets, agent.id))
        if state["tickets"] and not close_active_tickets:
            raise WhatsAppRegistryConflictError("Confirm closing active conversations before unlinking.")
        state["enabled"] = False
        return len(state["tickets"])

    async def remove(line_id):
        state["calls"].append(("remove", line_id))
        if state["fail"] == "native":
            raise api_module.WhatsAppSendError("Unlink confirmation unavailable", delivery_uncertain=True)
        if state["fail"] == "unconfirmed":
            return {"line_id": line_id, "removed": True, "unlinked": False}
        manifest = Path(settings.whatsapp_linked_device_data_directory) / "lines.json"
        manifest.write_text(json.dumps({"version": 1, "lines": {}, "requests": {}, "removed_line_ids": [line_id]}))
        return {"line_id": line_id, "removed": True, "unlinked": True}

    async def finish(agent, line_id, *, close_active_tickets):
        from app.whatsapp_registry import WhatsAppRegistryError
        state["calls"].append(("finish", line_id, agent.id))
        if state["fail"] == "database":
            raise WhatsAppRegistryError("Database archive unavailable; retry removal.")
        closed = len(state["tickets"])
        state["tickets"].clear()
        return closed

    async def registry(_agent):
        return {"accounts": [], "numbers": []}

    async def status():
        if state["fail"] == "status":
            raise api_module.WhatsAppSendError("Bridge status unavailable", delivery_uncertain=False)
        return {"lines": []}

    monkeypatch.setattr(whatsapp_registry_service, "prepare_linked_device_removal", prepare)
    monkeypatch.setattr(whatsapp_registry_service, "finish_linked_device_removal", finish)
    monkeypatch.setattr(whatsapp_registry_service, "list_payload", registry)
    monkeypatch.setattr(api_module.linked_device_client, "remove_line", remove)
    monkeypatch.setattr(api_module.linked_device_client, "get_status", status)
    return state


def test_remove_requires_explicit_active_conversation_confirmation(removal_bridge):
    response = request("DELETE", "/api/support/whatsapp-linked-device/lines/90001", payload={})
    assert response.status_code == 409
    assert removal_bridge["calls"] == [("prepare", "90001", False, "7")]
    assert removal_bridge["enabled"] and removal_bridge["tickets"] == [23]


def test_remove_native_confirmation_precedes_archive_and_retired_retry_is_safe(removal_bridge):
    path = "/api/support/whatsapp-linked-device/lines/90001"
    response = request("DELETE", path, payload={"close_active_tickets": True})
    assert response.status_code == 200
    assert response.json()["removed_line_id"] == "90001" and response.json()["unlinked"]
    assert response.json()["closed_conversations"] == 1
    assert removal_bridge["calls"][:3] == [("prepare", "90001", True, "7"), ("remove", "90001"), ("finish", "90001", "7")]
    assert api_module.get_transport("90001") == "removed"
    assert request("DELETE", path, payload={}).json()["closed_conversations"] == 0


@pytest.mark.parametrize("failure", ["native", "unconfirmed"])
def test_remove_does_not_close_conversations_when_whatsapp_unlink_is_unconfirmed(removal_bridge, failure):
    removal_bridge["fail"] = failure
    response = request("DELETE", "/api/support/whatsapp-linked-device/lines/90001", payload={"close_active_tickets": True})
    assert response.status_code == 503
    assert not removal_bridge["enabled"]
    assert removal_bridge["tickets"] == [23]
    assert not any(call[0] == "finish" for call in removal_bridge["calls"])


def test_remove_retries_archiving_after_database_failure_without_repairing(removal_bridge):
    removal_bridge["fail"] = "database"
    path = "/api/support/whatsapp-linked-device/lines/90001"
    assert request("DELETE", path, payload={"close_active_tickets": True}).status_code == 503
    assert api_module.get_transport("90001") == "removed" and removal_bridge["tickets"] == [23]
    removal_bridge["fail"] = None
    assert request("DELETE", path, payload={"close_active_tickets": True}).status_code == 200
    assert removal_bridge["tickets"] == []


def test_removed_callback_is_ignored_and_never_reopens_conversations(removal_bridge):
    assert request("DELETE", "/api/support/whatsapp-linked-device/lines/90001", payload={"close_active_tickets": True}).status_code == 200
    response = request("POST", "/api/whatsapp/linked-device/events", payload=message_event())
    assert response.json() == {"status": "ignored", "reason": "removed_line"}


def test_remove_success_survives_followup_status_outage(removal_bridge):
    removal_bridge["fail"] = "status"
    response = request("DELETE", "/api/support/whatsapp-linked-device/lines/90001", payload={"close_active_tickets": True})
    assert response.status_code == 200 and response.json()["unlinked"] and response.json()["status_unavailable"]


@pytest.mark.parametrize("role", ["agent", "driver"])
def test_remove_is_manager_only(removal_bridge, role):
    assert request("DELETE", "/api/support/whatsapp-linked-device/lines/90001", payload={"close_active_tickets": True}, role=role).status_code == 403
    assert removal_bridge["calls"] == []


def test_remove_rejects_cloud_ids_and_ambiguous_confirmation(removal_bridge):
    assert request("DELETE", "/api/support/whatsapp-linked-device/lines/123456789", payload={"close_active_tickets": True}).status_code == 404
    for body in ({"close_active_tickets": "yes"}, {"force": True}):
        assert request("DELETE", "/api/support/whatsapp-linked-device/lines/90001", payload=body).status_code == 422
    assert removal_bridge["calls"] == []


def test_remove_enforces_actual_csrf_dependency(removal_bridge):
    async def run():
        app = FastAPI()
        app.include_router(router)

        async def agent():
            return SupportAgent(id="7", name="Admin", role="admin", csrf_hash=hashlib.sha256(b"correct-csrf").hexdigest())

        app.dependency_overrides[get_current_agent] = agent
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = "/api/support/whatsapp-linked-device/lines/90001"
            body = {"close_active_tickets": True}
            for headers in ({}, {"X-CSRF-Token": "wrong"}):
                assert (await client.request("DELETE", path, json=body, headers=headers)).status_code == 403
            assert removal_bridge["calls"] == []
            assert (await client.request("DELETE", path, json=body, headers={"X-CSRF-Token": "correct-csrf"})).status_code == 200
    asyncio.run(run())


def test_linked_message_status_updates_ticks_and_drops_unknown_receipts(linked_config, monkeypatch):
    import time as time_module

    now_ms = int(time_module.time() * 1000)
    event = {
        "event_id": "status-1", "type": "message_status", "line_id": "90001", "from_me": True,
        "message_id": "ld:90001:SENT1", "status": "read",
        "sender_phone": "96171111111", "timestamp": now_ms,
    }
    seen = []
    found = True

    async def apply(**kwargs):
        seen.append(kwargs)
        return found

    monkeypatch.setattr(api_module.support_service, "apply_delivery_status", apply)
    endpoint = "/api/whatsapp/linked-device/events"
    response = request("POST", endpoint, payload=event)
    assert response.status_code == 200 and response.json() == {"status": "applied"}
    assert seen[0] == {
        "whatsapp_message_id": "ld:90001:SENT1", "status": "read", "timestamp_ms": now_ms,
        "business_phone_number_id": "90001", "phone_number": "96171111111",
    }

    found = False
    # A fresh receipt for a message that is not saved yet is retried by the bridge.
    assert request("POST", endpoint, payload=event).status_code == 503
    # An old one is dropped, so receipts for messages support never stored stop retrying.
    stale = dict(event, timestamp=now_ms - 10 * 60_000)
    response = request("POST", endpoint, payload=stale)
    assert response.status_code == 200 and response.json()["status"] == "ignored"

    for invalid in (dict(event, status="played"), dict(event, from_me=False), dict(event, message_id="wamid.cloud")):
        assert request("POST", endpoint, payload=invalid).status_code == 400


def test_inbound_snapshot_uses_transcript_sync_and_never_customer_ai(linked_config, monkeypatch):
    from unittest.mock import AsyncMock
    import app.main as main_module
    event = message_event()
    event.update(type='message_snapshot', from_me=False, sender_phone='96171111111',
                 message_id='ld:90001:MESSAGE1', timestamp=1_700_000_000_000)
    node = event['payload']['entry'][0]['changes'][0]['value']['messages'][0]
    node.update(type='unsupported', timestamp='1700000000')
    sync = AsyncMock(return_value=True)
    process = AsyncMock(side_effect=AssertionError('Snapshots must never send automatic replies'))
    monkeypatch.setattr(api_module.whatsapp_registry_service, 'observe_inbound', AsyncMock(return_value=True))
    monkeypatch.setattr(api_module.support_service, 'sync_linked_device_snapshot', sync)
    monkeypatch.setattr(main_module, 'process_whatsapp_payload', process)
    assert request('POST', '/api/whatsapp/linked-device/events', payload=event).status_code == 200
    sync.assert_awaited_once()
    process.assert_not_awaited()
    event['from_me'] = True
    assert request('POST', '/api/whatsapp/linked-device/events', payload=event).status_code == 400
    assert sync.await_count == 1


def test_missing_phone_reaction_retries_for_target_recovery_then_acknowledges_absence(linked_config, monkeypatch):
    from unittest.mock import AsyncMock
    now = 1_800_000_000
    monkeypatch.setattr(api_module.time, 'time', lambda: now)
    monkeypatch.setattr(api_module.whatsapp_registry_service, 'observe_inbound', AsyncMock(return_value=True))
    sync = AsyncMock(return_value=False)
    monkeypatch.setattr(api_module.support_service, 'sync_linked_device_outbound', sync)
    event = message_event()
    event.update(type='outgoing_message', from_me=True, sender_phone='96171111111', message_id='ld:90001:MESSAGE1')
    node = event['payload']['entry'][0]['changes'][0]['value']['messages'][0]
    node.update(type='reaction', reaction={'message_id': 'ld:90001:MISSING', 'emoji': '👍'})
    for age, expected in [(60, 503), (86_400, 200)]:
        event['timestamp'] = (now-age)*1000
        node['timestamp'] = str(now-age)
        response = request('POST', '/api/whatsapp/linked-device/events', payload=event)
        assert response.status_code == expected
    assert response.json()['reason'] == 'reaction_target_not_in_support'
    # Actual message content is never discarded just because it is old.
    node['type'] = 'text'
    assert request('POST', '/api/whatsapp/linked-device/events', payload=event).status_code == 503
