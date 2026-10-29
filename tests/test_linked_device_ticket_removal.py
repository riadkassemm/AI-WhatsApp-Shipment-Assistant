from __future__ import annotations

import asyncio

import pytest

from app.support_repository import SupportConflictError, SupportRepository
import app.support_repository as repository_module


class ChannelCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def execute(self, query, params):
        query = " ".join(query.split())
        self.connection.queries.append(query)
        if query.startswith("SELECT business_phone_number_id"):
            self.result = {"business_phone_number_id": "90002"}
        elif query.startswith("SELECT source, is_enabled, provider_present"):
            if self.connection.blocked is not None:
                self.connection.entered.set()
                await self.connection.blocked.wait()
            self.result = dict(self.connection.number) if self.connection.number else None
        elif query.startswith("SELECT id FROM whatsapp_integration_audit"):
            self.result = None
        else:
            raise AssertionError("A removed/inactive line must fail before ticket locks or writes: " + query)

    async def fetchone(self):
        return self.result


class ChannelConnection:
    def __init__(self):
        self.number = {"source": "LINKED_DEVICE", "is_enabled": False, "provider_present": True}
        self.queries = []
        self.rollbacks, self.commits = 0, 0
        self.blocked = self.entered = None

    def cursor(self, *_args):
        return ChannelCursor(self)

    async def begin(self):
        pass

    async def rollback(self):
        self.rollbacks += 1

    async def commit(self):
        self.commits += 1


class ChannelAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        pass


class ChannelPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return ChannelAcquire(self.connection)


def ticket_arguments():
    return {
        "ticket_reference": "T-late", "phone_number": "96170111111",
        "business_phone_number_id": "90002", "business_display_phone_number": None,
        "customer_id": None, "customer_userid": None, "customer_name": None,
        "reason": "HANDOFF", "ai_summary": "", "tracking_reference": None,
        "requested_action": None, "current_language": "en", "communication_style": "en",
        "context_json": "{}", "source_whatsapp_message_id": None,
    }


async def ownership_operation(repository, operation):
    if operation == "handoff":
        return await repository.create_ticket(**ticket_arguments())
    if operation == "start_chat":
        fields = {key: value for key, value in ticket_arguments().items() if key in {
            "ticket_reference", "phone_number", "business_phone_number_id", "business_display_phone_number",
            "customer_id", "customer_userid", "customer_name",
        }}
        return await repository.create_staff_chat(**fields, agent_id="7", agent_name="Admin")
    return await repository.reopen_staff_chat(42, agent_id="7", agent_name="Admin")


def repository_with_channel(monkeypatch):
    connection = ChannelConnection()
    repository = SupportRepository()
    repository._pool = ChannelPool(connection)

    async def no_existing(*_args):
        return None

    monkeypatch.setattr(repository, "find_active_ticket_by_phone", no_existing)
    return repository, connection


@pytest.mark.parametrize("operation", ["handoff", "start_chat", "reopen_chat"])
@pytest.mark.parametrize("transport", ["linked_device", "removed"])
def test_inactive_or_removed_number_cannot_create_or_reactivate_human_ownership(monkeypatch, operation, transport):
    repository, connection = repository_with_channel(monkeypatch)
    monkeypatch.setattr(repository_module, "get_transport", lambda _line: transport)
    with pytest.raises(SupportConflictError, match="inactive or removed"):
        asyncio.run(ownership_operation(repository, operation))
    assert connection.commits == 0 and connection.rollbacks == 1
    assert connection.queries[-1].startswith("SELECT source, is_enabled, provider_present")


def test_delayed_handoff_rechecks_number_after_waiting_for_removal_transaction(monkeypatch):
    async def run():
        repository, connection = repository_with_channel(monkeypatch)
        monkeypatch.setattr(repository_module, "get_transport", lambda _line: "linked_device")
        connection.number["is_enabled"] = True
        connection.blocked, connection.entered = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(ownership_operation(repository, "handoff"))
        await connection.entered.wait()
        assert not pending.done()
        # Removal acquired the number lock first, then durably disabled routing.
        # The waiting handoff sees that committed state before inserting a ticket.
        connection.number["is_enabled"] = False
        connection.blocked.set()
        with pytest.raises(SupportConflictError, match="inactive or removed"):
            await pending
        assert connection.commits == 0 and connection.rollbacks == 1
    asyncio.run(run())


def test_cloud_channels_do_not_require_a_linked_registry_row(monkeypatch):
    connection = ChannelConnection()
    connection.number = None
    monkeypatch.setattr(repository_module, "get_transport", lambda _line: "cloud")
    asyncio.run(SupportRepository._lock_available_whatsapp_line(connection, "123456789"))
    assert len(connection.queries) == 2
    assert "LOCK IN SHARE MODE" in connection.queries[1]
    assert 'removed_at' in connection.queries[0] and 'FOR UPDATE' in connection.queries[0]
