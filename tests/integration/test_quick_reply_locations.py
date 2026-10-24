import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

import app.support_auth as auth
import app.support_service as service
from app.config import settings
from app.support_api import router
from app.support_auth import SupportAgent
from app.support_repository import SupportConflictError

PIN = {'latitude': 33.89, 'longitude': 35.5, 'name': 'Office', 'address': 'Reception'}
PDF = b'%PDF-1.4\n%%EOF\n'
MANAGER = SupportAgent(id='8', name='Manager', role='supervisor')
AGENT = SupportAgent(id='7', name='Agent', role='agent')


async def client_for(sql, monkeypatch):
    for ident in (7,8):
        await sql('''INSERT INTO staff_sessions(id,staff_id,token_hash,csrf_hash,expires_at,created_at,last_seen_at)
            VALUES (%s,%s,%s,%s,UTC_TIMESTAMP(6)+INTERVAL 1 DAY,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))''',
            (ident,ident,auth._sha256(f'token-{ident}'),auth._sha256('csrf')))
    monkeypatch.setattr(auth, '_schedule_session_touch', lambda _: None)
    app = FastAPI(); app.include_router(router)
    asgi = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=httpx.MockTransport(asgi.handle_async_request), base_url='http://test')


@pytest.mark.asyncio
async def test_location_private_crud_and_confirmed_send(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(settings, 'support_media_directory', str(tmp_path))
    calls = []
    async def send_location(**kwargs):
        calls.append(kwargs)
        return {'messages': [{'id': 'test-location-delivery'}]}
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_location=send_location))
    async with await client_for(sql, monkeypatch) as client:
        path = '/api/support/quick-replies'
        assert (await client.post(path, json={'title':'Office','location':PIN})).status_code == 401
        client.cookies.set(settings.support_cookie_name, 'token-7')
        assert (await client.post(path, json={'title':'Office','location':PIN})).status_code == 403
        client.headers['X-CSRF-Token'] = 'csrf'
        response = await client.post(path, json={'title':'Office','location':PIN})
        assert response.status_code == 201, response.text
        row = response.json(); ident = row['id']
        assert row['location'] == PIN and row['body'] == '' and 'staff_id' not in row
        assert not calls, 'Saving never sends to WhatsApp'
        client.cookies.set(settings.support_cookie_name, 'token-8')
        assert (await client.get(path)).json() == {'items': []}
        assert (await client.put(f'{path}/{ident}', json={'title':'Stolen','location':PIN})).status_code == 404
        assert (await client.delete(f'{path}/{ident}')).status_code == 404
        client.cookies.set(settings.support_cookie_name, 'token-7')
        assert (await client.put(f'{path}/{ident}', json={'body':'Wrong type'})).status_code == 409
        response = await client.put(f'{path}/{ident}', json={'title':'New office','location':{'maps_url':'https://www.google.com/maps?q=0,0'}})
        assert response.status_code == 200, response.text
        pin = response.json()['location']
        assert pin == {'latitude':0, 'longitude':0, 'name':'New office', 'address':''}
        assert (await client.put(f'{path}/{ident}/pdf', files={'file':('x.pdf',PDF,'application/pdf')})).status_code == 409
        # Existing send permission/ownership and duplicate-delivery guards still apply.
        payload = {**pin, 'client_message_id':'saved-location-send'}
        assert (await client.post('/api/support/tickets/43/location', json=payload)).status_code == 403
        for _ in range(2):
            sent = await client.post('/api/support/tickets/42/location', json=payload)
            assert sent.status_code == 200, sent.text
        assert len(calls) == 1 and calls[0]['latitude'] == 0 and calls[0]['name'] == 'New office'
        assert sent.json()['message']['send_status'] == 'SENT'
        assert (await client.delete(f'{path}/{ident}')).status_code == 200
        assert (await sql('SELECT COUNT(*) AS total FROM support_ticket_messages WHERE ticket_id=42'))[0]['total'] == 1
        await sql(Path('migrations/20261024_quick_reply_locations.sql').read_text())


@pytest.mark.asyncio
async def test_assignment_roles_csrf_selection_and_private_copies(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    async with await client_for(sql, monkeypatch) as client:
        path = '/api/support/quick-replies'
        client.cookies.set(settings.support_cookie_name, 'token-7')
        assert (await client.get(path+'/recipients')).status_code == 403
        client.headers['X-CSRF-Token'] = 'csrf'
        request = {'title':'Office','location':PIN,'recipient_ids':[7]}
        assert (await client.post(path+'/assign', json=request)).status_code == 403
        client.cookies.set(settings.support_cookie_name, 'token-8')
        del client.headers['X-CSRF-Token']
        assert (await client.post(path+'/assign', json=request)).status_code == 403
        client.headers['X-CSRF-Token'] = 'csrf'
        assert {row['id'] for row in (await client.get(path+'/recipients')).json()['items']} == {7,8}
        for role in ('supervisor', 'admin'):
            await sql('UPDATE staff SET role=%s WHERE id=8', (role,))
            response = await client.post(path+'/assign', json=request)
            assert response.status_code == 201, response.text
            assert response.json() == {'saved_count':1,'item':None}
        assert (await client.get(path)).json() == {'items':[]}
        # Both manager roles may add copies, without reading recipients' private lists.
        client.cookies.set(settings.support_cookie_name, 'token-7')
        rows = (await client.get(path)).json()['items']
        assert len(rows) == 2 and all(row['location'] == PIN for row in rows)
        assert (await client.put(path+'/'+str(rows[0]['id']), json={'title':'My pin','location':PIN})).status_code == 200
        client.cookies.set(settings.support_cookie_name, 'token-8')
        for invalid in ({}, {'recipient_ids':[999]}, {'recipient_ids':[7], 'all_staff':True}):
            assert (await client.post(path+'/assign', json={'body':'Hi', **invalid})).status_code == 422
        response = await client.post(path+'/assign', json={'body':'Team text','all_staff':True})
        assert response.status_code == 201 and response.json()['saved_count'] == 2
        assert response.json()['item']['body'] == 'Team text'
        await sql('UPDATE staff SET is_active=0 WHERE id=7')
        assert (await client.post(path+'/assign', json=request)).status_code == 422
        response = await client.post(path+'/assign', json={'body':'Active only','all_staff':True})
        assert response.json()['saved_count'] == 1


@pytest.mark.asyncio
async def test_pdf_assignment_uses_independent_files_and_atomic_limit(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(settings, 'support_media_directory', str(tmp_path))
    import app.support_repository as repository_module
    monkeypatch.setattr(repository_module, 'MAX_QUICK_REPLIES_PER_STAFF', 1)
    async with await client_for(sql, monkeypatch) as client:
        path = '/api/support/quick-replies'
        client.cookies.set(settings.support_cookie_name, 'token-7')
        client.headers['X-CSRF-Token'] = 'csrf'
        assert (await client.post(path+'/assign/pdf', files={'file':('x.pdf',PDF,'application/pdf')},data={'assignment':'{"all_staff":true}'})).status_code == 403
        client.cookies.set(settings.support_cookie_name, 'token-8')
        result = await client.post(path+'/assign/pdf', files={'file':('Guide.pdf',PDF,'application/pdf')}, data={'assignment':'{"all_staff":true,"title":"Guide"}'})
        assert result.status_code == 201, result.text
        own = result.json()['item']
        rows = await sql('SELECT * FROM support_quick_replies ORDER BY staff_id')
        assert len({row['attachment_key'] for row in rows}) == 2
        assert len(list((tmp_path/'quick-replies').glob('*.pdf'))) == 2
        assert (await client.delete(f"{path}/{own['id']}")).status_code == 200
        # One target is at capacity: no partial save to the manager and no leaked files.
        result = await client.post(path+'/assign/pdf', files={'file':('More.pdf',PDF,'application/pdf')}, data={'assignment':'{"all_staff":true}'})
        assert result.status_code == 409, result.text
        assert len(list((tmp_path/'quick-replies').glob('*.pdf'))) == 1
        assert len(await sql('SELECT * FROM support_quick_replies')) == 1
        client.cookies.set(settings.support_cookie_name, 'token-7')
        assert (await client.get(f"{path}/{rows[0]['id']}/pdf")).content == PDF


@pytest.mark.asyncio
async def test_deactivation_or_manager_demotion_before_assignment_rolls_back(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    original = repo.create_quick_reply_copies
    entered, release = asyncio.Event(), asyncio.Event()
    async def pause(*args, **kwargs):
        entered.set(); await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(repo, 'create_quick_reply_copies', pause)
    for statement in ('UPDATE staff SET is_active=0 WHERE id=7', "UPDATE staff SET role='agent' WHERE id=8"):
        entered.clear(); release.clear()
        task = asyncio.create_task(service.support_service.assign_quick_reply(MANAGER,title='Test',body='Hello',recipient_ids=[7,8]))
        await asyncio.wait_for(entered.wait(), 5)
        await sql(statement)
        release.set()
        with pytest.raises(SupportConflictError):
            await task
        assert not await sql('SELECT * FROM support_quick_replies')
        await sql("UPDATE staff SET is_active=1,role=IF(id=8,'supervisor','agent')")


@pytest.mark.asyncio
async def test_concurrent_assignments_respect_per_staff_limit(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    import app.support_repository as repository_module
    monkeypatch.setattr(repository_module, 'MAX_QUICK_REPLIES_PER_STAFF', 1)
    start = asyncio.Event()
    async def save():
        await start.wait()
        return await service.support_service.assign_quick_reply(MANAGER,title='Office',body='',location=PIN,recipient_ids=[8,7,7])
    first, second = asyncio.create_task(save()), asyncio.create_task(save())
    start.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, SupportConflictError) for result in results) == 1
    assert len(await sql('SELECT * FROM support_quick_replies')) == 2


@pytest.mark.asyncio
async def test_ambiguous_batch_commit_retains_each_referenced_pdf(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(settings, 'support_media_directory', str(tmp_path))
    original = repo.create_quick_reply_copies
    async def lose_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError('Commit acknowledgement lost')
    monkeypatch.setattr(repo, 'create_quick_reply_copies', lose_ack)
    with pytest.raises(OSError):
        await service.support_service.assign_quick_reply(MANAGER,title='Guide',body='',recipient_ids=[7,8],content=PDF,filename='Guide.pdf')
    rows = await sql('SELECT id,staff_id FROM support_quick_replies')
    assert len(rows) == 2
    for row in rows:
        owner = AGENT if row['staff_id'] == 7 else MANAGER
        assert (await service.support_service.get_quick_reply_pdf(owner,row['id']))[0] == PDF
    assert len(list((tmp_path/'quick-replies').glob('*.pdf'))) == 2


@pytest.mark.asyncio
async def test_reuse_existing_text_pdf_and_location_for_fresh_staff(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(settings, 'support_media_directory', str(tmp_path))
    text = await service.support_service.create_quick_reply(MANAGER,title='Hours',body='Open daily')
    pin = await service.support_service.create_quick_reply(MANAGER,title='Office',body='',location=PIN)
    pdf = await service.support_service.save_quick_reply_pdf(MANAGER,title='Guide',body='Read this',content=PDF,filename='Guide.pdf')
    # The source replies predate the new account; no re-entry or PDF upload is needed.
    new_id = await auth.staff_repository.create_staff(name='New staff',email='new@example.test',email_normalized='new@example.test',password_hash='unused',role='agent')
    async with await client_for(sql, monkeypatch) as client:
        client.cookies.set(settings.support_cookie_name,'token-8')
        client.headers['X-CSRF-Token'] = 'csrf'
        for source in (text,pin,pdf):
            response = await client.post(f"/api/support/quick-replies/{source['id']}/assign",json={'recipient_ids':[new_id]})
            assert response.status_code == 201, response.text
            assert response.json() == {'saved_count':1,'item':None}
        copies = await repo.list_quick_replies(new_id)
        assert len(copies) == 3
        assert len(await repo.list_quick_replies(8)) == 3
        assert not await repo.list_quick_replies(7)
        by_title = {row['title']:row for row in copies}
        assert by_title['Office']['location_latitude'] == (await repo.get_quick_reply(8,pin['id']))['location_latitude']
        assert by_title['Hours']['body'] == 'Open daily'
        assert by_title['Guide']['attachment_key'] != (await repo.get_quick_reply(8,pdf['id']))['attachment_key']
        await service.support_service.delete_quick_reply(MANAGER,pdf['id'])
        recipient = SupportAgent(id=str(new_id),name='New staff',role='agent')
        assert (await service.support_service.get_quick_reply_pdf(recipient,by_title['Guide']['id']))[0] == PDF


@pytest.mark.asyncio
async def test_share_existing_authorization_csrf_scope_and_all_excludes_owner(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    own = await service.support_service.create_quick_reply(MANAGER,title='Greeting',body='Hi')
    private = await service.support_service.create_quick_reply(AGENT,title='Private',body='Private text')
    async with await client_for(sql, monkeypatch) as client:
        url = f"/api/support/quick-replies/{own['id']}/assign"
        assert (await client.post(url,json={'recipient_ids':[7]})).status_code == 401
        client.cookies.set(settings.support_cookie_name,'token-8')
        assert (await client.post(url,json={'recipient_ids':[7]})).status_code == 403
        client.headers['X-CSRF-Token'] = 'csrf'
        assert (await client.post(f"/api/support/quick-replies/{private['id']}/assign",json={'recipient_ids':[7]})).status_code == 404
        assert (await client.post(url,json={'recipient_ids':[7],'body':'Override'})).status_code == 422
        for payload in ({}, {'recipient_ids':[8]}, {'recipient_ids':[999]}, {'recipient_ids':[7],'all_staff':True}):
            assert (await client.post(url,json=payload)).status_code == 422
        client.cookies.set(settings.support_cookie_name,'token-7')
        assert (await client.post(url,json={'recipient_ids':[7]})).status_code == 403
        client.cookies.set(settings.support_cookie_name,'token-8')
        for role in ('supervisor','admin'):
            await sql('UPDATE staff SET role=%s WHERE id=8',(role,))
            response = await client.post(url,json={'all_staff':True})
            assert response.status_code == 201 and response.json()['saved_count'] == 1
        assert len(await repo.list_quick_replies(8)) == 1, 'All excludes the source owner'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['edit','delete'])
async def test_share_source_change_rejects_stale_copies_and_cleans_files(infrastructure, monkeypatch, tmp_path, change):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(settings,'support_media_directory',str(tmp_path))
    source = await service.support_service.save_quick_reply_pdf(MANAGER,title='Guide',body='',content=PDF,filename='Guide.pdf')
    original = repo.create_quick_reply_copies
    entered, release = asyncio.Event(), asyncio.Event()
    async def pause(*args, **kwargs):
        entered.set(); await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(repo,'create_quick_reply_copies',pause)
    task = asyncio.create_task(service.support_service.share_quick_reply(MANAGER,source['id'],recipient_ids=[7]))
    await asyncio.wait_for(entered.wait(),5)
    if change == 'edit':
        await service.support_service.update_quick_reply(MANAGER,source['id'],title='Changed',body='New caption')
    else:
        await service.support_service.delete_quick_reply(MANAGER,source['id'])
    release.set()
    from app.support_repository import SupportNotFoundError
    with pytest.raises(SupportConflictError if change == 'edit' else SupportNotFoundError):
        await task
    assert not await repo.list_quick_replies(7)
    assert len(list((tmp_path/'quick-replies').glob('*.pdf'))) == (1 if change == 'edit' else 0)
