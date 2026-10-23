from datetime import datetime
from types import SimpleNamespace
import asyncio

import pytest

from app.support_read_sync import sync_reply_reads


async def message(repo, *, incoming=False, ticket=42, key='one'):
    row, _ = await repo.add_ticket_message(ticket_id=ticket,
        sender_type='CUSTOMER' if incoming else 'AGENT', sender_id=None if incoming else '7',
        direction='INBOUND' if incoming else 'OUTBOUND', body='test',
        whatsapp_message_id=f'ld:90005:{key}' if incoming else None,
        client_message_id=None if incoming else key, send_status=None if incoming else 'PENDING')
    return row


@pytest.mark.asyncio
async def test_successful_reply_reads_only_prior_messages_and_failed_read_retries(infrastructure):
    repo, sql, _ = infrastructure
    old = await message(repo, incoming=True, key='old')
    reply = await message(repo, key='reply')
    token = await repo.claim_delivery(reply['id'])
    assert await repo.begin_delivery(reply['id'], token)
    newer = await message(repo, incoming=True, key='newer')
    await repo.finish_delivery(reply['id'], token, status='SENT', provider_id='ld:90005:reply')
    calls, events = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    async def send_read(**kwargs):
        calls.append(kwargs)
        entered.set()
        await release.wait()
        raise RuntimeError('read receipt connection failed')
    async def publish(*args, **kwargs):
        events.append((args, kwargs))
    task = asyncio.create_task(sync_reply_reads(repo, SimpleNamespace(mark_read=send_read), publish))
    await entered.wait()
    newest = await message(repo, incoming=True, key='newest')
    release.set()
    await task
    assert (await repo.get_message(old['id']))['read_at'] is None
    assert (await repo.get_message(reply['id']))['read_sync_pending'] == 1
    await sql('UPDATE support_ticket_messages SET read_sync_attempted_at=NULL WHERE id=%s', (reply['id'],))
    async def succeed(**kwargs):
        calls.append(kwargs)
    await sync_reply_reads(repo, SimpleNamespace(mark_read=succeed), publish)
    assert [c['message_id'] for c in calls] == ['ld:90005:old', 'ld:90005:old']
    assert (await repo.get_message(old['id']))['read_at'] is not None
    for row in (newer, newest):
        assert (await repo.get_message(row['id']))['read_at'] is None
    assert (await repo.get_message(reply['id']))['read_sync_pending'] == 0
    ticket = await repo.get_ticket(42)
    assert ticket['unread_count'] == 2
    assert ticket['awaiting_staff_reply'] is True
    # Reading is not answering: a customer message can be read but unanswered.
    await sql("UPDATE support_ticket_messages SET read_at=UTC_TIMESTAMP(6) WHERE direction='INBOUND'")
    ticket = await repo.get_ticket(42)
    assert ticket['unread_count'] == 0 and ticket['awaiting_staff_reply'] is True
    assert events[-1] == (('incoming_read',), {'ticket_id': 42})


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['FAILED', 'UNCERTAIN'])
async def test_failed_or_uncertain_reply_never_marks_customer_read(infrastructure, status):
    repo, sql, _ = infrastructure
    await message(repo, incoming=True)
    reply = await message(repo, key='reply')
    token = await repo.claim_delivery(reply['id'])
    assert await repo.begin_delivery(reply['id'], token)
    await repo.finish_delivery(reply['id'], token, status=status)
    assert await repo.pending_read_sync() == []
    ticket = await repo.get_ticket(42)
    assert ticket['unread_count'] == 1 and ticket['awaiting_staff_reply'] is True


@pytest.mark.asyncio
async def test_playback_is_monotonic_scoped_and_cloud_failure_can_be_retried(infrastructure):
    repo, sql, _ = infrastructure
    row = await message(repo, key='reply')
    token = await repo.claim_delivery(row['id'])
    assert await repo.begin_delivery(row['id'], token)
    await repo.finish_delivery(row['id'], token, status='SENT', provider_id='wamid.test')
    async def receipt(status, line='90005'):
        return await repo.apply_delivery_status('wamid.test', status=status, at=datetime(2026,10,23),
            business_phone_number_id=line, phone_number='155500042')
    assert await receipt('played', '90006') is None
    failed, changed = await receipt('failed')
    assert changed and failed['send_status'] == 'FAILED' and failed['delivery_retry_safe']
    next_token = await repo.claim_delivery(row['id'], retry=True, expected_token=token)
    assert next_token and next_token != token
    assert await repo.begin_delivery(row['id'], next_token)
    await repo.finish_delivery(row['id'], next_token, status='SENT', provider_id='wamid.retry')
    assert await receipt('failed') is None, 'old attempt callback cannot fail a new send'
    audit = await sql("SELECT details_json FROM support_ticket_events WHERE event_type='DELIVERY_RETRY'")
    assert 'wamid.test' in audit[0]['details_json']
    for status in ['delivered', 'read', 'played', 'read', 'failed']:
        updated, _ = await repo.apply_delivery_status('wamid.retry', status=status, at=datetime(2026,10,23),
            business_phone_number_id='90005', phone_number='155500042')
    assert updated['send_status'] == 'SENT' and updated['delivery_status'] == 'played'
    assert updated['delivered_at'] and updated['read_at']


@pytest.mark.asyncio
async def test_early_cloud_receipt_survives_send_race_and_replays(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    import app.support_service as module
    monkeypatch.setattr(module, 'support_repository', repo)
    seen = []
    async def publish(*args, **kwargs):
        seen.append((args, kwargs))
    monkeypatch.setattr(module, 'publish_support_event', publish)
    reply = await message(repo, key='early')
    token = await repo.claim_delivery(reply['id'])
    assert await repo.begin_delivery(reply['id'], token)
    args = dict(whatsapp_message_id='wamid.early', status='read', timestamp_ms=1792750000000,
                business_phone_number_id='90005', phone_number='155500042')
    assert await module.support_service.apply_delivery_status(**args) is False
    assert await module.support_service.apply_delivery_status(**args) is False
    assert len(await repo.pending_delivery_receipts()) == 1
    await repo.finish_delivery(reply['id'], token, status='SENT', provider_id='wamid.early')
    await module.support_service.reconcile_delivery_receipts()
    assert await repo.pending_delivery_receipts() == []
    assert (await repo.get_message(reply['id']))['delivery_status'] == 'read'
    assert seen[-1][0] == ('message_status',)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text', 'location', 'audio', 'image', 'document', 'video', 'sticker'])
async def test_content_types_share_failure_retry_and_read_boundary(infrastructure, tmp_path, kind):
    from app.support_delivery import deliver
    from app.whatsapp_client import WhatsAppSendError
    repo, sql, _ = infrastructure
    incoming = await message(repo, incoming=True, key='old')
    row = await message(repo, key='content')
    row.update(message_type=kind, body='Voice note' if kind == 'audio' else 'test', location_latitude=1, location_longitude=2)
    path = tmp_path / 'asset'
    path.write_bytes(b'fixture')
    asset = SimpleNamespace(path=path, mime_type='audio/ogg; codecs=opus', filename='test.ogg')
    calls = []
    fail = True
    async def upload(**kwargs):
        return 'media'
    async def send(**kwargs):
        calls.append(kwargs)
        if fail:
            raise WhatsAppSendError('Rejected', delivery_uncertain=False)
        return {'messages': [{'id': 'ld:90005:ok'}]}
    transport = SimpleNamespace(upload_media=upload, send_media=send, send_text=send, send_location=send)
    store = SimpleNamespace(get=lambda *_: asset)
    ticket = await repo.get_ticket(42)
    result = await deliver(repo, transport, store, ticket, row)
    assert result['send_status'] == 'FAILED' and result['delivery_retry_safe']
    assert await repo.pending_read_sync() == []
    fail = False
    row.update(result, message_type=kind, body=row['body'], location_latitude=1, location_longitude=2)
    result = await deliver(repo, transport, store, ticket, row, retry=True)
    assert result['send_status'] == 'SENT' and result['delivery_status'] is None
    assert len(calls) == 2
    assert len(await repo.pending_read_sync()) == 1
    assert (await repo.get_message(incoming['id']))['read_at'] is None
    if kind == 'audio':
        assert calls[-1]['voice'] is True


@pytest.mark.asyncio
async def test_reply_resyncs_last_already_read_message_without_reading_newer_or_other_ticket(infrastructure):
    repo, sql, _ = infrastructure
    old = await message(repo, incoming=True, key='already-read')
    await sql('UPDATE support_ticket_messages SET read_at=UTC_TIMESTAMP(6) WHERE id=%s', (old['id'],))
    original_read_at = (await repo.get_message(old['id']))['read_at']
    await message(repo, incoming=True, ticket=43, key='other-ticket')
    reply = await message(repo, key='resync')
    newer = await message(repo, incoming=True, key='after-reply')
    token = await repo.claim_delivery(reply['id'])
    assert await repo.begin_delivery(reply['id'], token)
    await repo.finish_delivery(reply['id'], token, status='SENT', provider_id='ld:90005:resync')
    calls = []
    async def read(**kwargs):
        calls.append(kwargs)
    async def publish(*args, **kwargs):
        pass
    await sync_reply_reads(repo, SimpleNamespace(mark_read=read), publish)
    assert [call['message_id'] for call in calls] == ['ld:90005:already-read']
    assert (await repo.get_message(old['id']))['read_at'] == original_read_at
    assert (await repo.get_message(newer['id']))['read_at'] is None
    assert (await repo.get_ticket(43))['unread_count'] == 1
    assert not (await repo.get_message(reply['id']))['read_sync_pending']


@pytest.mark.asyncio
async def test_cloud_window_rejection_survives_early_receipt_and_blocks_retry(infrastructure, monkeypatch):
    import app.support_service as module
    from app.support_auth import SupportAgent
    from app.support_repository import SupportConflictError
    repo, sql, _ = infrastructure
    async def publish(*args, **kwargs):
        pass
    monkeypatch.setattr(module, 'publish_support_event', publish)
    reply = await message(repo, key='window-closed')
    token = await repo.claim_delivery(reply['id'])
    assert await repo.begin_delivery(reply['id'], token)
    args = dict(whatsapp_message_id='wamid.window', status='failed', timestamp_ms=1792750000000,
                business_phone_number_id='90005', phone_number='155500042')
    # Generic receipt may arrive before the detailed provider rejection.
    assert not await module.support_service.apply_delivery_status(**args)
    assert not await module.support_service.apply_delivery_status(**args, failure_code=131047)
    inbox = await repo.pending_delivery_receipts()
    assert len(inbox) == 1 and inbox[0]['failure_code'] == 131047
    await repo.finish_delivery(reply['id'], token, status='SENT', provider_id='wamid.window')
    await module.support_service.reconcile_delivery_receipts()
    failed = await repo.get_message(reply['id'])
    assert failed['send_status'] == 'FAILED' and not failed['delivery_retry_safe']
    assert '131047' in failed['error_message'] and '24-hour reply window' in failed['error_message']
    assert not failed['read_sync_pending']
    assert not await repo.pending_delivery_receipts()
    # Duplicate generic callbacks cannot re-enable a rejected retry.
    await module.support_service.apply_delivery_status(**args)
    assert not (await repo.get_message(reply['id']))['delivery_retry_safe']
    assert await repo.claim_delivery(reply['id'], retry=True, expected_token=token) is None
    with pytest.raises(SupportConflictError, match='131047'):
        await module.support_service.resend_failed_message(42, reply['id'], agent=SupportAgent(id='7', name='Agent', role='agent'))


@pytest.mark.asyncio
async def test_failure_details_enrich_existing_failure_only_with_correct_scope(infrastructure):
    repo, _, _ = infrastructure
    row = await message(repo, key='details')
    token = await repo.claim_delivery(row['id'])
    assert await repo.begin_delivery(row['id'], token)
    await repo.finish_delivery(row['id'], token, status='SENT', provider_id='wamid.details')
    args = dict(status='failed', at=datetime(2026, 10, 23), business_phone_number_id='90005', phone_number='155500042')
    failed, changed = await repo.apply_delivery_status('wamid.details', **args)
    assert changed and failed['delivery_retry_safe']
    for wrong in ({'business_phone_number_id': '90006'}, {'phone_number': '155500043'}):
        assert await repo.apply_delivery_status('wamid.details', **(args | wrong), failure_code=131047) is None
    assert (await repo.get_message(row['id']))['delivery_retry_safe']
    failed, changed = await repo.apply_delivery_status('wamid.details', **args, failure_code=131047)
    assert changed and not failed['delivery_retry_safe']
    assert '131047' in failed['error_message']
    _, changed = await repo.apply_delivery_status('wamid.details', **args, failure_code=131047)
    assert not changed
