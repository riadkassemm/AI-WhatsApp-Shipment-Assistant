import asyncio
from unittest.mock import AsyncMock

import pytest

from app.conversation_store import Conversation
from app.support_auth import SupportAgent
from app.support_models import HandoffRequest
from app.support_repository import SupportConflictError
from app import support_service as service_module


def test_removed_staff_chat_cannot_reopen_before_sending(monkeypatch):
    ticket = {"id": 45, "business_phone_number_id": "90002", "status": "CLOSED",
              "active_key": None, "conversation_origin": "STAFF", "current_assignee_id": "10"}
    monkeypatch.setattr(service_module.support_repository, "get_ticket", AsyncMock(return_value=ticket))
    reopen = AsyncMock()
    monkeypatch.setattr(service_module.support_repository, "reopen_staff_chat", reopen)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "removed")
    with pytest.raises(SupportConflictError, match="history is read-only"):
        asyncio.run(service_module.support_service._prepare_agent_outbound_ticket(45, SupportAgent(id="10", name="Manager", role="admin")))
    reopen.assert_not_awaited()


def test_delayed_handoff_cannot_create_ticket_after_number_removal(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(service_module.support_repository, "create_ticket", create)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "removed")
    conversation = Conversation(phone_number="96170123456", authenticated_name="Customer", business_phone_number_id="90002")
    with pytest.raises(SupportConflictError, match="new ticket cannot be opened"):
        asyncio.run(service_module.support_service.finalize_handoff(
            conversation=conversation,
            handoff=HandoffRequest(reason="Human support", summary="Help requested"),
            source_whatsapp_message_id="ld:90002:incoming", source_message_text="Help",
        ))
    create.assert_not_awaited()
