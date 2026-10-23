"""Real MariaDB/Redis; authenticated ASGI requests, no provider traffic."""
import asyncio
from datetime import date, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import app.support_statistics as stats
from app.support_auth import SupportAgent, _sha256

BASE = datetime(2026, 10, 23, 12)
AGENT = SupportAgent(id='7', name='Agent', role='agent', session_id=1)


async def sessions(sql):
    for session, staff in [(1, 7), (2, 7), (3, 8)]:
        await sql("""INSERT INTO staff_sessions
            (id,staff_id,token_hash,csrf_hash,expires_at,created_at,last_seen_at)
            VALUES (%s,%s,%s,%s,UTC_TIMESTAMP(6)+INTERVAL 1 DAY,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
            (session, staff, _sha256(f'token-{session}'), _sha256('csrf')))


def pulse(sequence, tab=1, **kw):
    return stats.ActivityHeartbeat(tab_id=UUID(int=tab), sequence=sequence, active=True,
                                   activity_age_seconds=0, **kw)


async def activity_seconds(sql):
    rows = await sql('SELECT SUM(TIMESTAMPDIFF(MICROSECOND,started_at,ended_at))/1000000 AS seconds FROM support_agent_activity')
    return float(rows[0]['seconds'] or 0)


@pytest.mark.asyncio
async def test_activity_union_concurrent_tabs_and_sessions(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    await sessions(sql)
    tracker = stats.StatisticsRepository(repo)
    now = BASE
    async def clock(cur): return now
    monkeypatch.setattr(tracker, '_now', clock)
    other_session = SupportAgent(id='7', name='Agent', role='agent', session_id=2)
    await tracker.heartbeat(AGENT, pulse(1))
    now += timedelta(seconds=5)
    await tracker.heartbeat(other_session, pulse(1, tab=2))
    now = BASE + timedelta(seconds=20)
    ready, release = asyncio.Event(), asyncio.Event()
    entered = 0
    async def concurrent(agent, tab):
        nonlocal entered
        entered += 1
        if entered == 2: ready.set()
        await release.wait()
        await tracker.heartbeat(agent, pulse(2, tab=tab))
    tasks = [asyncio.create_task(concurrent(AGENT, 1)), asyncio.create_task(concurrent(other_session, 2))]
    await ready.wait(); release.set()
    await asyncio.gather(*tasks)
    assert await activity_seconds(sql) == 20  # overlap counted once, not 35
    now += timedelta(seconds=10)
    await tracker.heartbeat(AGENT, pulse(1))  # stale request must not rewind baseline
    assert await activity_seconds(sql) == 20
    await tracker.heartbeat(AGENT, pulse(3))
    assert await activity_seconds(sql) == 30
    assert len(await sql('SELECT * FROM support_agent_activity')) == 1


@pytest.mark.asyncio
async def test_activity_idle_resume_gaps_and_midnight(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    await sessions(sql)
    tracker = stats.StatisticsRepository(repo)
    now = datetime(2026, 10, 23, 23, 59, 50)
    async def clock(cur): return now
    monkeypatch.setattr(tracker, '_now', clock)
    await tracker.heartbeat(AGENT, stats.ActivityHeartbeat(tab_id=UUID(int=1), sequence=1, active=True, activity_age_seconds=55))
    now += timedelta(seconds=15)
    await tracker.heartbeat(AGENT, pulse(2))
    assert await activity_seconds(sql) == 5  # capped at last known inactivity deadline
    now += timedelta(seconds=15)
    await tracker.heartbeat(AGENT, pulse(3))
    assert await activity_seconds(sql) == 20
    now += timedelta(seconds=100)
    await tracker.heartbeat(AGENT, pulse(4))
    assert await activity_seconds(sql) == 20  # disconnected gap is not credited
    now += timedelta(seconds=10)
    await tracker.heartbeat(AGENT, pulse(5, reset=True))
    assert await activity_seconds(sql) == 20  # resume cannot bridge hidden time
    now += timedelta(seconds=15)
    await tracker.heartbeat(AGENT, pulse(6))
    assert await activity_seconds(sql) == 35
    # Explicit interval crossing midnight is split, preserving range accuracy.
    async with repo._pool.acquire() as conn:
        async with conn.cursor(stats.aiomysql.DictCursor) as cur:
            await tracker._merge_interval(cur, 8, datetime(2026, 10, 24, 23, 59, 55), datetime(2026, 10, 25, 0, 0, 5))
    report = await tracker.report(datetime(2026, 10, 25), datetime(2026, 10, 26), 8, 'active_seconds', True)
    assert report['totals']['active_seconds'] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    'UPDATE staff_sessions SET revoked_at=UTC_TIMESTAMP(6)',
    'UPDATE staff_sessions SET expires_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND',
    'UPDATE staff SET is_active=0 WHERE id=7',
])
async def test_heartbeat_fails_closed(infrastructure, mutation):
    repo, sql, _ = infrastructure
    await sessions(sql)
    await sql(mutation)
    with pytest.raises(HTTPException) as error:
        await stats.StatisticsRepository(repo).heartbeat(AGENT, pulse(1))
    assert error.value.status_code == 401
    assert not await sql('SELECT * FROM support_agent_heartbeats')


@pytest.mark.asyncio
async def test_actor_attribution_confirmation_range_filters_and_rankings(infrastructure):
    repo, sql, _ = infrastructure
    # All ticket rows belong to 8 now; their original actors must still get credit.
    await sql("UPDATE support_tickets SET current_assignee_id='8'")
    for actor, kind, ticket, stamp in [
        ('7','TICKET_CLAIMED',41,BASE), ('7','TICKET_CLAIMED',41,BASE),
        ('7','TICKET_CLAIMED',42,BASE), ('8','TICKET_CLAIMED',43,BASE),
        ('8','TICKET_RESOLVED',41,BASE), ('8','TICKET_RESOLVED',42,BASE),
        ('7','TICKET_RESOLVED',43,BASE), ('7','TICKET_RESOLVED',43,BASE),
        ('7','TICKET_RESOLVED',42,BASE+timedelta(days=1)),
    ]:
        await sql("""INSERT INTO support_ticket_events(ticket_id,event_type,actor_type,actor_id,created_at)
            VALUES (%s,%s,'AGENT',%s,%s)""", (ticket,kind,actor,stamp))
    await sql("""INSERT INTO support_ticket_events(ticket_id,event_type,actor_type,actor_id,created_at)
        VALUES (43,'TICKET_RESOLVED','SYSTEM','7',%s)""", (BASE,))
    for index, (sender, kind, status, provider) in enumerate([
        ('7','AGENT','SENT','a'), ('7','AGENT','SENT','b'), ('8','AGENT','SENT','c'),
        ('7','AI','SENT','d'), ('7','SYSTEM','SENT','e'), (None,'AGENT','SENT','f'),
        ('7','AGENT','UNCERTAIN','g'), ('7','AGENT','FAILED',None), ('7','AGENT','PENDING',None),
        ('7','AGENT','SENT',None),
    ]):
        await sql("""INSERT INTO support_ticket_messages(ticket_id,sender_type,sender_id,direction,body,
            send_status,whatsapp_message_id,created_at,delivery_started_at,updated_at)
            VALUES (42,%s,%s,'OUTBOUND','fixture',%s,%s,%s,%s,%s)""",
            (kind,sender,status,provider,BASE-timedelta(days=30),BASE,BASE+timedelta(days=2)))
    # Deleting for everyone preserves a successful send's contribution.
    await sql("UPDATE support_ticket_messages SET deleted_for_everyone_at=%s WHERE whatsapp_message_id='a'", (BASE,))
    await sql("INSERT INTO support_agent_activity VALUES (7,%s,%s,%s),(8,%s,%s,%s)",
              (BASE.date(),BASE,BASE+timedelta(seconds=15),BASE.date(),BASE,BASE+timedelta(seconds=30)))
    tracker = stats.StatisticsRepository(repo)
    for metric, winner in [('tickets_claimed',7), ('tickets_resolved',8), ('messages_sent',7), ('active_seconds',8)]:
        report = await tracker.report(BASE,BASE+timedelta(days=1),None,metric,True)
        assert report['rows'][0]['id'] == winner
        reverse = await tracker.report(BASE,BASE+timedelta(days=1),None,metric,False)
        assert reverse['rows'][-1]['id'] == winner
        assert report['rank_by'] == metric
        assert report['totals'] == dict(active_seconds=45, tickets_claimed=3, tickets_resolved=3, messages_sent=3,tickets_transferred=0,tickets_received_by_transfer=0)
    report = await tracker.report(BASE,BASE+timedelta(days=1),7,'tickets_resolved',True)
    assert report['totals'] == dict(active_seconds=15,tickets_claimed=2,tickets_resolved=1,messages_sent=2,tickets_transferred=0,tickets_received_by_transfer=0)
    assert len(report['rows']) == 1
    empty = await tracker.report(BASE-timedelta(days=60), BASE-timedelta(days=59),None,'tickets_claimed',True)
    assert [r['rank'] for r in empty['rows']] == [1,1]
    assert [r['id'] for r in empty['rows']] == [7,8]
    assert empty['activity_tracking_started_at']
    with pytest.raises(HTTPException) as error:
        await tracker.report(BASE,BASE+timedelta(days=1),999,'messages_sent',True)
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_api_real_sessions_roles_csrf_and_actor_forgery(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    await sessions(sql)
    monkeypatch.setattr(stats, 'statistics_repository', stats.StatisticsRepository(repo))
    import app.support_auth as auth
    from app.config import settings
    monkeypatch.setattr(auth, '_schedule_session_touch', lambda session_id: None)
    from app.support_api import router as support_router
    app = FastAPI(); app.include_router(support_router)
    # In-process ASGI only, through the fixture's allowed mock transport.
    asgi = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(asgi.handle_async_request), base_url='http://test') as client:
        path = '/api/support/statistics?start=2026-10-23&end=2026-10-23'
        assert (await client.get(path)).status_code == 401
        client.cookies.set(settings.support_cookie_name, 'token-1')
        assert (await client.get(path)).status_code == 403
        heartbeat = '/api/support/statistics/heartbeat'
        payload = pulse(1).model_dump(mode='json')
        assert (await client.post(heartbeat,json=payload)).status_code == 403
        assert (await client.post(heartbeat,json=payload,headers={'X-CSRF-Token':'csrf'})).status_code == 200
        assert (await client.post(heartbeat,json={**payload,'agent_id':8},headers={'X-CSRF-Token':'csrf'})).status_code == 422
        rows = await sql('SELECT staff_id FROM support_agent_heartbeats')
        assert [r['staff_id'] for r in rows] == [7]
        client.cookies.set(settings.support_cookie_name, 'token-3')
        for role in ['supervisor','admin']:
            await sql('UPDATE staff SET role=%s WHERE id=8',(role,))
            assert (await client.get(path)).status_code == 200
        assert (await client.get(path+'&rank_by=arbitrary')).status_code == 422
        assert (await client.get(path+'&timezone_name=Invalid')).status_code == 422
        await sql("UPDATE staff SET role='agent' WHERE id=8")
        assert (await client.get(path)).status_code == 403
        await sql("UPDATE staff SET role='admin',is_active=0 WHERE id=8")
        assert (await client.get(path)).status_code == 401
        await sql('UPDATE staff SET is_active=1 WHERE id=8')
        await sql('UPDATE staff_sessions SET revoked_at=UTC_TIMESTAMP(6) WHERE id=3')
        assert (await client.get(path)).status_code == 401


@pytest.mark.asyncio
async def test_statistics_migration_repeatable_and_legacy_send_date(infrastructure):
    from pathlib import Path
    repo, sql, _ = infrastructure
    before = await sql('SELECT * FROM support_statistics_metadata')
    await sql(Path('migrations/20261023_agent_statistics.sql').read_text())
    assert await sql('SELECT * FROM support_statistics_metadata') == before
    await sql("""INSERT INTO support_ticket_messages(ticket_id,sender_type,sender_id,direction,body,
        send_status,whatsapp_message_id,created_at,updated_at)
        VALUES (42,'AGENT','7','OUTBOUND','legacy','SENT','legacy',%s,%s)""", (BASE,BASE+timedelta(days=2)))
    report = await stats.StatisticsRepository(repo).report(BASE,BASE+timedelta(days=1),7,'messages_sent',True)
    assert report['totals']['messages_sent'] == 1
    assert report['message_history_since'] == BASE.isoformat()+'Z'


@pytest.mark.asyncio
async def test_transfer_metrics_use_actor_and_recorded_recipient_with_unique_tickets(infrastructure):
    import json
    repo, sql, _ = infrastructure
    await sql("""INSERT INTO staff(id,name,email,email_normalized,password_hash,role,is_active,created_at,updated_at)
        VALUES (9,'Recipient','c@example.test','c@example.test','unused','agent',1,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""")
    # Manager 8 performs the transfer, although staff 7 owned the ticket.
    await repo.transfer_ticket(42,actor_id='8',actor_name='Other',target_staff_id=9,expected_assignee_id='7')
    await sql("UPDATE support_ticket_events SET created_at=%s WHERE event_type='TICKET_TRANSFERRED'",(BASE,))
    for ticket,actor,recipient,stamp in [
        (42,'8','9',BASE),  # repeat: same actor and recipient ticket count only once
        (43,'8',7,BASE),    # numeric JSON ID also supported
        (41,'7','9',BASE),
        (43,'7','8',BASE-timedelta(seconds=1)),
        (42,'7','9',BASE+timedelta(days=1)),  # exclusive end
        (43,'999','9',BASE), # deleted actor does not erase recipient's credit
    ]:
        await sql("""INSERT INTO support_ticket_events(ticket_id,event_type,actor_type,actor_id,details_json,created_at)
            VALUES (%s,'TICKET_TRANSFERRED','AGENT',%s,%s,%s)""",
            (ticket,actor,json.dumps({'from_agent_id':'7','to_agent_id':recipient}),stamp))
    for details in ['not-json','{}','null','{"to_agent_id":"9junk"}']:
        await sql("""INSERT INTO support_ticket_events(ticket_id,event_type,actor_type,actor_id,details_json,created_at)
            VALUES (43,'TICKET_TRANSFERRED','AGENT','8',%s,%s)""",(details,BASE))
    await sql("""INSERT INTO support_ticket_events(ticket_id,event_type,actor_type,actor_id,details_json,created_at)
        VALUES (41,'TICKET_TRANSFERRED','SYSTEM','8','{"to_agent_id":"8"}',%s)""",(BASE,))
    await sql("UPDATE support_tickets SET current_assignee_id='8',conversation_origin='STAFF'")
    tracker=stats.StatisticsRepository(repo)
    report=await tracker.report(BASE,BASE+timedelta(days=1),None,'tickets_transferred',True)
    assert [r['id'] for r in report['rows']]==[8,7,9]
    rows={r['id']:r for r in report['rows']}
    assert [rows[i]['tickets_transferred'] for i in (7,8,9)]==[1,2,0]
    assert [rows[i]['tickets_received_by_transfer'] for i in (7,8,9)]==[1,0,3]
    assert report['totals']['tickets_transferred']==3
    assert report['totals']['tickets_received_by_transfer']==4
    assert report['transfer_history_since']==(BASE-timedelta(seconds=1)).isoformat()+'Z'
    received=await tracker.report(BASE,BASE+timedelta(days=1),None,'tickets_received_by_transfer',True)
    assert [r['id'] for r in received['rows']]==[9,7,8]
    ascending=await tracker.report(BASE,BASE+timedelta(days=1),None,'tickets_received_by_transfer',False)
    assert [r['id'] for r in ascending['rows']]==[8,7,9]
    filtered=await tracker.report(BASE,BASE+timedelta(days=1),9,'tickets_received_by_transfer',True)
    assert filtered['totals']['tickets_received_by_transfer']==3 and filtered['totals']['tickets_transferred']==0
    await sql('UPDATE staff SET is_active=0 WHERE id=9')
    assert (await tracker.report(BASE,BASE+timedelta(days=1),9,'tickets_received_by_transfer',True))['totals']['tickets_received_by_transfer']==3
    await sql('UPDATE staff SET deleted_at=UTC_TIMESTAMP(6) WHERE id=9')
    assert (await tracker.report(BASE,BASE+timedelta(days=1),None,'tickets_received_by_transfer',True))['totals']['tickets_received_by_transfer']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('metric',['tickets_transferred','tickets_received_by_transfer'])
async def test_transfer_metrics_api_and_pdf_permissions(infrastructure,monkeypatch,metric):
    from io import BytesIO
    from pypdf import PdfReader
    import app.support_auth as auth
    from app.config import settings
    from app.support_api import router
    repo, sql, _=infrastructure
    await sessions(sql)
    monkeypatch.setattr(stats,'statistics_repository',stats.StatisticsRepository(repo))
    monkeypatch.setattr(auth,'_schedule_session_touch',lambda _:None)
    await sql("""INSERT INTO support_ticket_events(ticket_id,event_type,actor_type,actor_id,details_json,created_at)
        VALUES (42,'TICKET_TRANSFERRED','AGENT','7','{"to_agent_id":"8"}',%s)""",(BASE,))
    app=FastAPI();app.include_router(router)
    transport=httpx.MockTransport(httpx.ASGITransport(app=app).handle_async_request)
    query=f'?start=2026-10-23&end=2026-10-23&timezone_name=UTC&rank_by={metric}'
    async with httpx.AsyncClient(transport=transport,base_url='http://test') as client:
        for suffix in ['', '/pdf']:
            path='/api/support/statistics'+suffix+query
            client.cookies.clear()
            assert (await client.get(path)).status_code==401
            client.cookies.set(settings.support_cookie_name,'token-1')
            assert (await client.get(path)).status_code==403
            client.cookies.set(settings.support_cookie_name,'token-3')
            response=await client.get(path)
            assert response.status_code==200,response.text
            if suffix:
                text=' '.join(' '.join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages).split())
                label='Tickets transferred' if metric=='tickets_transferred' else 'Tickets received by transfer'
                assert f'Ranked by {label}' in text
            else:
                report=response.json()
                assert report['rank_by']==metric
                assert report['rows'][0]['id']==(7 if metric=='tickets_transferred' else 8)
                assert report['totals']['tickets_transferred']==report['totals']['tickets_received_by_transfer']==1
