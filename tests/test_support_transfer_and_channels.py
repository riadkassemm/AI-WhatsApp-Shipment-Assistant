from __future__ import annotations

import asyncio
import sys
import types

import pytest

# Lightweight stubs for production-only dependencies unavailable in the artifact runtime.
fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {})
)
sys.modules["aiomysql"] = fake_aiomysql

fake_bcrypt = sys.modules.get("bcrypt") or types.ModuleType("bcrypt")
fake_bcrypt.checkpw = getattr(fake_bcrypt, "checkpw", lambda *_args, **_kwargs: False)
fake_bcrypt.hashpw = getattr(fake_bcrypt, "hashpw", lambda *_args, **_kwargs: b"hash")
fake_bcrypt.gensalt = getattr(fake_bcrypt, "gensalt", lambda *_args, **_kwargs: b"salt")
sys.modules["bcrypt"] = fake_bcrypt

fake_openai = sys.modules.get("openai") or types.ModuleType("openai")
if not hasattr(fake_openai, "AsyncOpenAI"):
    class _AsyncOpenAI:
        def __init__(self, *_args, **_kwargs) -> None:
            pass
    fake_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules["openai"] = fake_openai

from app.main import _combine_inbound_burst, _queued_inbound_item
from app.support_auth import SupportAgent
from app.support_service import SupportForbiddenError, SupportService
from app.support_ui import _HTML
from app.whatsapp_client import IncomingWhatsAppMessage, WhatsAppClient, extract_incoming_message
import app.support_service as support_service_module


def _webhook_payload(*, phone_number_id: str, display_phone_number: str, message: dict) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "phone_number_id": phone_number_id,
                                "display_phone_number": display_phone_number,
                            },
                            "messages": [message],
                        }
                    }
                ]
            }
        ]
    }


def test_extract_incoming_message_keeps_receiving_business_number() -> None:
    incoming = extract_incoming_message(
        _webhook_payload(
            phone_number_id="123456789",
            display_phone_number="+961 1 234 567",
            message={
                "from": "96170123456",
                "id": "wamid.channel",
                "timestamp": "1787962400",
                "type": "text",
                "text": {"body": "Hello"},
            },
        )
    )
    assert incoming is not None
    assert incoming.business_phone_number_id == "123456789"
    assert incoming.business_display_phone_number == "+961 1 234 567"


def test_message_burst_preserves_channel_and_never_merges_different_numbers() -> None:
    first = IncomingWhatsAppMessage(
        from_number="96170123456",
        message_id="wamid.1",
        message_type="text",
        text="I need help",
        timestamp="100",
        business_phone_number_id="business-a",
        business_display_phone_number="+961 1 111 111",
    )
    second_same = IncomingWhatsAppMessage(
        from_number="96170123456",
        message_id="wamid.2",
        message_type="text",
        text="with shipment 123",
        timestamp="101",
        business_phone_number_id="business-a",
        business_display_phone_number="+961 1 111 111",
    )
    combined = _combine_inbound_burst(
        [_queued_inbound_item(first, "claim-1"), _queued_inbound_item(second_same, "claim-2")]
    )
    assert combined is not None
    assert combined.text == "I need help\nwith shipment 123"
    assert combined.business_phone_number_id == "business-a"
    assert combined.business_display_phone_number == "+961 1 111 111"

    second_other = IncomingWhatsAppMessage(
        from_number="96170123456",
        message_id="wamid.3",
        message_type="text",
        text="different support line",
        timestamp="102",
        business_phone_number_id="business-b",
        business_display_phone_number="+961 1 222 222",
    )
    assert _combine_inbound_burst(
        [_queued_inbound_item(first, "claim-1"), _queued_inbound_item(second_other, "claim-3")]
    ) is None


def test_whatsapp_client_can_route_outbound_message_through_ticket_number() -> None:
    async def run() -> None:
        client = WhatsAppClient()
        seen: dict = {}

        async def fake_send(payload: dict, *, label: str, phone_number_id: str | None = None) -> dict:
            seen.update(payload=payload, label=label, phone_number_id=phone_number_id)
            return {"messages": [{"id": "wamid.out"}]}

        client._send_payload = fake_send  # type: ignore[method-assign]
        await client.send_text(
            "96170123456",
            "Hello",
            phone_number_id="business-2",
        )
        assert seen["phone_number_id"] == "business-2"
        assert seen["payload"]["to"] == "96170123456"

    asyncio.run(run())


def test_assigned_agent_can_transfer_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent A", role="agent")
        ticket = {
            "id": 42,
            "active_key": "business-a:96170123456",
            "status": "IN_PROGRESS",
            "current_assignee_id": "7",
            "current_assignee_name": "Agent A",
        }
        seen: dict = {}

        async def fake_get_ticket(ticket_id: int) -> dict:
            assert ticket_id == 42
            return dict(ticket)

        async def fake_transfer_ticket(ticket_id: int, **kwargs) -> dict:
            seen.update(ticket_id=ticket_id, **kwargs)
            return {
                **ticket,
                "current_assignee_id": "9",
                "current_assignee_name": "Specialist B",
                "status": "ASSIGNED",
            }

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "transfer_ticket",
            fake_transfer_ticket,
        )

        transferred = await service.transfer(42, target_staff_id=9, agent=agent)
        assert transferred["current_assignee_id"] == "9"
        assert seen["expected_assignee_id"] == "7"
        assert seen["target_staff_id"] == 9
        assert seen["actor_id"] == "7"

    asyncio.run(run())


def test_old_agent_cannot_reply_after_ticket_is_transferred(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        old_agent = SupportAgent(id="7", name="Agent A", role="agent")

        async def fake_get_ticket(_ticket_id: int) -> dict:
            return {
                "id": 42,
                "active_key": "business-a:96170123456",
                "status": "ASSIGNED",
                "current_assignee_id": "9",
                "current_assignee_name": "Specialist B",
                "whatsapp_phone": "96170123456",
            }

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)

        with pytest.raises(SupportForbiddenError, match="currently assigned agent"):
            await service.send_agent_message(
                42,
                agent=old_agent,
                body="This must not be sent",
                client_message_id="old-agent-after-transfer",
            )

    asyncio.run(run())


def test_supervisor_must_be_assignee_before_replying(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        supervisor = SupportAgent(id="1", name="Supervisor", role="supervisor")

        async def fake_get_ticket(_ticket_id: int) -> dict:
            return {
                "id": 42,
                "active_key": "business-a:96170123456",
                "status": "IN_PROGRESS",
                "current_assignee_id": "9",
                "current_assignee_name": "Specialist B",
                "whatsapp_phone": "96170123456",
            }

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)

        with pytest.raises(SupportForbiddenError, match="currently assigned agent"):
            await service.send_agent_message(
                42,
                agent=supervisor,
                body="Supervisor should transfer ownership first",
                client_message_id="supervisor-not-assignee",
            )

    asyncio.run(run())


def test_dashboard_exposes_channel_transfer_and_read_only_watching_controls() -> None:
    assert 'data-view="watching"' in _HTML
    assert 'id="transferModal"' in _HTML
    assert 'id="transferTarget"' in _HTML
    assert 'id="readOnlyBanner"' in _HTML
    assert "Received on " in _HTML
    assert "permissions.can_reply" in _HTML
    assert "is_read_only_observer" in _HTML
    assert "/api/support/transfer-targets" in _HTML
    assert "/transfer" in _HTML


def test_legacy_ticket_bind_race_never_crosses_business_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        find_calls = 0

        async def fake_find(_phone: str, _business_id: str | None = None) -> dict | None:
            nonlocal find_calls
            find_calls += 1
            if find_calls == 1:
                return {
                    "id": 42,
                    "whatsapp_phone": "96170123456",
                    "active_key": "96170123456",
                    "business_phone_number_id": None,
                    "status": "IN_PROGRESS",
                }
            return None

        async def fake_bind(_ticket_id: int, **_kwargs) -> dict:
            # Simulate another receiving line winning the legacy-row binding race.
            return {
                "id": 42,
                "whatsapp_phone": "96170123456",
                "active_key": "business-other:96170123456",
                "business_phone_number_id": "business-other",
                "status": "IN_PROGRESS",
            }

        monkeypatch.setattr(
            support_service_module.support_repository,
            "find_active_ticket_by_phone",
            fake_find,
        )
        monkeypatch.setattr(
            support_service_module.support_repository,
            "bind_ticket_channel",
            fake_bind,
        )

        ticket = await service.get_active_ticket(
            "96170123456",
            business_phone_number_id="business-requested",
            business_display_phone_number="+961 1 111 111",
        )
        assert ticket is None
        assert find_calls == 2

    asyncio.run(run())


def test_status_change_carries_assignment_snapshot_to_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent A", role="agent")
        seen: dict = {}

        async def fake_get_ticket(_ticket_id: int) -> dict:
            return {
                "id": 42,
                "status": "IN_PROGRESS",
                "active_key": "business-a:96170123456",
                "current_assignee_id": "7",
                "current_assignee_name": "Agent A",
            }

        async def fake_update_status(ticket_id: int, **kwargs) -> dict:
            seen.update(ticket_id=ticket_id, **kwargs)
            return {
                "id": ticket_id,
                "status": kwargs["status"],
                "current_assignee_id": "7",
            }

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(
            support_service_module.support_repository,
            "update_status",
            fake_update_status,
        )

        updated = await service.update_status(
            42,
            new_status="WAITING_CUSTOMER",
            agent=agent,
        )
        assert updated["status"] == "WAITING_CUSTOMER"
        assert seen["expected_assignee_id"] == "7"

    asyncio.run(run())
