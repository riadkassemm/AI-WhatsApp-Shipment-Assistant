"""Unit helpers. Integration tests use their own real service fixture."""
import os
from types import SimpleNamespace

import pytest

# Tests never talk to real providers. Environment variables outrank the
# developer's .env, so setting them here, before any test imports app.config,
# keeps real credentials out of the test process and out of failure output.
os.environ.update({
    "OPENAI_API_KEY": "unused-test-key",
    "WHATSAPP_TOKEN": "unused-test-token",
    "WHATSAPP_VERIFY_TOKEN": "",
    "WHATSAPP_APP_SECRET": "",
    "WHATSAPP_LINKED_DEVICE_SECRET": "",
    "DB_PASSWORD": "",
    "CUSTOMER_USERS_DB_HOST": "",
    "CUSTOMER_USERS_DB_NAME": "",
    "CUSTOMER_USERS_DB_USER": "",
    "CUSTOMER_USERS_DB_PASSWORD": "",
    "GOOGLE_CONTACTS_CLIENT_SECRET": "",
    "GOOGLE_CONTACTS_TOKEN_ENCRYPTION_KEY": "",
    "HUMAN_HANDOFF_WEBHOOK_URL": "",
})


@pytest.fixture
def outbound_unit(monkeypatch, tmp_path):
    from app.support_repository import support_repository
    from app.support_media import support_media_store

    async def claim(*args, **kwargs):
        return 'unit-claim'

    async def begin(*args, **kwargs):
        return True

    def install(repository):
        async def finish_for_repository(message_id, token, *, status, provider_id=None, error=None):
            await repository.update_message_send_state(message_id, send_status=status,
                                                       whatsapp_message_id=provider_id, error_message=error)
        monkeypatch.setattr(repository, 'claim_delivery', claim)
        monkeypatch.setattr(repository, 'begin_delivery', begin)
        monkeypatch.setattr(repository, 'finish_delivery', finish_for_repository)
    install(support_repository)
    monkeypatch.setattr(support_media_store, '_root', tmp_path)
    return install


@pytest.fixture
def fake_ai_ticket(monkeypatch):
    """Give automated turns an in-memory AI ticket (Step 3b).

    For tests about turn logic rather than tickets: the turn gets a ticket
    without touching MariaDB, and each AI reply goes straight to whatever
    ``app.main.whatsapp_client`` the test installed, once per inbound message,
    as the real fenced delivery path does. Returns the list of stored replies.
    """
    import app.main as main
    from app.support_repository import SupportConflictError

    replies = []
    async def trace_begin(*args, **kwargs):
        return True
    async def trace_finish(*args, **kwargs):
        return None
    monkeypatch.setattr(main.support_repository, 'begin_agent_trace', trace_begin)
    monkeypatch.setattr(main.support_repository, 'finish_agent_trace', trace_finish)
    state = {"handled_by_ai": True}

    async def ticket_for_turn(*, conversation, incoming, parsed_auth, ai_ticket):
        ticket = ai_ticket or {
            "id": 1, "handling_mode": "AI", "whatsapp_phone": conversation.phone_number,
            "business_phone_number_id": conversation.business_phone_number_id,
        }
        return ticket, None

    async def send_ai_reply(ticket, *, body, source_message_id, awaiting_customer=False):
        if not state["handled_by_ai"]:
            raise SupportConflictError("The AI assistant no longer handles this conversation.")
        if source_message_id and any(row["source_message_id"] == source_message_id for row in replies):
            return next(row for row in replies if row["source_message_id"] == source_message_id)
        line = ticket.get("business_phone_number_id")
        kwargs = {"phone_number_id": line} if line else {}
        response = await main.whatsapp_client.send_text(to=ticket["whatsapp_phone"], body=body, **kwargs)
        messages = response.get("messages") if isinstance(response, dict) else None
        row = {
            "source_message_id": source_message_id, "body": body, "send_status": "SENT",
            "whatsapp_message_id": (messages[0].get("id") if messages else None),
        }
        replies.append(row)
        return row

    monkeypatch.setattr(main, "_ai_ticket_for_turn", ticket_for_turn)
    monkeypatch.setattr(main.support_service, "send_ai_reply", send_ai_reply)
    return SimpleNamespace(replies=replies, state=state)
