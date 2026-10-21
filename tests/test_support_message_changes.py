from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.support_auth import SupportAgent
from app.support_repository import SupportConflictError, SupportNotFoundError, SupportRepository
from app.support_service import SupportForbiddenError, SupportSendError, SupportService, SupportValidationError, _serialize_support_messages
import app.support_service as service_module


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.result: dict[str, Any] | None = None
        self.rowcount = 0

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, query: str, params: tuple[Any, ...]) -> None:
        row = self.connection.row
        query = " ".join(query.split())
        self.rowcount = 1
        if query.startswith("SELECT * FROM support_ticket_messages"):
            self.result = dict(row) if params[0] == row["id"] and (len(params) == 1 or params[1] == row["ticket_id"]) else None
        elif query.startswith("SELECT * FROM support_message_change_operations"):
            operation = self.connection.operations.get(params[0])
            self.result = dict(operation) if operation else None
        elif query.startswith("INSERT IGNORE INTO support_message_user_deletions"):
            self.connection.hidden.add((params[0], params[1]))
        elif query.startswith("INSERT INTO support_message_change_operations"):
            self.connection.operations[params[0]] = dict(zip(("operation_id", "message_id", "base_version", "user_id", "action", "content"), params))
            self.connection.operations[params[0]].update(status="PENDING", lease_until=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=120))
        elif query.startswith("INSERT IGNORE INTO support_message_change_events"):
            self.rowcount = 0 if params[0] in self.connection.events else 1
            self.connection.events.add(params[0])
        elif "SET status = 'PENDING'" in query:
            self.connection.operations[params[0]].update(status="PENDING", lease_until=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=120))
        elif "SET status = 'CONFIRMED'" in query:
            self.connection.operations[params[1]].update(status="CONFIRMED", provider_timestamp=params[0], lease_until=None)
        elif "SET status = %s" in query:
            operation = self.connection.operations.get(params[1])
            if operation and operation["status"] != "CONFIRMED":
                operation.update(status=params[0], lease_until=None)
        elif "SET pending_change_operation_id = %s" in query:
            row["pending_change_operation_id"] = params[0]
        elif "SET pending_change_operation_id = NULL" in query:
            if row.get("pending_change_operation_id") == params[1]:
                row["pending_change_operation_id"] = None
        elif "SET body = %s" in query:
            row["body"] = params[0]
            row["edited_at"] = params[1]
            row["whatsapp_change_timestamp"] = params[2]
            row["version"] += params[3]
        elif "SET body = ''" in query:
            row["body"] = ""
            row["deleted_for_everyone_at"] = params[0]
            row["deleted_by"] = params[1]
            row["whatsapp_change_timestamp"] = max(row.get("whatsapp_change_timestamp") or 0, params[2])
            for field in ("provider_media_id", "provider_media_mime_type", "provider_media_filename", "location_latitude", "location_longitude", "location_name", "location_address", "error_message"):
                row[field] = None
            row["version"] += 1
        elif query.startswith("DELETE FROM support_message_reactions"):
            self.connection.reactions = []
        elif "SET content = NULL" in query:
            for operation in self.connection.operations.values():
                if operation["message_id"] == params[0]:
                    operation["content"] = None
        else:
            raise AssertionError(query)

    async def fetchone(self) -> dict[str, Any] | None:
        return self.result


class FakeConnection:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.hidden: set[tuple[int, str]] = set()
        self.commits = 0
        self.rollbacks = 0
        self.operations: dict[str, dict[str, Any]] = {}
        self.events: set[str] = set()
        self.reactions: list[str] = ["👍"]
        self.snapshot: Any = None

    def cursor(self, *_args: Any) -> FakeCursor:
        return FakeCursor(self)

    async def begin(self) -> None:
        self.snapshot = copy.deepcopy((self.row, self.operations, self.events, self.hidden, self.reactions))

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self.snapshot:
            self.row, self.operations, self.events, self.hidden, self.reactions = self.snapshot


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_args: Any) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def message(**overrides: Any) -> dict[str, Any]:
    return {"id": 5, "ticket_id": 42, "sender_type": "AGENT", "sender_id": "7",
            "message_type": "text", "send_status": "SENT", "body": "original",
            "version": 1, "deleted_for_everyone_at": None, "direction": "OUTBOUND",
            "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "whatsapp_message_id": "ld:90001:ORIGINAL", **overrides}


def test_repository_requires_remote_confirmation_and_preserves_owner_version() -> None:
    async def run() -> None:
        connection = FakeConnection(message())
        repository = SupportRepository()
        repository._pool = FakePool(connection)  # type: ignore[assignment]
        with pytest.raises(SupportConflictError, match="Remote WhatsApp confirmation"):
            await repository.change_message(5, ticket_id=42, user_id="7", action="edit", content="bad", version=1)
        with pytest.raises(SupportConflictError, match="original sender"):
            await repository.claim_message_change(5, ticket_id=42, user_id="8", action="edit", content="bad", version=1, operation_id="wrong")
        assert connection.row["body"] == "original"
        claim = await repository.claim_message_change(5, ticket_id=42, user_id="7", action="edit", content="new", version=1, operation_id="edit")
        assert claim["needs_remote"] and connection.row["body"] == "original"
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
        edited = await repository.confirm_message_change(5, operation_id="edit", timestamp=timestamp)
        assert edited["body"] == "new" and edited["version"] == 2 and edited["edited_at"]
        retry = await repository.claim_message_change(5, ticket_id=42, user_id="7", action="edit", content="new", version=1, operation_id="edit")
        assert not retry["needs_remote"] and retry["message"]["version"] == 2
        with pytest.raises(SupportConflictError, match="another session"):
            await repository.claim_message_change(5, ticket_id=42, user_id="7", action="edit", content="stale", version=1, operation_id="stale")
        await repository.claim_message_change(5, ticket_id=42, user_id="7", action="everyone", content=None, version=2, operation_id="delete")
        deleted = await repository.confirm_message_change(5, operation_id="delete", timestamp=timestamp+1)
        assert deleted["body"] == "" and deleted["deleted_for_everyone_at"] and deleted["version"] == 3
        with pytest.raises(SupportConflictError, match="already deleted"):
            await repository.claim_message_change(5, ticket_id=42, user_id="7", action="edit", content="late", version=3, operation_id="late")
        assert connection.reactions == [] and connection.operations["edit"]["content"] is None
    asyncio.run(run())


def test_delete_for_me_only_writes_requester_visibility() -> None:
    async def run() -> None:
        connection = FakeConnection(message())
        repository = SupportRepository()
        repository._pool = FakePool(connection)  # type: ignore[assignment]
        await repository.change_message(5, ticket_id=42, user_id="8", action="me")
        assert connection.hidden == {(5, "8")}
        assert connection.row["body"] == "original"
        assert connection.row["version"] == 1
    asyncio.run(run())


def test_service_access_and_realtime_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        owner = SupportAgent(id="7", name="Owner", role="admin")
        outsider = SupportAgent(id="8", name="Other", role="agent")
        events: list[tuple[str, dict[str, Any]]] = []

        async def get_ticket(_ticket_id: int) -> dict[str, Any]:
            return {"id": 42, "status": "IN_PROGRESS", "current_assignee_id": "7"}

        async def get_message(_message_id: int) -> dict[str, Any]:
            return message(whatsapp_message_id="wamid.old-cloud-message")

        async def participated(_ticket_id: int, _user_id: str) -> bool:
            return False

        async def hidden(_message_id: int, _user_id: str) -> bool:
            return False

        async def change(_message_id: int, **kwargs: Any) -> dict[str, Any]:
            return message(body="changed", version=2, edited_at="now")

        async def publish(event_type: str, **payload: Any) -> None:
            events.append((event_type, payload))

        monkeypatch.setattr(service_module.support_repository, "get_ticket", get_ticket)
        monkeypatch.setattr(service_module.support_repository, "get_message", get_message)
        monkeypatch.setattr(service_module.support_repository, "agent_has_participated", participated)
        monkeypatch.setattr(service_module.support_repository, "message_hidden_for_user", hidden)
        monkeypatch.setattr(service_module.support_repository, "change_message", change)
        monkeypatch.setattr(service_module, "publish_support_event", publish)

        with pytest.raises(SupportForbiddenError):
            await service.change_message(42, 5, agent=outsider, action="edit", content="x", version=1)
        with pytest.raises(SupportForbiddenError, match="original sender"):
            await service.change_message(42, 5, agent=SupportAgent(id="8", name="Supervisor", role="admin"), action="edit", content="x", version=1)
        with pytest.raises(SupportConflictError, match="cannot edit or recall"):
            await service.change_message(42, 5, agent=owner, action="edit", content="changed", version=1)
        with pytest.raises(SupportConflictError, match="cannot edit or recall"):
            await service.change_message(42, 5, agent=owner, action="everyone")
        assert events == []
        assert not await service.can_receive_support_event({"type": "message_deleted_for_me", "ticket_id": 42, "user_id": "8"}, owner)
        assert await service.can_receive_support_event({"type": "message_deleted_for_me", "ticket_id": 42, "user_id": "7"}, owner)
        private_result = await service.change_message(42, 5, agent=owner, action="me")
        assert private_result == {"id": 5, "deleted_for_me": True}
        assert events[-1][0] == "message_deleted_for_me"
        assert events[-1][1]["user_id"] == "7" and events[-1][1]["message"] is None
        async def now_hidden(_message_id: int, _user_id: str) -> bool:
            return True
        monkeypatch.setattr(service_module.support_repository, "message_hidden_for_user", now_hidden)
        assert not await service.can_receive_support_event(
            {"type": "outbound_message", "ticket_id": 42, "message_id": 5}, owner
        )
    asyncio.run(run())


def test_deleted_media_is_redacted_from_detail_and_reply_preview() -> None:
    deleted = message(message_type="image", body="secret caption", deleted_for_everyone_at="now",
                      provider_media_id="provider-secret", provider_media_filename="secret.png",
                      whatsapp_message_id="wa-5")
    reply = message(id=6, sender_type="CUSTOMER", sender_id=None, body="reply",
                    reply_to_whatsapp_message_id="wa-5")
    rows = _serialize_support_messages(42, [deleted, reply])
    assert rows[0]["body"] is None
    assert "media_url" not in rows[0]
    assert "provider_media_id" not in rows[0]
    assert rows[1]["reply_to"]["body"] == "Original message deleted"
    assert rows[1]["reply_to"]["deleted_for_everyone_at"]


def test_deleted_media_route_rejects_tombstones_and_private_deletions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Owner", role="admin")

        async def get_ticket(_ticket_id: int) -> dict[str, Any]:
            return {"id": 42}

        async def tombstone(_message_id: int) -> dict[str, Any]:
            return message(message_type="image", deleted_for_everyone_at="now")

        async def ordinary(_message_id: int) -> dict[str, Any]:
            return message(message_type="image")

        async def hidden(_message_id: int, _user_id: str) -> bool:
            return True

        monkeypatch.setattr(service_module.support_repository, "get_ticket", get_ticket)
        monkeypatch.setattr(service_module.support_repository, "get_message", tombstone)
        monkeypatch.setattr(service_module.support_repository, "message_hidden_for_user", hidden)
        with pytest.raises(SupportNotFoundError):
            await service.get_ticket_media(42, 5, agent)
        monkeypatch.setattr(service_module.support_repository, "get_message", ordinary)
        with pytest.raises(SupportNotFoundError):
            await service.get_ticket_media(42, 5, agent)
    asyncio.run(run())


def linked_service(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> tuple[SupportService, FakeConnection, list[Any]]:
    connection = FakeConnection(message(**overrides))
    repository = SupportRepository()
    repository._pool = FakePool(connection)  # type: ignore[assignment]
    events: list[Any] = []

    async def ticket(_ticket_id: int) -> dict[str, Any]:
        return {"id": 42, "status": "IN_PROGRESS", "current_assignee_id": "7", "business_phone_number_id": "90001", "whatsapp_phone": "96170123456"}

    async def by_provider(provider_id: str) -> dict[str, Any] | None:
        return dict(connection.row) if connection.row["whatsapp_message_id"] == provider_id else None

    async def hidden(message_id: int, user_id: str) -> bool:
        return (message_id, user_id) in connection.hidden

    async def publish(event_type: str, **payload: Any) -> None:
        events.append((event_type, payload))

    monkeypatch.setattr(repository, "get_ticket", ticket)
    monkeypatch.setattr(repository, "get_message_by_whatsapp_id", by_provider)
    monkeypatch.setattr(repository, "message_hidden_for_user", hidden)
    monkeypatch.setattr(service_module, "support_repository", repository)
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device" if line == "90001" else "cloud")
    monkeypatch.setattr(service_module.support_media_store, "delete_message_media", lambda *_args: None)
    return SupportService(), connection, events


def change_event(**overrides: Any) -> dict[str, Any]:
    return {"event_id": "event-edit", "type": "message_updated", "line_id": "90001", "message_id": "ld:90001:ORIGINAL", "body": "changed", "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000), "from_me": True, "sender_phone": "96170123456", **overrides}


@pytest.mark.parametrize("role", ["agent", "supervisor", "admin"])
@pytest.mark.parametrize("origin,status", [("CUSTOMER", "IN_PROGRESS"), ("STAFF", "ASSIGNED")])
@pytest.mark.parametrize("action", ["edit", "everyone"])
def test_every_staff_role_waits_for_remote_ack_to_change_own_message(
    monkeypatch: pytest.MonkeyPatch, role: str, origin: str, status: str, action: str,
) -> None:
    async def run() -> None:
        service, connection, events = linked_service(monkeypatch)
        calls: list[dict[str, Any]] = []
        remote_started = asyncio.Event()
        remote_confirmed = asyncio.Event()
        timestamp = change_event()["timestamp"]

        async def ticket(_ticket_id: int) -> dict[str, Any]:
            return {"id": 42, "status": status, "conversation_origin": origin,
                    "current_assignee_id": "7", "business_phone_number_id": "90001",
                    "whatsapp_phone": "96170123456", "active_key": "90001:96170123456"}

        async def remote(provider_action: str, **kwargs: Any) -> dict[str, Any]:
            assert provider_action == action
            calls.append(kwargs)
            assert connection.commits == 1  # claim committed; no open network transaction
            assert connection.row["body"] == "original" and connection.row["version"] == 1
            assert events == []
            remote_started.set()
            await remote_confirmed.wait()
            return {"confirmed": True, "id": kwargs["message_id"],
                    "operation_id": kwargs["operation_id"], "timestamp": timestamp}

        async def edit_remote(**kwargs: Any) -> dict[str, Any]:
            assert kwargs["body"] == "changed"
            return await remote("edit", **kwargs)

        async def delete_remote(**kwargs: Any) -> dict[str, Any]:
            assert "body" not in kwargs
            return await remote("everyone", **kwargs)

        monkeypatch.setattr(service_module.support_repository, "get_ticket", ticket)
        monkeypatch.setattr(service_module, "whatsapp_client", SimpleNamespace(edit_message=edit_remote, delete_message=delete_remote))
        owner = SupportAgent(id="7", name="Owner", role=role)
        change = asyncio.create_task(service.change_message(
            42, 5, agent=owner, action=action, content="changed", version=1,
        ))
        await asyncio.wait_for(remote_started.wait(), timeout=5)
        assert not change.done()
        assert connection.row["body"] == "original" and connection.row["version"] == 1
        assert connection.row["deleted_for_everyone_at"] is None and events == []
        operation = next(iter(connection.operations.values()))
        assert operation["status"] == "PENDING" and operation["user_id"] == owner.id

        remote_confirmed.set()
        result = await asyncio.wait_for(change, timeout=5)
        assert connection.row["version"] == 2
        assert result["body"] == ("changed" if action == "edit" else None)
        assert bool(connection.row["deleted_for_everyone_at"]) == (action == "everyone")
        assert operation["status"] == "CONFIRMED" and operation["provider_timestamp"] == timestamp
        assert connection.row["pending_change_operation_id"] is None
        assert connection.row["whatsapp_message_id"] == "ld:90001:ORIGINAL"
        assert len(calls) == 1 and calls[0]["message_id"] == "ld:90001:ORIGINAL"
        assert calls[0]["phone_number_id"] == "90001" and calls[0]["to"] == "96170123456"
        assert len(events) == 1
        assert events[0][0] == ("message_updated" if action == "edit" else "message_deleted_for_everyone")
        assert events[0][1]["message"]["version"] == 2
        assert "pending_change_operation_id" not in result
        # The original edit version, or repeat revoke, cannot create a second send.
        retry = await service.change_message(42, 5, agent=owner, action=action, content="changed", version=1)
        assert retry["version"] == 2 and len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("origin,status", [("CUSTOMER", "IN_PROGRESS"), ("STAFF", "ASSIGNED")])
def test_original_staff_sender_can_edit_repeatedly_in_either_conversation(
    monkeypatch: pytest.MonkeyPatch, origin: str, status: str,
) -> None:
    async def run() -> None:
        service, connection, events = linked_service(monkeypatch)
        owner = SupportAgent(id="7", name="Owner", role="agent")
        calls: list[str] = []
        timestamp = change_event()["timestamp"]

        async def ticket(_ticket_id: int) -> dict[str, Any]:
            return {"id": 42, "status": status, "conversation_origin": origin,
                    "current_assignee_id": "7", "business_phone_number_id": "90001",
                    "whatsapp_phone": "96170123456"}

        async def remote(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs["operation_id"])
            return {"confirmed": True, "id": kwargs["message_id"],
                    "operation_id": kwargs["operation_id"], "timestamp": timestamp + len(calls)}

        monkeypatch.setattr(service_module.support_repository, "get_ticket", ticket)
        monkeypatch.setattr(service_module, "whatsapp_client", SimpleNamespace(edit_message=remote))
        original_sent_at = connection.row["created_at"]
        for version, body in enumerate(("first edit", "second edit", "third edit"), start=1):
            result = await service.change_message(
                42, 5, agent=owner, action="edit", content=body, version=version,
            )
            assert result["body"] == body and result["version"] == version + 1
            assert result["can_edit"] and result["can_delete_for_everyone"]
        assert connection.row["body"] == "third edit"
        assert connection.row["created_at"] == original_sent_at
        assert len(set(calls)) == 3 and len(events) == 3
    asyncio.run(run())


@pytest.mark.parametrize("origin,action", [("CUSTOMER", "edit"), ("STAFF", "everyone")])
def test_original_sender_can_change_own_message_after_ticket_reassignment(
    monkeypatch: pytest.MonkeyPatch, origin: str, action: str,
) -> None:
    async def run() -> None:
        service, connection, _events = linked_service(monkeypatch)
        owner = SupportAgent(id="7", name="Former owner", role="agent")
        ticket_row = {"id": 42, "status": "CLOSED", "conversation_origin": origin,
                      "current_assignee_id": "8", "business_phone_number_id": "90001",
                      "whatsapp_phone": "96170123456"}
        calls: list[dict[str, Any]] = []

        async def ticket(_ticket_id: int) -> dict[str, Any]:
            return ticket_row

        async def no_audit(_ticket_id: int, _agent_id: str) -> bool:
            return False

        async def remote(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {"confirmed": True, "id": kwargs["message_id"],
                    "operation_id": kwargs["operation_id"], "timestamp": change_event()["timestamp"]}

        monkeypatch.setattr(service_module.support_repository, "get_ticket", ticket)
        monkeypatch.setattr(service_module.support_repository, "agent_has_participated", no_audit)
        monkeypatch.setattr(service_module, "whatsapp_client", SimpleNamespace(edit_message=remote, delete_message=remote))
        assert await service_module._can_view_ticket(ticket_row, owner)
        own_event = {"type": "message_updated", "ticket_id": 42, "message_id": 5}
        assert await service.can_receive_support_event(own_event, owner)
        assert await service.can_receive_support_event({"type": "customer_message", "ticket_id": 42}, owner)
        assert await service.can_receive_support_event({**own_event, "message_id": 99}, owner)
        result = await service.change_message(
            42, 5, agent=owner, action=action, content="corrected" if action == "edit" else None,
            version=1 if action == "edit" else None,
        )
        assert len(calls) == 1 and result["version"] == 2
        assert bool(result["deleted_for_everyone_at"]) == (action == "everyone")
        own_delete = await service.change_message(42, 5, agent=owner, action="me")
        assert own_delete == {"id": 5, "deleted_for_me": True}
        assert (5, "7") in connection.hidden and len(calls) == 1
        assert await service.can_receive_support_event(
            {"type": "message_deleted_for_me", "ticket_id": 42, "message_id": 5, "user_id": "7"}, owner,
        )
        assert not await service.can_receive_support_event(own_event, owner)
        outsider = SupportAgent(id="9", name="Unrelated", role="agent")
        assert await service.can_receive_support_event(own_event, outsider)
        assert await service.change_message(42, 5, agent=outsider, action="me") == {"id": 5, "deleted_for_me": True}
        assert not await service.can_receive_support_event(own_event, outsider)
        assert connection.row["version"] == 2
    asyncio.run(run())


def test_successful_staff_send_returns_message_change_capabilities(outbound_unit, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service, _connection, _events = linked_service(monkeypatch)
        outbound_unit(service_module.support_repository)
        owner = SupportAgent(id="7", name="Owner", role="agent")
        ticket = {"id": 42, "status": "IN_PROGRESS", "whatsapp_phone": "96170123456",
                  "business_phone_number_id": "90001"}
        original = message(send_status="PENDING", whatsapp_message_id=None)
        sent = message()

        async def prepare(_ticket_id: int, _agent: SupportAgent) -> dict[str, Any]:
            return ticket

        async def add_message(**_kwargs: Any) -> tuple[dict[str, Any], bool]:
            return {**original, "transport_client_id": "unit-wire-id"}, True

        async def send_text(**_kwargs: Any) -> dict[str, Any]:
            return {"messages": [{"id": sent["whatsapp_message_id"]}]}

        async def no_existing(_client_id: str, **_scope) -> None:
            return None

        async def no_reply(_ticket_id: int, _message_id: int | None) -> tuple[None, None]:
            return None, None

        async def update_state(_message_id: int, **_kwargs: Any) -> None:
            return None

        async def get_message(_message_id: int) -> dict[str, Any]:
            return sent

        async def publish(_event_type: str, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(service, "_prepare_agent_outbound_ticket", prepare)
        monkeypatch.setattr(service, "_resolve_reply_target", no_reply)
        monkeypatch.setattr(service_module.support_repository, "get_message_by_client_id", no_existing)
        monkeypatch.setattr(service_module.support_repository, "add_ticket_message", add_message)
        monkeypatch.setattr(service_module.support_repository, "update_message_send_state", update_state)
        monkeypatch.setattr(service_module.support_repository, "get_message", get_message)
        monkeypatch.setattr(service_module.whatsapp_client, "send_text", send_text)
        monkeypatch.setattr(service_module, "publish_support_event", publish)

        result = await service.send_agent_message(42, agent=owner, body="original", client_message_id="new-send")
        assert result["can_edit"] and result["can_delete_for_everyone"]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["definitive", "timeout", "unconfirmed", "wrong_id", "missing_timestamp"])
def test_failed_or_unconfirmed_provider_does_not_fake_local_success(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    async def run() -> None:
        service, connection, events = linked_service(monkeypatch)
        calls: list[str] = []

        async def remote(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs["operation_id"])
            if len(calls) > 1:
                return {"confirmed": True, "id": kwargs["message_id"], "timestamp": change_event()["timestamp"]}
            if failure in {"definitive", "timeout"}:
                error = RuntimeError("provider rejected" if failure == "definitive" else "connection lost")
                error.definitive_failure = failure == "definitive"  # type: ignore[attr-defined]
                raise error
            return {"confirmed": failure != "unconfirmed", "id": "ld:90001:OTHER" if failure == "wrong_id" else kwargs["message_id"], "timestamp": None if failure == "missing_timestamp" else change_event()["timestamp"]}

        monkeypatch.setattr(service_module, "whatsapp_client", SimpleNamespace(edit_message=remote))
        owner = SupportAgent(id="7", name="Owner", role="admin")
        with pytest.raises(SupportSendError):
            await service.change_message(42, 5, agent=owner, action="edit", content="changed", version=1)
        assert connection.row["body"] == "original" and connection.row["version"] == 1 and events == []
        operation = next(iter(connection.operations.values()))
        assert operation["status"] == ("FAILED" if failure == "definitive" else "UNCERTAIN")
        if failure == "definitive":
            assert connection.row["pending_change_operation_id"] is None
        else:
            await service.change_message(42, 5, agent=owner, action="edit", content="changed", version=1)
            assert calls[0] == calls[1] and connection.row["body"] == "changed"
    asyncio.run(run())


@pytest.mark.parametrize("overrides,action,error", [
    ({"sender_id": "8"}, "edit", "original sender"),
    ({"sender_type": "CUSTOMER", "direction": "INBOUND"}, "everyone", "original sender"),
    ({"whatsapp_message_id": "wamid.legacy"}, "edit", "cannot edit or recall"),
    ({"whatsapp_message_id": "ld:90002:OTHER"}, "everyone", "cannot edit or recall"),
    ({"created_at": datetime.now(timezone.utc) - timedelta(minutes=16)}, "edit", "15 minutes"),
    ({"created_at": datetime.now(timezone.utc) - timedelta(days=3)}, "everyone", "two days"),
    ({"send_status": "PENDING"}, "everyone", "confirmed as sent"),
    ({"message_type": "audio"}, "edit", "sent text"),
])
def test_mutation_ownership_transport_and_whatsapp_windows(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], action: str, error: str) -> None:
    async def run() -> None:
        service, connection, _events = linked_service(monkeypatch, **overrides)

        async def forbidden_remote(**_kwargs: Any) -> None:
            raise AssertionError("Invalid mutation must be rejected before network IO")

        monkeypatch.setattr(service_module, "whatsapp_client", SimpleNamespace(edit_message=forbidden_remote, delete_message=forbidden_remote))
        with pytest.raises((SupportConflictError, SupportForbiddenError), match=error):
            await service.change_message(42, 5, agent=SupportAgent(id="7", name="Owner", role="admin"), action=action, content="changed", version=1)
        assert connection.row["body"] == "original" and connection.operations == {}
    asyncio.run(run())


def test_callbacks_deduplicate_order_reconcile_and_keep_deletion_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service, connection, events = linked_service(monkeypatch, sender_type="CUSTOMER", sender_id=None, direction="INBOUND")
        timestamp = change_event()["timestamp"]
        edit = change_event(from_me=False, timestamp=timestamp)
        assert await service.apply_linked_device_change(edit)
        assert await service.apply_linked_device_change(edit)
        assert connection.row["body"] == "changed" and connection.row["version"] == 2 and len(events) == 1
        assert await service.apply_linked_device_change(change_event(event_id="stale", from_me=False, body="stale", timestamp=timestamp-1))
        assert await service.apply_linked_device_change(change_event(event_id="equal", from_me=False, body="ambiguous", timestamp=timestamp))
        assert connection.row["body"] == "changed" and connection.row["version"] == 2
        assert await service.apply_linked_device_change(change_event(event_id="delete", type="message_deleted_for_everyone", from_me=False, timestamp=timestamp+1))
        assert connection.row["body"] == "" and connection.row["version"] == 3 and not connection.reactions
        assert await service.apply_linked_device_change(change_event(event_id="resurrect", from_me=False, body="resurrect", timestamp=timestamp+2))
        assert connection.row["body"] == "" and connection.row["version"] == 3 and len(events) == 2
    asyncio.run(run())


def test_customer_callback_waits_for_original_and_validates_chat_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service, connection, _events = linked_service(monkeypatch)
        assert not await service.apply_linked_device_change(change_event(message_id="ld:90001:MISSING"))
        for overrides in ({"sender_phone": "96170000000"}, {"from_me": False}, {"line_id": "90002"}, {"timestamp": None}):
            with pytest.raises(SupportValidationError):
                await service.apply_linked_device_change(change_event(**overrides))
        assert connection.row["body"] == "original" and connection.events == set()
    asyncio.run(run())


def test_remote_echo_can_confirm_a_request_even_if_http_response_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service, connection, _events = linked_service(monkeypatch)

        async def remote(**kwargs: Any) -> dict[str, Any]:
            assert await service.apply_linked_device_change(change_event(operation_id=kwargs["operation_id"]))
            raise TimeoutError("acknowledgment response lost after callback")

        monkeypatch.setattr(service_module, "whatsapp_client", SimpleNamespace(edit_message=remote))
        result = await service.change_message(42, 5, agent=SupportAgent(id="7", name="Owner", role="admin"), action="edit", content="changed", version=1)
        assert result["body"] == "changed" and result["version"] == 2
        assert next(iter(connection.operations.values()))["status"] == "CONFIRMED"
        assert connection.row["pending_change_operation_id"] is None
    asyncio.run(run())


def test_newer_callback_wins_over_an_earlier_http_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service, connection, _events = linked_service(monkeypatch)
        timestamp = change_event()["timestamp"]

        async def remote(**kwargs: Any) -> dict[str, Any]:
            assert await service.apply_linked_device_change(change_event(event_id="external-newer", body="later linked device edit", timestamp=timestamp+1))
            return {"confirmed": True, "id": kwargs["message_id"], "timestamp": timestamp}

        monkeypatch.setattr(service_module, "whatsapp_client", SimpleNamespace(edit_message=remote))
        result = await service.change_message(42, 5, agent=SupportAgent(id="7", name="Owner", role="admin"), action="edit", content="changed", version=1)
        assert result["body"] == "later linked device edit" and result["version"] == 2
        assert connection.row["pending_change_operation_id"] is None
    asyncio.run(run())


def test_uncertain_revoke_reuses_its_request_after_an_external_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service, connection, _events = linked_service(monkeypatch)
        calls: list[str] = []
        timestamp = change_event()["timestamp"]

        async def remote(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs["operation_id"])
            if len(calls) == 1:
                assert await service.apply_linked_device_change(change_event(body="external edit before revoke ack", timestamp=timestamp))
                raise TimeoutError("revoke response lost")
            return {"confirmed": True, "id": kwargs["message_id"], "timestamp": timestamp+1}

        monkeypatch.setattr(service_module, "whatsapp_client", SimpleNamespace(delete_message=remote))
        owner = SupportAgent(id="7", name="Owner", role="admin")
        with pytest.raises(SupportSendError):
            await service.change_message(42, 5, agent=owner, action="everyone")
        assert connection.row["version"] == 2 and connection.row["body"] == "external edit before revoke ack"
        result = await service.change_message(42, 5, agent=owner, action="everyone")
        assert result["deleted_for_everyone_at"] and result["version"] == 3
        assert calls[0] == calls[1]
    asyncio.run(run())


def test_uncertain_edit_can_reconcile_the_same_request_after_window_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service, connection, _events = linked_service(monkeypatch)
        calls: list[str] = []

        async def remote(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs["operation_id"])
            if len(calls) == 1:
                raise TimeoutError("edit acceptance unknown")
            return {"confirmed": True, "id": kwargs["message_id"], "timestamp": change_event()["timestamp"]}

        monkeypatch.setattr(service_module, "whatsapp_client", SimpleNamespace(edit_message=remote))
        owner = SupportAgent(id="7", name="Owner", role="admin")
        with pytest.raises(SupportSendError):
            await service.change_message(42, 5, agent=owner, action="edit", content="changed", version=1)
        connection.row["created_at"] = datetime.now(timezone.utc)-timedelta(minutes=16)
        result = await service.change_message(42, 5, agent=owner, action="edit", content="changed", version=1)
        assert result["body"] == "changed" and calls[0] == calls[1]
        retry = await service.change_message(42, 5, agent=owner, action="edit", content="changed", version=1)
        assert retry["version"] == 2 and len(calls) == 2
    asyncio.run(run())


def test_concurrent_tabs_cannot_send_competing_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service, connection, _events = linked_service(monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def remote(**kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"confirmed": True, "id": kwargs["message_id"], "timestamp": change_event()["timestamp"]}

        monkeypatch.setattr(service_module, "whatsapp_client", SimpleNamespace(edit_message=remote))
        owner = SupportAgent(id="7", name="Owner", role="admin")
        first = asyncio.create_task(service.change_message(42, 5, agent=owner, action="edit", content="changed", version=1))
        await entered.wait()
        for body in ("changed", "competing"):
            with pytest.raises(SupportConflictError, match="awaiting WhatsApp confirmation"):
                await service.change_message(42, 5, agent=owner, action="edit", content=body, version=1)
        assert calls == 1 and connection.row["body"] == "original"
        release.set()
        await first
        assert connection.row["body"] == "changed"
    asyncio.run(run())


def test_capabilities_are_per_line_per_message_and_do_not_expose_operation_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    linked_service(monkeypatch)
    rows = _serialize_support_messages(42, [message(pending_change_operation_id="secret-operation"), message(id=6, whatsapp_message_id="wamid.cloud"), message(id=7, message_type="audio"), message(id=8, created_at=datetime.now(timezone.utc)-timedelta(minutes=16))])
    assert rows[0]["can_edit"] and rows[0]["can_delete_for_everyone"]
    assert "pending_change_operation_id" not in rows[0]
    assert not rows[1]["can_edit"] and not rows[1]["can_delete_for_everyone"]
    assert not rows[2]["can_edit"] and rows[2]["can_delete_for_everyone"]
    assert not rows[3]["can_edit"] and rows[3]["can_delete_for_everyone"]
