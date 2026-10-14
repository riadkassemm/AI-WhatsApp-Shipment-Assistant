from __future__ import annotations

import asyncio
import copy
import sys
import types
from pathlib import Path

import pytest

fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
sys.modules["aiomysql"] = fake_aiomysql

from app.support_auth import SupportAgent
from app.whatsapp_client import extract_incoming_message
from app.whatsapp_registry import (
    WhatsAppRegistryConflictError,
    WhatsAppRegistryForbiddenError,
    WhatsAppRegistryRepository,
    WhatsAppRegistrySchemaError,
    WhatsAppRegistryService,
    WhatsAppRegistryValidationError,
)
from app import support_ui


class FakeRepository:
    def __init__(self) -> None:
        self.number = {
            "phone_number_id": "1375394715655384",
            "waba_id": "999999999999999",
            "display_phone_number": "+961 00 000 000",
            "display_name": None,
            "verified_name": "MIK Export",
            "quality_rating": "GREEN",
            "is_enabled": False,
            "provider_present": True,
            "source": "META_SYNC",
            "last_synced_at": None,
            "created_at": None,
            "updated_at": None,
        }
        self.audit_actions: list[str] = []
        self.has_display_name_column = True

    async def fetch_all(self, query, params=()):
        if "INFORMATION_SCHEMA.TABLES" in query:
            return [
                {"table_name": "whatsapp_business_accounts"},
                {"table_name": "whatsapp_phone_numbers"},
                {"table_name": "whatsapp_integration_audit"},
            ]
        if "INFORMATION_SCHEMA.COLUMNS" in query:
            return [{"column_name": "display_name"}] if self.has_display_name_column else []
        if "FROM whatsapp_business_accounts" in query:
            return [{"waba_id": "999999999999999", "name": "MIK", "is_active": 1}]
        if "FROM whatsapp_phone_numbers" in query:
            return [dict(self.number)]
        return []

    async def fetch_one(self, query, params=()):
        if "COUNT(*)" in query:
            return {"count": 0}
        if "FROM whatsapp_phone_numbers" in query:
            return dict(self.number)
        return None

    async def execute(self, query, params=()):
        if "UPDATE whatsapp_phone_numbers" in query and "SET is_enabled" in query:
            self.number["is_enabled"] = bool(params[0])
        if "UPDATE whatsapp_phone_numbers" in query and "SET display_name" in query:
            self.number["display_name"] = params[0]
        if "INSERT INTO whatsapp_integration_audit" in query:
            self.audit_actions.append(str(params[0]))
        return 1

    async def persist_sync(self, **kwargs):
        self.persisted = kwargs


def actor(role: str) -> SupportAgent:
    return SupportAgent(id="10", name=role.title(), role=role)


def test_new_linked_number_requires_its_own_pairing_and_stores_paired_phone(monkeypatch) -> None:
    from app import whatsapp_registry

    class LinkedRepository(FakeRepository):
        def __init__(self):
            super().__init__()
            self.number.update(phone_number_id="90002", source="LINKED_DEVICE", display_phone_number=None)
            self.updates = []

        async def execute(self, query, params=()):
            if "UPDATE whatsapp_phone_numbers" in query and "SET is_enabled" in query:
                self.updates.append(params)
                self.number["display_phone_number"] = params[1]
            return await super().execute(query, params)

    repository = LinkedRepository()
    service = WhatsAppRegistryService(repository)
    monkeypatch.setattr(whatsapp_registry, "get_transport", lambda line_id: "linked_device")
    monkeypatch.setattr(whatsapp_registry, "configured_linked_device_lines", lambda: {"90001": "", "90002": ""})
    statuses = [{"line_id": "90001", "status": "connected", "display_phone_number": "96170111111"},
                {"line_id": "90002", "status": "disconnected", "display_phone_number": "96170222222"}]

    async def status():
        return {"lines": statuses}

    monkeypatch.setattr(whatsapp_registry.linked_device_client, "get_status", status)
    with pytest.raises(WhatsAppRegistryValidationError, match="Pair this WhatsApp line"):
        asyncio.run(service.set_number_enabled(actor("admin"), "90002", True))
    assert not repository.updates
    statuses[1]["status"] = "connected"
    result = asyncio.run(service.set_number_enabled(actor("admin"), "90002", True))
    assert repository.updates == [(1, "96170222222", 10, "90002")]
    assert result["numbers"][0]["display_phone_number"] == "96170222222"
    assert result["numbers"][0]["is_enabled"] is True


def test_only_supervisor_or_admin_can_manage_numbers() -> None:
    service = WhatsAppRegistryService(FakeRepository())
    with pytest.raises(WhatsAppRegistryForbiddenError):
        asyncio.run(service.list_payload(actor("agent")))

    assert asyncio.run(service.list_payload(actor("supervisor")))["numbers"]
    assert asyncio.run(service.list_payload(actor("admin")))["numbers"]


def test_manager_can_enable_discovered_number() -> None:
    repo = FakeRepository()
    service = WhatsAppRegistryService(repo)
    result = asyncio.run(
        service.set_number_enabled(actor("admin"), "1375394715655384", True)
    )
    assert result["numbers"][0]["is_enabled"] is True
    assert "WHATSAPP_NUMBER_ENABLED" in repo.audit_actions


def test_manager_can_rename_meta_number_and_clear_name_without_changing_routing() -> None:
    repo = FakeRepository()
    service = WhatsAppRegistryService(repo)
    original_phone = repo.number["display_phone_number"]
    original_id = repo.number["phone_number_id"]

    result = asyncio.run(service.set_number_display_name(actor("supervisor"), original_id, "  TestNumber  "))
    assert result["numbers"][0]["display_name"] == "TestNumber"
    assert result["numbers"][0]["display_phone_number"] == original_phone
    assert repo.number["phone_number_id"] == original_id
    assert repo.audit_actions == ["WHATSAPP_NUMBER_RENAMED"]

    result = asyncio.run(service.set_number_display_name(actor("admin"), original_id, "   "))
    assert result["numbers"][0]["display_name"] is None
    assert result["numbers"][0]["display_phone_number"] == original_phone
    assert repo.audit_actions == ["WHATSAPP_NUMBER_RENAMED", "WHATSAPP_NUMBER_RENAMED"]


def test_linked_number_rename_is_separate_from_paired_phone(monkeypatch) -> None:
    from app import whatsapp_registry

    repo = FakeRepository()
    repo.number.update(phone_number_id="90005", source="LINKED_DEVICE", display_phone_number="447451212752")
    monkeypatch.setattr(whatsapp_registry, "configured_linked_device_lines", lambda: {"90005": ""})
    monkeypatch.setattr(whatsapp_registry, "get_transport", lambda line_id: "linked_device")
    service = WhatsAppRegistryService(repo)
    result = asyncio.run(service.set_number_display_name(actor("admin"), "90005", "TestNumber"))
    assert result["numbers"][0]["display_name"] == "TestNumber"
    assert result["numbers"][0]["display_phone_number"] == "447451212752"
    repo.number["is_enabled"] = True
    assert asyncio.run(service.list_enabled_numbers(actor("agent")))[0]["display_name"] == "TestNumber"


def test_number_rename_rejects_agent_unknown_number_and_invalid_name() -> None:
    repo = FakeRepository()
    service = WhatsAppRegistryService(repo)
    with pytest.raises(WhatsAppRegistryForbiddenError):
        asyncio.run(service.set_number_display_name(actor("agent"), repo.number["phone_number_id"], "Label"))
    with pytest.raises(WhatsAppRegistryValidationError, match="numeric"):
        asyncio.run(service.set_number_display_name(actor("admin"), "other", "Label"))
    with pytest.raises(WhatsAppRegistryValidationError, match="control characters"):
        asyncio.run(service.set_number_display_name(actor("admin"), repo.number["phone_number_id"], "Bad\nName"))
    with pytest.raises(WhatsAppRegistryValidationError, match="not available"):
        repo.number = {}
        asyncio.run(service.set_number_display_name(actor("admin"), "1375394715655384", "Label"))
    assert repo.audit_actions == []


def test_number_alias_migration_is_additive_and_schema_guard_is_actionable() -> None:
    migration = (Path(__file__).resolve().parents[1] / "migrations" / "20261014_whatsapp_number_display_names.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS display_name" in migration
    assert "DROP " not in migration.upper()
    repo = FakeRepository()
    repo.has_display_name_column = False
    with pytest.raises(WhatsAppRegistrySchemaError, match="20261014_whatsapp_number_display_names.sql"):
        asyncio.run(WhatsAppRegistryService(repo).list_payload(actor("admin")))


def test_number_rename_api_contract_uses_manager_service_and_returns_updated_registry(monkeypatch) -> None:
    from app import support_api

    calls = []

    async def rename(agent, phone_number_id, display_name):
        calls.append((agent.role, phone_number_id, display_name))
        return {"accounts": [], "numbers": [{"phone_number_id": phone_number_id, "display_name": display_name}]}

    monkeypatch.setattr(support_api.whatsapp_registry_service, "set_number_display_name", rename)
    payload = support_api.WhatsAppNumberNameRequest(display_name="TestNumber")
    result = asyncio.run(support_api.update_whatsapp_number_name("90005", payload, actor("admin")))
    assert result == {"accounts": [], "numbers": [{"phone_number_id": "90005", "display_name": "TestNumber"}]}
    assert calls == [("admin", "90005", "TestNumber")]


def test_disabling_number_with_active_tickets_is_rejected() -> None:
    class ActiveTicketRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__()
            self.number["is_enabled"] = True

        async def fetch_one(self, query, params=()):
            if "COUNT(*)" in query:
                return {"count": 2}
            return await super().fetch_one(query, params)

    service = WhatsAppRegistryService(ActiveTicketRepository())
    with pytest.raises(WhatsAppRegistryConflictError, match="active support ticket"):
        asyncio.run(
            service.set_number_enabled(actor("supervisor"), "1375394715655384", False)
        )


def test_webhook_parser_captures_waba_and_receiving_number() -> None:
    payload = {
        "entry": [
            {
                "id": "999999999999999",
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": "+96100000000",
                                "phone_number_id": "1375394715655384",
                            },
                            "messages": [
                                {
                                    "from": "96171111111",
                                    "id": "wamid.dynamic",
                                    "timestamp": "1",
                                    "type": "text",
                                    "text": {"body": "Hello"},
                                }
                            ],
                        }
                    }
                ],
            }
        ]
    }
    incoming = extract_incoming_message(payload)
    assert incoming is not None
    assert incoming.business_account_id == "999999999999999"
    assert incoming.business_phone_number_id == "1375394715655384"
    assert incoming.business_display_phone_number == "+96100000000"


def test_dynamic_number_migration_is_additive() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "20260921_whatsapp_number_registry.sql"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS whatsapp_business_accounts" in migration
    assert "CREATE TABLE IF NOT EXISTS whatsapp_phone_numbers" in migration
    assert "CREATE TABLE IF NOT EXISTS whatsapp_integration_audit" in migration
    assert "DEFAULT 0" in migration  # newly discovered lines default disabled
    assert "DROP " not in migration.upper()


def test_frontend_has_manager_number_control_without_removed_manage_ai_button() -> None:
    html = support_ui._HTML
    assert 'id="manageWhatsAppBtn"' in html
    assert 'id="whatsappModal"' in html
    assert 'id="wabaSyncForm"' in html
    assert "'/api/support/whatsapp-numbers'" in html
    assert "'/api/support/whatsapp-accounts/sync'" in html
    assert "state.agent.can_manage_whatsapp" in html
    assert 'id="manageAiBtn"' not in html


class RemovalCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None
        self.results = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def execute(self, query, params=()):
        query = " ".join(query.split())
        state = self.connection
        self.result, self.results = None, []
        if query.startswith("SELECT * FROM whatsapp_phone_numbers"):
            self.result = dict(state.number) if state.number["phone_number_id"] == params[0] else None
        elif query.startswith("SELECT id FROM support_tickets") or query.startswith("SELECT * FROM support_tickets"):
            self.results = [dict(ticket) for ticket in state.tickets if ticket["business_phone_number_id"] == params[0] and ticket.get("active_key") is not None]
        elif query.startswith("SELECT m.id FROM support_ticket_messages"):
            self.result = {"id": 10} if state.pending_send else None
        elif query.startswith("UPDATE whatsapp_phone_numbers"):
            state.number["is_enabled"] = False
            state.number["updated_by_staff_id"] = params[0]
            if "provider_present = 0" in query:
                state.number["provider_present"] = False
        elif query.startswith("UPDATE support_tickets"):
            ticket = next(ticket for ticket in state.tickets if ticket["id"] == params[1])
            ticket.update(status="CLOSED", active_key=None, resolved_by_staff_id=params[0])
        elif query.startswith("INSERT INTO support_ticket_events"):
            if state.fail_event:
                raise RuntimeError("Simulated database write failure")
            state.events.append(params)
        elif query.startswith("INSERT INTO whatsapp_integration_audit"):
            state.audits.append(("removed" if "LINE_REMOVED" in query else "started", params))
        else:
            raise AssertionError(query)

    async def fetchone(self):
        return self.result

    async def fetchall(self):
        return self.results


class RemovalConnection:
    def __init__(self):
        self.number = {"phone_number_id": "90002", "source": "LINKED_DEVICE", "is_enabled": True, "provider_present": True}
        self.tickets = [
            {"id": 1, "business_phone_number_id": "90002", "whatsapp_phone": "96170111111", "status": "IN_PROGRESS", "active_key": "90002:96170111111", "current_assignee_id": 9, "conversation_origin": "CUSTOMER"},
            {"id": 2, "business_phone_number_id": "90002", "whatsapp_phone": "96170222222", "status": "ASSIGNED", "active_key": "90002:96170222222", "current_assignee_id": 8, "conversation_origin": "STAFF"},
            {"id": 3, "business_phone_number_id": "90001", "status": "NEW", "active_key": "90001:96170333333", "current_assignee_id": None},
            {"id": 4, "business_phone_number_id": "90002", "status": "RESOLVED", "active_key": None, "current_assignee_id": 7},
        ]
        self.events, self.audits = [], []
        self.pending_send, self.fail_event = False, False
        self.commits, self.rollbacks = 0, 0

    def cursor(self, *_args):
        return RemovalCursor(self)

    async def begin(self):
        self.snapshot = copy.deepcopy((self.number, self.tickets, self.events, self.audits))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1
        self.number, self.tickets, self.events, self.audits = self.snapshot


class RemovalAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        pass


class RemovalPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return RemovalAcquire(self.connection)


def removal_repository():
    repository = WhatsAppRegistryRepository()
    connection = RemovalConnection()
    repository._pool = RemovalPool(connection)
    return repository, connection


def test_complete_removal_preflight_only_disables_routing_until_native_confirmation():
    repository, connection = removal_repository()
    before = copy.deepcopy(connection.tickets)
    count = asyncio.run(repository.prepare_linked_device_removal("90002", actor_id=10, close_active_tickets=True))
    assert count == 2 and connection.number["is_enabled"] is False
    assert connection.number["provider_present"] is True
    assert connection.tickets == before and connection.events == []
    assert connection.audits[0][0] == "started" and connection.audits[0][1][:2] == (10, "90002")


def test_complete_removal_requires_confirmation_and_waits_for_inflight_send():
    repository, connection = removal_repository()
    with pytest.raises(WhatsAppRegistryConflictError, match="2 active conversation"):
        asyncio.run(repository.prepare_linked_device_removal("90002", actor_id=10, close_active_tickets=False))
    assert connection.number["is_enabled"] and connection.events == []
    connection.pending_send = True
    with pytest.raises(WhatsAppRegistryConflictError, match="still being sent"):
        asyncio.run(repository.prepare_linked_device_removal("90002", actor_id=10, close_active_tickets=True))
    assert connection.number["is_enabled"] and connection.audits == []


def test_confirmed_removal_archives_only_target_line_preserving_history_and_owners():
    repository, connection = removal_repository()
    asyncio.run(repository.prepare_linked_device_removal("90002", actor_id=10, close_active_tickets=True))
    tickets = asyncio.run(repository.finish_linked_device_removal("90002", actor_id=10, actor_name="Admin", close_active_tickets=True))
    assert [ticket["id"] for ticket in tickets] == [1, 2]
    assert len(connection.tickets) == 4
    assert [ticket["current_assignee_id"] for ticket in connection.tickets] == [9, 8, None, 7]
    assert [ticket["status"] for ticket in connection.tickets] == ["CLOSED", "CLOSED", "NEW", "RESOLVED"]
    assert [event[1] for event in connection.events] == ["TICKET_CLOSED", "CHAT_CLOSED"]
    assert all(event[2:4] == ("10", "Admin") for event in connection.events)
    assert not connection.number["provider_present"] and not connection.number["is_enabled"]
    assert asyncio.run(repository.finish_linked_device_removal("90002", actor_id=10, actor_name="Admin", close_active_tickets=True)) == []
    assert [audit[0] for audit in connection.audits] == ["started", "removed"]


def test_archiving_failure_rolls_back_all_ticket_updates_and_keeps_retry_descriptor():
    repository, connection = removal_repository()
    asyncio.run(repository.prepare_linked_device_removal("90002", actor_id=10, close_active_tickets=True))
    before = copy.deepcopy(connection.tickets)
    connection.fail_event = True
    with pytest.raises(RuntimeError, match="database write failure"):
        asyncio.run(repository.finish_linked_device_removal("90002", actor_id=10, actor_name="Admin", close_active_tickets=True))
    assert connection.tickets == before and connection.events == []
    assert not connection.number["is_enabled"] and connection.number["provider_present"]


def test_archive_rechecks_consent_for_conversation_created_during_unlink():
    repository, connection = removal_repository()
    connection.tickets = []
    asyncio.run(repository.prepare_linked_device_removal("90002", actor_id=10, close_active_tickets=False))
    connection.tickets.append({"id": 9, "business_phone_number_id": "90002", "active_key": "90002:late", "status": "NEW", "current_assignee_id": None})
    with pytest.raises(WhatsAppRegistryConflictError, match="became active"):
        asyncio.run(repository.finish_linked_device_removal("90002", actor_id=10, actor_name="Admin", close_active_tickets=False))
    assert connection.tickets[0]["status"] == "NEW" and connection.tickets[0]["active_key"] is not None
    assert connection.number["provider_present"] and not connection.events


def test_complete_removal_rejects_cloud_descriptors_before_any_mutation():
    repository, connection = removal_repository()
    connection.number["source"] = "META_SYNC"
    with pytest.raises(WhatsAppRegistryValidationError, match="Only a registered linked-device"):
        asyncio.run(repository.prepare_linked_device_removal("90002", actor_id=10, close_active_tickets=True))
    assert connection.number["is_enabled"] and connection.audits == []


def test_registry_hides_confirmed_tombstones_and_preserves_finalize_retry_rows(monkeypatch):
    from app import whatsapp_registry

    repo = FakeRepository()
    repo.number.update(phone_number_id="90002", source="LINKED_DEVICE", is_enabled=False)
    service = WhatsAppRegistryService(repo)
    monkeypatch.setattr(whatsapp_registry, "configured_linked_device_lines", lambda: {})
    monkeypatch.setattr(whatsapp_registry, "removed_linked_device_line_ids", lambda: {"90002"})
    assert asyncio.run(service.list_payload(actor("admin")))["numbers"]
    repo.number["provider_present"] = False
    assert asyncio.run(service.list_payload(actor("admin")))["numbers"] == []
