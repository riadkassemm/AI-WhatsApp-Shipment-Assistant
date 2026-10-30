"""Quote navigation uses real storage and must never mutate chat state."""
import pytest

from app.support_auth import SupportAgent
from app.support_service import (
    support_service, _serialize_support_messages,
    SupportForbiddenError, SupportNotFoundError,
)

AGENT = SupportAgent(id='7', name='Agent', role='agent')


async def message(repo, ticket=42, reference=None, provider_id=None, kind='text'):
    row, _ = await repo.add_ticket_message(
        ticket_id=ticket, sender_type='CUSTOMER', direction='INBOUND',
        body='Synthetic test message', message_type=kind,
        whatsapp_message_id=provider_id, reply_to_whatsapp_message_id=reference,
    )
    return row


@pytest.mark.asyncio
async def test_old_reply_targets_all_types_and_contiguous_pagination(infrastructure):
    repo, sql, _ = infrastructure
    originals = []
    for kind in ('text', 'image', 'audio', 'video', 'document', 'sticker', 'location', 'contacts'):
        originals.append(await message(repo, provider_id='quote-'+kind, kind=kind))
    for _ in range(155):
        await message(repo)
    replies = [await message(repo, reference=row['whatsapp_message_id']) for row in originals]
    before = await sql('SELECT * FROM support_tickets WHERE id=42')
    events = await sql('SELECT * FROM support_ticket_events WHERE ticket_id=42')
    previews = _serialize_support_messages(42, replies)
    assert all(row['reply_to']['source_message_id'] == row['id'] for row in previews)
    assert all(row['reply_to']['body'] == 'Earlier message' for row in previews)
    for original, reply in zip(originals, replies):
        result = await support_service.reply_destination(42, reply['id'], AGENT)
        assert result['message_id'] == original['id']
        assert result['ticket']['id'] == 42
        assert result['messages_has_more'] is True
        assert len(result['messages']) == 150
        earlier = await support_service.messages_page(
            42, AGENT, before_message_id=result['messages'][0]['id'], limit=100,
        )
        assert original['id'] in {row['id'] for row in earlier['messages']}
        assert earlier['messages_has_more'] is False
    assert await sql('SELECT * FROM support_tickets WHERE id=42') == before
    assert await sql('SELECT * FROM support_ticket_events WHERE ticket_id=42') == events


@pytest.mark.asyncio
async def test_reply_lookup_access_hidden_deleted_and_forged_references(infrastructure):
    repo, sql, _ = infrastructure
    target = await message(repo, provider_id='original')
    source = await message(repo, reference='original')
    with pytest.raises(SupportForbiddenError):
        await support_service.reply_destination(42, source['id'], SupportAgent(id='9', name='Other', role='agent'))
    for hidden in (source, target):
        await sql('INSERT INTO support_message_user_deletions (message_id,user_id) VALUES (%s,%s)', (hidden['id'], '7'))
        with pytest.raises(SupportNotFoundError):
            await support_service.reply_destination(42, source['id'], AGENT)
        await sql('DELETE FROM support_message_user_deletions WHERE message_id=%s', (hidden['id'],))
    await sql('UPDATE support_ticket_messages SET deleted_for_everyone_at=UTC_TIMESTAMP(6) WHERE id=%s', (target['id'],))
    result = await support_service.reply_destination(42, source['id'], AGENT)
    deleted = next(row for row in result['messages'] if row['id'] == target['id'])
    assert deleted['body'] is None and deleted['deleted_for_everyone_at']
    foreign = await message(repo, ticket=43, provider_id='foreign')
    await sql('UPDATE support_ticket_messages SET reply_to_whatsapp_message_id=%s WHERE id=%s', ('foreign', source['id']))
    # Different customer remains hidden even after ticket closes.
    for status in ('ASSIGNED', 'RESOLVED'):
        await sql('UPDATE support_tickets SET status=%s, active_key=NULL WHERE id=43', (status,))
        with pytest.raises(SupportNotFoundError):
            await support_service.reply_destination(42, source['id'], AGENT)
    # Same customer but wrong business line is also forbidden.
    await sql("UPDATE support_tickets SET whatsapp_phone='155500042', business_phone_number_id='90006' WHERE id=43")
    with pytest.raises(SupportNotFoundError):
        await support_service.reply_destination(42, source['id'], AGENT)
    with pytest.raises(SupportNotFoundError):
        await support_service.reply_destination(42, foreign['id'], AGENT)
    missing = await message(repo, reference='not-stored')
    with pytest.raises(SupportNotFoundError):
        await support_service.reply_destination(42, missing['id'], AGENT)


@pytest.mark.asyncio
async def test_cross_ticket_quote_in_shared_history_does_not_grant_operations(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    await sql("UPDATE support_tickets SET whatsapp_phone='155500042', active_key=NULL, status='RESOLVED' WHERE id=41")
    from app.support_media import SupportMediaStore
    import app.support_service as service
    monkeypatch.setattr(service.settings, 'support_media_directory', str(tmp_path))
    store = SupportMediaStore()
    monkeypatch.setattr(service, 'support_media_store', store)
    original = await message(repo, ticket=41, provider_id='historical', kind='audio')
    store.save(ticket_id=41, message_id=original['id'], content=b'synthetic audio', mime_type='audio/ogg', filename='voice.ogg')
    reply = await message(repo, reference='historical')
    result = await support_service.reply_destination(42, reply['id'], AGENT)
    assert result['message_id'] == original['id'] and result['ticket']['id'] == 41
    assert result['messages'][0]['media_url'] == f"/api/support/tickets/41/messages/{original['id']}/media?context_ticket_id=42"
    historical_reply = await message(repo, ticket=41, reference='historical')
    assert (await support_service.reply_destination(42, historical_reply['id'], AGENT))['message_id'] == original['id']
    assert (await repo.get_ticket(41))['current_assignee_id'] is None
    # Reopening historical conversation makes the shared-history grant expire.
    await sql("UPDATE support_tickets SET active_key='reopened',status='ASSIGNED',current_assignee_id='8' WHERE id=41")
    with pytest.raises(SupportNotFoundError):
        await support_service.reply_destination(42, historical_reply['id'], AGENT)


@pytest.mark.asyncio
async def test_reply_routes_require_current_session_and_ticket_access(infrastructure, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.config import settings
    from app.support_api import router
    import app.support_auth as auth
    from tests.integration.test_quick_reply_pdfs import create_sessions

    repo, sql, _ = infrastructure
    await create_sessions(sql)
    monkeypatch.setattr(auth, '_schedule_session_touch', lambda _: None)
    original = await message(repo, provider_id='route-original')
    source = await message(repo, reference='route-original')
    app = FastAPI()
    app.include_router(router)
    asgi = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(asgi.handle_async_request), base_url='http://test') as client:
        for kind in ('tickets', 'chats'):
            path = f'/api/support/{kind}/42/messages/{source["id"]}/reply'
            client.cookies.clear()
            assert (await client.get(path)).status_code == 401
            client.cookies.set(settings.support_cookie_name, 'token-7')
            result = await client.get(path)
            assert result.status_code == 200 and result.json()['message_id'] == original['id']
            forbidden = await client.get(f'/api/support/{kind}/43/messages/{source["id"]}/reply')
            assert forbidden.status_code == 403
        await sql("UPDATE staff SET is_active=0 WHERE id=7")
        assert (await client.get(path)).status_code == 401
