from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.support_auth import SupportAgent
from app.whatsapp_client import IncomingWhatsAppMessage
from app.support_service import support_service
import app.support_service as service


@pytest.mark.asyncio
async def test_contact_upgrades_existing_marker_once_without_reviving_deleted_message(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(service, 'get_transport', lambda *_: 'linked_device')
    monkeypatch.setattr(service, 'publish_support_event', AsyncMock())
    old, _ = await repo.add_ticket_message(ticket_id=42, sender_type='CUSTOMER', direction='INBOUND', body='unsupported',
        message_type='unsupported', whatsapp_message_id='ld:90005:CARD')
    incoming = IncomingWhatsAppMessage(from_number='155500042', message_id='ld:90005:CARD', text=None,
        message_type='contacts', timestamp='1792591188', business_phone_number_id='90005',
        contacts=[{'name': {'formatted_name': 'Example Person'}, 'phones': [{'phone': '+96170123456'}]}])
    assert await support_service.sync_linked_device_snapshot(incoming)
    assert await support_service.sync_linked_device_snapshot(incoming)
    updated = await repo.get_message(old['id'])
    assert updated['body'] == 'Example Person\nPhone: +96170123456'
    assert updated['message_type'] == 'contacts' and updated['version'] == old['version'] + 1
    assert len(await repo.list_messages(42)) == 1
    await sql("UPDATE support_ticket_messages SET message_type='unsupported',body='',deleted_for_everyone_at=UTC_TIMESTAMP(6) WHERE id=%s", (old['id'],))
    assert await support_service.sync_linked_device_snapshot(incoming)
    assert (await repo.get_message(old['id']))['body'] == ''
    assert (await repo.get_message(old['id']))['deleted_for_everyone_at']


@pytest.mark.asyncio
async def test_staff_password_survives_send_duplicate_history_and_search(infrastructure, monkeypatch):
    repo, _, _ = infrastructure
    send = AsyncMock(return_value={'messages': [{'id': 'accepted-password-test'}]})
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send))
    monkeypatch.setattr(service, 'publish_support_event', AsyncMock())
    agent = SupportAgent(id='7', name='Agent', role='agent')
    body = 'code:lb12953\npassword: sample-test-password'
    for _ in range(2):
        result = await support_service.send_agent_message(42, agent=agent, body=body, client_message_id='password-test')
        assert result['body'] == body
    send.assert_awaited_once()
    assert send.call_args.kwargs['body'] == body
    assert (await repo.get_message(result['id']))['body'] == body
    found = await support_service.search_messages(query='password: sample-test-password', ticket_id=42, agent=agent)
    assert found['items'][0]['preview'] == body


@pytest.mark.asyncio
async def test_new_chat_first_message_keeps_staff_password(infrastructure, monkeypatch):
    monkeypatch.setattr(service.whatsapp_registry_service, 'is_number_enabled', AsyncMock(return_value=True))
    monkeypatch.setattr(service.whatsapp_registry_service, 'list_enabled_numbers', AsyncMock(return_value=[{'phone_number_id': '90005'}]))
    monkeypatch.setattr(support_service, '_whatsapp_number_for_line', AsyncMock(return_value='155500099'))
    monkeypatch.setattr(service.customer_directory, 'resolve', AsyncMock(return_value=SimpleNamespace(name=None, source='guest')))
    monkeypatch.setattr(service, 'publish_support_event', AsyncMock())
    send = AsyncMock(return_value={'messages': [{'id': 'accepted-new-chat'}]})
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send))
    body = 'code:lb12953\npassword: sample-test-password'
    result = await support_service.start_chat(agent=SupportAgent(id='7', name='Agent', role='agent'),
        phone_number='155500099', business_phone_number_id='90005', body=body)
    assert send.call_args.kwargs['body'] == body
    assert result['message']['body'] == body


@pytest.mark.asyncio
async def test_edited_staff_password_reaches_provider_and_stays_visible(infrastructure, monkeypatch):
    import time
    repo, _, _ = infrastructure
    monkeypatch.setattr(service, 'get_transport', lambda *_: 'linked_device')
    monkeypatch.setattr(service, 'publish_support_event', AsyncMock())
    row, _ = await repo.add_ticket_message(ticket_id=42, sender_type='AGENT', sender_id='7',
        direction='OUTBOUND', body='original', send_status='SENT', whatsapp_message_id='ld:90005:EDIT',
        whatsapp_timestamp=str(int(time.time())))
    edit = AsyncMock(return_value={'confirmed': True, 'id': 'ld:90005:EDIT', 'timestamp': int(time.time()*1000)})
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(edit_message=edit))
    body = 'code:lb12953\npassword: edited-test-password'
    result = await support_service.change_message(42, row['id'], agent=SupportAgent(id='7', name='Agent', role='agent'),
        action='edit', content=body, version=row['version'])
    assert edit.call_args.kwargs['body'] == body
    assert result['body'] == body and (await repo.get_message(row['id']))['body'] == body
