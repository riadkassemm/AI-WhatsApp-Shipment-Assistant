"""Contact forwards reuse active threads without granting general chat access."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import app.support_auth as auth
import app.support_repository as repository_module
import app.support_service as service
from app.config import settings
from app.support_api import router
from app.support_auth import SupportAgent
from app.support_media import SupportMediaStore
from app.whatsapp_client import WhatsAppSendError
from tests.integration.test_quick_reply_pdfs import create_sessions


AGENT = SupportAgent(id='7', name='Agent', role='agent')
PHONE = '155500043'
PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n'


async def setup_forward(infrastructure, monkeypatch, tmp_path, *, kind='text'):
    repo, sql, _ = infrastructure
    await sql("""INSERT INTO whatsapp_phone_numbers
        (phone_number_id,display_name,source,is_enabled,provider_present)
        VALUES ('90005','Source line','LINKED_DEVICE',1,1),
               ('90006','Other line','LINKED_DEVICE',1,1)""")
    await sql("""UPDATE support_tickets SET active_key='90005:155500043',
        customer_name='Private destination identity',ai_summary='Private summary',
        reason='Private reason',context_json='{"private":"Private context"}' WHERE id=43""")
    monkeypatch.setattr(settings, 'support_media_directory', str(tmp_path))
    media = SupportMediaStore()
    monkeypatch.setattr(service, 'support_media_store', media)
    monkeypatch.setattr(service, 'publish_support_event', AsyncMock())
    monkeypatch.setattr(service, 'get_transport', lambda *_: 'linked_device')
    monkeypatch.setattr(repository_module, 'get_transport', lambda *_: 'linked_device')
    monkeypatch.setattr(service.whatsapp_registry_service, 'is_number_enabled', AsyncMock(return_value=True))
    monkeypatch.setattr(service.whatsapp_registry_service, 'list_enabled_numbers',
        AsyncMock(return_value=[{'phone_number_id': '90005'}]))
    monkeypatch.setattr(service.support_service, '_whatsapp_number_for_line', AsyncMock(return_value=PHONE))
    monkeypatch.setattr(service.customer_directory, 'resolve',
        AsyncMock(return_value=SimpleNamespace(name=None, source='guest')))
    monkeypatch.setattr(auth, '_schedule_session_touch', lambda _: None)
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)
        return {'messages': [{'id': f'ld:90005:CONTACT-FORWARD-{len(calls)}'}]}

    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(
        send_text=send, send_media=send, send_location=send,
        upload_media=AsyncMock(return_value='synthetic-forward-file'),
    ))
    extra = {
        'location_latitude': 33.89, 'location_longitude': 35.50,
        'location_name': 'Office', 'location_address': 'Example address',
    } if kind == 'location' else {}
    original, _ = await repo.add_ticket_message(
        ticket_id=42, sender_type='CUSTOMER', direction='INBOUND', body='Forward this content',
        message_type=kind, whatsapp_message_id='ld:90005:CONTACT-SOURCE', **extra,
    )
    if kind == 'document':
        media.save(ticket_id=42, message_id=original['id'], content=PDF,
            mime_type='application/pdf', filename='Guide.pdf')
    return original, calls


def api_client():
    app = FastAPI()
    app.include_router(router)
    asgi = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=httpx.MockTransport(asgi.handle_async_request), base_url='http://test',
        cookies={settings.support_cookie_name: 'token-7'}, headers={'X-CSRF-Token': 'csrf'},
    )


async def forward(original, *, key='contact-forward'):
    return await service.support_service.forward_message_to_contact(
        source_ticket_id=42, source_message_id=original['id'], phone_number=PHONE,
        customer_name='Entered contact name', agent=AGENT, client_message_id=key,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', ['CUSTOMER', 'STAFF'])
@pytest.mark.parametrize('kind', ['text', 'document', 'location'])
async def test_contact_forward_reuses_other_staff_thread_without_granting_access(
    infrastructure, monkeypatch, tmp_path, origin, kind,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_forward(infrastructure, monkeypatch, tmp_path, kind=kind)
    await sql('UPDATE support_tickets SET conversation_origin=%s WHERE id=43', (origin,))
    await create_sessions(sql)
    before = await repo.get_ticket(43)
    path = f'/api/support/tickets/42/messages/{original["id"]}/forward-contact'
    async with api_client() as client:
        for _ in range(2):
            response = await client.post(path, json={
                'phone_number': PHONE, 'customer_name': 'Entered contact name',
                'client_message_id': 'reused-contact-forward',
            })
            assert response.status_code == 200, response.text
            payload = response.json()
            assert 'Private ' not in response.text
        assert (await client.get('/api/support/tickets/43')).status_code == 403
        assert (await client.post('/api/support/tickets/43/messages', json={
            'body': 'Unrelated message', 'client_message_id': 'unrelated-send',
        })).status_code == 403
        assert (await client.post(f'/api/support/tickets/42/messages/{original["id"]}/forward',
            json={'destination_ticket_id': 43, 'client_message_id': 'direct-forward'})).status_code == 403
    assert len(calls) == 1
    assert calls[0]['to'] == PHONE and calls[0]['phone_number_id'] == '90005'
    stored = await repo.get_message(payload['message']['id'])
    assert stored['ticket_id'] == 43 and stored['sender_id'] == '7'
    assert stored['message_type'] == kind and stored['send_status'] == 'SENT'
    assert stored['is_forwarded'] and stored['forwarded_from_ticket_id'] == 42
    assert stored['forwarded_from_message_id'] == original['id']
    after = await repo.get_ticket(43)
    for field in ('current_assignee_id', 'active_key', 'status', 'conversation_origin', 'customer_name'):
        assert after[field] == before[field]
    assert len(await sql('SELECT id FROM support_tickets')) == 3
    events = await sql("SELECT actor_id FROM support_ticket_events WHERE ticket_id=43 AND event_type='MESSAGE_FORWARDED'")
    assert [event['actor_id'] for event in events] == ['7']
    service.customer_directory.resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_contact_forward_requires_session_csrf_and_source_visibility(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    original, calls = await setup_forward(infrastructure, monkeypatch, tmp_path)
    await create_sessions(sql)
    path = f'/api/support/tickets/42/messages/{original["id"]}/forward-contact'
    payload = {'phone_number': PHONE, 'client_message_id': 'protected-contact-forward'}
    async with api_client() as client:
        client.cookies.clear()
        assert (await client.post(path, json=payload)).status_code == 401
        client.cookies.set(settings.support_cookie_name, 'token-7')
        client.headers.pop('X-CSRF-Token')
        assert (await client.post(path, json=payload)).status_code == 403
        client.headers['X-CSRF-Token'] = 'csrf'
        await sql("UPDATE support_tickets SET current_assignee_id='8' WHERE id=42")
        assert (await client.post(path, json=payload)).status_code == 403
    assert not calls
    assert len(await repo.list_messages(43)) == 0
    service.support_service._whatsapp_number_for_line.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['hidden', 'deleted', 'wrong_source', 'disabled_line'])
async def test_contact_forward_preserves_source_message_restrictions(
    infrastructure, monkeypatch, tmp_path, boundary,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_forward(infrastructure, monkeypatch, tmp_path)
    if boundary == 'hidden':
        await sql('INSERT INTO support_message_user_deletions(message_id,user_id) VALUES (%s,%s)', (original['id'], '7'))
    elif boundary == 'deleted':
        await sql('UPDATE support_ticket_messages SET deleted_for_everyone_at=UTC_TIMESTAMP(6) WHERE id=%s', (original['id'],))
    elif boundary == 'wrong_source':
        await sql('UPDATE support_ticket_messages SET ticket_id=41 WHERE id=%s', (original['id'],))
    else:
        service.whatsapp_registry_service.is_number_enabled.return_value = False
    with pytest.raises((service.SupportNotFoundError, service.SupportConflictError)):
        await forward(original)
    assert not calls
    assert len(await repo.list_messages(43)) == 0


@pytest.mark.asyncio
async def test_contact_forward_reuses_unassigned_thread_without_claiming(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    original, calls = await setup_forward(infrastructure, monkeypatch, tmp_path)
    await sql("UPDATE support_tickets SET current_assignee_id=NULL,status='NEW' WHERE id=43")
    result = await forward(original)
    assert result['message']['ticket_id'] == 43 and len(calls) == 1
    destination = await repo.get_ticket(43)
    assert destination['current_assignee_id'] is None and destination['status'] == 'NEW'
    assert len(await sql('SELECT id FROM support_tickets')) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('same_line_exists', [True, False])
async def test_contact_forward_resolves_only_source_line(infrastructure, monkeypatch, tmp_path, same_line_exists):
    repo, sql, _ = infrastructure
    original, calls = await setup_forward(infrastructure, monkeypatch, tmp_path)
    await sql("""UPDATE support_tickets SET whatsapp_phone=%s,business_phone_number_id='90006',
        active_key='90006:155500043',status='ASSIGNED',current_assignee_id='8' WHERE id=41""", (PHONE,))
    if not same_line_exists:
        await sql("UPDATE support_tickets SET active_key=NULL,status='RESOLVED' WHERE id=43")
    other_line = await repo.get_ticket(41)
    result = await forward(original)
    destination_id = result['message']['ticket_id']
    assert destination_id != 41
    assert (destination_id == 43) is same_line_exists
    destination = await repo.get_ticket(destination_id)
    assert destination['business_phone_number_id'] == '90005'
    assert destination['current_assignee_id'] == ('8' if same_line_exists else '7')
    assert calls[0]['phone_number_id'] == '90005' and calls[0]['to'] == PHONE
    assert await repo.get_ticket(41) == other_line
    service.support_service._whatsapp_number_for_line.assert_awaited_once_with(
        PHONE, line_id='90005', line_display_phone=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('attempt', ['create', 'reopen'])
async def test_contact_forward_reuses_winner_of_conversation_creation_race(
    infrastructure, monkeypatch, tmp_path, attempt,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_forward(infrastructure, monkeypatch, tmp_path)
    if attempt == 'reopen':
        await sql("""UPDATE support_tickets SET whatsapp_phone=%s,active_key=NULL,
            status='CLOSED',conversation_origin='STAFF',current_assignee_id='8' WHERE id=41""", (PHONE,))
    before = await repo.get_ticket(43)
    find_active = repo.find_active_ticket_by_phone
    lookups = 0

    async def missed_initial_lookup(phone_number, business_phone_number_id=None):
        nonlocal lookups
        lookups += 1
        # The next repository create/reopen sees a conversation that appeared
        # after the service lookup. No timing or concurrent customer sends.
        if lookups == 1:
            return None
        return await find_active(phone_number, business_phone_number_id)

    monkeypatch.setattr(repo, 'find_active_ticket_by_phone', missed_initial_lookup)
    result = await forward(original)
    assert result['message']['ticket_id'] == 43 and len(calls) == 1
    assert len(await sql('SELECT id FROM support_tickets')) == 3
    assert (await repo.get_ticket(43))['current_assignee_id'] == before['current_assignee_id']
    if attempt == 'reopen':
        previous = await repo.get_ticket(41)
        assert previous['status'] == 'CLOSED' and previous['active_key'] is None
        assert previous['current_assignee_id'] == '8'


@pytest.mark.asyncio
async def test_contact_forward_fences_destination_closed_before_persistence(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    original, calls = await setup_forward(infrastructure, monkeypatch, tmp_path)
    add_message = repo.add_ticket_message

    async def close_before_add(**kwargs):
        await sql("UPDATE support_tickets SET active_key=NULL,status='RESOLVED' WHERE id=43")
        return await add_message(**kwargs)

    monkeypatch.setattr(repo, 'add_ticket_message', close_before_add)
    with pytest.raises(service.SupportConflictError):
        await forward(original)
    assert not calls
    assert len(await repo.list_messages(43)) == 0


@pytest.mark.asyncio
async def test_contact_forward_never_resends_uncertain_result(infrastructure, monkeypatch, tmp_path):
    repo, _, _ = infrastructure
    original, _ = await setup_forward(infrastructure, monkeypatch, tmp_path)
    send = AsyncMock(side_effect=WhatsAppSendError('Ambiguous provider result', delivery_uncertain=True))
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send))
    first = await forward(original, key='uncertain-contact-forward')
    duplicate = await forward(original, key='uncertain-contact-forward')
    assert first['message']['id'] == duplicate['message']['id']
    assert first['message']['send_status'] == duplicate['message']['send_status'] == 'UNCERTAIN'
    assert (await repo.get_message(first['message']['id']))['sender_id'] == '7'
    send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement_exists', [False, True])
async def test_contact_forward_retry_keeps_uncertain_original_after_destination_closes(
    infrastructure, monkeypatch, tmp_path, replacement_exists,
):
    repo, sql, _ = infrastructure
    original, _ = await setup_forward(infrastructure, monkeypatch, tmp_path)
    send = AsyncMock(side_effect=WhatsAppSendError('Ambiguous provider result', delivery_uncertain=True))
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send))
    first = await forward(original, key='uncertain-closed-contact-forward')
    await sql("UPDATE support_tickets SET active_key=NULL,status='RESOLVED' WHERE id=43")
    replacement = None
    if replacement_exists:
        replacement = await repo.create_staff_chat(
            ticket_reference='REPLACEMENT-CHAT', phone_number=PHONE,
            business_phone_number_id='90005', business_display_phone_number=None,
            customer_id=None, customer_userid=None, customer_name='Replacement contact',
            agent_id='8', agent_name='Other',
        )
    before = await sql('SELECT * FROM support_tickets ORDER BY id')
    duplicate = await forward(original, key='uncertain-closed-contact-forward')
    assert duplicate['message']['id'] == first['message']['id']
    assert duplicate['message']['ticket_id'] == 43
    assert duplicate['message']['send_status'] == 'UNCERTAIN'
    assert await sql('SELECT * FROM support_tickets ORDER BY id') == before
    if replacement:
        assert len(await repo.list_messages(replacement['id'])) == 0
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_contact_forward_retry_rejects_changed_source_content(infrastructure, monkeypatch, tmp_path):
    _, sql, _ = infrastructure
    original, calls = await setup_forward(infrastructure, monkeypatch, tmp_path)
    await forward(original, key='edited-source-contact-forward')
    await sql('UPDATE support_ticket_messages SET body=%s WHERE id=%s', ('Changed source content', original['id']))
    with pytest.raises(service.SupportConflictError):
        await forward(original, key='edited-source-contact-forward')
    assert len(calls) == 1
