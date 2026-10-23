import asyncio
import json
from datetime import datetime

import pytest

import app.staff_management as management
import app.support_auth as auth
from app.support_auth import SupportAgent
from app.support_statistics import StatisticsRepository


@pytest.mark.asyncio
async def test_rename_updates_all_displays_and_broadcasts_without_rewriting_history(infrastructure,monkeypatch):
    repo, sql, redis = infrastructure
    monkeypatch.setattr(management,'staff_repository',auth.staff_repository)
    await sql("UPDATE support_tickets SET current_assignee_name='Old name',initiated_by_staff_id=7,initiated_by_staff_name='Old name' WHERE id=42")
    message,_=await repo.add_ticket_message(ticket_id=42,sender_type='AGENT',sender_id='7',sender_name='Old name',direction='OUTBOUND',body='name test',client_message_id='name-test')
    await sql("UPDATE support_ticket_messages SET whatsapp_message_id='name-test-provider' WHERE id=%s",(message['id'],))
    customer,_=await repo.add_ticket_message(ticket_id=42,sender_type='CUSTOMER',sender_id='7',sender_name='Customer name',direction='INBOUND',body='customer')
    await repo.add_event(42,event_type='TICKET_TRANSFERRED',actor_type='AGENT',actor_id='7',actor_name='Old name',details={'from_agent_id':'7','from_agent_name':'Old name','to_agent_id':'8','to_agent_name':'Other'})
    actor=SupportAgent(id='8',name='Other',role='supervisor')
    async with redis.pubsub() as subscription:
        await subscription.subscribe('support:events:v1')
        assert (await asyncio.wait_for(anext(subscription.listen()),5))['type']=='subscribe'
        updated=await management.update_staff_account(actor,7,name='Renamed Agent')
        event=await asyncio.wait_for(anext(subscription.listen()),5)
        assert json.loads(event['data'])=={'type':'staff_profile_updated','staff_id':'7','name':'Renamed Agent'}
    assert updated['sessions_revoked'] is False
    ticket=await repo.get_ticket(42)
    assert ticket['current_assignee_name']==ticket['initiated_by_staff_name']=='Renamed Agent'
    assert ticket['current_assignee_id']=='7' and ticket['status']=='ASSIGNED'
    inbox=await repo.list_tickets(view='mine',agent_id='7')
    assert inbox['items'][0]['current_assignee_name']=='Renamed Agent'
    for row in [await repo.get_message(message['id']), await repo.get_message_by_whatsapp_id('name-test-provider'),
                await repo.get_message_by_client_id('name-test',ticket_id=42,sender_type='AGENT',sender_id='7')]:
        assert row['sender_name']=='Renamed Agent'
    messages=await repo.list_messages(42)
    assert [m['sender_name'] for m in messages]==['Renamed Agent','Customer name']
    assert (await repo.list_messages_around(42,message['id']))[0]['sender_name']=='Renamed Agent'
    assert (await repo.search_messages(query='name test',agent_id='7'))[0]['sender_name']=='Renamed Agent'
    events=await repo.list_events(42)
    assert events[0]['actor_name']=='Renamed Agent'
    assert json.loads(events[0]['details_json'])['from_agent_name']=='Renamed Agent'
    report=await StatisticsRepository(repo).report(datetime(2026,10,1),datetime(2026,11,1),7,'messages_sent',True)
    assert report['rows'][0]['name']=='Renamed Agent'
    from app.ai_availability import AIControlRepository
    ai = AIControlRepository(); ai._pool = repo._pool
    await sql("UPDATE ai_control_settings SET updated_by_staff_id=7,updated_by_staff_name='Old name' WHERE id=1")
    assert (await ai.get()).updated_by_staff_name == 'Renamed Agent'
    # These are snapshots, not identity sources. No message/version/delivery writes.
    stored=(await sql('SELECT current_assignee_name FROM support_tickets WHERE id=42'))[0]
    assert stored['current_assignee_name']=='Old name'
    assert (await sql('SELECT sender_name FROM support_ticket_messages WHERE id=%s',(message['id'],)))[0]['sender_name']=='Old name'
    assert (await sql('SELECT actor_name FROM support_ticket_events WHERE ticket_id=42'))[0]['actor_name']=='Old name'
    # Historical tickets/chats use the same mapping, including inactive accounts.
    await sql("UPDATE support_tickets SET status='RESOLVED',active_key=NULL WHERE id=42")
    await sql('UPDATE staff SET is_active=0 WHERE id=7')
    previous=await repo.list_previous_customer_tickets(current_ticket_id=41,phone_number='155500042')
    assert previous[0]['current_assignee_name']=='Renamed Agent'
    await auth.staff_repository.delete_staff(7)
    assert (await repo.get_ticket(42))['current_assignee_name']=='Old name'
    assert (await repo.get_message(message['id']))['sender_name']=='Old name'


@pytest.mark.asyncio
async def test_assignment_with_stale_request_name_cannot_restore_old_display_name(infrastructure,monkeypatch):
    repo, sql, _=infrastructure
    continue_claim=asyncio.Event()
    async def stale_claim():
        await continue_claim.wait()
        return await repo.claim_ticket(41,agent_id='7',agent_name='Old captured request name')
    task=asyncio.create_task(stale_claim())
    await auth.staff_repository.update_staff(7,name='Latest name')
    continue_claim.set()
    ticket,changed=await task
    assert changed and ticket['current_assignee_name']=='Latest name'
    message,_=await repo.add_ticket_message(ticket_id=41,sender_type='AGENT',sender_id='7',sender_name='Old captured request name',direction='OUTBOUND',body='stale sender')
    assert message['sender_name']=='Latest name'
    assert (await repo.list_events(41))[0]['actor_name']=='Latest name'
