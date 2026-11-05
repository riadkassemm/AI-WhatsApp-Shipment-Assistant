"""Previously queued forwards must not dispatch through another office."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.support_delivery import deliver
from tests.integration.test_forward_contact_reuse import setup_forward


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text', 'location', 'image', 'video', 'audio', 'document', 'sticker'])
async def test_old_wrong_office_forward_stops_before_upload_or_send(
    infrastructure, monkeypatch, tmp_path, kind,
):
    repo, sql, _ = infrastructure
    source, _ = await setup_forward(infrastructure, monkeypatch, tmp_path, kind=kind)
    await sql("""UPDATE support_tickets SET business_phone_number_id='90006',
        active_key=CONCAT('90006:',whatsapp_phone),current_assignee_id='7' WHERE id=43""")
    queued, _ = await repo.add_ticket_message(
        ticket_id=43, sender_type='AGENT', sender_id='7', sender_name='Agent',
        direction='OUTBOUND', body='Historical forward', message_type=kind,
        send_status='PENDING', client_message_id=f'forward:42:{source["id"]}:old-office',
        is_forwarded=True, forwarded_from_ticket_id=42,
        forwarded_from_message_id=source['id'],
    )
    transport = SimpleNamespace(
        send_text=AsyncMock(), send_location=AsyncMock(),
        send_media=AsyncMock(), upload_media=AsyncMock(),
    )
    media = SimpleNamespace(get=Mock())

    result = await deliver(repo, transport, media, await repo.get_ticket(43), queued)

    assert result['id'] == queued['id'] and result['ticket_id'] == 43
    assert result['send_status'] == 'FAILED'
    assert result['delivery_started_at'] is None
    assert result['forwarded_from_ticket_id'] == 42
    assert result['forwarded_from_message_id'] == source['id']
    for operation in vars(transport).values():
        operation.assert_not_awaited()
    media.get.assert_not_called()
    assert len(await repo.list_messages(43)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('source_change', ['missing', 'unscoped'])
async def test_forward_without_identifiable_source_office_fails_closed(
    infrastructure, monkeypatch, tmp_path, source_change,
):
    repo, sql, _ = infrastructure
    source, _ = await setup_forward(infrastructure, monkeypatch, tmp_path)
    await sql("UPDATE support_tickets SET current_assignee_id='7' WHERE id=43")
    if source_change == 'unscoped':
        await sql('UPDATE support_tickets SET business_phone_number_id=NULL WHERE id=42')
    queued, _ = await repo.add_ticket_message(
        ticket_id=43, sender_type='AGENT', sender_id='7', sender_name='Agent',
        direction='OUTBOUND', body='Historical forward', message_type='text',
        send_status='PENDING', client_message_id=f'forward:42:{source["id"]}:missing-office',
        is_forwarded=True, forwarded_from_ticket_id=999999 if source_change == 'missing' else 42,
        forwarded_from_message_id=source['id'],
    )
    transport = SimpleNamespace(send_text=AsyncMock())

    result = await deliver(repo, transport, SimpleNamespace(), await repo.get_ticket(43), queued)

    assert result['send_status'] == 'FAILED' and result['delivery_started_at'] is None
    transport.send_text.assert_not_awaited()
