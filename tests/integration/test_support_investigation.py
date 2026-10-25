"""Real lifecycle/audit, auth and read-only investigation against disposable services."""
import asyncio
from datetime import datetime, timedelta
import json
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
import app.support_statistics as stats
import app.support_auth as auth
from app.support_auth import SupportAgent
from app.support_investigation import InvestigationRepository
from app.config import settings
from tests.integration.test_agent_statistics import sessions

BASE = datetime(2026,10,1,10)
MANAGER = SupportAgent(id='8',name='Other',role='supervisor',session_id=3)
AGENT = SupportAgent(id='7',name='Agent',role='agent',session_id=1)


async def stamp_latest(sql, kind, at):
    await sql('UPDATE support_ticket_events SET created_at=%s WHERE id=(SELECT MAX(id) FROM support_ticket_events WHERE event_type=%s)', (at,kind))


@pytest.mark.asyncio
async def test_real_transfer_resolution_and_reopen_periods(infrastructure):
    repo, sql, _ = infrastructure
    await sql('UPDATE support_tickets SET created_at=%s', (BASE,))
    await repo.claim_ticket(41,agent_id='7',agent_name='Agent')
    await stamp_latest(sql,'TICKET_CLAIMED',BASE)
    await repo.transfer_ticket(41,expected_assignee_id='7',target_staff_id=8,actor_id='7',actor_name='Agent')
    await stamp_latest(sql,'TICKET_TRANSFERRED',BASE+timedelta(minutes=5))
    ticket, _ = await repo.resolve_ticket(41,agent_id='8',agent_name='Other')
    await stamp_latest(sql,'TICKET_RESOLVED',BASE+timedelta(minutes=15))
    report = await stats.StatisticsRepository(repo).report(BASE,BASE+timedelta(days=1),None,'average_resolution_seconds',True)
    by_id = {r['id']:r for r in report['rows']}
    assert by_id[7]['average_handling_seconds'] == 300 and by_id[7]['average_resolution_seconds'] is None
    assert by_id[8]['average_handling_seconds'] == 600 and by_id[8]['average_resolution_seconds'] == 600
    assert report['rows'][0]['id'] == 8
    assert report['timing_totals']['median_handling_seconds'] == 450
    # Real manager reopen takes ownership; preserves previous resolution and audit.
    await repo.reopen_customer_ticket(41,agent_id='8',agent_name='Other',supervisor=True,expected_resolved_at=ticket['resolved_at'])
    await stamp_latest(sql,'TICKET_REOPENED',BASE+timedelta(hours=2))
    await repo.resolve_ticket(41,agent_id='8',agent_name='Other')
    await stamp_latest(sql,'TICKET_RESOLVED',BASE+timedelta(hours=2,minutes=5))
    report = await stats.StatisticsRepository(repo).report(BASE,BASE+timedelta(days=1),8,'median_resolution_seconds',False)
    assert report['rows'][0]['resolution_samples'] == 2
    assert report['rows'][0]['median_resolution_seconds'] == 450
    detail = await InvestigationRepository(stats.StatisticsRepository(repo)).investigate(41,MANAGER)
    assert [p['seconds'] for p in detail['periods']] == [300,600,300]
    assert detail['periods'][-1]['cycle_resolution_seconds'] == 300
    assert detail['periods'][-1]['resolver_name'] == 'Other'


@pytest.mark.asyncio
async def test_agent_review_includes_message_participation_and_current_owner_names(infrastructure):
    repo, sql, _ = infrastructure
    await sql('UPDATE support_tickets SET created_at=%s', (BASE,))
    message, _ = await repo.add_ticket_message(ticket_id=42, sender_type='AGENT', sender_id='8',
        sender_name='Other', direction='OUTBOUND', body='Synthetic manager reply',
        send_status='SENT', whatsapp_message_id='test-review-reply')
    await sql('UPDATE support_ticket_messages SET created_at=%s WHERE id=%s', (BASE, message['id']))
    investigation = InvestigationRepository(stats.StatisticsRepository(repo))
    result = await investigation.tickets(BASE, BASE+timedelta(days=1), 8, None, False)
    ticket = next(item for item in result['items'] if item['id'] == 42)
    assert ticket['current_assignee_name'] == 'Agent'
    assert ticket['whatsapp_phone'] == '155500042'
    assert not any(item['id'] == 42 for item in (await investigation.tickets(
        BASE+timedelta(days=1), BASE+timedelta(days=2), 8, None, False))['items'])
    # Renames are current display names; no assignment change or audit fabrication.
    await sql("UPDATE staff SET name='Renamed agent' WHERE id=7")
    result = await investigation.tickets(BASE, BASE+timedelta(days=1), 8, None, False)
    assert next(item for item in result['items'] if item['id'] == 42)['current_assignee_name'] == 'Renamed agent'
    assert (await repo.get_ticket(42))['current_assignee_id'] == '7'


@pytest.mark.asyncio
async def test_api_scope_idempotent_views_and_read_only_pagination(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    await sessions(sql)
    monkeypatch.setattr(stats,'statistics_repository',stats.StatisticsRepository(repo))
    monkeypatch.setattr(auth,'_schedule_session_touch',lambda _:None)
    from app.support_api import router
    app=FastAPI();app.include_router(router)
    asgi=httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=httpx.MockTransport(asgi.handle_async_request),base_url='http://test') as client:
        path='/api/support/statistics/tickets/42'
        assert (await client.get(path)).status_code == 401
        client.cookies.set(settings.support_cookie_name,'token-1')
        assert (await client.get(path)).status_code == 403
        payload={'request_id':str(uuid4())}
        assert (await client.post(path+'/view',json=payload)).status_code == 403
        client.headers['X-CSRF-Token']='csrf'
        assert (await client.post('/api/support/statistics/tickets/43/view',json=payload)).status_code == 404
        assert (await client.post(path+'/view',json={**payload,'actor_id':8})).status_code == 422
        results=await asyncio.gather(*[client.post(path+'/view',json=payload) for _ in range(2)])
        assert [r.status_code for r in results] == [200,200]
        events=await sql("SELECT * FROM support_ticket_events WHERE event_type='CONVERSATION_VIEWED'")
        assert len(events) == 1 and events[0]['actor_id']=='7' and events[0]['created_at'] is not None
        # Enough records to exercise both independent pagination streams.
        for n in range(105):
            await sql("INSERT INTO support_ticket_messages(ticket_id,sender_type,direction,body,send_status,created_at,updated_at) VALUES (42,'CUSTOMER','INBOUND',%s,'RECEIVED',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))",(f'fixture {n}',))
            await repo.add_event(42,event_type='STATUS_CHANGED',actor_type='AGENT',actor_id='7',details={'from':'ASSIGNED','to':'IN_PROGRESS'})
        before_ticket=await repo.get_ticket(42)
        before_messages=await sql('SELECT * FROM support_ticket_messages WHERE ticket_id=42')
        before_events=await sql('SELECT * FROM support_ticket_events WHERE ticket_id=42')
        client.cookies.set(settings.support_cookie_name,'token-3')
        for role in ('supervisor','admin'):
            await sql('UPDATE staff SET role=%s WHERE id=8',(role,))
            response=await client.get(path)
            assert response.status_code==200,response.text
            result=response.json()
            assert result['read_only'] and len(result['messages'])==100 and len(result['events'])==100
            older=(await client.get(path+f"?before_message={result['next_message_before']}&before_event={result['next_event_before']}")).json()
            assert len(older['messages'])==5 and len(older['events'])==6
            assert not set(m['id'] for m in result['messages']) & set(m['id'] for m in older['messages'])
        assert await repo.get_ticket(42)==before_ticket
        assert await sql('SELECT * FROM support_ticket_messages WHERE ticket_id=42')==before_messages
        assert await sql('SELECT * FROM support_ticket_events WHERE ticket_id=42')==before_events
        await sql("UPDATE staff SET role='agent' WHERE id=8")
        assert (await client.get(path)).status_code==403
        await sql('UPDATE staff_sessions SET revoked_at=UTC_TIMESTAMP(6) WHERE id=3')
        assert (await client.get(path)).status_code==401


@pytest.mark.asyncio
async def test_confirmed_read_audit_attributes_original_sender_and_deduplicates(infrastructure):
    repo,sql,_=infrastructure
    incoming,_=await repo.add_ticket_message(ticket_id=42,sender_type='CUSTOMER',direction='INBOUND',body='fixture',whatsapp_message_id='in-read')
    reply,_=await repo.add_ticket_message(ticket_id=42,sender_type='AGENT',sender_id='7',sender_name='Agent',direction='OUTBOUND',body='fixture',send_status='SENT',whatsapp_message_id='out-read')
    await sql("UPDATE support_tickets SET current_assignee_id='8' WHERE id=42")
    job={'id':reply['id'],'ticket_id':42}
    target={'id':incoming['id'],'whatsapp_message_id':'in-read'}
    await repo.confirm_incoming_read(job,target)
    await repo.confirm_incoming_read(job,target)
    events=await sql("SELECT * FROM support_ticket_events WHERE event_type='INCOMING_MARKED_READ'")
    assert len(events)==1 and events[0]['actor_id']=='7'
    assert json.loads(events[0]['details_json'])=={'mode':'reply_sync','reply_id':reply['id'],'message_id':incoming['id']}
    assert (await repo.get_message(incoming['id']))['read_at'] is not None


@pytest.mark.asyncio
async def test_timing_cross_date_range_missing_history_and_indicator_filter(infrastructure):
    repo,sql,_=infrastructure
    await sql('UPDATE support_tickets SET created_at=%s',(BASE-timedelta(days=1),))
    for minute,kind in [(0,'TICKET_CLAIMED'),(1,'CONVERSATION_VIEWED'),(2,'CONVERSATION_VIEWED'),(3,'CONVERSATION_VIEWED')]:
        await sql("INSERT INTO support_ticket_events(ticket_id,event_type,actor_type,actor_id,created_at) VALUES (42,%s,'AGENT','7',%s)",(kind,BASE+timedelta(minutes=minute)))
    investigation=InvestigationRepository(stats.StatisticsRepository(repo))
    result=await investigation.tickets(BASE,BASE+timedelta(hours=1),7,None,True)
    assert [i['id'] for i in result['items']]==[42]
    assert result['items'][0]['flags'][0]['code']=='repeated_views_without_response'
    assert (await investigation.tickets(BASE,BASE+timedelta(hours=1),8,None,True))['items']==[]
    await sql("INSERT INTO support_ticket_events(ticket_id,event_type,actor_type,actor_id,created_at) VALUES (42,'TICKET_RESOLVED','AGENT','7',%s)",(BASE+timedelta(days=1),))
    report=await stats.StatisticsRepository(repo).report(BASE+timedelta(days=1),BASE+timedelta(days=2),7,'average_handling_seconds',True)
    assert report['rows'][0]['average_handling_seconds']==86400
    assert report['rows'][0]['handling_samples']==1
    from pathlib import Path
    tracking=await sql('SELECT * FROM support_statistics_metadata')
    await sql(Path('migrations/20261025_statistics_investigation.sql').read_text())
    assert await sql('SELECT * FROM support_statistics_metadata')==tracking


@pytest.mark.asyncio
async def test_deleted_staff_excluded_from_review_and_future_window_empty(infrastructure):
    repo,sql,_=infrastructure
    await sql('UPDATE support_tickets SET created_at=%s',(BASE,))
    for kind in ('TICKET_CLAIMED','TICKET_RESOLVED'):
        await sql("INSERT INTO support_ticket_events(ticket_id,event_type,actor_type,actor_id,created_at) VALUES (42,%s,'AGENT','7',%s)",(kind,BASE))
    investigation=InvestigationRepository(stats.StatisticsRepository(repo))
    assert (await investigation.tickets(BASE,BASE+timedelta(hours=1),None,None,True))['items']
    await sql('UPDATE staff SET deleted_at=UTC_TIMESTAMP(6) WHERE id=7')
    assert (await investigation.tickets(BASE,BASE+timedelta(hours=1),None,None,True))['items']==[]
    future=datetime(2099,1,1)
    assert (await investigation.tickets(future,future+timedelta(days=1),None,None,False))['items']==[]
