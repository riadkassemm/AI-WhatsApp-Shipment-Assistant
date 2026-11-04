"""History actions retain source identity and claimed-conversation authority."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import app.support_auth as auth
import app.support_service as service
from app.config import settings
from app.support_api import router
from app.support_auth import SupportAgent
from app.support_media import SupportMediaStore
from tests.integration.test_quick_reply_pdfs import create_sessions

AGENT = SupportAgent(id='7', name='Agent', role='agent')
PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n'


async def setup_history(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    await sql("""UPDATE support_tickets SET whatsapp_phone='155500042',
        active_key=NULL,status='RESOLVED',current_assignee_id='8' WHERE id=41""")
    original, _ = await repo.add_ticket_message(
        ticket_id=41, sender_type='CUSTOMER', direction='INBOUND', body='Earlier question',
        whatsapp_message_id='ld:90005:HISTORY',
    )
    monkeypatch.setattr(settings, 'support_media_directory', str(tmp_path))
    monkeypatch.setattr(service, 'support_media_store', SupportMediaStore())
    monkeypatch.setattr(service, 'publish_support_event', AsyncMock())
    monkeypatch.setattr(service, 'get_transport', lambda *_: 'linked_device')
    calls = []

    async def send(**kwargs):
        calls.append(kwargs)
        return {'messages': [{'id': f'ld:90005:RESULT{len(calls)}'}]}

    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(
        send_text=send, send_media=send, send_location=send, send_reaction=send,
        upload_media=AsyncMock(return_value='synthetic-file'),
    ))
    return original, calls


def api_client():
    app = FastAPI()
    app.include_router(router)
    asgi = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=httpx.MockTransport(asgi.handle_async_request), base_url='http://test',
        cookies={settings.support_cookie_name: 'token-7'}, headers={'X-CSRF-Token': 'csrf'},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text', 'location', 'document'])
async def test_history_reply_routes_send_on_claimed_ticket(infrastructure, monkeypatch, tmp_path, kind):
    repo, sql, _ = infrastructure
    original, calls = await setup_history(infrastructure, monkeypatch, tmp_path)
    await create_sessions(sql)
    monkeypatch.setattr(auth, '_schedule_session_touch', lambda _: None)
    previous = await repo.get_ticket(41)
    async with api_client() as client:
        for _ in range(2):
            payload = {'client_message_id': 'historical-reply', 'reply_to_message_id': original['id']}
            if kind == 'text':
                response = await client.post('/api/support/tickets/42/messages', json={**payload, 'body': 'Answer'})
            elif kind == 'location':
                response = await client.post('/api/support/tickets/42/location', json={
                    **payload, 'latitude': 1, 'longitude': 2, 'name': 'Office',
                })
            else:
                response = await client.post('/api/support/tickets/42/media',
                    data={**payload, 'media_type': 'document'}, files={'file': ('Guide.pdf', PDF, 'application/pdf')})
            assert response.status_code == 200, response.text
        sent = response.json()['message']
    assert len(calls) == 1
    assert calls[0]['context_message_id'] == original['whatsapp_message_id']
    assert calls[0]['phone_number_id'] == '90005' and calls[0]['to'] == '155500042'
    stored = await repo.get_message(sent['id'])
    assert stored['ticket_id'] == 42 and stored['sender_id'] == '7'
    assert stored['reply_to_whatsapp_message_id'] == original['whatsapp_message_id']
    destination = await service.support_service.reply_destination(42, sent['id'], AGENT)
    assert destination['ticket']['id'] == 41 and destination['message_id'] == original['id']
    assert await repo.get_ticket(41) == previous


@pytest.mark.asyncio
async def test_history_reaction_route_keeps_source_and_deduplicates(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    original, calls = await setup_history(infrastructure, monkeypatch, tmp_path)
    await create_sessions(sql)
    monkeypatch.setattr(auth, '_schedule_session_touch', lambda _: None)
    previous = await repo.get_ticket(41)
    path = f'/api/support/tickets/41/messages/{original["id"]}/reaction'
    payload = {'emoji': '👍', 'client_reaction_id': 'history-reaction', 'context_ticket_id': 42}
    async with api_client() as client:
        client.cookies.clear()
        assert (await client.post(path, json=payload)).status_code == 401
        client.cookies.set(settings.support_cookie_name, 'token-7')
        client.headers.pop('X-CSRF-Token')
        assert (await client.post(path, json=payload)).status_code == 403
        client.headers['X-CSRF-Token'] = 'csrf'
        for _ in range(2):
            response = await client.post(path, json=payload)
            assert response.status_code == 200, response.text
        reaction = response.json()['reaction']
        assert reaction['ticket_id'] == 41 and reaction['message_id'] == original['id']
        assert reaction['reactor_id'] == '7'
        assert (await client.post(path, json={**payload, 'emoji': '❤️'})).status_code == 409
        client.cookies.set(settings.support_cookie_name, 'token-8')
        assert (await client.post(path, json=payload)).status_code == 403
    assert len(calls) == 1 and calls[0]['message_id'] == original['whatsapp_message_id']
    assert calls[0]['phone_number_id'] == '90005'
    updated = await repo.get_ticket(41)
    assert {key: value for key, value in updated.items() if key not in {'updated_at', 'last_activity_at'}} == {
        key: value for key, value in previous.items() if key not in {'updated_at', 'last_activity_at'}
    }
    event = service.publish_support_event.call_args
    assert event.kwargs['ticket_id'] == 41
    assert event.kwargs['reactor_type'] == 'AGENT'


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', [
    'unclaimed', 'transferred', 'reopened_source', 'other_customer', 'other_line',
    'hidden', 'deleted', 'relinked_reference', 'closed_staff_context',
])
async def test_history_outbound_rejects_invalid_authority(infrastructure, monkeypatch, tmp_path, boundary):
    repo, sql, _ = infrastructure
    original, calls = await setup_history(infrastructure, monkeypatch, tmp_path)
    mutations = {
        'unclaimed': "UPDATE support_tickets SET status='NEW',current_assignee_id=NULL WHERE id=42",
        'transferred': "UPDATE support_tickets SET current_assignee_id='8' WHERE id=42",
        'reopened_source': "UPDATE support_tickets SET active_key='reopened',status='ASSIGNED' WHERE id=41",
        'other_customer': "UPDATE support_tickets SET whatsapp_phone='155599999' WHERE id=41",
        'other_line': "UPDATE support_tickets SET business_phone_number_id='90006' WHERE id=41",
        'closed_staff_context': "UPDATE support_tickets SET conversation_origin='STAFF',active_key=NULL,status='CLOSED' WHERE id=42",
    }
    if boundary in mutations:
        await sql(mutations[boundary])
    elif boundary == 'hidden':
        await sql('INSERT INTO support_message_user_deletions(message_id,user_id) VALUES (%s,%s)', (original['id'], '7'))
    elif boundary == 'deleted':
        await sql('UPDATE support_ticket_messages SET deleted_for_everyone_at=UTC_TIMESTAMP(6) WHERE id=%s', (original['id'],))
    else:
        await sql("UPDATE support_ticket_messages SET whatsapp_message_id='ld:90004:HISTORY' WHERE id=%s", (original['id'],))
    before = await sql('SELECT * FROM support_tickets ORDER BY id')
    errors = (service.SupportForbiddenError, service.SupportNotFoundError,
              service.SupportConflictError, service.SupportValidationError)
    with pytest.raises(errors):
        await service.support_service.send_agent_reaction(41, original['id'], agent=AGENT,
            emoji='👍', client_reaction_id='denied-reaction', context_ticket_id=42)
    # Starting a new send on a closed staff chat intentionally reopens it. The
    # new history reaction context must never invoke that separate behavior.
    if boundary != 'closed_staff_context':
        with pytest.raises(errors):
            await service.support_service.send_agent_message(42, agent=AGENT, body='Answer',
                client_message_id='denied-reply', reply_to_message_id=original['id'])
    assert not calls
    assert await sql('SELECT * FROM support_tickets ORDER BY id') == before


@pytest.mark.asyncio
async def test_history_forward_and_delete_keep_existing_permissions(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    original, calls = await setup_history(infrastructure, monkeypatch, tmp_path)
    previous = await repo.get_ticket(41)
    for _ in range(2):
        forwarded = await service.support_service.forward_message(source_ticket_id=41,
            source_message_id=original['id'], destination_ticket_id=42, agent=AGENT, client_message_id='history-forward')
        assert forwarded['forwarded_from_ticket_id'] == 41
        assert forwarded['forwarded_from_message_id'] == original['id']
    assert len(calls) == 1
    with pytest.raises(service.SupportForbiddenError):
        await service.support_service.forward_message(source_ticket_id=41,
            source_message_id=original['id'], destination_ticket_id=43, agent=AGENT, client_message_id='forbidden-forward')
    await service.support_service.change_message(41, original['id'], agent=AGENT, action='me')
    assert await repo.message_hidden_for_user(original['id'], '7')
    assert not await repo.message_hidden_for_user(original['id'], '8')
    with pytest.raises(service.SupportNotFoundError):
        await service.support_service.forward_message(source_ticket_id=41,
            source_message_id=original['id'], destination_ticket_id=42, agent=AGENT, client_message_id='hidden-forward')
    assert await repo.get_ticket(41) == previous


@pytest.mark.asyncio
async def test_history_everyone_delete_requires_original_sender(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    await setup_history(infrastructure, monkeypatch, tmp_path)
    monkeypatch.setattr(service, 'get_transport', lambda *_: 'linked_device')
    delete = AsyncMock(return_value={'confirmed': True, 'id': 'ld:90005:OWN7', 'timestamp': int(time.time() * 1000)})
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(delete_message=delete))
    for sender in ('7', '8'):
        row, _ = await repo.add_ticket_message(ticket_id=41, sender_type='AGENT', sender_id=sender,
            direction='OUTBOUND', body='Earlier answer', send_status='SENT',
            whatsapp_message_id=f'ld:90005:OWN{sender}', whatsapp_timestamp=str(int(time.time())))
        if sender == '7':
            result = await service.support_service.change_message(41, row['id'], agent=AGENT, action='everyone')
            assert result['deleted_for_everyone_at']
        else:
            with pytest.raises(service.SupportForbiddenError):
                await service.support_service.change_message(41, row['id'], agent=AGENT, action='everyone')
    assert delete.await_count == 1
    assert (await repo.get_ticket(41))['status'] == 'RESOLVED'
    assert (await repo.get_ticket(42))['current_assignee_id'] == '7'
