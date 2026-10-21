"""Real transactions, controlled interleavings, and no external provider calls."""
import asyncio
from types import SimpleNamespace

import pytest

from app.support_delivery import deliver
from app.support_repository import SupportConflictError


OPERATIONS = ['transfer', 'resolve', 'close']


async def setup_ticket(sql, operation):
    if operation == 'close':
        await sql("UPDATE support_tickets SET conversation_origin='STAFF', initiated_by_staff_id='7' WHERE id=42")


async def lifecycle(repo, operation, ticket_id=42):
    if operation == 'transfer':
        return await repo.transfer_ticket(ticket_id, expected_assignee_id='7', target_staff_id=8,
                                          actor_id='7', actor_name='Agent')
    method = repo.close_staff_chat if operation == 'close' else repo.resolve_ticket
    return await method(ticket_id, agent_id='7', agent_name='Agent', expected_assignee_id='7')


async def message(repo, **kwargs):
    row, _ = await repo.add_ticket_message(ticket_id=42, sender_type='AGENT', sender_id='7',
        direction='OUTBOUND', body='test', client_message_id='lifecycle', send_status='PENDING', **kwargs)
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', OPERATIONS)
@pytest.mark.parametrize('phase', ['preparing', 'dispatching', 'retry_dispatching'])
async def test_active_attempt_blocks_lifecycle_until_completion(infrastructure, monkeypatch, operation, phase):
    repo, sql, _ = infrastructure
    await setup_ticket(sql, operation)
    row = await message(repo)
    # Message age is unrelated to the age of its delivery attempt.
    await sql('UPDATE support_ticket_messages SET created_at=UTC_TIMESTAMP(6)-INTERVAL 2 DAY WHERE id=%s', (row['id'],))
    if phase == 'retry_dispatching':
        token = await repo.claim_delivery(row['id'])
        await repo.finish_delivery(row['id'], token, status='FAILED', error='preparation failed')
        row = await repo.get_message(row['id'])
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def send(**kwargs):
        calls.append(kwargs)
        return {'messages': [{'id': 'accepted'}]}
    # Pause either after preparation is claimed or immediately after dispatch
    # commits, before calling the provider. No sleeps or real network traffic.
    original = repo.claim_delivery if phase == 'preparing' else repo.begin_delivery
    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        assert result
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(repo, 'claim_delivery' if phase == 'preparing' else 'begin_delivery', paused)
    task = asyncio.create_task(deliver(repo, SimpleNamespace(send_text=send), None,
        await repo.get_ticket(42), row, retry=phase == 'retry_dispatching'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not calls
        assert (await repo.get_message(row['id']))['send_status'] == ('PENDING' if phase == 'preparing' else 'DISPATCHING')
        with pytest.raises(SupportConflictError, match='still being sent'):
            await asyncio.wait_for(lifecycle(repo, operation), 5)
        assert (await repo.get_ticket(42))['current_assignee_id'] == '7'
    finally:
        release.set()
        result = await asyncio.wait_for(task, 5)
    assert result['send_status'] == 'SENT' and len(calls) == 1
    await lifecycle(repo, operation)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', OPERATIONS)
@pytest.mark.parametrize('worker_state', ['unclaimed', 'expired'])
async def test_lifecycle_wins_and_fences_suspended_work(infrastructure, monkeypatch, operation, worker_state):
    repo, sql, _ = infrastructure
    await setup_ticket(sql, operation)
    row = await message(repo)
    token = None
    if worker_state == 'expired':
        token = await repo.claim_delivery(row['id'])
        await sql('UPDATE support_ticket_messages SET delivery_claimed_at=UTC_TIMESTAMP(6)-INTERVAL 6 MINUTE WHERE id=%s', (row['id'],))
    holding_ticket, release_lifecycle, worker_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_guard, original_lock = repo._fence_deliveries_for_lifecycle, repo._lock_delivery_ticket
    async def paused_guard(cur, ticket_id):
        allowed = await original_guard(cur, ticket_id)
        assert allowed
        holding_ticket.set()
        await release_lifecycle.wait()
        return allowed
    async def observed_lock(*args):
        worker_entered.set()
        return await original_lock(*args)
    monkeypatch.setattr(repo, '_fence_deliveries_for_lifecycle', paused_guard)
    monkeypatch.setattr(repo, '_lock_delivery_ticket', observed_lock)
    changing = asyncio.create_task(lifecycle(repo, operation))
    worker = None
    try:
        await asyncio.wait_for(holding_ticket.wait(), 5)
        worker = asyncio.create_task(repo.begin_delivery(row['id'], token) if token else repo.claim_delivery(row['id']))
        await asyncio.wait_for(worker_entered.wait(), 5)
    finally:
        release_lifecycle.set()
        await asyncio.wait_for(changing, 5)
    assert not await asyncio.wait_for(worker, 5)
    cancelled = await repo.get_message(row['id'])
    assert cancelled['send_status'] == 'FAILED' and cancelled['delivery_retry_safe']
    assert cancelled['delivery_token'] != token
    # Even reopening/transferring back cannot revive the obsolete token.
    await sql("UPDATE support_tickets SET current_assignee_id='7',status='ASSIGNED',active_key='90005:42' WHERE id=42")
    if token:
        assert not await repo.begin_delivery(row['id'], token)
        await repo.finish_delivery(row['id'], token, status='SENT', provider_id='obsolete')
    assert (await repo.get_message(row['id']))['send_status'] == 'FAILED'
    assert await repo.claim_delivery(row['id']) is None
    # Only an explicit retry with the newly observed attempt token can resume.
    assert await repo.claim_delivery(row['id'], retry=True, expected_token=cancelled['delivery_token'])


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', OPERATIONS)
async def test_dispatch_transaction_wins_ticket_lock(infrastructure, monkeypatch, operation):
    repo, sql, _ = infrastructure
    await setup_ticket(sql, operation)
    row = await message(repo)
    token = await repo.claim_delivery(row['id'])
    locked, release = asyncio.Event(), asyncio.Event()
    original = repo._lock_delivery_ticket
    async def paused(*args):
        ticket = await original(*args)
        locked.set()
        await release.wait()
        return ticket
    monkeypatch.setattr(repo, '_lock_delivery_ticket', paused)
    dispatch = asyncio.create_task(repo.begin_delivery(row['id'], token))
    changing = None
    try:
        await asyncio.wait_for(locked.wait(), 5)
        # The lifecycle transaction must wait on the same parent row and then
        # observe the committed DISPATCHING state (never a snapshot from before).
        started = asyncio.Event()
        async def change():
            started.set()
            return await lifecycle(repo, operation)
        changing = asyncio.create_task(change())
        await started.wait()
    finally:
        release.set()
        assert await asyncio.wait_for(dispatch, 5)
    with pytest.raises(SupportConflictError, match='still being sent'):
        await asyncio.wait_for(changing, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', OPERATIONS)
async def test_expired_dispatch_requires_recovery_and_preserves_uncertainty(infrastructure, operation):
    repo, sql, _ = infrastructure
    await setup_ticket(sql, operation)
    row = await message(repo)
    token = await repo.claim_delivery(row['id'])
    assert await repo.begin_delivery(row['id'], token)
    await sql('UPDATE support_ticket_messages SET delivery_claimed_at=UTC_TIMESTAMP(6)-INTERVAL 6 MINUTE WHERE id=%s', (row['id'],))
    with pytest.raises(SupportConflictError, match='still being sent'):
        await lifecycle(repo, operation)
    assert (await repo.get_message(row['id']))['send_status'] == 'DISPATCHING'
    assert await repo.recover_abandoned_deliveries() == 1
    assert await repo.claim_delivery(row['id'], retry=True, expected_token=token) is None
    await repo.finish_delivery(row['id'], token, status='FAILED', error='late failure')
    assert (await repo.get_message(row['id']))['send_status'] == 'UNCERTAIN'
    await lifecycle(repo, operation)
    uncertain = await repo.get_message(row['id'])
    assert uncertain['send_status'] == 'UNCERTAIN' and uncertain['delivery_token'] == token
    # Authentic acceptance of that same attempt is still useful after closure.
    await repo.finish_delivery(row['id'], token, status='SENT', provider_id='late-acceptance')
    assert (await repo.get_message(row['id']))['send_status'] == 'SENT'


@pytest.mark.asyncio
async def test_expired_preparation_cannot_dispatch_or_finish_and_reclaim_fences(infrastructure):
    repo, sql, _ = infrastructure
    row = await message(repo)
    first = await repo.claim_delivery(row['id'])
    await sql('UPDATE support_ticket_messages SET delivery_claimed_at=UTC_TIMESTAMP(6)-INTERVAL 6 MINUTE WHERE id=%s', (row['id'],))
    assert not await repo.begin_delivery(row['id'], first)  # No other worker needs to reclaim first.
    await repo.finish_delivery(row['id'], first, status='FAILED')
    assert (await repo.get_message(row['id']))['send_status'] == 'PENDING'
    second = await repo.claim_delivery(row['id'])
    assert second and second != first
    assert not await repo.begin_delivery(row['id'], first)
    for status in ('FAILED', 'UNCERTAIN', 'SENT'):
        await repo.finish_delivery(row['id'], first, status=status, provider_id='obsolete')
        assert (await repo.get_message(row['id']))['delivery_token'] == second
        assert (await repo.get_message(row['id']))['send_status'] == 'PENDING'
    assert await repo.begin_delivery(row['id'], second)
    await repo.finish_delivery(row['id'], second, status='SENT', provider_id='current')


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', OPERATIONS)
async def test_other_ticket_dispatch_does_not_block_lifecycle(infrastructure, operation):
    repo, sql, _ = infrastructure
    await setup_ticket(sql, operation)
    row, _ = await repo.add_ticket_message(ticket_id=43, sender_type='AGENT', sender_id='8',
        direction='OUTBOUND', body='other', client_message_id='other', send_status='PENDING')
    token = await repo.claim_delivery(row['id'])
    assert await repo.begin_delivery(row['id'], token)
    await lifecycle(repo, operation)
    assert (await repo.get_message(row['id']))['send_status'] == 'DISPATCHING'


@pytest.mark.asyncio
async def test_system_preparation_is_fenced_but_new_resolution_notice_can_send(infrastructure):
    repo, sql, _ = infrastructure
    row, _ = await repo.add_ticket_message(ticket_id=42, sender_type='SYSTEM', direction='OUTBOUND',
        body='greeting', client_message_id='greeting', send_status='PENDING')
    token = await repo.claim_delivery(row['id'])
    with pytest.raises(SupportConflictError, match='still being sent'):
        await lifecycle(repo, 'resolve')
    await sql('UPDATE support_ticket_messages SET delivery_claimed_at=UTC_TIMESTAMP(6)-INTERVAL 6 MINUTE WHERE id=%s', (row['id'],))
    await lifecycle(repo, 'resolve')
    assert not await repo.begin_delivery(row['id'], token)
    notice, _ = await repo.add_ticket_message(ticket_id=42, sender_type='SYSTEM', direction='OUTBOUND',
        body='resolved', client_message_id='resolved', send_status='PENDING')
    fresh = await repo.claim_delivery(notice['id'])
    assert await repo.begin_delivery(notice['id'], fresh)
