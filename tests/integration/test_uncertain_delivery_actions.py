"""Real database/Redis, controlled interleavings, no customer/provider traffic."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.support_auth import SupportAgent
from app.support_repository import SupportConflictError, SupportNotFoundError, SupportSchemaError
from app.support_service import SupportForbiddenError, SupportValidationError, SupportSendError
import app.support_service as service

AGENT = SupportAgent(id='7', name='Agent', role='agent')
OTHER = SupportAgent(id='8', name='Supervisor', role='supervisor')


async def uncertain(repo, *, key='uncertain', kind='text'):
    row, _ = await repo.add_ticket_message(ticket_id=42, sender_type='AGENT', sender_id='7',
        direction='OUTBOUND', body='Example message', client_message_id=key, send_status='PENDING', message_type=kind,
        location_latitude=33.89 if kind == 'location' else None,
        location_longitude=35.5 if kind == 'location' else None)
    token = await repo.claim_delivery(row['id'])
    assert await repo.begin_delivery(row['id'], token)
    await repo.finish_delivery(row['id'], token, status='UNCERTAIN')
    return await repo.get_message(row['id'])


def provider(monkeypatch, *, status='unconfirmed'):
    calls = []
    async def check(**kwargs):
        calls.append(('check', kwargs))
        return {'status': status, 'message_id': 'ld:90005:evidence', 'timestamp': 1792660000000}
    async def send(**kwargs):
        calls.append(('send', kwargs))
        return {'messages': [{'id': 'ld:90005:copy'}]}
    async def upload(**kwargs):
        calls.append(('upload', kwargs))
        return 'uploaded'
    monkeypatch.setattr(service, 'get_transport', lambda line: 'linked_device')
    monkeypatch.setattr(service, 'linked_device_client', SimpleNamespace(get_delivery_status=check))
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send, send_location=send,
                                                                  send_media=send, upload_media=upload))
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize('status,expected', [('confirmed', 'SENT'), ('unconfirmed', 'UNCERTAIN'), ('unknown', 'UNCERTAIN')])
async def test_manual_and_automatic_checks_never_dispatch(infrastructure, monkeypatch, status, expected):
    repo, _, _ = infrastructure
    row = await uncertain(repo)
    calls = provider(monkeypatch, status=status)
    response = await service.support_service.check_message_delivery(42, row['id'], agent=AGENT)
    assert response['message']['send_status'] == expected
    assert response['message'].get('delivery_status') is None  # Acceptance does not invent a delivered receipt.
    assert 'transport_client_id' not in response['message']
    assert await service.support_service.reconcile_uncertain_deliveries() == 0
    assert (await repo.get_message(row['id']))['send_status'] == expected
    assert all(action == 'check' for action, _ in calls)


@pytest.mark.asyncio
async def test_checks_enforce_ticket_access_hidden_messages_and_cross_ticket_ids(infrastructure, monkeypatch):
    repo, _, _ = infrastructure
    row = await uncertain(repo)
    calls = provider(monkeypatch)
    outsider = SupportAgent(id='9', name='Outsider', role='agent')
    for ticket, agent in [(42, outsider), (43, OTHER)]:
        with pytest.raises(SupportNotFoundError):
            await service.support_service.check_message_delivery(ticket, row['id'], agent=agent)
    await repo.change_message(row['id'], ticket_id=42, user_id='7', action='me')
    with pytest.raises(SupportNotFoundError):
        await service.support_service.check_message_delivery(42, row['id'], agent=AGENT)
    assert not calls


@pytest.mark.asyncio
async def test_cloud_and_unavailable_bridge_do_not_infer_failure_or_dispatch(infrastructure, monkeypatch):
    repo, _, _ = infrastructure
    row = await uncertain(repo)
    calls = provider(monkeypatch)
    monkeypatch.setattr(service, 'get_transport', lambda line: 'cloud')
    response = await service.support_service.check_message_delivery(42, row['id'], agent=AGENT)
    assert response['message']['send_status'] == 'UNCERTAIN' and not calls
    monkeypatch.setattr(service, 'get_transport', lambda line: 'linked_device')
    async def unavailable(**kwargs):
        raise RuntimeError('simulated outage')
    monkeypatch.setattr(service.linked_device_client, 'get_delivery_status', unavailable)
    with pytest.raises(SupportSendError):
        await service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT, accept_duplicate_risk=True)
    assert await repo.get_delivery_replacement(42, row['id']) is None
    assert (await repo.get_message(row['id']))['send_status'] == 'UNCERTAIN'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text', 'location', 'image'])
async def test_copy_retains_original_and_audits_risk_with_fresh_transport_identity(infrastructure, monkeypatch, tmp_path, kind):
    repo, sql, _ = infrastructure
    row = await uncertain(repo, kind=kind)
    calls = provider(monkeypatch)
    monkeypatch.setattr(service.settings, 'support_media_directory', str(tmp_path))
    from app.support_media import SupportMediaStore
    monkeypatch.setattr(service, 'support_media_store', SupportMediaStore())
    if kind == 'image':
        service.support_media_store.save(ticket_id=42, message_id=row['id'], content=b'test image',
                                         mime_type='image/jpeg', filename='example.jpg')
    copy = await service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT, accept_duplicate_risk=True)
    stored = await repo.get_message(copy['id'])
    original = await repo.get_message(row['id'])
    assert original['send_status'] == 'UNCERTAIN' and original['body'] == row['body']
    assert stored['replacement_for_message_id'] == row['id'] and stored['send_status'] == 'SENT'
    assert stored['transport_client_id'] != original['transport_client_id']
    assert stored['body'] == original['body'] and stored['message_type'] == kind
    events = await sql("SELECT actor_id,details_json FROM support_ticket_events WHERE event_type='UNCERTAIN_MESSAGE_COPY'")
    assert len(events) == 1 and events[0]['actor_id'] == '7'
    assert json.loads(events[0]['details_json']) == {'original_message_id': row['id'], 'message_id': copy['id'], 'duplicate_risk_accepted': True}
    again = await service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT, accept_duplicate_risk=True)
    assert again['id'] == copy['id'] and sum(action == 'send' for action, _ in calls) == 1


@pytest.mark.asyncio
async def test_concurrent_copy_requests_reserve_one_copy_and_dispatch_once(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    row = await uncertain(repo)
    calls = provider(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    async def send(**kwargs):
        calls.append(('send', kwargs)); entered.set(); await release.wait()
        return {'messages': [{'id': 'ld:90005:concurrent-copy'}]}
    monkeypatch.setattr(service.whatsapp_client, 'send_text', send)
    first = asyncio.create_task(service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT, accept_duplicate_risk=True))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = await service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT, accept_duplicate_risk=True)
        assert second['send_status'] == 'DISPATCHING'
    finally:
        release.set()
        result = await asyncio.wait_for(first, 5)
    assert result['id'] == second['id']
    assert sum(action == 'send' for action, _ in calls) == 1
    assert len(await sql("SELECT id FROM support_ticket_messages WHERE replacement_for_message_id=%s", (row['id'],))) == 1


@pytest.mark.asyncio
async def test_copy_permissions_confirmation_and_resolved_history(infrastructure, monkeypatch):
    repo, _, _ = infrastructure
    row = await uncertain(repo)
    calls = provider(monkeypatch)
    with pytest.raises(SupportValidationError):
        await service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT)
    with pytest.raises(SupportForbiddenError):
        await service.support_service.send_uncertain_copy(42, row['id'], agent=OTHER, accept_duplicate_risk=True)
    await repo.resolve_ticket(42, agent_id='7', agent_name='Agent', expected_assignee_id='7')
    with pytest.raises(SupportConflictError):
        await service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT, accept_duplicate_risk=True)
    assert not calls
    # Shared history may check evidence, but never grants a right to send.
    reader = SupportAgent(id='9', name='Reader', role='agent')
    await service.support_service.check_message_delivery(42, row['id'], agent=reader)
    assert len(calls) == 1 and calls[0][0] == 'check'


@pytest.mark.asyncio
async def test_confirmed_original_and_lifecycle_winner_prevent_copy(infrastructure, monkeypatch):
    repo, _, _ = infrastructure
    row = await uncertain(repo)
    calls = provider(monkeypatch, status='confirmed')
    with pytest.raises(SupportConflictError):
        await service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT, accept_duplicate_risk=True)
    assert await repo.get_delivery_replacement(42, row['id']) is None
    assert all(action == 'check' for action, _ in calls)
    row = await uncertain(repo, key='second')
    provider(monkeypatch)
    add = repo.add_ticket_message
    async def resolve_before_insert(**kwargs):
        await repo.resolve_ticket(42, agent_id='7', agent_name='Agent', expected_assignee_id='7')
        return await add(**kwargs)
    monkeypatch.setattr(repo, 'add_ticket_message', resolve_before_insert)
    with pytest.raises(SupportConflictError):
        await service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT, accept_duplicate_risk=True)
    assert await repo.get_delivery_replacement(42, row['id']) is None


@pytest.mark.asyncio
async def test_migration_repeatable_and_unique_guard(infrastructure):
    from pathlib import Path
    repo, sql, _ = infrastructure
    async with repo._pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(Path('migrations/20261022_uncertain_delivery_copies.sql').read_text())
            while await cur.nextset(): pass
    await repo._validate_schema(repo._pool)
    await sql('ALTER TABLE support_ticket_messages DROP INDEX uq_support_delivery_replacement')
    with pytest.raises(SupportSchemaError, match='20261022_uncertain_delivery_copies'):
        await repo._validate_schema(repo._pool)


@pytest.mark.asyncio
async def test_two_copies_both_miss_lookup_but_insert_and_dispatch_once(infrastructure, monkeypatch):
    repo, _, _ = infrastructure
    row = await uncertain(repo)
    calls = provider(monkeypatch)
    gate = asyncio.Event()
    arrivals = 0
    async def miss(*args):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            gate.set()
        await gate.wait()
        return None
    monkeypatch.setattr(repo, 'get_delivery_replacement', miss)
    responses = await asyncio.wait_for(asyncio.gather(*[
        service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT, accept_duplicate_risk=True)
        for _ in range(2)]), 5)
    assert responses[0]['id'] == responses[1]['id']
    assert sum(action == 'send' for action, _ in calls) == 1


@pytest.mark.asyncio
async def test_late_confirmation_wins_before_copy_insert(infrastructure, monkeypatch):
    repo, _, _ = infrastructure
    row = await uncertain(repo)
    calls = provider(monkeypatch)
    add = repo.add_ticket_message
    async def confirm_before_insert(**kwargs):
        await repo.finish_delivery(row['id'], row['delivery_token'], status='SENT', provider_id='ld:90005:late')
        return await add(**kwargs)
    monkeypatch.setattr(repo, 'add_ticket_message', confirm_before_insert)
    with pytest.raises(SupportConflictError):
        await service.support_service.send_uncertain_copy(42, row['id'], agent=AGENT, accept_duplicate_risk=True)
    assert all(action == 'check' for action, _ in calls)
    assert await repo.get_delivery_replacement(42, row['id']) is None


@pytest.mark.asyncio
async def test_background_verification_continues_after_one_bridge_lookup_fails(infrastructure, monkeypatch):
    repo, _, _ = infrastructure
    first = await uncertain(repo)
    second = await uncertain(repo, key='second')
    provider(monkeypatch)
    async def check(**kwargs):
        if kwargs['client_message_id'] == first['transport_client_id']:
            raise RuntimeError('line unavailable')
        return {'status': 'confirmed', 'message_id': 'ld:90005:second', 'timestamp': 1792660000000}
    monkeypatch.setattr(service.linked_device_client, 'get_delivery_status', check)
    assert await service.support_service.reconcile_uncertain_deliveries() == 0
    assert (await repo.get_message(first['id']))['send_status'] == 'UNCERTAIN'
    assert (await repo.get_message(second['id']))['send_status'] == 'SENT'


@pytest.mark.asyncio
async def test_api_actions_require_csrf_and_strict_risk_confirmation(infrastructure, monkeypatch):
    from pydantic import ValidationError
    import app.support_api as api
    repo, _, _ = infrastructure
    row = await uncertain(repo)
    provider(monkeypatch)
    for suffix in ('check-delivery', 'new-copy'):
        route = next(route for route in api.router.routes if getattr(route, 'path', '').endswith('/messages/{message_id}/'+suffix))
        assert any(dependency.call is api.require_support_csrf for dependency in route.dependant.dependencies)
    for invalid in ({}, {'accept_duplicate_risk': 'true'}, {'accept_duplicate_risk': 1}):
        with pytest.raises(ValidationError):
            api.UncertainCopyRequest(**invalid)
    assert (await api.check_ticket_message_delivery(42, row['id'], AGENT))['message']['send_status'] == 'UNCERTAIN'
    response = await api.copy_uncertain_ticket_message(42, row['id'], api.UncertainCopyRequest(accept_duplicate_risk=True), AGENT)
    assert response['message']['replacement_for_message_id'] == row['id']


@pytest.mark.asyncio
async def test_candidate_cursor_includes_uncertain_and_old_dispatch_not_live_work(infrastructure):
    repo, sql, _ = infrastructure
    first = await uncertain(repo)
    second = await uncertain(repo, key='second')
    await sql("UPDATE support_ticket_messages SET send_status='DISPATCHING' WHERE id=%s", (second['id'],))
    candidates = await repo.list_delivery_reconciliation_candidates(limit=1)
    assert [row['id'] for row in candidates] == [first['id']]
    assert not await repo.list_delivery_reconciliation_candidates(first['id'])
    await sql('UPDATE support_ticket_messages SET delivery_claimed_at=UTC_TIMESTAMP(6)-INTERVAL 6 MINUTE WHERE id=%s', (second['id'],))
    assert [row['id'] for row in await repo.list_delivery_reconciliation_candidates(first['id'])] == [second['id']]
