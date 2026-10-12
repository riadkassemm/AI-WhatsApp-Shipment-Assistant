from __future__ import annotations

import asyncio
from dataclasses import replace
import time

import pytest

from app.conversation_store import InMemoryConversationStore
from app.support_media import SupportMediaError
from app.support_service import SupportService, SupportValidationError
from app.whatsapp_client import IncomingWhatsAppMessage
import app.support_service as service_module


@pytest.fixture(autouse=True)
def _memory_conversation_store(monkeypatch):
    monkeypatch.setattr(service_module, "conversation_store", InMemoryConversationStore())


def outgoing(*, message_id: str = "ld:90001:PHONE1", message_type: str = "text", text: str | None = "Reply from WhatsApp") -> IncomingWhatsAppMessage:
    return IncomingWhatsAppMessage(
        from_number="96171111111", message_id=message_id,
        message_type=message_type, text=text, timestamp="1700000000",
        business_phone_number_id="90001", business_display_phone_number="96170000000",
    )


class FakeRepository:
    def __init__(self, *, active: bool = True) -> None:
        self.ticket = {
            "id": 200, "whatsapp_phone": "96171111111",
            "business_phone_number_id": "90001", "status": "NEW",
            "active_key": "90001:96171111111",
        } if active else None
        self.messages: dict[str, dict] = {}
        self.historical: dict | None = None
        self.asof_missing = False
        self.created_tickets = 0
        self.insert_args: list[dict] = []
        self.reactions: list[dict] = []

    async def find_active_ticket_by_phone(self, phone, line):
        assert phone == "96171111111" and line == "90001"
        return self.ticket

    async def find_ticket_at_whatsapp_timestamp(self, phone, line, _sent_at):
        assert phone == "96171111111" and line == "90001"
        return None if self.asof_missing else self.historical or self.ticket

    async def get_message_by_whatsapp_id(self, provider_id):
        return self.messages.get(provider_id)

    async def get_ticket(self, ticket_id):
        for ticket in (self.ticket, self.historical):
            if ticket and int(ticket_id) == ticket["id"]:
                return ticket
        return None

    async def create_ticket(self, **kwargs):
        self.created_tickets += 1
        assert kwargs["phone_number"] == "96171111111"
        assert kwargs["business_phone_number_id"] == "90001"
        self.ticket = {
            "id": 201, "whatsapp_phone": kwargs["phone_number"],
            "business_phone_number_id": kwargs["business_phone_number_id"],
            "status": "NEW", "active_key": "90001:96171111111",
        }
        return self.ticket, True

    async def add_ticket_message(self, **kwargs):
        self.insert_args.append(kwargs)
        ticket = await self.get_ticket(kwargs["ticket_id"])
        assert ticket is not None
        assert kwargs["expected_phone"] == ticket["whatsapp_phone"]
        assert kwargs["expected_line_id"] == ticket["business_phone_number_id"]
        provider_id = kwargs["whatsapp_message_id"]
        if provider_id in self.messages:
            return self.messages[provider_id], False
        message = {**kwargs, "id": len(self.messages) + 1, "version": 1}
        self.messages[provider_id] = message
        return message, True

    async def apply_linked_device_reaction(self, **kwargs):
        if self.reactions and self.reactions[-1]["provider_reaction_id"] == kwargs["provider_reaction_id"]:
            return self.reactions[-1], False
        self.reactions.append(kwargs)
        return kwargs, True

    async def apply_message_change_event(self, message_id, **kwargs):
        message = next(row for row in self.messages.values() if row["id"] == message_id)
        if message.get("deleted_for_everyone_at"):
            return message, False
        assert kwargs["action"] == "everyone" and kwargs["content"] is None
        message.update(body="", deleted_for_everyone_at=True, version=message["version"] + 1)
        return message, True


def test_phone_origin_message_syncs_once_into_existing_unassigned_ticket(monkeypatch):
    repo = FakeRepository()
    events = []

    async def publish(kind, **payload):
        events.append((kind, payload))

    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    service = SupportService()

    async def run():
        assert await service.sync_linked_device_outbound(outgoing())
        assert await service.sync_linked_device_outbound(outgoing())

    asyncio.run(run())
    assert repo.created_tickets == 0
    assert len(repo.insert_args) == 1
    row = repo.messages["ld:90001:PHONE1"]
    assert row["direction"] == "OUTBOUND" and row["send_status"] == "SENT"
    assert row["sender_type"] == "AGENT" and row["sender_id"] is None
    assert row["sender_name"] == "WhatsApp app"
    assert [kind for kind, _ in events] == ["outbound_message"]


def test_phone_origin_message_does_not_claim_a_staff_members_edit_controls(monkeypatch):
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    capabilities = service_module._message_change_capabilities({
        "whatsapp_message_id": "ld:90001:PHONE1", "whatsapp_timestamp": str(int(time.time())),
        "direction": "OUTBOUND", "sender_type": "AGENT", "sender_id": None,
        "send_status": "SENT", "message_type": "text",
    })
    assert capabilities == {"can_edit": False, "can_delete_for_everyone": False}


def test_phone_origin_first_message_creates_unassigned_ticket_and_keeps_unsupported(monkeypatch):
    repo = FakeRepository(active=False)
    events = []

    async def publish(kind, **payload):
        events.append((kind, payload))

    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    service = SupportService()

    async def run():
        assert await service.sync_linked_device_outbound(outgoing(message_type="unsupported", text=None))

    asyncio.run(run())
    assert repo.created_tickets == 1
    assert repo.ticket["status"] == "NEW"
    assert repo.messages["ld:90001:PHONE1"]["body"] == "[Unsupported WhatsApp message sent]"
    assert [kind for kind, _ in events] == ["ticket_created", "outbound_message"]
    assert events[0][1]["phone_initiated"] is True


def test_delayed_phone_message_attaches_to_conversation_open_when_sent(monkeypatch):
    repo = FakeRepository()
    repo.historical = {
        "id": 199, "whatsapp_phone": "96171111111",
        "business_phone_number_id": "90001", "status": "RESOLVED",
        "active_key": None,
    }
    events = []

    async def publish(kind, **payload):
        events.append((kind, payload))

    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    asyncio.run(SupportService().sync_linked_device_outbound(outgoing()))
    assert repo.created_tickets == 0
    assert repo.insert_args[0]["ticket_id"] == 199
    assert repo.insert_args[0]["require_active"] is False
    assert events[0][1]["ticket_id"] == 199


def test_phone_message_uses_current_active_conversation_without_historical_match(monkeypatch):
    repo = FakeRepository()
    repo.asof_missing = True
    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")

    async def publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service_module, "publish_support_event", publish)
    asyncio.run(SupportService().sync_linked_device_outbound(outgoing()))
    assert repo.insert_args[0]["ticket_id"] == 200
    assert repo.created_tickets == 0


def test_long_phone_message_is_visible_with_truncation_notice(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")

    async def publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service_module, "publish_support_event", publish)
    asyncio.run(SupportService().sync_linked_device_outbound(outgoing(text="A" * 5000)))
    body = repo.messages["ld:90001:PHONE1"]["body"]
    assert len(body) == 4096
    assert body.endswith("[Message exceeded dashboard length; truncated]")


def test_inbound_tombstone_is_saved_deleted_without_ai_and_replay_is_quiet(monkeypatch):
    repo = FakeRepository()
    events = []

    async def publish(kind, **payload):
        events.append((kind, payload))

    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    service = SupportService()
    monkeypatch.setattr(service, "_purge_deleted_message_media", lambda *_args: None)
    event = {
        "event_id": "tombstone-1", "type": "message_tombstone", "line_id": "90001",
        "from_me": False, "message_id": "ld:90001:REVOKED1",
        "sender_phone": "96171111111", "timestamp": 1_700_000_000_000,
        "deleted_timestamp": 1_700_000_001_000,
    }

    async def run():
        assert await service.sync_linked_device_tombstone(event)
        assert await service.sync_linked_device_tombstone(event)

    asyncio.run(run())
    message = repo.messages["ld:90001:REVOKED1"]
    assert message["direction"] == "INBOUND" and message["sender_type"] == "CUSTOMER"
    assert message["deleted_for_everyone_at"] is True and message["body"] == ""
    assert [kind for kind, _ in events] == ["customer_message"]
    assert events[0][1]["message"]["body"] is None
    assert events[0][1]["message"]["deleted_for_everyone_at"] is True


def test_inbound_tombstone_deletes_existing_customer_message(monkeypatch):
    repo = FakeRepository()
    repo.messages["ld:90001:REVOKED1"] = {
        "id": 3, "ticket_id": 200, "direction": "INBOUND",
        "sender_type": "CUSTOMER", "message_type": "text",
        "body": "Original content", "version": 1,
    }
    events = []

    async def publish(kind, **payload):
        events.append((kind, payload))

    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    service = SupportService()
    monkeypatch.setattr(service, "_purge_deleted_message_media", lambda *_args: None)
    event = {
        "event_id": "tombstone-1", "type": "message_tombstone", "line_id": "90001",
        "from_me": False, "message_id": "ld:90001:REVOKED1",
        "sender_phone": "96171111111", "timestamp": 1_700_000_000_000,
        "deleted_timestamp": 1_700_000_001_000,
    }

    asyncio.run(service.sync_linked_device_tombstone(event))
    assert repo.insert_args == []
    assert repo.messages["ld:90001:REVOKED1"]["body"] == ""
    assert [kind for kind, _ in events] == ["message_deleted_for_everyone"]


def test_inbound_tombstone_from_ordinary_chat_does_not_open_support_ticket(monkeypatch):
    repo = FakeRepository(active=False)
    events = []

    async def publish(kind, **_payload):
        events.append(kind)

    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    event = {
        "event_id": "tombstone-1", "type": "message_tombstone", "line_id": "90001",
        "from_me": False, "message_id": "ld:90001:REVOKED1",
        "sender_phone": "96171111111", "timestamp": 1_700_000_000_000,
        "deleted_timestamp": 1_700_000_001_000,
    }

    assert asyncio.run(SupportService().sync_linked_device_tombstone(event))
    assert repo.created_tickets == 0 and repo.insert_args == []
    assert events == []


def test_delayed_inbound_tombstone_uses_closed_conversation_at_original_time(monkeypatch):
    repo = FakeRepository()
    repo.historical = {
        "id": 199, "whatsapp_phone": "96171111111",
        "business_phone_number_id": "90001", "status": "RESOLVED", "active_key": None,
    }

    async def publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    service = SupportService()
    monkeypatch.setattr(service, "_purge_deleted_message_media", lambda *_args: None)
    event = {
        "event_id": "tombstone-1", "type": "message_tombstone", "line_id": "90001",
        "from_me": False, "message_id": "ld:90001:REVOKED1",
        "sender_phone": "96171111111", "timestamp": 1_700_000_000_000,
        "deleted_timestamp": 1_700_000_001_000,
    }

    asyncio.run(service.sync_linked_device_tombstone(event))
    assert repo.insert_args[0]["ticket_id"] == 199
    assert repo.insert_args[0]["require_active"] is False


def test_phone_origin_rejects_cross_line_or_customer_direction_duplicate(monkeypatch):
    repo = FakeRepository()
    repo.messages["ld:90001:PHONE1"] = {"id": 1, "ticket_id": 200, "direction": "INBOUND"}
    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    service = SupportService()

    async def run():
        with pytest.raises(SupportValidationError):
            await service.sync_linked_device_outbound(outgoing())
        with pytest.raises(SupportValidationError):
            await service.sync_linked_device_outbound(replace(outgoing(), business_phone_number_id="90002"))

    asyncio.run(run())


def test_phone_origin_media_keeps_voice_note_and_retries_capture(monkeypatch):
    repo = FakeRepository()
    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    async def publish(*_args, **_kwargs):
        return None
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    service = SupportService()
    captures = []

    async def capture(**kwargs):
        captures.append(kwargs)
        if len(captures) == 1:
            raise SupportMediaError("temporary download problem", retryable=True)

    monkeypatch.setattr(service, "capture_inbound_media", capture)
    voice = replace(
        outgoing(message_type="audio", text=None),
        media_id="ldmedia:90001:123e4567-e89b-12d3-a456-426614174000",
        media_mime_type="audio/ogg", is_voice=True,
    )

    async def run():
        with pytest.raises(SupportMediaError):
            await service.sync_linked_device_outbound(voice)
        assert await service.sync_linked_device_outbound(voice)

    asyncio.run(run())
    assert len(repo.insert_args) == 1
    assert repo.messages[voice.message_id]["body"] == "Voice note"
    assert len(captures) == 2 and captures[1]["media_id"] == voice.media_id


def test_phone_origin_reaction_updates_existing_message_without_new_bubble(monkeypatch):
    repo = FakeRepository()
    repo.messages["ld:90001:TARGET"] = {
        "id": 9, "ticket_id": 200, "direction": "INBOUND", "message_type": "text",
    }
    events = []

    async def publish(kind, **_payload):
        events.append(kind)

    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    service = SupportService()
    reaction = replace(
        outgoing(message_id="ld:90001:REACT", message_type="reaction", text=None),
        reaction_message_id="ld:90001:TARGET", reaction_emoji="👍",
    )

    async def run():
        assert await service.sync_linked_device_outbound(reaction)
        assert await service.sync_linked_device_outbound(reaction)

    asyncio.run(run())
    assert repo.insert_args == []
    assert repo.reactions[0]["line_id"] == "90001"
    assert len(repo.reactions) == 1
    assert events == ["message_reaction"]


def test_bridge_confirmation_updates_existing_dashboard_bubble_only_once(monkeypatch):
    repo = FakeRepository()
    calls = []
    row = {
        "id": 14, "ticket_id": 200, "sender_type": "AGENT", "sender_id": "7",
        "direction": "OUTBOUND", "message_type": "text", "body": "Sent in dashboard",
        "send_status": "SENT", "whatsapp_message_id": "ld:90001:WEB1",
        "whatsapp_timestamp": "1700000000", "version": 1,
    }

    async def confirm(**kwargs):
        calls.append(kwargs)
        return row, len(calls) == 1

    events = []
    async def publish(kind, **payload):
        events.append((kind, payload))

    monkeypatch.setattr(repo, "confirm_linked_device_outgoing", confirm, raising=False)
    monkeypatch.setattr(service_module, "support_repository", repo)
    monkeypatch.setattr(service_module, "get_transport", lambda line: "linked_device")
    monkeypatch.setattr(service_module, "publish_support_event", publish)
    service = SupportService()
    event = {
        "type": "outgoing_confirmation", "line_id": "90001",
        "message_id": "ld:90001:WEB1", "client_message_id": "browser-request-1",
        "sender_phone": "96171111111", "timestamp": 1_700_000_000_123,
        "from_me": True,
    }

    async def run():
        assert await service.confirm_linked_device_outgoing(event)
        assert await service.confirm_linked_device_outgoing(event)
        with pytest.raises(SupportValidationError):
            await service.confirm_linked_device_outgoing({**event, "from_me": False})

    asyncio.run(run())
    assert len(calls) == 2
    assert calls[0]["phone_number"] == "96171111111"
    assert calls[0]["whatsapp_timestamp"] == "1700000000"
    assert [kind for kind, _ in events] == ["outbound_message"]
