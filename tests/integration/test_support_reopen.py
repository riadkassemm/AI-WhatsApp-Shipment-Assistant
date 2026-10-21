import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.conversation_store import RedisConversationStore
from app.support_auth import SupportAgent
from app.support_repository import SupportConflictError
from app.support_service import SupportForbiddenError, support_service
import app.support_service as service
import app.support_repository as repository_module
import app.support_api as api

AGENT = SupportAgent(id='7', name='Agent', role='agent')
OTHER = SupportAgent(id='8', name='Other', role='agent')
SUPERVISOR = SupportAgent(id='8', name='Supervisor', role='supervisor')
ADMIN = SupportAgent(id='8', name='Admin', role='admin')


@pytest_asyncio.fixture
async def reopening(infrastructure, monkeypatch):
    repo, sql, client = infrastructure
    store = object.__new__(RedisConversationStore)
    store._client, store._ttl_seconds = client, 300
    monkeypatch.setattr(service, 'conversation_store', store)
    monkeypatch.setattr(service.customer_directory, 'resolve', AsyncMock(return_value=SimpleNamespace(source='guest', name=None)))
    await sql("UPDATE support_tickets SET status='WAITING_CUSTOMER' WHERE id=42")
    await repo.add_ticket_message(ticket_id=42, sender_type='CUSTOMER', direction='INBOUND', body='existing history')
    resolved, _ = await repo.resolve_ticket(42, agent_id='7', agent_name='Agent', expected_assignee_id='7')
    return repo, sql, client, resolved


async def reopen(resolved, agent=AGENT):
    return await support_service.reopen_ticket(int(resolved['id']), agent=agent, resolved_at=resolved.get('resolved_at'))


@pytest.mark.asyncio
async def test_eligible_agent_reopens_keeps_history_sends_and_broadcasts(reopening, monkeypatch):
    repo, sql, client, resolved = reopening
    before = await repo.list_messages(42)
    detail = await support_service.ticket_detail(42, AGENT)
    assert detail['permissions']['can_reopen'] and not detail['permissions']['can_reply']
    with pytest.raises(SupportConflictError):
        await support_service.send_agent_message(42, agent=AGENT, body='not yet', client_message_id='before')
    async with client.pubsub() as subscription:
        await subscription.subscribe('support:events:v1')
        assert (await asyncio.wait_for(anext(subscription.listen()), 5))['type'] == 'subscribe'
        result = await reopen(resolved)
        event = await asyncio.wait_for(anext(subscription.listen()), 5)
        assert json.loads(event['data']) == {'type': 'ticket_queues_changed'}
    assert result['changed'] and result['ticket']['status'] == 'WAITING_CUSTOMER'
    assert result['ticket']['current_assignee_id'] == '7'
    stored = await repo.get_ticket(42)
    assert stored['resolved_at'] == resolved['resolved_at']
    assert stored['resolved_by_staff_id'] == 7
    assert await repo.list_messages(42) == before
    events = await sql("SELECT * FROM support_ticket_events WHERE ticket_id=42 ORDER BY id")
    assert [e['event_type'] for e in events] == ['TICKET_RESOLVED', 'RETURNED_TO_AI', 'TICKET_REOPENED']
    assert events[-1]['actor_id'] == '7' and events[-1]['created_at']
    assert json.loads(events[-1]['details_json'])['resolved_by_staff_id'] == 7
    detail = await support_service.ticket_detail(42, AGENT)
    assert detail['permissions']['can_reply'] and not detail['permissions']['can_reopen']
    inbox = await repo.list_tickets(view='mine', agent_id='7')
    assert 42 in {t['id'] for t in inbox['items']}
    mode = await service.conversation_store.get_or_create('155500042', conversation_key='90005:155500042')
    assert mode.mode == 'human' and mode.active_ticket_id == '42'
    send = AsyncMock(return_value={'messages': [{'id': 'accepted'}]})
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send))
    assert (await support_service.send_agent_message(42, agent=AGENT, body='after', client_message_id='after'))['send_status'] == 'SENT'
    send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('assignee,resolver,actor', [('7', 7, OTHER), ('7', 8, AGENT), ('8', 7, AGENT), ('7', None, AGENT), (None, 7, AGENT)])
async def test_shared_history_and_missing_legacy_ids_do_not_grant_reopening(reopening, assignee, resolver, actor):
    repo, sql, _, resolved = reopening
    await sql('UPDATE support_tickets SET current_assignee_id=%s,resolved_by_staff_id=%s WHERE id=42', (assignee, resolver))
    detail = await support_service.ticket_detail(42, actor)
    assert not detail['permissions']['can_reopen'] and not detail['permissions']['can_reply']
    with pytest.raises(SupportForbiddenError):
        await reopen(resolved, actor)
    assert (await repo.get_ticket(42))['status'] == 'RESOLVED'


@pytest.mark.asyncio
@pytest.mark.parametrize('actor', [ADMIN, SUPERVISOR])
async def test_managers_reopen_take_ownership_and_can_send(reopening, actor, monkeypatch):
    repo, sql, _, resolved = reopening
    await sql('UPDATE support_tickets SET resolved_by_staff_id=NULL WHERE id=42')
    assert (await support_service.ticket_detail(42, actor))['permissions']['can_reopen']
    result = await reopen(resolved, actor)
    assert result['ticket']['current_assignee_id'] == actor.id
    stored = await repo.get_ticket(42)
    assert stored['current_assignee_name'] == 'Other'  # Persisted staff name.
    assert stored['assigned_at'] and stored['resolved_at'] == resolved['resolved_at']
    detail = await support_service.ticket_detail(42, actor)
    assert detail['permissions']['can_transfer'] and detail['permissions']['can_reply']
    with pytest.raises(SupportForbiddenError):
        await support_service.ticket_detail(42, AGENT)
    with pytest.raises(SupportForbiddenError):
        await support_service.send_agent_message(42, agent=AGENT, body='former owner', client_message_id='forbidden')
    send = AsyncMock(return_value={'messages': [{'id': 'manager-accepted'}]})
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send))
    assert (await support_service.send_agent_message(42, agent=actor, body='reopened', client_message_id='manager'))['send_status'] == 'SENT'
    send.assert_awaited_once()
    inbox = await repo.list_tickets(view='mine', agent_id=actor.id)
    assert 42 in {t['id'] for t in inbox['items']}
    old_inbox = await repo.list_tickets(view='mine', agent_id=AGENT.id)
    assert 42 not in {t['id'] for t in old_inbox['items']}
    event = (await sql("SELECT * FROM support_ticket_events WHERE ticket_id=42 AND event_type='TICKET_REOPENED'"))[0]
    details = json.loads(event['details_json'])
    assert event['actor_id'] == actor.id
    assert str(details['last_assignee_id']) == AGENT.id
    assert details['current_assignee_id'] == actor.id
    assert not (await reopen(resolved, actor))['changed']


@pytest.mark.asyncio
async def test_repeated_and_concurrent_requests_are_idempotent_and_old_cycle_conflicts(reopening):
    repo, sql, _, resolved = reopening
    gate = asyncio.Event()
    async def request(actor):
        await gate.wait()
        return await reopen(resolved, actor)
    tasks = [asyncio.create_task(request(actor)) for actor in (AGENT, AGENT, AGENT)]
    gate.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
    assert sum(item['changed'] for item in results) == 1
    assert not (await reopen(resolved))['changed']
    assert len(await sql("SELECT id FROM support_ticket_events WHERE ticket_id=42 AND event_type='TICKET_REOPENED'")) == 1
    newer, _ = await repo.resolve_ticket(42, agent_id='7', agent_name='Agent')
    with pytest.raises(SupportConflictError, match='resolution changed'):
        await reopen(resolved)
    assert (await reopen(newer))['changed']
    assert len(await sql("SELECT id FROM support_ticket_events WHERE ticket_id=42 AND event_type='TICKET_RESOLVED'")) == 2


@pytest.mark.asyncio
async def test_conflicting_active_conversation_and_missing_assignee_are_explicit(reopening):
    repo, sql, _, resolved = reopening
    await sql("UPDATE support_tickets SET active_key='90005:155500042',whatsapp_phone='155500042' WHERE id=43")
    with pytest.raises(SupportConflictError, match='already has an active conversation'):
        await reopen(resolved)
    assert (await repo.get_ticket(42))['status'] == 'RESOLVED'
    await sql("UPDATE support_tickets SET active_key='90005:43' WHERE id=43")
    await sql("UPDATE support_tickets SET current_assignee_id='7' WHERE id=42")
    await sql('UPDATE staff SET is_active=0 WHERE id=7')
    with pytest.raises(SupportConflictError, match='unavailable'):
        await reopen(resolved, AGENT)


@pytest.mark.asyncio
async def test_different_resolved_tickets_race_for_one_active_key(reopening, monkeypatch):
    repo, sql, _, resolved = reopening
    await sql("UPDATE support_tickets SET whatsapp_phone='155500042',active_key=NULL,status='RESOLVED',resolved_by_staff_id=8,resolved_at=%s WHERE id=43", (resolved['resolved_at'],))
    other = await repo.get_ticket(43)
    entered = 0
    gate = asyncio.Event()
    original = repo.reopen_customer_ticket
    async def together(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            gate.set()
        await gate.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(repo, 'reopen_customer_ticket', together)
    results = await asyncio.wait_for(asyncio.gather(reopen(resolved, ADMIN), reopen(other, ADMIN), return_exceptions=True), 10)
    assert sum(isinstance(item, dict) and item['changed'] for item in results) == 1
    assert sum(isinstance(item, SupportConflictError) for item in results) == 1
    assert len(await sql("SELECT id FROM support_tickets WHERE active_key='90005:155500042'")) == 1


@pytest.mark.asyncio
async def test_permission_is_rechecked_under_ticket_lock(reopening, monkeypatch):
    repo, sql, _, resolved = reopening
    original = repo.reopen_customer_ticket
    async def raced(*args, **kwargs):
        await sql('UPDATE support_tickets SET resolved_by_staff_id=8 WHERE id=42')
        return await original(*args, **kwargs)
    monkeypatch.setattr(repo, 'reopen_customer_ticket', raced)
    with pytest.raises(SupportConflictError, match='permission changed'):
        await reopen(resolved)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['PENDING', 'DISPATCHING', 'UNCERTAIN'])
async def test_reopening_respects_outbound_guard_and_does_not_resend(reopening, status):
    repo, sql, _, resolved = reopening
    notice, _ = await repo.add_ticket_message(ticket_id=42, sender_type='SYSTEM', direction='OUTBOUND', body='notice', client_message_id='notice', send_status='PENDING')
    token = await repo.claim_delivery(notice['id'])
    if status != 'PENDING':
        assert await repo.begin_delivery(notice['id'], token)
    if status == 'UNCERTAIN':
        await repo.finish_delivery(notice['id'], token, status='UNCERTAIN')
        assert (await reopen(resolved))['changed']
    else:
        with pytest.raises(SupportConflictError, match='still being sent'):
            await reopen(resolved)
    assert (await repo.get_message(notice['id']))['send_status'] == status


@pytest.mark.asyncio
async def test_removed_line_staff_chat_and_closed_ticket_are_not_reopened(reopening, monkeypatch):
    repo, sql, _, resolved = reopening
    with monkeypatch.context() as patch:
        patch.setattr(service, 'get_transport', lambda *_: 'removed')
        assert not (await support_service.ticket_detail(42, AGENT))['permissions']['can_reopen']
        with pytest.raises(SupportConflictError, match='removed'):
            await reopen(resolved)
    await sql("UPDATE support_tickets SET conversation_origin='STAFF' WHERE id=42")
    with pytest.raises(SupportForbiddenError):
        await reopen(resolved, ADMIN)
    await sql("UPDATE support_tickets SET conversation_origin='CUSTOMER',status='CLOSED' WHERE id=42")
    with pytest.raises(SupportConflictError, match='Only resolved'):
        await reopen(resolved)


@pytest.mark.asyncio
async def test_api_requires_csrf_and_resolution_version_and_translates_denial(reopening):
    _, _, _, resolved = reopening
    route = next(r for r in api.router.routes if r.path.endswith('/tickets/{ticket_id}/reopen'))
    assert any(d.call is api.require_support_csrf for d in route.dependant.dependencies)
    with pytest.raises(ValueError):
        api.ReopenTicketRequest()
    payload = api.ReopenTicketRequest(resolved_at=resolved['resolved_at'])
    with pytest.raises(HTTPException) as exc:
        await api.reopen_ticket(42, payload, OTHER)
    assert exc.value.status_code == 403
    assert (await api.reopen_ticket(42, payload, AGENT))['changed']


@pytest.mark.asyncio
async def test_delayed_messages_map_to_reopened_interval_and_not_resolved_gap(reopening):
    repo, sql, _, resolved = reopening
    origin = datetime(2025, 1, 1)
    await sql('UPDATE support_tickets SET created_at=%s,resolved_at=%s WHERE id=42', (origin, origin+timedelta(hours=1)))
    await sql("UPDATE support_ticket_events SET created_at=%s WHERE ticket_id=42", (origin+timedelta(hours=1),))
    resolved = await repo.get_ticket(42)
    await reopen(resolved)
    await sql("UPDATE support_ticket_events SET created_at=%s WHERE ticket_id=42 AND event_type='TICKET_REOPENED'", (origin+timedelta(hours=3),))
    await repo.update_status(42, status='IN_PROGRESS', agent_id='7', agent_name='Agent')
    await sql("UPDATE support_ticket_events SET created_at=%s WHERE ticket_id=42 AND event_type='STATUS_CHANGED'", (origin+timedelta(hours=4),))
    for hours, expected in [(0.5, 42), (2, None), (5, 42)]:
        ticket = await repo.find_ticket_at_whatsapp_timestamp('155500042', '90005', origin+timedelta(hours=hours))
        assert (ticket['id'] if ticket else None) == expected


@pytest.mark.asyncio
async def test_legacy_resolution_keeps_known_owner_and_defaults_to_assigned(reopening):
    repo, sql, _, resolved = reopening
    await sql("UPDATE support_ticket_events SET details_json='{}' WHERE ticket_id=42 AND event_type='TICKET_RESOLVED'")
    assert (await reopen(resolved))['ticket']['status'] == 'ASSIGNED'
    assert (await repo.get_ticket(42))['current_assignee_id'] == '7'


@pytest.mark.asyncio
async def test_inbound_handoff_racing_reopen_reuses_unique_active_ticket(reopening, monkeypatch):
    repo, sql, _, resolved = reopening
    lookup = repo.find_active_ticket_by_phone
    looked_up, release = asyncio.Event(), asyncio.Event()
    first = True
    async def paused_lookup(*args):
        nonlocal first
        result = await lookup(*args)
        if first:
            first = False
            assert result is None
            looked_up.set()
            await release.wait()
        return result
    monkeypatch.setattr(repo, 'find_active_ticket_by_phone', paused_lookup)
    incoming = asyncio.create_task(repo.create_ticket(
        ticket_reference='NEW-HANDOFF', phone_number='155500042', business_phone_number_id='90005',
        business_display_phone_number=None, customer_id=None, customer_userid=None, customer_name=None,
        reason='test', ai_summary='', tracking_reference=None, requested_action=None,
        current_language='en', communication_style='en', context_json='{}', source_whatsapp_message_id=None,
    ))
    try:
        await asyncio.wait_for(looked_up.wait(), 5)
        assert (await reopen(resolved))['changed']
    finally:
        release.set()
        ticket, created = await asyncio.wait_for(incoming, 5)
    assert ticket['id'] == 42 and not created
    assert len(await sql("SELECT id FROM support_tickets WHERE active_key='90005:155500042'")) == 1


@pytest.mark.asyncio
async def test_linked_line_removal_rechecked_inside_transaction(reopening, monkeypatch):
    repo, sql, _, resolved = reopening
    monkeypatch.setattr(repository_module, 'get_transport', lambda *_: 'linked_device')
    # Service preflight sees a configured line, but the transactional registry
    # lookup sees it missing/removed and must fail before changing the ticket.
    with pytest.raises(SupportConflictError, match='inactive or removed'):
        await reopen(resolved)
    assert (await repo.get_ticket(42))['status'] == 'RESOLVED'


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_owner', [True, False])
async def test_manager_reopening_does_not_require_available_previous_owner(reopening, missing_owner):
    repo, sql, _, resolved = reopening
    if missing_owner:
        await sql('UPDATE support_tickets SET current_assignee_id=NULL WHERE id=42')
    else:
        await sql('UPDATE staff SET is_active=0 WHERE id=7')
    result = await reopen(resolved, SUPERVISOR)
    assert result['changed'] and result['ticket']['current_assignee_id'] == SUPERVISOR.id
    assert (await repo.get_ticket(42))['resolved_by_staff_id'] == 7


@pytest.mark.asyncio
async def test_concurrent_managers_only_first_reopener_takes_ownership(reopening, monkeypatch):
    repo, sql, _, resolved = reopening
    await sql("UPDATE staff SET role='admin' WHERE id=7")
    actors = [SupportAgent(id='7', name='Agent', role='admin'), SUPERVISOR]
    original = repo.reopen_customer_ticket
    entered = 0
    gate = asyncio.Event()
    async def together(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            gate.set()
        await gate.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(repo, 'reopen_customer_ticket', together)
    results = await asyncio.wait_for(asyncio.gather(*(reopen(resolved, actor) for actor in actors)), 10)
    assert sum(item['changed'] for item in results) == 1
    winner = actors[next(i for i, result in enumerate(results) if result['changed'])]
    assert (await repo.get_ticket(42))['current_assignee_id'] == winner.id
    for actor in actors:
        assert not (await reopen(resolved, actor))['changed']
    assert (await repo.get_ticket(42))['current_assignee_id'] == winner.id
    assert len(await sql("SELECT id FROM support_ticket_events WHERE ticket_id=42 AND event_type='TICKET_REOPENED'")) == 1


@pytest.mark.asyncio
async def test_manager_deactivated_before_reopening_cannot_become_assignee(reopening):
    repo, sql, _, resolved = reopening
    await sql('UPDATE staff SET is_active=0 WHERE id=8')
    with pytest.raises(SupportConflictError, match='unavailable'):
        await reopen(resolved, SUPERVISOR)
    stored = await repo.get_ticket(42)
    assert stored['status'] == 'RESOLVED' and stored['current_assignee_id'] == '7'
