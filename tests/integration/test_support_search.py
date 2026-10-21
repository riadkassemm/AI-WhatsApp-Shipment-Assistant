"""Search real MariaDB data without contacting a WhatsApp provider."""
import pytest

from app.support_auth import SupportAgent
from app.support_service import support_service


@pytest.mark.asyncio
async def test_customer_search_visibility_formatting_and_empty_conversations(infrastructure):
    repo, sql, _ = infrastructure
    await sql("UPDATE support_tickets SET customer_name='Maya Customer'")
    await sql("UPDATE support_tickets SET whatsapp_phone='+961 (76) 123-456' WHERE id=42")

    async def search(query, **kwargs):
        return await repo.search_messages(query=query, agent_id='7', **kwargs)

    for query in ('maya', 'Customer', 'TEST-42', '96176123456', '+961 76 123 456', '123-456'):
        rows = await search(query)
        assert 42 in {row['ticket_id'] for row in rows}
        assert 43 not in {row['ticket_id'] for row in rows}
        assert all(row['result_type'] == 'conversation' and row['message_id'] is None for row in rows)
    assert {row['ticket_id'] for row in await search('Maya')} == {41, 42}
    assert {row['ticket_id'] for row in await search('Maya', can_view_all=True)} == {41, 42, 43}
    assert await search('Maya', ticket_id=42) == []  # In-conversation search remains message-only.
    assert len(await search('Maya', limit=1)) == 1
    await sql("UPDATE support_tickets SET status='RESOLVED', active_key=NULL WHERE id=43")
    assert {row['ticket_id'] for row in await search('Maya')} == {41, 42, 43}
    result = await support_service.search_messages(query='Maya', agent=SupportAgent(id='7', name='Agent', role='agent'))
    assert result['count'] == 3
    assert all(item['result_type'] == 'conversation' for item in result['items'])
    await sql('UPDATE support_tickets SET customer_name=%s WHERE id=42', ('Literal%_!Name',))
    assert [row['ticket_id'] for row in await search('%_!')] == [42]
    assert await search('%%') == []


@pytest.mark.asyncio
async def test_message_search_preserves_deletions_and_access(infrastructure):
    repo, sql, _ = infrastructure
    rows = []
    for ticket_id in (42, 42, 42, 43):
        row, _ = await repo.add_ticket_message(
            ticket_id=ticket_id, sender_type='CUSTOMER', direction='INBOUND', body='search needle',
        )
        rows.append(row)
    await sql('UPDATE support_ticket_messages SET deleted_for_everyone_at=UTC_TIMESTAMP(6) WHERE id=%s', (rows[1]['id'],))
    await sql('INSERT INTO support_message_user_deletions (message_id,user_id) VALUES (%s,%s)', (rows[2]['id'], '7'))
    result = await repo.search_messages(query='needle', agent_id='7')
    assert [item['message_id'] for item in result] == [rows[0]['id']]
    # Customer hits must not reveal a hidden/deleted message as their preview.
    await sql("UPDATE support_tickets SET customer_name='needle' WHERE id=42")
    result = await repo.search_messages(query='needle', agent_id='7')
    assert [item['result_type'] for item in result] == ['conversation', 'message']
    assert 'body' not in result[0]
    assert result[1]['message_id'] == rows[0]['id']
