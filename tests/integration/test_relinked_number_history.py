"""Re-linking a removed WhatsApp number restores its history (real MariaDB/Redis)."""
import json

import pytest

from app.conversation_store import RedisConversationStore
from app.support_auth import SupportAgent
from app.support_timing import responsibility_periods
from app.whatsapp_registry import WhatsAppRegistryRepository, WhatsAppRegistryService

OLD, NEW, PHONE = '90020', '90026', '96176063972'
MANAGER = SupportAgent(id='8', name='Manager', role='supervisor')


async def events(sql, ticket_id):
    return await sql('SELECT * FROM support_ticket_events WHERE ticket_id=%s ORDER BY id', (ticket_id,))


async def removed_and_relinked(infrastructure, monkeypatch):
    """Line OLD holds live, resolved and older-archived work; it is removed and PHONE re-pairs as NEW."""
    repo, sql, client = infrastructure
    import app.whatsapp_registry as module
    store = object.__new__(RedisConversationStore)
    store._client, store._ttl_seconds = client, 300
    monkeypatch.setattr('app.conversation_store.conversation_store', store)
    monkeypatch.setattr('app.support_service.conversation_store', store)
    await sql("""INSERT INTO whatsapp_phone_numbers (phone_number_id,display_phone_number,display_name,source,is_enabled,provider_present)
        VALUES (%s,%s,'Saida Office','LINKED_DEVICE',0,1)""", (OLD, PHONE))
    # Seeded 41 is unassigned NEW, 42 belongs to staff 7, 43 to staff 8.
    await sql("""UPDATE support_tickets SET business_phone_number_id=%s, business_display_phone_number=%s,
        active_key=CONCAT(%s, ':', whatsapp_phone) WHERE id IN (41,42,43)""", (OLD, PHONE, OLD))
    await sql("UPDATE support_tickets SET status='IN_PROGRESS', assigned_at='2026-10-30 08:00:00' WHERE id=42")
    await sql("UPDATE support_tickets SET conversation_origin='STAFF', initiated_by_staff_id=8 WHERE id=43")
    await sql("""INSERT INTO support_ticket_events (ticket_id,event_type,actor_type,actor_id,actor_name,details_json,created_at)
        VALUES (42,'TICKET_CLAIMED','AGENT','7','Agent','{}','2026-10-30 08:00:00'),
               (42,'STATUS_CHANGED','AGENT','7','Agent','{"to":"IN_PROGRESS"}','2026-10-30 08:01:00')""")
    # 44: resolved history. 45: archived by a removal that predates previous_status.
    for ticket_id, status, closed in [(44, 'RESOLVED', None), (45, 'CLOSED', '2026-10-23 12:41:30')]:
        await sql("""INSERT INTO support_tickets (id,ticket_reference,whatsapp_phone,reason,ai_summary,status,current_assignee_id,
            current_assignee_name,resolved_by_staff_id,business_phone_number_id,business_display_phone_number,closed_at,
            created_at,updated_at,last_activity_at)
            VALUES (%s,%s,%s,'test','test',%s,'7','Agent','7',%s,%s,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
            (ticket_id, f'TEST-{ticket_id}', f'1555000{ticket_id}', status, OLD, PHONE, closed))
    await sql("""INSERT INTO support_ticket_events (ticket_id,event_type,actor_type,actor_id,actor_name,details_json,created_at)
        VALUES (45,'TICKET_CLAIMED','AGENT','7','Agent','{}','2026-10-23 10:00:00'),
               (45,'STATUS_CHANGED','AGENT','7','Agent','{"to":"WAITING_CUSTOMER"}','2026-10-23 10:05:00'),
               (45,'TICKET_CLOSED','AGENT','8','Manager',%s,'2026-10-23 12:41:30')""",
        (json.dumps({'reason': 'whatsapp_line_removed', 'phone_number_id': OLD}),))
    await sql('UPDATE support_tickets SET resolved_by_staff_id=NULL WHERE id=42')

    storage = WhatsAppRegistryRepository()
    storage._pool = repo._pool
    service = WhatsAppRegistryService(storage)
    removed = await storage.finish_linked_device_removal(OLD, actor_id=8, actor_name='Manager', close_active_tickets=True)
    assert sorted(row['id'] for row in removed) == [41, 42, 43]
    closing = json.loads((await events(sql, 42))[-1]['details_json'])
    assert closing == {'reason': 'whatsapp_line_removed', 'phone_number_id': OLD,
                       'previous_status': 'IN_PROGRESS', 'previous_resolved_by_staff_id': None}
    assert (await repo.get_ticket(44))['business_number_removed_at'] is not None

    # The customer of 41 wrote again on the new line before it was enabled.
    await sql("""INSERT INTO support_tickets (id,ticket_reference,active_key,whatsapp_phone,reason,ai_summary,status,
        business_phone_number_id,created_at,updated_at,last_activity_at)
        VALUES (46,'TEST-46',%s,'155500041','test','test','NEW',%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
        (f'{NEW}:155500041', NEW))
    await sql("""INSERT INTO whatsapp_phone_numbers (phone_number_id,display_phone_number,display_name,source,is_enabled,provider_present)
        VALUES (%s,NULL,'Saida Office','LINKED_DEVICE',0,1)""", (NEW,))
    await sql("INSERT INTO whatsapp_integration_audit (action,actor_staff_id,phone_number_id,details_json) VALUES ('LINKED_DEVICE_LINE_CREATED',8,%s,'{}')", (NEW,))
    async def status():
        return {'lines': [{'line_id': NEW, 'status': 'connected', 'display_phone_number': PHONE}]}
    monkeypatch.setattr(module, 'get_transport', lambda *_: 'linked_device')
    monkeypatch.setattr(module, 'configured_linked_device_lines', lambda: {NEW: PHONE})
    monkeypatch.setattr(module.linked_device_client, 'get_status', status)
    return storage, service, store


@pytest.mark.asyncio
async def test_relinking_restores_archived_conversations_and_moves_history(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    storage, service, store = await removed_and_relinked(infrastructure, monkeypatch)

    before = {row['id']: row for row in await sql('SELECT * FROM support_tickets WHERE id IN (41,42,43,44,45)')}
    dry = await storage.adopt_retired_history(NEW, '+961 76 063 972', actor_id=8, actor_name='Manager', apply=False)
    assert dry['from_lines'] == [OLD] and dry['moved'] == 5
    assert {item['id']: item['status'] for item in dry['restored']} == {42: 'IN_PROGRESS', 43: 'ASSIGNED', 45: 'WAITING_CUSTOMER'}
    assert dry['conflicts'] == [{'id': 41, 'ticket_reference': 'TEST-41', 'active_ticket_reference': 'TEST-46'}]
    assert {row['id']: row for row in await sql('SELECT * FROM support_tickets WHERE id IN (41,42,43,44,45)')} == before

    # Enabling the paired line adopts the history automatically.
    await service.set_number_enabled(MANAGER, NEW, True)

    rows = {row['id']: row for row in await sql('SELECT * FROM support_tickets WHERE id IN (41,42,43,44,45)')}
    assert all(row['business_phone_number_id'] == NEW and row['business_number_retired_at'] is None for row in rows.values())
    assert rows[42]['status'] == 'IN_PROGRESS' and rows[42]['active_key'] == f'{NEW}:155500042'
    assert rows[42]['current_assignee_id'] == '7' and rows[42]['assigned_at'] == before[42]['assigned_at']
    assert rows[42]['closed_at'] is None and rows[42]['resolved_by_staff_id'] is None
    assert rows[43]['status'] == 'ASSIGNED' and rows[43]['current_assignee_id'] == '8'
    assert rows[45]['status'] == 'WAITING_CUSTOMER' and rows[45]['active_key'] == f'{NEW}:155500045'
    assert rows[41]['status'] == 'CLOSED' and rows[41]['active_key'] is None  # 46 stays the live conversation
    assert rows[44]['status'] == 'RESOLVED' and (await repo.get_ticket(44))['business_number_removed_at'] is None

    restored = (await events(sql, 42))[-1]
    assert restored['event_type'] == 'TICKET_RESTORED' and restored['actor_id'] == '8'
    assert json.loads(restored['details_json']) == {'reason': 'whatsapp_line_relinked', 'from_phone_number_id': OLD,
        'phone_number_id': NEW, 'restored_status': 'IN_PROGRESS', 'current_assignee_id': '7'}
    assert (await events(sql, 43))[-1]['event_type'] == 'CHAT_RESTORED'
    audit = await sql("SELECT details_json FROM whatsapp_integration_audit WHERE action='LINKED_DEVICE_HISTORY_RESTORED'")
    assert json.loads(audit[0]['details_json']) == {'from_phone_number_ids': [OLD], 'archived_only': False, 'moved_conversations': 5,
        'restored_conversations': 3, 'conflicting_conversations': 1}

    # Assignment history is continuous: the archive never ended staff 7's period.
    from datetime import datetime
    timing = responsibility_periods(await events(sql, 42), datetime.utcnow())
    assert [(p['agent_id'], p['end_reason']) for p in timing['periods']] == [('7', 'ongoing')] and not timing['issues']

    conversation = await store.get_or_create('155500042', conversation_key=f'{NEW}:155500042',
                                                          business_phone_number_id=NEW)
    assert conversation.mode == 'human' and str(conversation.active_ticket_id) == '42'

    # Repeating the restore finds nothing left to adopt.
    again = await service.restore_relinked_history(MANAGER, NEW, PHONE)
    assert again == {'from_lines': [], 'moved': 0, 'restored': [], 'conflicts': []}


@pytest.mark.asyncio
async def test_archived_only_restore_leaves_history_and_is_never_repeated(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    storage, service, store = await removed_and_relinked(infrastructure, monkeypatch)
    await sql('UPDATE whatsapp_phone_numbers SET is_enabled=1, display_phone_number=%s WHERE phone_number_id=%s', (PHONE, NEW))
    summary = await service.restore_relinked_history(MANAGER, NEW, PHONE, archived_only=True)
    assert summary['moved'] == 3 and sorted(item['id'] for item in summary['restored']) == [42, 43, 45]
    rows = {row['id']: row for row in await sql('SELECT * FROM support_tickets WHERE id IN (41,42,43,44,45)')}
    for ticket_id in (42, 43, 45):
        assert rows[ticket_id]['business_phone_number_id'] == NEW and rows[ticket_id]['business_number_retired_at'] is None
    assert rows[42]['status'] == 'IN_PROGRESS' and rows[42]['active_key'] == f'{NEW}:155500042'
    # Resolved history and the conflicting archive stay read-only on the removed line.
    for ticket_id in (41, 44):
        assert rows[ticket_id]['business_phone_number_id'] == OLD and rows[ticket_id]['business_number_retired_at'] is not None
    assert (await repo.get_ticket(44))['business_number_removed_at'] is not None
    # Neither a repeat nor re-enabling the line ever moves what stayed behind.
    assert (await service.restore_relinked_history(MANAGER, NEW, PHONE))['from_lines'] == []
    await sql('UPDATE whatsapp_phone_numbers SET is_enabled=0 WHERE phone_number_id=%s', (NEW,))
    await service.set_number_enabled(MANAGER, NEW, True)
    assert (await sql('SELECT business_phone_number_id FROM support_tickets WHERE id=44'))[0]['business_phone_number_id'] == OLD
    audits = await sql("SELECT details_json FROM whatsapp_integration_audit WHERE action='LINKED_DEVICE_HISTORY_RESTORED'")
    assert [json.loads(row['details_json'])['archived_only'] for row in audits] == [True]
