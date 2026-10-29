from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.support_auth import SupportAgent
from app.support_service import SupportForbiddenError, SupportService, SupportValidationError


@pytest.fixture(autouse=True)
def visible_message_fixture(monkeypatch):
    # These lifecycle/forwarding fixtures model visible messages. Private
    # deletion behavior is exercised separately in the message-change tests.
    monkeypatch.setattr(
        "app.support_service.support_repository.message_hidden_for_user",
        AsyncMock(return_value=False),
    )


@pytest.mark.asyncio
async def test_start_chat_creates_staff_origin_and_sends_first_message(monkeypatch):
    service = SupportService()
    agent = SupportAgent(id="7", name="Riad", role="agent")
    monkeypatch.setattr(
        "app.support_service.whatsapp_registry_service.is_number_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.support_service.whatsapp_registry_service.list_enabled_numbers",
        AsyncMock(return_value=[{"phone_number_id": "12345", "display_phone_number": "+961 1 234567"}]),
    )
    monkeypatch.setattr(
        "app.support_service.customer_directory.resolve",
        AsyncMock(return_value=SimpleNamespace(source="guest", name=None, user_id=None, userid=None)),
    )
    monkeypatch.setattr(
        "app.support_service.support_repository.find_active_ticket_by_phone",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.support_service.support_repository.find_latest_staff_chat_by_phone",
        AsyncMock(return_value=None),
    )
    created = {
        "id": 44,
        "ticket_reference": "CS-X",
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "12345",
        "conversation_origin": "STAFF",
        "current_assignee_id": "7",
        "status": "ASSIGNED",
    }
    monkeypatch.setattr(
        "app.support_service.support_repository.create_staff_chat",
        AsyncMock(return_value=created),
    )
    send = AsyncMock(return_value={"id": 90})
    monkeypatch.setattr(service, "send_agent_message", send)
    monkeypatch.setattr("app.support_service.publish_support_event", AsyncMock())

    result = await service.start_chat(
        agent=agent,
        phone_number="+961 70 123 456",
        business_phone_number_id="12345",
        body="Hello",
        customer_name="Guest",
    )

    assert result["chat"]["conversation_origin"] == "STAFF"
    send.assert_awaited_once()
    assert send.await_args.kwargs["body"] == "Hello"


@pytest.mark.asyncio
async def test_start_chat_rejects_disabled_line(monkeypatch):
    service = SupportService()
    agent = SupportAgent(id="7", name="Riad", role="agent")
    monkeypatch.setattr(
        "app.support_service.whatsapp_registry_service.is_number_enabled",
        AsyncMock(return_value=False),
    )
    with pytest.raises(SupportValidationError):
        await service.start_chat(
            agent=agent,
            phone_number="96170123456",
            business_phone_number_id="12345",
            body="Hello",
        )


@pytest.mark.asyncio
async def test_forward_requires_destination_assigned_to_current_agent(monkeypatch):
    service = SupportService()
    agent = SupportAgent(id="7", name="Riad", role="agent")
    source_ticket = {"id": 1, "current_assignee_id": "7", "active_key": "a", "status": "ASSIGNED"}
    destination = {"id": 2, "current_assignee_id": "8", "active_key": "b", "status": "ASSIGNED"}
    monkeypatch.setattr(
        "app.support_service.support_repository.get_ticket",
        AsyncMock(side_effect=[source_ticket, destination]),
    )
    monkeypatch.setattr(
        "app.support_service.support_repository.get_message",
        AsyncMock(return_value={"id": 11, "ticket_id": 1, "message_type": "text", "body": "Forward me"}),
    )
    with pytest.raises(SupportForbiddenError):
        await service.forward_message(
            source_ticket_id=1,
            source_message_id=11,
            destination_ticket_id=2,
            agent=agent,
        )


@pytest.mark.asyncio
async def test_forward_allows_accessible_resolved_history_as_source(monkeypatch):
    service = SupportService()
    agent = SupportAgent(id="7", name="Riad", role="agent")
    source_ticket = {
        "id": 1,
        "current_assignee_id": None,
        "active_key": None,
        "status": "RESOLVED",
    }
    destination = {
        "id": 2,
        "current_assignee_id": "7",
        "active_key": "active:2",
        "status": "ASSIGNED",
    }
    monkeypatch.setattr(
        "app.support_service.support_repository.get_ticket",
        AsyncMock(side_effect=[source_ticket, destination]),
    )
    monkeypatch.setattr(
        "app.support_service.support_repository.agent_has_participated",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.support_service.support_repository.get_message",
        AsyncMock(return_value={
            "id": 11,
            "ticket_id": 1,
            "message_type": "text",
            "body": "Previous customer proof",
        }),
    )
    send = AsyncMock(return_value={"id": 91, "is_forwarded": 1})
    monkeypatch.setattr(service, "send_agent_message", send)
    monkeypatch.setattr(
        "app.support_service.support_repository.add_event",
        AsyncMock(),
    )

    result = await service.forward_message(
        source_ticket_id=1,
        source_message_id=11,
        destination_ticket_id=2,
        agent=agent,
    )

    assert result["is_forwarded"] == 1
    send.assert_awaited_once()
    assert send.await_args.kwargs["is_forwarded"] is True
    assert send.await_args.kwargs["forwarded_from_ticket_id"] == 1
    assert send.await_args.kwargs["forwarded_from_message_id"] == 11


def test_support_ui_keeps_forward_enabled_for_history_and_marks_forwarded_messages():
    from pathlib import Path

    from app.support_ui import _HTML
    source = _HTML
    assert "message-action-btn message-forward-btn" in source
    assert "forwardButton.disabled = state.busy || state.forwardBusy" in source
    assert "forwarded-label" in source
    assert "document.createTextNode('Forwarded')" in source


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_owner", ["7", "9"])
async def test_reinitiating_closed_staff_chat_reopens_same_thread(monkeypatch, previous_owner):
    service = SupportService()
    agent = SupportAgent(id="7", name="Riad", role="agent")
    monkeypatch.setattr(
        "app.support_service.whatsapp_registry_service.is_number_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.support_service.whatsapp_registry_service.list_enabled_numbers",
        AsyncMock(return_value=[{"phone_number_id": "12345", "display_phone_number": "+961 1 234567"}]),
    )
    monkeypatch.setattr(
        "app.support_service.customer_directory.resolve",
        AsyncMock(return_value=SimpleNamespace(source="guest", name="Guest", user_id=None, userid=None)),
    )
    previous = {
        "id": 44,
        "ticket_reference": "CH-OLD",
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "12345",
        "conversation_origin": "STAFF",
        "current_assignee_id": previous_owner,
        "current_assignee_name": "Riad",
        "active_key": None,
        "status": "RESOLVED",
        "customer_name": "Guest",
    }
    reopened = dict(previous, active_key="12345:96170123456", status="ASSIGNED",
                    current_assignee_id="7", current_assignee_name="Riad")
    monkeypatch.setattr(
        "app.support_service.support_repository.find_active_ticket_by_phone",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.support_service.support_repository.find_latest_staff_chat_by_phone",
        AsyncMock(return_value=previous),
    )
    reopen = AsyncMock(return_value=(reopened, True))
    monkeypatch.setattr("app.support_service.support_repository.reopen_staff_chat", reopen)
    create = AsyncMock()
    monkeypatch.setattr("app.support_service.support_repository.create_staff_chat", create)
    send = AsyncMock(return_value={"id": 91})
    monkeypatch.setattr(service, "send_agent_message", send)
    monkeypatch.setattr("app.support_service.publish_support_event", AsyncMock())

    result = await service.start_chat(
        agent=agent,
        phone_number="+961 70 123 456",
        business_phone_number_id="12345",
        body="Following up again",
    )

    assert result["reopened"] is True
    assert result["chat"]["id"] == 44
    reopen.assert_awaited_once_with(44, agent_id="7", agent_name="Riad")
    create.assert_not_awaited()
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_sending_from_closed_staff_chat_reopens_same_thread(monkeypatch):
    service = SupportService()
    agent = SupportAgent(id="7", name="Riad", role="agent")
    closed = {
        "id": 44,
        "ticket_reference": "CH-OLD",
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "12345",
        "conversation_origin": "STAFF",
        "current_assignee_id": "7",
        "current_assignee_name": "Riad",
        "active_key": None,
        "status": "RESOLVED",
    }
    reopened = dict(closed, active_key="12345:96170123456", status="ASSIGNED")
    monkeypatch.setattr(
        "app.support_service.support_repository.get_ticket",
        AsyncMock(return_value=closed),
    )
    reopen = AsyncMock(return_value=(reopened, True))
    monkeypatch.setattr("app.support_service.support_repository.reopen_staff_chat", reopen)
    publish = AsyncMock()
    monkeypatch.setattr("app.support_service.publish_support_event", publish)

    result = await service._prepare_agent_outbound_ticket(44, agent)

    assert result["id"] == 44
    assert result["active_key"] == "12345:96170123456"
    reopen.assert_awaited_once_with(44, agent_id="7", agent_name="Riad")
    publish.assert_awaited_once_with("staff_chat_reopened", ticket_id=44)


@pytest.mark.asyncio
async def test_incoming_reaction_updates_original_message_without_creating_message(monkeypatch):
    service = SupportService()
    target = {"id": 91, "ticket_id": 44, "whatsapp_message_id": "wamid.original"}
    ticket = {
        "id": 44,
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "12345",
        "customer_name": "Maya",
    }
    monkeypatch.setattr(
        "app.support_service.support_repository.get_message_by_whatsapp_id",
        AsyncMock(return_value=target),
    )
    monkeypatch.setattr(
        "app.support_service.support_repository.get_ticket",
        AsyncMock(return_value=ticket),
    )
    upsert = AsyncMock(
        return_value={
            "id": 5,
            "ticket_id": 44,
            "message_id": 91,
            "emoji": "👍",
            "provider_reaction_id": "wamid.reaction",
        }
    )
    monkeypatch.setattr(
        "app.support_service.support_repository.upsert_message_reaction", upsert
    )
    add_message = AsyncMock()
    monkeypatch.setattr(
        "app.support_service.support_repository.add_ticket_message", add_message
    )
    publish = AsyncMock()
    monkeypatch.setattr("app.support_service.publish_support_event", publish)

    result = await service.route_whatsapp_reaction(
        from_number="+961 70 123 456",
        target_whatsapp_message_id="wamid.original",
        emoji="👍",
        provider_reaction_id="wamid.reaction",
        provider_timestamp="1720000000",
        business_phone_number_id="12345",
    )

    assert result is not None and result["message_id"] == 91
    add_message.assert_not_awaited()
    assert upsert.await_args.kwargs["reactor_key"] == "customer:96170123456"
    assert upsert.await_args.kwargs["provider_timestamp"] == 1720000000
    publish.assert_awaited_once_with(
        "message_reaction",
        ticket_id=44,
        message_id=91,
        customer_name="Maya",
        customer_phone="96170123456",
    )


@pytest.mark.asyncio
async def test_closing_staff_chat_uses_chat_lifecycle_without_resolution_notice(monkeypatch):
    service = SupportService()
    agent = SupportAgent(id="7", name="Riad", role="agent")
    active = {
        "id": 44,
        "conversation_origin": "STAFF",
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "12345",
        "current_assignee_id": "7",
        "active_key": "12345:96170123456",
        "status": "ASSIGNED",
    }
    closed = dict(active, active_key=None, status="RESOLVED")
    monkeypatch.setattr("app.support_service.support_repository.get_ticket", AsyncMock(return_value=active))
    close = AsyncMock(return_value=(closed, True))
    monkeypatch.setattr("app.support_service.support_repository.close_staff_chat", close)
    conversation = SimpleNamespace(mode="human", active_ticket_id=44, messages=[])
    monkeypatch.setattr("app.support_service.conversation_store.get_or_create", AsyncMock(return_value=conversation))
    monkeypatch.setattr("app.support_service.conversation_store.save", AsyncMock())
    resolution_send = AsyncMock()
    monkeypatch.setattr(service, "_send_system_customer_message", resolution_send)

    result = await service.close_staff_chat(44, agent=agent)

    assert result["chat"]["active_key"] is None
    close.assert_awaited_once()
    resolution_send.assert_not_awaited()
    assert conversation.mode == "ai"
    assert conversation.active_ticket_id is None


@pytest.mark.asyncio
async def test_ticket_resolve_endpoint_semantics_reject_staff_chat(monkeypatch):
    service = SupportService()
    agent = SupportAgent(id="7", name="Riad", role="agent")
    chat = {
        "id": 44,
        "conversation_origin": "STAFF",
        "current_assignee_id": "7",
        "active_key": "12345:96170123456",
        "status": "ASSIGNED",
    }
    monkeypatch.setattr("app.support_service.support_repository.get_ticket", AsyncMock(return_value=chat))
    resolve = AsyncMock()
    monkeypatch.setattr("app.support_service.support_repository.resolve_ticket", resolve)

    with pytest.raises(SupportValidationError, match="not support tickets"):
        await service.resolve_and_return_to_ai(44, agent=agent)
    resolve.assert_not_awaited()


def test_staff_chat_ui_uses_chat_not_ticket_lifecycle():
    from pathlib import Path

    from app.support_ui import _HTML
    source = _HTML
    assert "actionButton('Close chat', closeStaffChat" in source
    assert "'/api/support/chats/' + state.ticketId + '/close'" in source
    assert "const detailBase = chatRequest ? '/api/support/chats/' : '/api/support/tickets/'" in source
    assert "state.view === 'chats'" in source and "'/api/support/chats?'" in source
    assert "Existing WhatsApp chat reopened with its full history." in source
    assert "setComposerEnabled(Boolean(data.permissions.can_reply))" in source
    assert "Sending will reopen this chat and preserve its full history." in source
