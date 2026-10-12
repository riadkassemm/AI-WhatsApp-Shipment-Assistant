import asyncio
from types import SimpleNamespace

import pytest

from app.support_auth import SupportAgent
import app.support_service as support_service_module


def _agent() -> SupportAgent:
    return SupportAgent(id="7", name="Agent Seven", role="agent")


def test_new_ticket_detail_includes_previous_ticket_messages_as_read_only_context(monkeypatch: pytest.MonkeyPatch) -> None:
    current = {
        "id": 52,
        "ticket_reference": "CS-CURRENT",
        "conversation_origin": "CUSTOMER",
        "active_key": "business:96170123456",
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "business",
        "status": "NEW",
        "current_assignee_id": None,
        "customer_id": None,
        "tracking_reference": None,
        "context_json": "{}",
    }
    previous = {
        "id": 40,
        "ticket_reference": "CS-PREVIOUS",
        "conversation_origin": "CUSTOMER",
        "active_key": None,
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "business",
        "status": "RESOLVED",
        "current_assignee_id": "7",
        "customer_id": None,
        "tracking_reference": None,
        "context_json": '{"secret_should_not_leak": "value"}',
    }
    current_message = {
        "id": 500,
        "ticket_id": 52,
        "sender_type": "CUSTOMER",
        "direction": "INBOUND",
        "message_type": "text",
        "body": "New issue",
    }
    previous_message = {
        "id": 400,
        "ticket_id": 40,
        "sender_type": "CUSTOMER",
        "direction": "INBOUND",
        "message_type": "image",
        "body": "Image",
        "provider_media_id": "meta-image-id",
        "provider_media_mime_type": "image/jpeg",
    }

    async def get_ticket(ticket_id: int):
        return dict(current if ticket_id == 52 else previous)

    async def list_messages(ticket_id: int, *, limit: int = 100, before_id=None, user_id=None):
        return [dict(current_message)] if ticket_id == 52 else [dict(previous_message)]

    async def list_events(ticket_id: int, limit: int = 100):
        return []

    async def list_previous_customer_tickets(**kwargs):
        assert kwargs["current_ticket_id"] == 52
        assert kwargs["phone_number"] == "96170123456"
        return [dict(previous)]

    async def resolve_identity(_phone: str):
        return SimpleNamespace(source="guest", name=None, user_id=None, userid=None)

    monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(support_service_module.support_repository, "list_messages", list_messages)
    monkeypatch.setattr(support_service_module.support_repository, "list_events", list_events)
    monkeypatch.setattr(
        support_service_module.support_repository,
        "list_previous_customer_tickets",
        list_previous_customer_tickets,
    )
    monkeypatch.setattr(support_service_module.customer_directory, "resolve", resolve_identity)
    monkeypatch.setattr(support_service_module.support_media_store, "get", lambda *_args: None)
    monkeypatch.setattr(
        support_service_module.support_media_store,
        "get_remote_reference",
        lambda *_args: None,
    )

    detail = asyncio.run(
        support_service_module.support_service.ticket_detail(
            52, _agent(), include_previous_history=True
        )
    )

    assert detail["messages"][0]["body"] == "New issue"
    assert len(detail["previous_ticket_history"]) == 1
    thread = detail["previous_ticket_history"][0]
    assert thread["ticket"]["id"] == 40
    assert thread["ticket"]["ticket_reference"] == "CS-PREVIOUS"
    assert "context_json" not in thread["ticket"]
    assert thread["messages"][0]["media_url"] == (
        "/api/support/tickets/40/messages/400/media?context_ticket_id=52"
    )


def test_staff_chat_shows_its_previous_closed_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = {
        "id": 60,
        "ticket_reference": "CH-TEST",
        "conversation_origin": "STAFF",
        "active_key": "business:96170123456",
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "business",
        "status": "ASSIGNED",
        "current_assignee_id": "7",
        "customer_id": None,
        "tracking_reference": None,
        "context_json": "{}",
    }
    previous = {
        **chat,
        "id": 40,
        "ticket_reference": "CH-OLDER",
        "status": "RESOLVED",
        "active_key": None,
    }
    previous_message = {
        "id": 400,
        "ticket_id": 40,
        "sender_type": "AGENT",
        "direction": "OUTBOUND",
        "message_type": "text",
        "body": "Earlier chat",
    }

    async def get_ticket(ticket_id: int):
        return dict(chat if ticket_id == 60 else previous)

    async def list_messages(ticket_id: int, *, limit: int = 100, before_id=None, user_id=None):
        return [dict(previous_message)] if ticket_id == 40 else []

    async def list_events(_ticket_id: int, limit: int = 100):
        return []

    async def list_previous_customer_tickets(**_kwargs):
        return [dict(previous)]

    async def resolve_identity(_phone: str):
        return SimpleNamespace(source="guest", name=None, user_id=None, userid=None)

    monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(support_service_module.support_repository, "list_messages", list_messages)
    monkeypatch.setattr(support_service_module.support_repository, "list_events", list_events)
    monkeypatch.setattr(
        support_service_module.support_repository,
        "list_previous_customer_tickets",
        list_previous_customer_tickets,
    )
    monkeypatch.setattr(support_service_module.customer_directory, "resolve", resolve_identity)

    detail = asyncio.run(
        support_service_module.support_service.ticket_detail(
            60, _agent(), include_previous_history=True
        )
    )
    assert detail["previous_ticket_history"][0]["ticket"]["id"] == 40
    assert detail["previous_ticket_history"][0]["messages"][0]["body"] == "Earlier chat"


def test_shared_previous_ticket_media_is_available_to_other_staff(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = {
        "id": 40,
        "conversation_origin": "CUSTOMER",
        "active_key": None,
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "business",
        "status": "RESOLVED",
        "current_assignee_id": "9",
    }
    current = {
        "id": 52,
        "conversation_origin": "CUSTOMER",
        "active_key": "business:96170123456",
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "business",
        "status": "NEW",
        "current_assignee_id": None,
    }
    async def get_ticket(ticket_id: int):
        return dict(previous if ticket_id == 40 else current)

    async def get_message(_message_id: int):
        return {"id": 400, "ticket_id": 40, "message_type": "image"}

    async def participated(_ticket_id: int, _agent_id: str):
        return False

    monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(support_service_module.support_repository, "get_message", get_message)
    monkeypatch.setattr(
        support_service_module.support_repository, "agent_has_participated", participated
    )
    async def hidden(_message_id: int, _agent_id: str):
        return False

    asset = SimpleNamespace(path="/tmp/test.jpg", mime_type="image/jpeg")
    monkeypatch.setattr(support_service_module.support_repository, "message_hidden_for_user", hidden)
    monkeypatch.setattr(support_service_module.support_media_store, "get", lambda *_args: asset)
    assert asyncio.run(
        support_service_module.support_service.get_ticket_media(
            40, 400, _agent(), context_ticket_id=52
        )
    ) is asset


def test_previous_ticket_media_remains_available_to_its_staff_participant(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = {
        "id": 40,
        "conversation_origin": "CUSTOMER",
        "active_key": None,
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "business",
        "status": "RESOLVED",
        "current_assignee_id": "9",
    }
    message = {"id": 400, "ticket_id": 40, "message_type": "image"}
    asset = SimpleNamespace(path="/tmp/test.jpg", mime_type="image/jpeg")

    async def get_ticket(ticket_id: int):
        assert ticket_id == 40
        return dict(previous)

    async def get_message(_message_id: int):
        return dict(message)

    async def participated(ticket_id: int, agent_id: str):
        return ticket_id == 40 and agent_id == "7"

    async def hidden(_message_id: int, _agent_id: str):
        return False

    monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(support_service_module.support_repository, "get_message", get_message)
    monkeypatch.setattr(support_service_module.support_repository, "message_hidden_for_user", hidden)
    monkeypatch.setattr(support_service_module.support_repository, "agent_has_participated", participated)
    monkeypatch.setattr(support_service_module.support_media_store, "get", lambda *_args: asset)

    result = asyncio.run(
        support_service_module.support_service.get_ticket_media(40, 400, _agent())
    )
    assert result is asset


def test_previous_ticket_realtime_events_reach_all_staff(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = {
        "id": 40,
        "conversation_origin": "CUSTOMER",
        "active_key": None,
        "whatsapp_phone": "96170123456",
        "status": "RESOLVED",
        "current_assignee_id": "9",
    }

    async def get_ticket(_ticket_id: int):
        return dict(previous)

    async def participated(_ticket_id: int, agent_id: str):
        return agent_id == "7"

    async def get_message(_message_id: int):
        return {"id": 400, "ticket_id": 40, "sender_type": "CUSTOMER", "direction": "INBOUND"}

    async def hidden(_message_id: int, _agent_id: str):
        return False

    monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(support_service_module.support_repository, "agent_has_participated", participated)
    monkeypatch.setattr(support_service_module.support_repository, "get_message", get_message)
    monkeypatch.setattr(support_service_module.support_repository, "message_hidden_for_user", hidden)

    async def run():
        participant = _agent()
        other = SupportAgent(id="8", name="Another Agent", role="agent")
        for event in (
            {"type": "customer_message", "ticket_id": 40},
            {"type": "message_updated", "ticket_id": 40, "message_id": 400},
        ):
            assert await support_service_module.support_service.can_receive_support_event(event, participant)
            assert await support_service_module.support_service.can_receive_support_event(event, other)

    asyncio.run(run())


def test_support_ui_requests_and_preserves_previous_ticket_history() -> None:
    from app.support_ui import _HTML

    assert "include_previous_history" in _HTML
    assert "Ticket · " in _HTML
    assert "state.previousTicketHistory" in _HTML
    assert "Load earlier conversations" in _HTML
