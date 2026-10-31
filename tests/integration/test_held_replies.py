"""Replies refused because their line was offline wait, then send once it reconnects."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.support_delivery import deliver
from app.support_service import support_service
from app.whatsapp_client import WhatsAppSendError
import app.support_service as service


def offline():
    error = WhatsAppSendError('The linked-device bridge rejected the request with HTTP 503.', delivery_uncertain=False)
    error.bridge_code = 'NOT_CONNECTED'
    return error


@pytest.mark.asyncio
async def test_offline_replies_are_held_and_sent_in_order_after_reconnect(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(service, 'publish_support_event', AsyncMock())
    online, sent = {'value': False}, []

    async def send_text(**kwargs):
        if not online['value']:
            raise offline()
        sent.append(kwargs['body'])
        return {'messages': [{'id': f'ld:90005:SENT{len(sent)}'}]}

    transport = SimpleNamespace(send_text=send_text)
    monkeypatch.setattr(service, 'whatsapp_client', transport)
    status = {'lines': [{'line_id': '90005', 'status': 'disconnected'}]}
    monkeypatch.setattr(service.linked_device_client, 'get_status', AsyncMock(return_value=status))
    ticket = await repo.get_ticket(42)

    async def reply(n):
        message, _ = await repo.add_ticket_message(ticket_id=42, sender_type='AGENT', sender_id='7', direction='OUTBOUND',
            body=f'reply {n}', client_message_id=f'request-{n}', send_status='PENDING')
        return await deliver(repo, transport, None, ticket, message)

    held = [await reply(1), await reply(2)]
    for row in held:
        assert row['send_status'] == 'FAILED' and row['delivery_retry_safe'] and row['held_until'] is not None
    first_deadline = held[0]['held_until']

    # Still offline: nothing is attempted, and the deadline never moves.
    assert await support_service.resend_held_replies() == 0
    assert sent == []
    assert (await repo.get_message(held[0]['id']))['held_until'] == first_deadline

    online['value'] = True
    status['lines'][0]['status'] = 'connected'
    assert await support_service.resend_held_replies() == 2
    assert sent == ['reply 1', 'reply 2']
    rows = await sql('SELECT send_status, held_until FROM support_ticket_messages WHERE id IN (%s, %s) ORDER BY id',
                     (held[0]['id'], held[1]['id']))
    assert [(row['send_status'], row['held_until']) for row in rows] == [('SENT', None), ('SENT', None)]

    # A hold past its window stops waiting; staff can still resend by hand.
    online['value'] = False
    late = await reply(3)
    await sql('UPDATE support_ticket_messages SET held_until=UTC_TIMESTAMP(6) - INTERVAL 1 MINUTE WHERE id=%s', (late['id'],))
    online['value'] = True
    assert await support_service.resend_held_replies() == 0
    row = await repo.get_message(late['id'])
    assert row['send_status'] == 'FAILED' and row['held_until'] is None and row['delivery_retry_safe']
    assert sent == ['reply 1', 'reply 2']


@pytest.mark.asyncio
async def test_deleting_history_records_who_deleted_it(infrastructure):
    repo, sql, _ = infrastructure
    await repo.add_ticket_message(ticket_id=42, sender_type='CUSTOMER', direction='INBOUND', body='kept out of the audit')
    await repo.resolve_ticket(42, agent_id='7', agent_name='Agent')
    await sql('UPDATE support_tickets SET active_key=NULL WHERE id=42')
    await repo.delete_history_ticket(42, actor_id='8', actor_name='Other')
    assert not await sql('SELECT id FROM support_tickets WHERE id=42')
    audit = await sql('SELECT * FROM support_deletion_audit WHERE ticket_id=42')
    assert len(audit) == 1
    assert audit[0]['ticket_reference'] == 'TEST-42' and audit[0]['status'] == 'RESOLVED'
    assert audit[0]['deleted_by_staff_id'] == 8 and audit[0]['deleted_by_name'] == 'Other'
    assert audit[0]['message_count'] >= 1 and audit[0]['business_phone_number_id'] == '90005'
