"""Unsupported inbound recovery uses real persistence, never customer/AI sends."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.whatsapp_client import IncomingWhatsAppMessage
from app.support_service import support_service, SupportValidationError
import app.support_service as service


def snapshot(provider_id='ld:90005:CONTACT'):
    return IncomingWhatsAppMessage(from_number='155500042', message_id=provider_id,
        message_type='unsupported', text=None, timestamp=str(int(datetime.now(timezone.utc).timestamp()) - 60),
        business_phone_number_id='90005', business_display_phone_number='155500000')


@pytest.mark.asyncio
async def test_snapshot_recovers_history_idempotently_and_unblocks_phone_reaction(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(service, 'get_transport', lambda *_: 'linked_device')
    sends = AsyncMock(side_effect=AssertionError('Recovery must not send customer messages'))
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=sends))
    monkeypatch.setattr(service, 'publish_support_event', AsyncMock())
    incoming = snapshot()
    # Put the original send inside this ticket's interval, before resolution.
    await sql("UPDATE support_tickets SET created_at=UTC_TIMESTAMP(6)-INTERVAL 1 HOUR WHERE id=42")
    await repo.resolve_ticket(42, agent_id='7', agent_name='Agent')
    assert await support_service.sync_linked_device_snapshot(incoming)
    assert await support_service.sync_linked_device_snapshot(incoming)
    rows = await sql("SELECT * FROM support_ticket_messages WHERE whatsapp_message_id=%s", (incoming.message_id,))
    assert len(rows) == 1
    row = rows[0]
    assert row['ticket_id'] == 42 and row['sender_type'] == 'CUSTOMER' and row['direction'] == 'INBOUND'
    assert row['send_status'] == 'RECEIVED' and 'Unsupported WhatsApp message received' in row['body']
    assert (await repo.get_ticket(42))['status'] == 'RESOLVED'
    reaction = IncomingWhatsAppMessage(from_number=incoming.from_number, message_id='ld:90005:REACTION',
        message_type='reaction', text=None, timestamp=incoming.timestamp, business_phone_number_id='90005',
        reaction_message_id=incoming.message_id, reaction_emoji='👍')
    assert await support_service.sync_linked_device_outbound(reaction)
    assert await support_service.sync_linked_device_outbound(reaction)
    reactions = await sql('SELECT * FROM support_message_reactions WHERE message_id=%s', (row['id'],))
    assert len(reactions) == 1
    sends.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_cannot_cross_conversations_or_accept_arbitrary_text(infrastructure, monkeypatch):
    repo, _, _ = infrastructure
    monkeypatch.setattr(service, 'get_transport', lambda *_: 'linked_device')
    await repo.add_ticket_message(ticket_id=43, sender_type='CUSTOMER', direction='INBOUND', body='existing',
                                 whatsapp_message_id='ld:90005:CONTACT')
    with pytest.raises(SupportValidationError, match='another conversation'):
        await support_service.sync_linked_device_snapshot(snapshot())
    incoming = snapshot()
    from dataclasses import replace
    with pytest.raises(SupportValidationError, match='Only contacts and unsupported'):
        await support_service.sync_linked_device_snapshot(replace(incoming, message_type='text', text='not a snapshot'))


@pytest.mark.asyncio
async def test_first_snapshot_opens_one_unassigned_ticket_without_customer_sends(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(service, 'get_transport', lambda *_: 'linked_device')
    monkeypatch.setattr(service, 'publish_support_event', AsyncMock())
    monkeypatch.setattr(service, 'conversation_store', SimpleNamespace(get_or_create=AsyncMock(side_effect=AssertionError('No AI or conversation replay'))))
    await sql('DELETE FROM support_tickets WHERE id=42')
    incoming = snapshot()
    assert await support_service.sync_linked_device_snapshot(incoming)
    assert await support_service.sync_linked_device_snapshot(incoming)
    rows = await sql("SELECT * FROM support_tickets WHERE whatsapp_phone='155500042' AND business_phone_number_id='90005'")
    assert len(rows) == 1 and rows[0]['status'] == 'NEW' and rows[0]['current_assignee_id'] is None
    assert len(await repo.list_messages(rows[0]['id'])) == 1
