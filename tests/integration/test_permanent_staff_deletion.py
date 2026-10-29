"""Account removal preserves conversations while removing credentials/private data."""
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import app.staff_management as management
import app.support_auth as auth
import app.support_statistics as stats
from app.config import settings
from app.support_api import router
from app.support_auth import SupportAgent
from tests.integration.test_agent_statistics import sessions, pulse, AGENT


@pytest.mark.asyncio
async def test_permanent_deletion_api_and_statistics(infrastructure, monkeypatch, tmp_path):
    repo, sql, _ = infrastructure
    await sessions(sql)
    monkeypatch.setattr(management, 'staff_repository', auth.staff_repository)
    tracker = stats.StatisticsRepository(repo)
    monkeypatch.setattr(stats, 'statistics_repository', tracker)
    monkeypatch.setattr(auth, '_schedule_session_touch', lambda _: None)
    monkeypatch.setattr(settings, 'support_media_directory', str(tmp_path))
    from app.quick_reply_media import quick_reply_media_store
    pdf = quick_reply_media_store.save(b'%PDF-1.4\n%%EOF', 'private.pdf')
    await sql('INSERT INTO support_quick_replies(staff_id,title,body,attachment_key,created_at,updated_at) VALUES (7,\'private\',\'\',%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))', (pdf['attachment_key'],))
    await tracker.heartbeat(AGENT, pulse(1))
    await sql('''INSERT INTO support_agent_activity(staff_id,activity_day,started_at,ended_at)
        VALUES (7,'2026-10-23','2026-10-23 12:00:00','2026-10-23 12:01:00')''')
    await sql("UPDATE support_tickets SET status='RESOLVED',active_key=NULL,resolved_by_staff_id=7 WHERE id=42")
    await sql("""INSERT INTO support_ticket_events(ticket_id,event_type,actor_type,actor_id,actor_name,created_at)
        VALUES (42,'TICKET_RESOLVED','AGENT','7','Agent','2026-10-23 12:00:00')""")
    await sql("""INSERT INTO support_ticket_messages(ticket_id,sender_type,sender_id,sender_name,direction,body,created_at,updated_at)
        VALUES (42,'AGENT','7','Agent','OUTBOUND','retained history',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""")
    app = FastAPI(); app.include_router(router)
    transport = httpx.MockTransport(httpx.ASGITransport(app=app).handle_async_request)
    async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
        client.headers['X-CSRF-Token'] = 'csrf'
        assert (await client.delete('/api/support/staff/7')).status_code == 401
        client.cookies.set(settings.support_cookie_name,'token-1')
        assert (await client.delete('/api/support/staff/8')).status_code == 403
        client.cookies.set(settings.support_cookie_name,'token-3')
        assert (await client.delete('/api/support/staff/8')).status_code == 409
        result = await client.delete('/api/support/staff/7')
        assert result.status_code == 200, result.text
        assert 'attachment_keys' not in result.json()['staff']
        assert (await client.delete('/api/support/staff/7')).status_code == 404
        for table in ['staff','staff_sessions','support_quick_replies','support_agent_activity','support_agent_heartbeats']:
            column = 'id' if table == 'staff' else 'staff_id'
            assert not await sql(f'SELECT * FROM {table} WHERE {column}=7')
        assert not list(tmp_path.rglob('*.pdf'))
        ticket = (await sql('SELECT * FROM support_tickets WHERE id=42'))[0]
        assert ticket['current_assignee_id'] == '7' and ticket['resolved_by_staff_id'] is None
        queue = await repo.list_tickets(view='history', agent_id='8', can_view_all=True)
        assert queue['filter_options']['staff'] == [{'id': '8', 'name': 'Other'}]
        assert (await sql('SELECT actor_id FROM support_ticket_events WHERE ticket_id=42'))[0]['actor_id']=='7'
        assert (await sql('SELECT body FROM support_ticket_messages WHERE ticket_id=42'))[0]['body']=='retained history'
        path='/api/support/statistics?start=2026-10-23&end=2026-10-23&timezone_name=UTC'
        report = (await client.get(path)).json()
        assert [row['id'] for row in report['agents']] == [8]
        assert report['totals']['tickets_resolved'] == 0
        assert (await client.get(path+'&agent_id=7')).status_code==404
        client.cookies.set(settings.support_cookie_name,'token-1')
        assert (await client.get('/api/support/me')).status_code==401
    with pytest.raises(HTTPException) as error:
        await tracker.heartbeat(AGENT,pulse(2))
    assert error.value.status_code==401
    assert not await sql('SELECT * FROM support_agent_heartbeats WHERE staff_id=7')


@pytest.mark.asyncio
async def test_pdf_export_authorization_filters_and_deleted_staff(infrastructure,monkeypatch):
    from io import BytesIO
    from pypdf import PdfReader
    repo, sql, _=infrastructure
    await sessions(sql)
    monkeypatch.setattr(stats,'statistics_repository',stats.StatisticsRepository(repo))
    monkeypatch.setattr(auth,'_schedule_session_touch',lambda _:None)
    app=FastAPI(); app.include_router(router)
    transport=httpx.MockTransport(httpx.ASGITransport(app=app).handle_async_request)
    path='/api/support/statistics/pdf?start=2026-10-01&end=2026-10-23&timezone_name=UTC&rank_by=messages_sent&direction=asc&agent_id=8'
    async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
        assert (await client.get(path)).status_code==401
        client.cookies.set(settings.support_cookie_name,'token-1')
        assert (await client.get(path)).status_code==403
        client.cookies.set(settings.support_cookie_name,'token-3')
        for role in ['supervisor','admin']:
            await sql('UPDATE staff SET role=%s WHERE id=8',(role,))
            result=await client.get(path)
            assert result.status_code==200,result.text
            assert result.headers['content-type']=='application/pdf'
            assert 'attachment;' in result.headers['content-disposition']
            assert 'no-store' in result.headers['cache-control']
            text='\n'.join(p.extract_text() for p in PdfReader(BytesIO(result.content)).pages)
            assert 'Staff filter: Other' in text and 'Lowest first' in text
            assert 'Outbound messages sent' in text
        assert (await client.get(path+'&start=2026-11-01')).status_code==422
        await sql('UPDATE staff SET deleted_at=UTC_TIMESTAMP(6),is_active=0 WHERE id=7')
        report=await stats.statistics_repository.report(datetime(2026,10,1),datetime(2026,11,1),None,'messages_sent',True)
        assert [row['id'] for row in report['rows']]==[8]
        await sql('UPDATE staff_sessions SET revoked_at=UTC_TIMESTAMP(6) WHERE id=3')
        assert (await client.get(path)).status_code==401


@pytest.mark.asyncio
async def test_concurrent_deletion_is_atomic(infrastructure):
    import asyncio
    from app.staff_repository import StaffNotFoundError
    _, sql, _ = infrastructure
    ready, release = asyncio.Event(), asyncio.Event()
    entered = 0
    async def remove():
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await release.wait()
        try:
            await auth.staff_repository.delete_staff(7)
            return 'deleted'
        except StaffNotFoundError:
            return 'not found'
    tasks = [asyncio.create_task(remove()), asyncio.create_task(remove())]
    await ready.wait()
    release.set()
    assert sorted(await asyncio.gather(*tasks)) == ['deleted','not found']
    assert not await sql('SELECT id FROM staff WHERE id=7')
    assert len(await sql('SELECT id FROM support_tickets')) == 3
