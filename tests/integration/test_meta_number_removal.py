"""Saved-number retirement uses real MariaDB; no requests to Meta are allowed."""
import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.support_auth import SupportAgent
from app.whatsapp_registry import (
    WhatsAppRegistryRepository, WhatsAppRegistryService, WhatsAppRegistryConflictError,
    WhatsAppRegistryValidationError,
)

LINE = '1375394715655384'
WABA = '26526540993689063'
MANAGER = SupportAgent(id='8', name='Manager', role='supervisor')


async def registry(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    import app.whatsapp_registry as module
    monkeypatch.setattr(settings, 'whatsapp_phone_number_id', LINE)
    monkeypatch.setattr(module, 'configured_linked_device_lines', lambda: {})
    monkeypatch.setattr(module, 'get_transport', lambda *_: 'cloud')
    await sql("INSERT INTO whatsapp_business_accounts (waba_id) VALUES (%s)", (WABA,))
    await sql("""INSERT INTO whatsapp_phone_numbers
        (phone_number_id,waba_id,display_phone_number,display_name,source,is_enabled)
        VALUES (%s,%s,'96176063972','Old office','META_SYNC',0)""", (LINE, WABA))
    storage = WhatsAppRegistryRepository()
    storage._pool = repo._pool
    return WhatsAppRegistryService(storage), sql


@pytest.mark.asyncio
async def test_remove_retains_history_and_survives_sync_webhooks_and_legacy_seed(infrastructure, monkeypatch):
    service, sql = await registry(infrastructure, monkeypatch)
    repo, _, _ = infrastructure
    await sql("UPDATE support_tickets SET business_phone_number_id=%s,status='RESOLVED',active_key=NULL WHERE id=42", (LINE,))
    message, _ = await repo.add_ticket_message(ticket_id=42, sender_type='CUSTOMER', sender_id=None,
        direction='INBOUND', body='Historical content', whatsapp_message_id='wamid.history')
    before = await sql('SELECT * FROM support_tickets WHERE id=42')
    assert (await service.remove_saved_number(MANAGER, LINE))['numbers'] == []
    assert not await sql('SELECT * FROM whatsapp_phone_numbers WHERE phone_number_id=%s', (LINE,))
    assert (await repo.get_message(message['id']))['body'] == 'Historical content'
    after = (await sql('SELECT * FROM support_tickets WHERE id=42'))[0]
    assert after['business_display_name'] == 'Old office'
    assert after['business_display_phone_number'] == '96176063972'
    assert after['business_number_retired_at'] is not None
    for column, value in before[0].items():
        if column not in {'business_display_name', 'business_display_phone_number', 'business_number_retired_at'}:
            assert after[column] == value, column
    assert (await repo.get_ticket(42))['display_name'] == 'Old office'
    # Re-discovery, including the configured legacy default, must not revive it.
    for numbers in ([], [{'id': LINE, 'display_phone_number': '96176063972', 'verified_name': 'Meta name'}]):
        await service.repository.persist_sync(waba_id=WABA, account_name='Business', numbers=numbers, actor_id=8)
        assert (await service.list_payload(MANAGER))['numbers'] == []
    assert not await service.observe_inbound(waba_id=WABA, phone_number_id=LINE, display_phone_number='96176063972')
    assert not await service.observe_inbound(waba_id=None, phone_number_id=None, display_phone_number=None)
    assert not await service.is_number_enabled(LINE)
    assert await service.list_enabled_numbers(SupportAgent(id='7', name='Agent', role='agent')) == []
    for method, value in ((service.set_number_enabled, True), (service.set_number_display_name, 'New name')):
        with pytest.raises((WhatsAppRegistryConflictError, WhatsAppRegistryValidationError), match='removed|not available'):
            await method(MANAGER, LINE, value)
    assert (await service.list_payload(MANAGER))['numbers'] == []
    audit = await sql("SELECT actor_staff_id FROM whatsapp_integration_audit WHERE action='META_NUMBER_REMOVED'")
    assert audit == [{'actor_staff_id': 8}]
    # Saved history remains visible but cannot reopen via a stale composer.
    from app.support_service import support_service
    from app.support_repository import SupportConflictError
    with pytest.raises(SupportConflictError, match='read-only'):
        await support_service._prepare_agent_outbound_ticket(42, MANAGER)
    with pytest.raises(SupportConflictError, match='read-only'):
        await repo.reopen_staff_chat(42, agent_id=MANAGER.id, agent_name=MANAGER.name)


@pytest.mark.asyncio
async def test_concurrent_repeated_removal_is_idempotent_and_audited_once(infrastructure, monkeypatch):
    service, sql = await registry(infrastructure, monkeypatch)
    ready, release = asyncio.Event(), asyncio.Event()
    original = service.repository.remove_saved_meta_number
    calls = 0
    async def remove(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            ready.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(service.repository, 'remove_saved_meta_number', remove)
    tasks = [asyncio.create_task(service.remove_saved_number(MANAGER, LINE)) for _ in range(2)]
    await asyncio.wait_for(ready.wait(), 5)
    release.set()
    results = await asyncio.gather(*tasks)
    assert all(result['numbers'] == [] for result in results)
    await service.remove_saved_number(MANAGER, LINE)
    assert len(await sql("SELECT id FROM whatsapp_integration_audit WHERE action='META_NUMBER_REMOVED'")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('enable_first', [False, True])
async def test_removal_and_enabling_cannot_revive_retired_number(infrastructure, monkeypatch, enable_first):
    service, sql = await registry(infrastructure, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.repository.execute
    async def paused(query, params=()):
        if 'SET is_enabled' in query:
            entered.set()
            await release.wait()
        return await original(query, params)
    monkeypatch.setattr(service.repository, 'execute', paused)
    enabling = asyncio.create_task(service.set_number_enabled(MANAGER, LINE, True))
    await asyncio.wait_for(entered.wait(), 5)
    if enable_first:
        release.set()
        await enabling
        with pytest.raises(WhatsAppRegistryConflictError, match='before removing'):
            await service.remove_saved_number(MANAGER, LINE)
    else:
        await service.remove_saved_number(MANAGER, LINE)
        release.set()
        with pytest.raises(WhatsAppRegistryConflictError, match='permanently removed'):
            await enabling
    rows = await sql('SELECT is_enabled,removed_at FROM whatsapp_phone_numbers WHERE phone_number_id=%s', (LINE,))
    if enable_first:
        assert rows[0]['is_enabled'] and rows[0]['removed_at'] is None
    else:
        assert not rows


@pytest.mark.asyncio
async def test_removal_rejects_active_linked_and_unknown_numbers_and_migration_repeats(infrastructure, monkeypatch):
    service, sql = await registry(infrastructure, monkeypatch)
    await sql('UPDATE whatsapp_phone_numbers SET is_enabled=1 WHERE phone_number_id=%s', (LINE,))
    with pytest.raises(WhatsAppRegistryConflictError):
        await service.remove_saved_number(MANAGER, LINE)
    await sql('UPDATE whatsapp_phone_numbers SET is_enabled=0 WHERE phone_number_id=%s', (LINE,))
    await sql('UPDATE support_tickets SET business_phone_number_id=%s WHERE id=42', (LINE,))
    with pytest.raises(WhatsAppRegistryConflictError, match='active conversations'):
        await service.remove_saved_number(MANAGER, LINE)
    await sql("UPDATE whatsapp_phone_numbers SET is_enabled=0,source='LINKED_DEVICE' WHERE phone_number_id=%s", (LINE,))
    with pytest.raises(WhatsAppRegistryValidationError, match='Only saved Meta'):
        await service.remove_saved_number(MANAGER, LINE)
    with pytest.raises(WhatsAppRegistryValidationError):
        await service.remove_saved_number(MANAGER, '99999999')
    assert not await sql("SELECT id FROM whatsapp_integration_audit WHERE action='META_NUMBER_REMOVED'")
    for _ in range(2):
        await sql(Path('migrations/20261023_removed_meta_numbers.sql').read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize('reopen_first', [False, True])
async def test_remove_and_reopen_share_number_then_ticket_lock_order(infrastructure, monkeypatch, reopen_first):
    from app.support_repository import SupportConflictError
    service, sql = await registry(infrastructure, monkeypatch)
    repo, _, _ = infrastructure
    await sql("UPDATE support_tickets SET business_phone_number_id=%s,conversation_origin='STAFF',status='CLOSED',active_key=NULL WHERE id=42", (LINE,))
    entered, release = asyncio.Event(), asyncio.Event()
    original = repo._lock_available_whatsapp_line
    async def pause(conn, line):
        entered.set()
        await release.wait()
        await original(conn, line)
    monkeypatch.setattr(repo, '_lock_available_whatsapp_line', pause)
    task = asyncio.create_task(repo.reopen_staff_chat(42, agent_id='8', agent_name='Manager'))
    await asyncio.wait_for(entered.wait(), 5)
    if reopen_first:
        release.set()
        await task
        with pytest.raises(WhatsAppRegistryConflictError, match='active conversations'):
            await service.remove_saved_number(MANAGER, LINE)
    else:
        await service.remove_saved_number(MANAGER, LINE)
        release.set()
        with pytest.raises(SupportConflictError, match='read-only'):
            await task
    ticket = await repo.get_ticket(42)
    assert bool(ticket['active_key']) is reopen_first
    assert bool(ticket['business_number_removed_at']) is not reopen_first


@pytest.mark.asyncio
async def test_api_requires_valid_session_csrf_and_current_manager_role(infrastructure, monkeypatch):
    import app.support_api as api
    import app.support_auth as auth
    service, sql = await registry(infrastructure, monkeypatch)
    for session, staff in ((1, 7), (3, 8)):
        await sql("""INSERT INTO staff_sessions
            (id,staff_id,token_hash,csrf_hash,expires_at,created_at,last_seen_at)
            VALUES (%s,%s,%s,%s,UTC_TIMESTAMP(6)+INTERVAL 1 DAY,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
            (session, staff, auth._sha256(f'token-{session}'), auth._sha256('csrf')))
    monkeypatch.setattr(api, 'whatsapp_registry_service', service)
    monkeypatch.setattr(auth, '_schedule_session_touch', lambda *_: None)
    app = FastAPI()
    app.include_router(api.router)
    transport = httpx.MockTransport(httpx.ASGITransport(app=app).handle_async_request)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        path = f'/api/support/whatsapp-numbers/{LINE}'
        assert (await client.delete(path)).status_code == 401
        client.cookies.set(settings.support_cookie_name, 'token-1')
        headers = {'X-CSRF-Token': 'csrf'}
        assert (await client.delete(path, headers=headers)).status_code == 403
        client.cookies.set(settings.support_cookie_name, 'token-3')
        assert (await client.delete(path)).status_code == 403
        assert not (await sql('SELECT removed_at FROM whatsapp_phone_numbers WHERE phone_number_id=%s', (LINE,)))[0]['removed_at']
        for role in ('supervisor', 'admin'):
            await sql('UPDATE staff SET role=%s WHERE id=8', (role,))
            response = await client.delete(path, headers=headers)
            assert response.status_code == 200 and response.json()['numbers'] == []
        await sql("UPDATE staff SET is_active=0 WHERE id=8")
        assert (await client.delete(path, headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_confirmed_linked_removal_deletes_profile_and_preserves_history(infrastructure, monkeypatch):
    service, sql = await registry(infrastructure, monkeypatch)
    repo, _, _ = infrastructure
    await sql("UPDATE whatsapp_phone_numbers SET source='LINKED_DEVICE',provider_present=0 WHERE phone_number_id=%s", (LINE,))
    await sql("UPDATE support_tickets SET business_phone_number_id=%s,status='RESOLVED',active_key=NULL WHERE id=42", (LINE,))
    await service.repository.finish_linked_device_removal(LINE, actor_id=8, actor_name='Manager', close_active_tickets=False)
    assert not await sql('SELECT * FROM whatsapp_phone_numbers WHERE phone_number_id=%s', (LINE,))
    ticket = await repo.get_ticket(42)
    assert ticket['display_name'] == 'Old office' and ticket['business_number_removed_at']
    assert ticket['current_assignee_id'] == '7' and ticket['status'] == 'RESOLVED'
    assert await service.repository.finish_linked_device_removal(LINE, actor_id=8, actor_name='Manager', close_active_tickets=False) == []
    monkeypatch.setattr('app.whatsapp_registry.configured_linked_device_lines', lambda: {LINE: '96176063972'})
    monkeypatch.setattr('app.whatsapp_registry.get_transport', lambda *_: 'linked_device')
    await service._seed_legacy_number()
    assert not await sql('SELECT * FROM whatsapp_phone_numbers WHERE phone_number_id=%s', (LINE,))
    assert len(await sql("SELECT id FROM whatsapp_integration_audit WHERE action='LINKED_DEVICE_LINE_REMOVED'")) == 1
    for _ in range(2):
        await sql(Path('migrations/20261029_support_number_profile_deletion.sql').read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize('sync_first', [True, False])
async def test_sync_and_delete_serialize_without_resurrection(infrastructure, monkeypatch, sync_first):
    from contextlib import asynccontextmanager
    service, sql = await registry(infrastructure, monkeypatch)
    entered, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = service.repository.mutation_connection
    @asynccontextmanager
    async def pause_first():
        async with original() as conn:
            if not entered.is_set():
                entered.set()
                await release.wait()
            yield conn
    monkeypatch.setattr(service.repository, 'mutation_connection', pause_first)
    # Repository methods isolate the critical transactions from service startup seeding.
    async def sync():
        await service.repository.persist_sync(waba_id=WABA, account_name='Business',
            numbers=[{'id':LINE}], actor_id=8)
    async def remove():
        await service.repository.remove_saved_meta_number(LINE, actor_id=8)
    first = asyncio.create_task(sync() if sync_first else remove())
    await asyncio.wait_for(entered.wait(), 5)
    async def second():
        started.set()
        await (remove() if sync_first else sync())
    follower = asyncio.create_task(second())
    await asyncio.wait_for(started.wait(), 5)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, follower), 5)
    assert not await sql('SELECT * FROM whatsapp_phone_numbers WHERE phone_number_id=%s', (LINE,))
    assert len(await sql("SELECT id FROM whatsapp_integration_audit WHERE action='META_NUMBER_REMOVED'")) == 1
