import asyncio
from types import SimpleNamespace

import pytest

from app.support_auth import SupportAgent
from app.support_delivery import deliver
from app.support_repository import SupportConflictError
from app.support_service import SupportForbiddenError, support_service
from app.whatsapp_client import WhatsAppSendError
import app.support_service as service

class Barrier:
    def __init__(self, count):
        self.count = count
        self.event = asyncio.Event()

    async def wait(self):
        self.count -= 1
        if not self.count:
            self.event.set()
        await self.event.wait()


AGENT = SupportAgent(id='7', name='Agent', role='agent')


async def create(repository, **kwargs):
    return await repository.add_ticket_message(ticket_id=42, sender_type='AGENT', sender_id='7', direction='OUTBOUND',
        body='test', client_message_id='request', send_status='PENDING', **kwargs)


@pytest.mark.asyncio
async def test_concurrent_claims(infrastructure):
    repo, sql, _ = infrastructure
    gate = Barrier(2)
    async def claim(agent):
        await gate.wait()
        return await repo.claim_ticket(41, agent_id=agent, agent_name=agent)
    results = await asyncio.gather(claim('7'), claim('8'), return_exceptions=True)
    assert sum(isinstance(result, SupportConflictError) for result in results) == 1
    assert len(await sql("SELECT id FROM support_ticket_events WHERE ticket_id=41 AND event_type='TICKET_CLAIMED'")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['text', 'location', 'image'])
async def test_concurrent_sends_one_dispatch_and_scoped_ids(infrastructure, monkeypatch, tmp_path, kind):
    repo, sql, _ = infrastructure
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def send(**kwargs):
        calls.append(kwargs)
        entered.set()
        await release.wait()
        return {'messages': [{'id': 'ld:90005:confirmed'}]}
    async def upload(**kwargs):
        return 'asset'
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send, send_location=send, send_media=send, upload_media=upload))
    monkeypatch.setattr(service.settings, 'support_media_directory', str(tmp_path))
    # Use the real media store with an isolated directory.
    from app.support_media import SupportMediaStore
    store = SupportMediaStore()
    monkeypatch.setattr(service, 'support_media_store', store)
    async def request():
        common = dict(agent=AGENT, client_message_id='same-browser-key')
        if kind == 'text': return await support_service.send_agent_message(42, body='test', **common)
        if kind == 'location': return await support_service.send_agent_location(42, latitude=1, longitude=2, name=None, address=None, **common)
        return await support_service.send_agent_media(42, media_type='image', content=b'image', content_type='image/png', filename='test.png', caption=None, **common)
    task = asyncio.create_task(request())
    await asyncio.wait_for(entered.wait(), 5)
    duplicate = await request()
    assert duplicate['send_status'] == 'DISPATCHING'
    assert len(calls) == 1
    release.set()
    result = await task
    assert (await request())['id'] == result['id']
    assert len(calls) == 1
    assert calls[0]['client_message_id'] != 'same-browser-key'
    with pytest.raises(SupportForbiddenError):
        await support_service.send_agent_message(43, agent=AGENT, body='test', client_message_id='same-browser-key')
    # Same browser ID belongs independently to another sender/ticket.
    other, created = await repo.add_ticket_message(ticket_id=43, sender_type='AGENT', sender_id='8', direction='OUTBOUND', body='private', client_message_id='same-browser-key')
    assert created and other['id'] != result['id']
    assert other['transport_client_id'] != calls[0]['client_message_id']


@pytest.mark.asyncio
async def test_insert_race_returns_one_row(infrastructure):
    repo, _, _ = infrastructure
    gate = Barrier(2)
    async def insert():
        await gate.wait()
        return await create(repo)
    results = await asyncio.gather(insert(), insert())
    assert sum(created for _, created in results) == 1
    assert results[0][0]['id'] == results[1][0]['id']


@pytest.mark.asyncio
async def test_crash_fencing_and_uncertain_recovery(infrastructure):
    repo, sql, _ = infrastructure
    row, _ = await create(repo)
    first = await repo.claim_delivery(row['id'])
    assert first and await repo.claim_delivery(row['id']) is None
    await sql("UPDATE support_ticket_messages SET delivery_claimed_at=UTC_TIMESTAMP(6)-INTERVAL 6 MINUTE WHERE id=%s", (row['id'],))
    second = await repo.claim_delivery(row['id'])
    assert second and second != first
    assert not await repo.begin_delivery(row['id'], first)
    assert await repo.begin_delivery(row['id'], second)
    await sql("UPDATE support_ticket_messages SET delivery_claimed_at=UTC_TIMESTAMP(6)-INTERVAL 6 MINUTE WHERE id=%s", (row['id'],))
    assert await repo.claim_delivery(row['id'], retry=True) is None
    assert (await repo.get_message(row['id']))['send_status'] == 'UNCERTAIN'
    # A delayed authenticated bridge confirmation resolves the uncertain row.
    confirmed, _ = await repo.confirm_linked_device_outgoing(client_message_id=row['transport_client_id'],
        whatsapp_message_id='ld:90005:late', phone_number='155500042', business_phone_number_id='90005', whatsapp_timestamp='1')
    assert confirmed['send_status'] == 'SENT'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,expected', [('rejected','FAILED'), ('timeout','UNCERTAIN'), ('unknown','UNCERTAIN'), ('invalid','UNCERTAIN')])
async def test_failures_and_retry(infrastructure, failure, expected):
    repo, _, _ = infrastructure
    row, _ = await create(repo)
    ticket = await repo.get_ticket(42)
    calls = []
    async def send(**kwargs):
        calls.append(kwargs)
        if failure == 'invalid': return {}
        if failure == 'unknown': raise RuntimeError('unknown')
        raise WhatsAppSendError('failure', delivery_uncertain=failure != 'rejected')
    transport = SimpleNamespace(send_text=send)
    result = await deliver(repo, transport, None, ticket, row)
    assert result['send_status'] == expected
    await asyncio.gather(deliver(repo, transport, None, ticket, result, retry=True), deliver(repo, transport, None, ticket, result, retry=True))
    # A definitive rejection can be retried; uncertainty cannot.
    assert len(calls) == (2 if expected == 'FAILED' else 1)


@pytest.mark.asyncio
async def test_crash_after_acceptance_before_database_update(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    row, _ = await create(repo)
    async def send(**kwargs): return {'messages':[{'id':'accepted'}]}
    async def broken(*args, **kwargs): raise RuntimeError('database unavailable')
    monkeypatch.setattr(repo, 'finish_delivery', broken)
    with pytest.raises(RuntimeError):
        await deliver(repo, SimpleNamespace(send_text=send), None, await repo.get_ticket(42), row)
    assert (await repo.get_message(row['id']))['send_status'] == 'DISPATCHING'
    await sql("UPDATE support_ticket_messages SET delivery_claimed_at=UTC_TIMESTAMP(6)-INTERVAL 6 MINUTE")
    assert await repo.claim_delivery(row['id'], retry=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['cloud', 'linked'])
@pytest.mark.parametrize('outcome,expected', [('success','SENT'), ('400','FAILED'), ('500','UNCERTAIN'), ('timeout','UNCERTAIN'), ('invalid','UNCERTAIN')])
async def test_actual_transports_do_not_retry_uncertain_http(infrastructure, monkeypatch, provider, outcome, expected):
    import httpx
    from app.config import settings
    from app.whatsapp_client import WhatsAppClient
    from app.linked_device_client import LinkedDeviceWhatsAppClient
    repo, _, _ = infrastructure
    monkeypatch.setattr(settings, 'whatsapp_linked_device_secret', 'x' * 40)
    monkeypatch.setattr(settings, 'whatsapp_linked_device_url', 'http://127.0.0.1:3010')
    monkeypatch.setattr(settings, 'whatsapp_linked_device_lines', {'90005':''})
    calls = []
    def handler(request):
        calls.append(request)
        if outcome == 'timeout': raise httpx.ReadTimeout('lost response', request=request)
        if outcome == 'invalid': return httpx.Response(200, json={})
        if outcome in {'400','500'}: return httpx.Response(int(outcome), json={'error':'rejected'})
        return httpx.Response(200, json={'messages':[{'id':'ld:90005:accepted' if provider == 'linked' else 'wamid.accepted'}]})
    actual = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: actual(**kwargs, transport=httpx.MockTransport(handler)))
    row, _ = await create(repo)
    transport = WhatsAppClient() if provider == 'cloud' else LinkedDeviceWhatsAppClient()
    result = await deliver(repo, transport, None, await repo.get_ticket(42), row)
    assert result['send_status'] == expected
    if expected == 'UNCERTAIN':
        await deliver(repo, transport, None, await repo.get_ticket(42), row, retry=True)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_two_service_requests_both_miss_lookup_but_dispatch_once(infrastructure, monkeypatch):
    repo, _, _ = infrastructure
    gate = Barrier(2)
    lookup = repo.get_message_by_client_id
    count = 0
    async def racing_lookup(*args, **kwargs):
        nonlocal count
        count += 1
        if count <= 2:
            await gate.wait()
            return None
        return await lookup(*args, **kwargs)
    monkeypatch.setattr(repo, 'get_message_by_client_id', racing_lookup)
    calls = []
    async def send(**kwargs):
        calls.append(kwargs)
        return {'messages':[{'id':'ld:90005:race'}]}
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send))
    results = await asyncio.gather(*[support_service.send_agent_message(42, agent=AGENT, body='test', client_message_id='race') for _ in range(2)])
    assert results[0]['id'] == results[1]['id']
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_delivery_migration_is_repeatable_and_quarantines_legacy(infrastructure):
    from pathlib import Path
    repo, sql, _ = infrastructure
    row, _ = await create(repo)
    await sql("UPDATE support_ticket_messages SET transport_client_id=NULL,send_status='FAILED' WHERE id=%s", (row['id'],))
    async with repo._pool.acquire() as conn:
        async with conn.cursor() as cur:
            for _ in range(2):
                await cur.execute(Path('migrations/20261021_outbound_delivery_ownership.sql').read_text())
                while await cur.nextset(): pass
    recovered = await repo.get_message(row['id'])
    assert recovered['send_status'] == 'UNCERTAIN'
    assert recovered['transport_client_id'] == 'request'
    assert await repo.claim_delivery(row['id'], retry=True) is None


@pytest.mark.asyncio
async def test_transfer_before_dispatch_prevents_old_agent_send(infrastructure):
    repo, sql, _ = infrastructure
    row, _ = await create(repo)
    token = await repo.claim_delivery(row['id'])
    await sql("UPDATE support_tickets SET current_assignee_id='8' WHERE id=42")
    assert not await repo.begin_delivery(row['id'], token)
    with pytest.raises(SupportForbiddenError):
        await support_service.send_agent_message(42, agent=AGENT, body='test', client_message_id='request')
