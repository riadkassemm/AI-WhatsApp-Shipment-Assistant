from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.support_service as service_module
from app.support_auth import SupportAgent
from app.support_repository import SupportConflictError, SupportNotFoundError
from app.support_service import SupportForbiddenError, SupportService, SupportValidationError


@pytest.fixture
def history_actions(monkeypatch: pytest.MonkeyPatch, tmp_path):
    agent = SupportAgent(id="7", name="Assigned agent", role="agent")
    current = {
        "id": 42, "status": "IN_PROGRESS", "active_key": "90001:96170123456",
        "current_assignee_id": agent.id, "whatsapp_phone": "96170123456",
        "business_phone_number_id": "90001", "conversation_origin": "CUSTOMER",
    }
    previous = {
        **current, "id": 20, "status": "CLOSED", "active_key": None,
        "current_assignee_id": "9",
    }
    message = {
        "id": 5, "ticket_id": 20, "sender_type": "CUSTOMER", "direction": "INBOUND",
        "body": "Previous ticket message", "message_type": "text",
        "whatsapp_message_id": "ld:90001:PREVIOUS", "send_status": "SENT",
        "created_at": datetime.now(timezone.utc),
    }
    state = SimpleNamespace(
        service=SupportService(), agent=agent, current=current, previous=previous,
        tickets={42: current, 20: previous}, message=message, hidden=False,
        stored=[], delivered=[], reactions=[], sent=[], events=[], existing_reaction=None,
    )

    async def get_ticket(ticket_id: int):
        row = state.tickets.get(ticket_id)
        return dict(row) if row else None

    async def get_message(message_id: int):
        return dict(state.message) if message_id == state.message["id"] else None

    async def hidden(message_id: int, user_id: str):
        assert message_id == 5 and user_id == agent.id
        return state.hidden

    async def no_message(*_args, **_kwargs):
        return None

    async def add_message(**kwargs):
        state.stored.append(kwargs)
        ticket = state.tickets[kwargs["ticket_id"]]
        if kwargs.get("require_assignee_id") != ticket["current_assignee_id"]:
            raise SupportConflictError("Ticket ownership changed.")
        return {"id": 99, **kwargs}, True

    async def deliver(ticket, outgoing):
        state.delivered.append((ticket, outgoing))
        return {**outgoing, "send_status": "SENT", "whatsapp_message_id": "ld:90001:NEW"}

    async def send_reaction(**kwargs):
        state.sent.append(kwargs)
        return {"messages": [{"id": "ld:90001:REACTION"}]}

    async def get_reaction(_client_id: str):
        return state.existing_reaction

    async def upsert_reaction(**kwargs):
        state.reactions.append(kwargs)
        return {"id": 3, **kwargs}

    async def publish(event_type: str, **kwargs):
        state.events.append((event_type, kwargs))

    monkeypatch.setattr(service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(service_module.support_repository, "get_message", get_message)
    monkeypatch.setattr(service_module.support_repository, "message_hidden_for_user", hidden)
    monkeypatch.setattr(service_module.support_repository, "get_message_by_client_id", no_message)
    monkeypatch.setattr(service_module.support_repository, "find_contact_forward", no_message)
    monkeypatch.setattr(service_module.support_repository, "add_ticket_message", add_message)
    monkeypatch.setattr(service_module.support_repository, "add_event", no_message)
    monkeypatch.setattr(service_module.support_repository, "get_reaction_by_client_id", get_reaction)
    monkeypatch.setattr(service_module.support_repository, "upsert_message_reaction", upsert_reaction)
    monkeypatch.setattr(service_module.whatsapp_client, "send_reaction", send_reaction)
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    monkeypatch.setattr(service_module, "get_transport", lambda _line: "linked_device")
    monkeypatch.setattr(service_module.whatsapp_registry_service, "is_number_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(service_module.support_media_store, "_root", tmp_path)
    monkeypatch.setattr(state.service, "_deliver_outbound", deliver)
    return state


async def send_reply(state, kind: str = "text"):
    kwargs = {"agent": state.agent, "client_message_id": "reply-1", "reply_to_message_id": 5}
    if kind == "location":
        return await state.service.send_agent_location(
            42, latitude=33.9, longitude=35.5, name="Office", address=None, **kwargs,
        )
    if kind == "document":
        return await state.service.send_agent_media(
            42, media_type="document", content=b"%PDF-1.4 unit fixture",
            content_type="application/pdf", filename="ticket.pdf", caption=None, **kwargs,
        )
    return await state.service.send_agent_message(42, body="Following up", **kwargs)


async def send_reaction(state, emoji: str = "👍", **kwargs):
    return await state.service.send_agent_reaction(
        20, 5, agent=state.agent, emoji=emoji,
        client_reaction_id="reaction-1", context_ticket_id=42, **kwargs,
    )


@pytest.mark.parametrize("kind", ["text", "location", "document"])
def test_history_reply_sends_on_claimed_ticket_and_preserves_provider_quote(history_actions, kind):
    state = history_actions
    result = asyncio.run(send_reply(state, kind))
    assert result["ticket_id"] == 42
    assert result["reply_to_whatsapp_message_id"] == "ld:90001:PREVIOUS"
    assert state.stored[0]["require_active"] is True
    assert state.stored[0]["require_assignee_id"] == state.agent.id
    assert state.stored[0]["expected_phone"] == "96170123456"
    assert state.stored[0]["expected_line_id"] == "90001"
    assert state.delivered[0][0]["id"] == 42
    assert state.previous["active_key"] is None


@pytest.mark.parametrize("change", [
    {"whatsapp_phone": "96170999999"},
    {"business_phone_number_id": "90002"},
    {"business_phone_number_id": None},
    {"active_key": "90001:96170123456", "status": "IN_PROGRESS"},
    {"active_key": None, "status": "NEW"},
])
def test_history_reply_rejects_unrelated_or_live_source(history_actions, change):
    state = history_actions
    state.previous.update(change)
    with pytest.raises(SupportValidationError):
        asyncio.run(send_reply(state))
    assert not state.stored and not state.delivered


@pytest.mark.parametrize("provider_id", [None, "ld:90002:PREVIOUS", "wamid.cloud-message"])
def test_history_reply_rejects_unavailable_provider_before_creating_outbound(history_actions, provider_id):
    state = history_actions
    state.message["whatsapp_message_id"] = provider_id
    with pytest.raises(SupportValidationError):
        asyncio.run(send_reply(state))
    assert not state.stored and not state.delivered


@pytest.mark.parametrize("same_ticket", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
def test_reply_rejects_hidden_or_globally_deleted_messages(history_actions, same_ticket, hidden):
    state = history_actions
    if same_ticket:
        state.message["ticket_id"] = 42
    state.hidden = hidden
    if not hidden:
        state.message["deleted_for_everyone_at"] = datetime.now(timezone.utc)
    with pytest.raises(SupportValidationError):
        asyncio.run(send_reply(state))
    assert not state.stored and not state.delivered


def test_reopened_older_staff_chat_can_reply_to_later_closed_ticket(history_actions):
    state = history_actions
    state.current["conversation_origin"] = "STAFF"
    state.previous["id"] = 50
    state.tickets[50] = state.tickets.pop(20)
    state.message["ticket_id"] = 50
    asyncio.run(send_reply(state))
    assert state.stored[0]["reply_to_whatsapp_message_id"] == "ld:90001:PREVIOUS"


@pytest.mark.parametrize("emoji", ["👍", ""])
def test_history_reaction_uses_original_ticket_and_emits_history_event(history_actions, emoji):
    state = history_actions
    result = asyncio.run(send_reaction(state, emoji))
    assert result["ticket_id"] == 20 and result["message_id"] == 5
    assert state.sent == [{
        "to": "96170123456", "message_id": "ld:90001:PREVIOUS",
        "emoji": emoji, "phone_number_id": "90001",
    }]
    assert state.reactions[0]["reactor_id"] == state.agent.id
    assert state.events == [("message_reaction", {"ticket_id": 20, "message_id": 5, "reactor_type": "AGENT"})]
    assert state.previous["active_key"] is None


@pytest.mark.parametrize("current_change,previous_change,error", [
    ({"current_assignee_id": None}, {}, SupportForbiddenError),
    ({"current_assignee_id": "9"}, {}, SupportForbiddenError),
    ({"active_key": None, "status": "CLOSED"}, {}, SupportConflictError),
    ({"business_phone_number_id": None}, {}, SupportNotFoundError),
    ({}, {"business_phone_number_id": "90002"}, SupportNotFoundError),
    ({}, {"whatsapp_phone": "96170999999"}, SupportNotFoundError),
    ({}, {"active_key": "90001:96170123456", "status": "IN_PROGRESS"}, SupportNotFoundError),
])
def test_history_reaction_requires_same_conversation_and_owned_active_context(
    history_actions, current_change, previous_change, error,
):
    state = history_actions
    state.current.update(current_change)
    state.previous.update(previous_change)
    with pytest.raises(error):
        asyncio.run(send_reaction(state))
    assert not state.sent and not state.reactions


def test_supervisor_must_also_claim_context_before_reacting(history_actions):
    state = history_actions
    state.agent = SupportAgent(id="8", name="Supervisor", role="supervisor")
    with pytest.raises(SupportForbiddenError):
        asyncio.run(send_reaction(state))
    assert not state.sent


@pytest.mark.parametrize("hidden", [False, True])
def test_history_reaction_rejects_hidden_and_deleted_messages(history_actions, hidden):
    state = history_actions
    state.hidden = hidden
    if not hidden:
        state.message["deleted_for_everyone_at"] = datetime.now(timezone.utc)
    with pytest.raises(SupportNotFoundError):
        asyncio.run(send_reaction(state))
    assert not state.sent and not state.reactions


def test_history_reaction_rechecks_assignment_after_source_reads(history_actions, monkeypatch):
    state = history_actions

    async def transfer_during_lookup(_client_id):
        state.current["current_assignee_id"] = "9"
        return None

    monkeypatch.setattr(service_module.support_repository, "get_reaction_by_client_id", transfer_during_lookup)
    with pytest.raises(SupportForbiddenError):
        asyncio.run(send_reaction(state))
    assert not state.sent and not state.reactions


@pytest.mark.parametrize("override", [{"reactor_id": "9"}, {"emoji": "❤️"}, {"ticket_id": 42}])
def test_reaction_request_id_cannot_be_reused_for_another_actor_or_payload(history_actions, override):
    state = history_actions
    state.existing_reaction = {
        "id": 3, "ticket_id": 20, "message_id": 5,
        "reactor_id": state.agent.id, "emoji": "👍", **override,
    }
    with pytest.raises(SupportConflictError):
        asyncio.run(send_reaction(state))
    assert not state.sent and not state.reactions


def test_current_ticket_reaction_still_works_without_context(history_actions):
    state = history_actions
    state.message["ticket_id"] = 42
    result = asyncio.run(state.service.send_agent_reaction(
        42, 5, agent=state.agent, emoji="👍", client_reaction_id="current-1",
    ))
    assert result["ticket_id"] == 42
    assert len(state.sent) == 1


def test_claim_does_not_grant_delete_for_everyone_on_another_staff_message(history_actions):
    state = history_actions
    state.message.update(sender_type="AGENT", sender_id="9", direction="OUTBOUND")
    with pytest.raises(SupportForbiddenError, match="original sender"):
        asyncio.run(state.service.change_message(20, 5, agent=state.agent, action="everyone"))
    assert not state.sent


def test_forward_history_message_preserves_source_and_destination_permissions(history_actions):
    state = history_actions
    state.tickets[60] = {**state.current, "id": 60, "whatsapp_phone": "96170999999"}

    async def forward():
        return await state.service.forward_message(
            source_ticket_id=20, source_message_id=5, destination_ticket_id=60,
            agent=state.agent, client_message_id="forward-1",
        )

    result = asyncio.run(forward())
    assert result["ticket_id"] == 60
    assert state.stored[0]["forwarded_from_ticket_id"] == 20
    assert state.stored[0]["forwarded_from_message_id"] == 5
    state.tickets[60]["current_assignee_id"] = "9"
    with pytest.raises(SupportForbiddenError):
        asyncio.run(forward())
    assert len(state.delivered) == 1
