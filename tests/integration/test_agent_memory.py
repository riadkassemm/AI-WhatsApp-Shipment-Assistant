import pytest

from app.agent import memory


@pytest.mark.asyncio
async def test_previous_lookup_requires_same_customer_line_and_closed_ticket(infrastructure):
    repo, sql, _ = infrastructure
    await sql("UPDATE support_tickets SET customer_id='replica:9', whatsapp_phone='155500041', status='CLOSED', active_key=NULL WHERE id IN (41,42,43)")
    current = {'id': 99, 'whatsapp_phone': '155500041', 'business_phone_number_id': '90005'}
    await sql("UPDATE support_tickets SET business_phone_number_id='other' WHERE id=43")
    await sql("UPDATE support_tickets SET customer_id='replica:10' WHERE id=42")
    earlier = await repo.ai_previous_ticket(current=current, customer_id='replica:9', reference='latest')
    assert earlier['id'] == 41
    assert await repo.ai_previous_ticket(current=current, customer_id='replica:9', reference='TEST-42') is None
    assert await repo.ai_previous_ticket(current=current, customer_id='9', reference='latest') is None
    assert await repo.ai_previous_ticket(current={**current, 'business_phone_number_id': None}, customer_id='replica:9', reference='latest') is None
    await sql("UPDATE support_tickets SET status='NEW', active_key='90005:155500041' WHERE id=41")
    assert await repo.ai_previous_ticket(current=current, customer_id='replica:9', reference='latest') is None


@pytest.mark.asyncio
async def test_memory_persists_and_invalidates_without_internal_or_future_messages(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    for index in range(1, 26):
        await repo.add_ticket_message(ticket_id=41, sender_type='CUSTOMER', direction='INBOUND',
                                     body=f'Customer request {index}', whatsapp_message_id=f'memory.{index}', message_type='text')
    await repo.add_ticket_message(ticket_id=41, sender_type='SYSTEM', direction='INTERNAL', body='Private staff note')
    calls = []
    async def summarize(previous, rows, style):
        calls.append((previous, rows))
        return 'The customer asked about a parcel.'
    monkeypatch.setattr(memory, 'summarize', summarize)
    first = await memory.ticket_summary(repo, 41, source_message_id='memory.25', style='en')
    assert first and len(calls) == 1 and len(calls[0][1]) == 13
    assert all(row['sender_type'] == 'CUSTOMER' for row in calls[0][1])
    assert await memory.ticket_summary(repo, 41, source_message_id='memory.25', style='en') == first
    assert len(calls) == 1
    await sql("UPDATE support_ticket_messages SET deleted_for_everyone_at=UTC_TIMESTAMP() WHERE whatsapp_message_id='memory.1'")
    await memory.ticket_summary(repo, 41, source_message_id='memory.25', style='en')
    assert len(calls) == 2 and calls[-1][0] == ''
    assert not any(row['whatsapp_message_id'] == 'memory.1' for row in calls[-1][1])
    await repo.add_ticket_message(ticket_id=41, sender_type='CUSTOMER', direction='INBOUND', body='Future queued request', whatsapp_message_id='memory.26')
    _, snapshot = await repo.ai_memory_snapshot(41, source_message_id='memory.25')
    assert not any(row['whatsapp_message_id'] == 'memory.26' for row in snapshot)
    # Migration is additive and repeatable against existing data.
    from pathlib import Path
    await sql(Path('migrations/20261123_ai_ticket_memory.sql').read_text())
    assert (await repo.ai_memory_snapshot(41, source_message_id=None))[0]['ai_memory_summary'] == first
