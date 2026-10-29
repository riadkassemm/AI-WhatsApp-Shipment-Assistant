"""Full-dataset queues, per-staff reads and current-assignee event eligibility."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.support_api import router
from app.support_auth import SupportAgent
from app.support_service import SupportService, SupportForbiddenError
from app.support_events import CHANNEL
from tests.integration.test_agent_statistics import sessions, AGENT
from tests.integration.test_support_events import session


async def incoming(sql, ticket_id, body='needle', timestamp=None):
    await sql("""INSERT INTO support_ticket_messages (ticket_id,sender_type,direction,body,whatsapp_timestamp,created_at,updated_at)
        VALUES (%s,'CUSTOMER','INBOUND',%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""", (ticket_id, body, timestamp))
    return int((await sql('SELECT MAX(id) AS id FROM support_ticket_messages'))[0]['id'])


@pytest.mark.asyncio
async def test_combined_filters_counts_pages_and_permissions(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    await sessions(sql)
    await sql("INSERT INTO whatsapp_phone_numbers(phone_number_id,display_name,is_enabled,provider_present,source) VALUES ('90005','Export',1,1,'LINKED_DEVICE'),('90006','Office',1,1,'LINKED_DEVICE')")
    for n in range(50, 58):
        await sql("""INSERT INTO support_tickets(id,ticket_reference,whatsapp_phone,customer_name,reason,ai_summary,status,
            conversation_origin,current_assignee_id,business_phone_number_id,created_at,updated_at,last_activity_at)
            VALUES (%s,%s,%s,'Customer','test','test','CLOSED','STAFF','7','90005',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""", (n, f'CHAT-{n}', str(n)))
        await incoming(sql, n)
    await sql("UPDATE support_tickets SET conversation_origin='STAFF' WHERE id IN (42,43)")
    await incoming(sql, 42)
    await incoming(sql, 43)
    await sql("UPDATE support_tickets SET business_phone_number_id='90006' WHERE id=57")
    app = FastAPI(); app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.MockTransport(httpx.ASGITransport(app=app).handle_async_request), base_url='http://test') as client:
        client.cookies.set(settings.support_cookie_name, 'token-1')
        path = '/api/support/chats?staff_id=7&support_number=90005&chat_status=closed&q=needle&page_size=2'
        found = []
        for page in range(1, 5):
            response = await client.get(path + '&page=' + str(page))
            assert response.status_code == 200, response.text
            data = response.json()
            assert data['total'] == 7
            found.extend(row['id'] for row in data['items'])
            assert all(row.get('search_message_id') and row['search_preview'] == 'needle' for row in data['items'])
            assert {'id': '90005', 'name': 'Export'} in data['filter_options']['support_numbers']
        assert len(set(found)) == 7 and 57 not in found
        data = (await client.get('/api/support/chats?chat_status=open&staff_id=8')).json()
        assert data['total'] == 0  # filters cannot grant shared active access
        client.cookies.set(settings.support_cookie_name, 'token-3')
        data = (await client.get('/api/support/chats?chat_status=open&staff_id=8')).json()
        assert [row['id'] for row in data['items']] == [43]
        assert data['total'] == 1
    await sql("UPDATE support_tickets SET conversation_origin='CUSTOMER' WHERE id IN (42,43)")
    mine = await repo.list_tickets(view='mine', agent_id='7', staff_id='8', support_number='90005')
    assert [row['id'] for row in mine['items']] == [42]
    unassigned = await repo.list_tickets(view='unassigned', agent_id='7', staff_id='8')
    assert [row['id'] for row in unassigned['items']] == [41]
    for view, status in [('waiting','WAITING_CUSTOMER'), ('history','RESOLVED'), ('active','ASSIGNED')]:
        await sql('UPDATE support_tickets SET status=%s WHERE id IN (42,43)', (status,))
        data = await repo.list_tickets(view=view, agent_id='8', can_view_all=True, staff_id='7', support_number='90005', query='155500042')
        assert data['total'] == 1 and data['items'][0]['id'] == 42
    # Search is literal, honors message visibility, and reaches beyond the first page.
    match = await incoming(sql, 57, 'literal%_')
    data = await repo.list_tickets(view='chats', agent_id='7', query='literal%_')
    assert data['total'] == 1
    await sql('INSERT INTO support_message_user_deletions(message_id,user_id,deleted_at) VALUES (%s,7,UTC_TIMESTAMP(6))', (match,))
    assert (await repo.list_tickets(view='chats', agent_id='7', query='literal%_'))['total'] == 0


@pytest.mark.asyncio
async def test_read_boundaries_persist_isolate_and_never_include_newer_messages(infrastructure):
    repo, sql, _ = infrastructure
    first = await incoming(sql, 42)
    service = SupportService()
    other = SupportAgent(id='8', name='Other', role='supervisor', session_id=3)
    await service.mark_dashboard_read(42, first, other)
    async def count(staff):
        data = await repo.list_tickets(view='mine', agent_id=staff)
        return next(row['dashboard_unread_count'] for row in data['items'] if row['id'] == 42)
    assert await count('7') == 1  # manager view did not clear assignee
    newer = await incoming(sql, 42, 'arrived while viewing')
    result = await service.mark_dashboard_read(42, first, AGENT)
    assert result['dashboard_unread_count'] == 1
    assert await count('7') == 1
    # Older tab requests cannot rewind the cursor.
    ready, release = asyncio.Event(), asyncio.Event()
    async def old_tab():
        ready.set(); await release.wait()
        return await service.mark_dashboard_read(42, first, AGENT)
    task = asyncio.create_task(old_tab())
    await ready.wait()
    await service.mark_dashboard_read(42, newer, AGENT)
    release.set(); await task
    assert await count('7') == 0
    assert (await sql('SELECT read_at FROM support_ticket_messages WHERE id=%s', (first,)))[0]['read_at'] is None
    # Authorization and boundary ownership are validated server-side.
    third = await incoming(sql, 43)
    with pytest.raises(SupportForbiddenError):
        await service.mark_dashboard_read(43, third, AGENT)
    assert await repo.mark_dashboard_read(42, '7', third) is None
    await sql("UPDATE support_tickets SET current_assignee_id='8' WHERE id=42")
    assert await count('8') == 1  # newly assigned staff still has their own cursor


@pytest.mark.asyncio
async def test_chat_event_assignment_is_current_and_backlog_silent(infrastructure):
    repo, sql, redis = infrastructure
    await sql("UPDATE support_tickets SET conversation_origin='STAFF',initiated_by_staff_id='7' WHERE id=42")
    message_id = await incoming(sql, 42)
    service = SupportService()
    manager = SupportAgent(id='8', name='Other', role='supervisor', session_id=3)
    async def event(agent):
        payload = {'type': 'customer_message', 'ticket_id': 42, 'message_id': message_id, 'chat_notification': True}
        assert await service.can_receive_support_event(payload, agent)
        return payload
    assert (await event(AGENT))['chat_notification'] is True
    assert (await event(manager))['chat_notification'] is False
    await sql("UPDATE support_tickets SET current_assignee_id='8' WHERE id=42")
    assert (await event(AGENT))['chat_notification'] is False  # creator retains view only
    assert (await event(manager))['chat_notification'] is True
    stored = await repo.get_message(message_id)
    from datetime import timezone
    cutoff = stored['created_at'].replace(tzinfo=timezone.utc).timestamp() + 1
    backlog = {'type':'customer_message','ticket_id':42,'message_id':message_id}
    assert await service.can_receive_support_event(backlog, manager, notification_since=cutoff)
    assert backlog['chat_notification'] is False  # reconnect/navigation backlog is silent
    await sql("UPDATE support_ticket_messages SET whatsapp_timestamp='1' WHERE id=%s", (message_id,))
    assert (await event(manager))['chat_notification'] is False
    await sql("UPDATE support_ticket_messages SET whatsapp_timestamp=NULL,direction='OUTBOUND' WHERE id=%s", (message_id,))
    assert (await event(manager))['chat_notification'] is False
    assert not await service.can_receive_support_event({'type':'dashboard_read','ticket_id':42,'staff_id':'7'}, manager)
    # Real Redis/SSE delivery retains per-session eligibility.
    stream = await session(sql)
    message_id = await incoming(sql, 42, "fresh after stream connected")
    await redis.publish(CHANNEL, json.dumps({'type':'customer_message','ticket_id':42,'message_id':message_id}))
    async def next_data():
        while True:
            chunk = await stream.__anext__()
            if chunk.startswith('data:'): return json.loads(chunk[6:])
    payload = await asyncio.wait_for(next_data(), 3)
    assert payload['authorized'] and payload['chat_notification'] and payload['current_assignee_id'] == '8'
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('role,expected', [('agent',False),('supervisor',True),('admin',True)])
async def test_view_and_read_events_are_manager_only(infrastructure, monkeypatch, role, expected):
    repo, sql, _ = infrastructure
    import app.support_service as service
    async def no_identity(*args): return None
    monkeypatch.setattr(service.customer_directory, 'resolve', no_identity)
    for event_type in ['CONVERSATION_VIEWED', 'INCOMING_MARKED_READ', 'TICKET_CLAIMED']:
        await repo.add_event(42, event_type=event_type, actor_type='AGENT', actor_id='7', actor_name='Agent')
    agent = SupportAgent(id='7', name='Agent', role=role, session_id=1)
    data = await service.support_service.ticket_detail(42, agent)
    types = {row['event_type'] for row in data['events']}
    assert ('CONVERSATION_VIEWED' in types) is expected
    assert ('INCOMING_MARKED_READ' in types) is expected
    assert 'TICKET_CLAIMED' in types
    assert len(await repo.list_events(42)) == 3  # audit is retained


@pytest.mark.asyncio
async def test_read_endpoint_requires_session_csrf_and_ticket_access(infrastructure):
    repo, sql, _ = infrastructure
    await sessions(sql)
    first = await incoming(sql, 42)
    other = await incoming(sql, 43)
    app = FastAPI(); app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.MockTransport(httpx.ASGITransport(app=app).handle_async_request), base_url='http://test') as client:
        path = '/api/support/tickets/42/dashboard-read'
        assert (await client.post(path, json={'message_id':first})).status_code == 401
        client.cookies.set(settings.support_cookie_name, 'token-1')
        assert (await client.post(path, json={'message_id':first})).status_code == 403
        client.headers['X-CSRF-Token'] = 'csrf'
        assert (await client.post('/api/support/tickets/43/dashboard-read', json={'message_id':other})).status_code == 403
        assert (await client.post(path, json={'message_id':other})).status_code == 422
        for _ in range(2):
            response = await client.post(path, json={'message_id':first})
            assert response.status_code == 200, response.text
            assert response.json()['dashboard_unread_count'] == 0
    audits = await sql("SELECT * FROM support_ticket_events WHERE event_type='INCOMING_MARKED_READ'")
    assert len(audits) == 1 and audits[0]['actor_id'] == '7'
    # Private activity must not consume the agent's visible-event limit.
    await repo.add_event(42, event_type='TICKET_CLAIMED', actor_type='AGENT', actor_id='7')
    assert (await repo.list_events(42, limit=1, include_private_activity=False))[0]['event_type'] == 'TICKET_CLAIMED'


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_id', ['agent-1', '7abc', '7'])
@pytest.mark.parametrize('collation', ['utf8mb4_unicode_ci', 'utf8mb4_general_ci', 'utf8mb4_bin'])
async def test_filter_options_exclude_legacy_accounts_but_preserve_history_identity(infrastructure, legacy_id, collation):
    import warnings
    repo, sql, _ = infrastructure
    await sql(f'ALTER TABLE support_tickets MODIFY current_assignee_id VARCHAR(100) CHARACTER SET utf8mb4 COLLATE {collation} NULL')
    await sql("UPDATE support_tickets SET status='RESOLVED',active_key=NULL,current_assignee_id=%s,current_assignee_name='Legacy staff' WHERE id=42", (legacy_id,))
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        result = await repo.list_tickets(view='history', agent_id='8', can_view_all=True)
    expected_name = 'Agent' if legacy_id == '7' else 'Legacy staff'
    assert result['filter_options']['staff'] == [{'id': '7', 'name': 'Agent'}, {'id': '8', 'name': 'Other'}]
    assert result['items'][0]['current_assignee_name'] == expected_name


@pytest.mark.asyncio
async def test_filters_use_active_directory_even_without_ticket_history(infrastructure):
    repo, sql, _ = infrastructure
    await sql("UPDATE staff SET is_active=0 WHERE id=7")
    await sql("UPDATE support_tickets SET current_assignee_id='deleted-staff' WHERE id=42")
    await sql("""INSERT INTO whatsapp_phone_numbers(phone_number_id,display_name,source,is_enabled,provider_present,removed_at)
        VALUES ('90011','Active','LINKED_DEVICE',1,1,NULL),
               ('90012','Disabled','LINKED_DEVICE',0,1,NULL),
               ('90013','Unpaired','LINKED_DEVICE',1,0,NULL),
               ('90014','Removed','LINKED_DEVICE',1,1,UTC_TIMESTAMP(6)),
               ('12345678','Meta','META_SYNC',1,1,NULL)""")
    for view in ('unassigned','mine','chats','active','history','waiting'):
        queue = await repo.list_tickets(view=view, agent_id='8', can_view_all=True)
        assert queue['filter_options'] == {
            'staff': [{'id':'8','name':'Other'}],
            'support_numbers': [{'id':'90011','name':'Active'}]}
    await sql("UPDATE staff SET deleted_at=UTC_TIMESTAMP(6) WHERE id=8")
    assert (await repo.list_tickets(view='history', agent_id='8', can_view_all=True))['filter_options']['staff'] == []

@pytest.mark.asyncio
async def test_count_only_reuses_access_and_filter_rules_without_loading_rows(infrastructure, monkeypatch):
    import app.support_repository as repository_module
    repo, sql, _ = infrastructure
    await incoming(sql, 42)
    expected = {}
    for role in [False, True]:
        for view in ['unassigned','mine','watching','active','waiting','history','chats']:
            args = dict(view=view, agent_id='7', can_view_all=role, support_number='90005', include_filter_options=False)
            expected[(role,view)] = (await repo.list_tickets(**args))['total']
    async def forbidden(*args, **kwargs):
        raise AssertionError('Count-only path must not load messages/read state/names/filter options')
    for name in ['attach_current_staff_names','attach_conversation_read_state','attach_dashboard_unread']:
        monkeypatch.setattr(repository_module, name, forbidden)
    monkeypatch.setattr(repo, 'queue_filter_options', forbidden)
    for (role,view), total in expected.items():
        result = await repo.list_tickets(view=view, agent_id='7', can_view_all=role, support_number='90005', count_only=True)
        assert result == {'total':total}
