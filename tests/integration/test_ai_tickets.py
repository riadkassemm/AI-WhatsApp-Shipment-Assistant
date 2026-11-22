"""AI tickets (Step 3a) with real MariaDB transactions and no provider calls."""
import asyncio
from types import SimpleNamespace

import pytest

from app.conversation_store import Conversation
from app.support_auth import SupportAgent
from app.support_models import HandoffRequest
from app.support_repository import SupportConflictError


AGENT = SupportAgent(id='7', name='Agent', role='agent')
SUPERVISOR = SupportAgent(id='8', name='Other', role='supervisor')
PHONE = '15550009001'
LINE = '90005'


class _ConversationCache:
    def __init__(self):
        self.saved = []

    async def get_or_create(self, phone, **kwargs):
        return Conversation(phone_number=phone, conversation_key=kwargs.get('conversation_key'),
                            business_phone_number_id=kwargs.get('business_phone_number_id'))

    async def save(self, conversation):
        self.saved.append((conversation.mode, conversation.active_ticket_id))


@pytest.fixture
def service_env(infrastructure, monkeypatch):
    import app.support_service as service
    repo, sql, _client = infrastructure
    sent = []

    async def send_text(**kwargs):
        sent.append(kwargs)
        return {'messages': [{'id': f'wamid.ai.{len(sent)}'}]}

    async def no_identity(_phone):
        return None

    async def no_notify(**_kwargs):
        return None

    cache = _ConversationCache()
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send_text))
    monkeypatch.setattr(service, 'conversation_store', cache)
    monkeypatch.setattr(service, 'customer_directory', SimpleNamespace(resolve=no_identity))
    monkeypatch.setattr(service, 'notify_human_agents', no_notify)
    return SimpleNamespace(repo=repo, sql=sql, sent=sent, cache=cache, service=service.support_service)


def _conversation():
    return Conversation(phone_number=PHONE, conversation_key=f'{LINE}:{PHONE}', business_phone_number_id=LINE)


async def _open_ai_ticket(env, source='wamid.in.1'):
    ticket, created = await env.service.open_ai_ticket(
        conversation=_conversation(), source_whatsapp_message_id=source, business_phone_number_id=LINE,
    )
    assert created and ticket['handling_mode'] == 'AI'
    return ticket


async def _ids(repo, view, agent=AGENT):
    data = await repo.list_tickets(view=view, agent_id=agent.id, can_view_all=agent.is_supervisor)
    return [int(row['id']) for row in data['items']]


@pytest.mark.asyncio
async def test_ai_ticket_is_listed_only_in_the_ai_queue(service_env):
    env = service_env
    ticket = await _open_ai_ticket(env)
    # Opening again for the same customer and line returns the same ticket.
    again, created = await env.service.open_ai_ticket(
        conversation=_conversation(), source_whatsapp_message_id='wamid.in.2', business_phone_number_id=LINE,
    )
    assert not created and again['id'] == ticket['id']
    assert ticket['id'] in await _ids(env.repo, 'ai')
    assert ticket['id'] in await _ids(env.repo, 'ai', SUPERVISOR)
    for view in ('unassigned', 'mine', 'watching', 'waiting'):
        assert ticket['id'] not in await _ids(env.repo, view)
    assert ticket['id'] not in await _ids(env.repo, 'active', SUPERVISOR)
    counts = await env.service.queue_counts(AGENT)
    assert counts['ai'] == 1
    events = await env.sql("SELECT event_type, details_json FROM support_ticket_events WHERE ticket_id=%s", (ticket['id'],))
    assert events[0]['event_type'] == 'TICKET_CREATED' and '"handling_mode": "AI"' in events[0]['details_json']


@pytest.mark.asyncio
async def test_ai_reply_is_stored_sent_once_and_starts_the_quiet_clock(service_env):
    env = service_env
    ticket = await _open_ai_ticket(env)
    message = await env.service.send_ai_reply(ticket, body='Hello from the assistant', source_message_id='wamid.in.1',
                                              awaiting_customer=True)
    assert message['sender_type'] == 'AI' and message['send_status'] == 'SENT'
    assert message['sender_name'] == 'AI assistant' and message['whatsapp_message_id'] == 'wamid.ai.1'
    assert len(env.sent) == 1 and env.sent[0]['to'] == PHONE and env.sent[0]['phone_number_id'] == LINE
    stored = await env.repo.get_ticket(ticket['id'])
    assert stored['ai_last_reply_at'] is not None and stored['ai_awaiting_customer'] == 1
    # The same inbound message never produces a second reply.
    repeat = await env.service.send_ai_reply(ticket, body='Different text', source_message_id='wamid.in.1')
    assert repeat['id'] == message['id'] and len(env.sent) == 1


@pytest.mark.asyncio
async def test_take_over_assigns_the_ticket_and_fences_ai_replies(service_env):
    env = service_env
    ticket = await _open_ai_ticket(env)
    with pytest.raises(SupportConflictError, match='Take over'):
        await env.service.claim(ticket['id'], AGENT)
    with pytest.raises(SupportConflictError, match='Take it over'):
        await env.repo.add_ticket_message(ticket_id=ticket['id'], sender_type='AGENT', sender_id='8',
                                          direction='OUTBOUND', body='hi', client_message_id='early', send_status='PENDING')
    for attempt in (
        env.repo.resolve_ticket(ticket['id'], agent_id='8', agent_name='Other'),
        env.repo.update_status(ticket['id'], status='ASSIGNED', agent_id='8', agent_name='Other'),
    ):
        with pytest.raises(SupportConflictError, match='AI assistant is handling'):
            await attempt

    result = await env.service.take_over(ticket['id'], AGENT)
    assert result['changed'] is True
    taken = result['ticket']
    assert taken['handling_mode'] == 'HUMAN' and taken['status'] == 'ASSIGNED'
    assert taken['current_assignee_id'] == '7'
    events = [row['event_type'] for row in await env.sql(
        "SELECT event_type FROM support_ticket_events WHERE ticket_id=%s ORDER BY id", (ticket['id'],))]
    assert events[-2:] == ['AI_TAKEN_OVER', 'TICKET_CLAIMED']
    assert env.cache.saved[-1] == ('human', str(ticket['id']))
    assert ticket['id'] in await _ids(env.repo, 'mine') and ticket['id'] not in await _ids(env.repo, 'ai')
    # A retried take-over by the same agent is harmless; another agent cannot take it.
    assert (await env.service.take_over(ticket['id'], AGENT))['changed'] is False
    with pytest.raises(SupportConflictError):
        await env.service.take_over(ticket['id'], SUPERVISOR)
    with pytest.raises(SupportConflictError, match='no longer handles'):
        await env.service.send_ai_reply(ticket, body='Too late', source_message_id='wamid.in.9')
    assert env.sent == []


@pytest.mark.asyncio
async def test_take_over_during_ai_delivery_preparation_wins(service_env, monkeypatch):
    env = service_env
    repo = env.repo
    ticket = await _open_ai_ticket(env)
    entered, release = asyncio.Event(), asyncio.Event()
    original = repo.claim_delivery

    async def paused(*args, **kwargs):
        token = await original(*args, **kwargs)
        assert token
        entered.set()
        await release.wait()
        return token

    monkeypatch.setattr(repo, 'claim_delivery', paused)
    task = asyncio.create_task(env.service.send_ai_reply(ticket, body='Racing reply', source_message_id='wamid.in.1'))
    await asyncio.wait_for(entered.wait(), 5)
    await env.service.take_over(ticket['id'], AGENT)
    release.set()
    with pytest.raises(SupportConflictError, match='no longer handles'):
        await asyncio.wait_for(task, 5)
    assert env.sent == []
    rows = await env.sql("SELECT send_status, error_message FROM support_ticket_messages WHERE ticket_id=%s AND sender_type='AI'",
                         (ticket['id'],))
    assert rows[0]['send_status'] == 'FAILED' and rows[0]['error_message'].startswith('Not sent: a staff member took over')


@pytest.mark.asyncio
async def test_escalation_moves_the_ticket_to_unassigned_once(service_env):
    env = service_env
    ticket = await _open_ai_ticket(env)
    escalated, changed = await env.service.escalate_ai_ticket(
        ticket, reason_code='identity verification required', summary='Customer asks for their wallet balance.',
    )
    assert changed and escalated['handling_mode'] == 'HUMAN' and escalated['status'] == 'NEW'
    assert escalated['ai_escalation_reason'] == 'IDENTITY_VERIFICATION_REQUIRED'
    assert escalated['ai_summary'] == 'Customer asks for their wallet balance.'
    assert escalated['ai_escalated_at'] is not None
    assert ticket['id'] in await _ids(env.repo, 'unassigned') and ticket['id'] not in await _ids(env.repo, 'ai')
    events = await env.sql("SELECT event_type, actor_type, actor_name FROM support_ticket_events WHERE ticket_id=%s ORDER BY id DESC LIMIT 1",
                           (ticket['id'],))
    assert events[0] == {'event_type': 'AI_ESCALATED', 'actor_type': 'AI', 'actor_name': 'AI assistant'}
    _same, again = await env.service.escalate_ai_ticket(escalated, reason_code='OTHER')
    assert again is False
    # It is now an ordinary unclaimed ticket.
    claimed = await env.service.claim(ticket['id'], AGENT)
    assert claimed['ticket']['current_assignee_id'] == '7'


@pytest.mark.asyncio
async def test_handoff_escalates_the_existing_ai_ticket(service_env):
    env = service_env
    ticket = await _open_ai_ticket(env)
    handed, created = await env.service.finalize_handoff(
        conversation=_conversation(),
        handoff=HandoffRequest(reason='MEDIA_UNSUPPORTED_BY_AI', summary='Customer sent an image.'),
        source_whatsapp_message_id='wamid.in.5',
        source_message_text='[image]',
        business_phone_number_id=LINE,
    )
    assert handed['id'] == ticket['id'] and created is True
    assert handed['handling_mode'] == 'HUMAN' and handed['ai_escalation_reason'] == 'MEDIA_UNSUPPORTED_BY_AI'
    assert (await env.sql("SELECT COUNT(*) AS n FROM support_tickets WHERE whatsapp_phone=%s", (PHONE,)))[0]['n'] == 1
    # A retried handoff for the same message is idempotent.
    _again, created_again = await env.service.finalize_handoff(
        conversation=_conversation(), handoff=HandoffRequest(reason='MEDIA_UNSUPPORTED_BY_AI'),
        source_whatsapp_message_id='wamid.in.5', source_message_text='[image]', business_phone_number_id=LINE,
    )
    assert created_again is False


async def _age_ai_ticket(sql, ticket_id, minutes):
    await sql("UPDATE support_tickets SET ai_last_reply_at=UTC_TIMESTAMP(6)-INTERVAL %s MINUTE WHERE id=%s", (minutes, ticket_id))
    await sql("UPDATE support_ticket_messages SET created_at=UTC_TIMESTAMP(6)-INTERVAL %s MINUTE WHERE ticket_id=%s",
              (minutes + 1, ticket_id))


@pytest.mark.asyncio
async def test_quiet_ai_ticket_is_resolved_by_the_ai(service_env, monkeypatch):
    env = service_env
    from app.config import settings
    monkeypatch.setattr(settings, 'support_ai_ticket_quiet_resolve_minutes', 120)
    ticket = await _open_ai_ticket(env)
    await env.repo.add_ticket_message(ticket_id=ticket['id'], sender_type='CUSTOMER', direction='INBOUND', body='hi',
                                      whatsapp_message_id='wamid.in.1', send_status='RECEIVED', require_active=True)
    await env.service.send_ai_reply(ticket, body='Your parcel is on its way.', source_message_id='wamid.in.1')

    # Not quiet long enough yet.
    assert await env.service.auto_resolve_idle_ai_tickets() == 0
    await _age_ai_ticket(env.sql, ticket['id'], 121)
    # The AI asked a question: it waits for the answer.
    await env.sql("UPDATE support_tickets SET ai_awaiting_customer=1 WHERE id=%s", (ticket['id'],))
    assert await env.service.auto_resolve_idle_ai_tickets() == 0
    await env.sql("UPDATE support_tickets SET ai_awaiting_customer=0 WHERE id=%s", (ticket['id'],))

    assert await env.service.auto_resolve_idle_ai_tickets() == 1
    resolved = await env.repo.get_ticket(ticket['id'])
    assert resolved['status'] == 'RESOLVED' and resolved['active_key'] is None
    assert resolved['resolved_by_staff_id'] is None
    event = (await env.sql("SELECT event_type, actor_type FROM support_ticket_events WHERE ticket_id=%s ORDER BY id DESC LIMIT 1",
                           (ticket['id'],)))[0]
    assert event == {'event_type': 'TICKET_RESOLVED', 'actor_type': 'AI'}
    assert env.cache.saved[-1] == ('ai', None)
    assert ticket['id'] in await _ids(env.repo, 'history')
    # AI resolutions never count as agent work.
    agent_events = await env.sql("SELECT COUNT(*) AS n FROM support_ticket_events WHERE ticket_id=%s AND actor_type='AGENT'",
                                 (ticket['id'],))
    assert agent_events[0]['n'] == 0

    # A Super Admin can reopen it; it comes back as a human ticket.
    reopened = await env.service.reopen_ticket(ticket['id'], agent=SUPERVISOR, resolved_at=resolved['resolved_at'])
    assert reopened['ticket']['handling_mode'] == 'HUMAN'


@pytest.mark.asyncio
async def test_customer_message_after_the_ai_reply_keeps_the_ticket_open(service_env, monkeypatch):
    env = service_env
    from app.config import settings
    monkeypatch.setattr(settings, 'support_ai_ticket_quiet_resolve_minutes', 120)
    ticket = await _open_ai_ticket(env)
    await env.service.send_ai_reply(ticket, body='Anything else?', source_message_id='wamid.in.1')
    await env.repo.add_ticket_message(ticket_id=ticket['id'], sender_type='CUSTOMER', direction='INBOUND', body='yes',
                                      whatsapp_message_id='wamid.in.2', send_status='RECEIVED', require_active=True)
    await env.sql("UPDATE support_tickets SET ai_last_reply_at=UTC_TIMESTAMP(6)-INTERVAL 200 MINUTE WHERE id=%s", (ticket['id'],))
    await env.sql("""UPDATE support_ticket_messages SET created_at=UTC_TIMESTAMP(6)-INTERVAL 199 MINUTE
                     WHERE ticket_id=%s AND direction='INBOUND'""", (ticket['id'],))
    await env.sql("""UPDATE support_ticket_messages SET created_at=UTC_TIMESTAMP(6)-INTERVAL 201 MINUTE
                     WHERE ticket_id=%s AND direction='OUTBOUND'""", (ticket['id'],))
    # The customer wrote after the AI's last reply and got no answer: never auto-resolved.
    assert await env.service.auto_resolve_idle_ai_tickets() == 0
    assert (await env.repo.get_ticket(ticket['id']))['active_key'] is not None


@pytest.mark.asyncio
async def test_phone_app_idle_rule_ignores_ai_tickets(service_env, monkeypatch):
    env = service_env
    from app.config import settings
    monkeypatch.setattr(settings, 'support_phone_app_idle_resolve_minutes', 120)
    ticket = await _open_ai_ticket(env)
    await env.repo.add_ticket_message(ticket_id=ticket['id'], sender_type='AGENT', direction='OUTBOUND', body='from phone',
                                      sender_name='WhatsApp app', whatsapp_message_id='wamid.phone.1', send_status='SENT')
    await env.sql("UPDATE support_tickets SET created_at=UTC_TIMESTAMP(6)-INTERVAL 300 MINUTE WHERE id=%s", (ticket['id'],))
    await env.sql("UPDATE support_ticket_messages SET created_at=UTC_TIMESTAMP(6)-INTERVAL 300 MINUTE WHERE ticket_id=%s",
                  (ticket['id'],))
    assert await env.repo.list_idle_phone_app_tickets(idle_minutes=120) == []


@pytest.mark.asyncio
async def test_reply_from_the_whatsapp_app_stops_the_ai(service_env, monkeypatch):
    import app.support_service as service
    from datetime import datetime, timezone
    from app.whatsapp_client import IncomingWhatsAppMessage
    env = service_env
    monkeypatch.setattr(service, 'get_transport', lambda *_: 'linked_device')
    ticket = await _open_ai_ticket(env)
    phone_reply = IncomingWhatsAppMessage(
        from_number=PHONE, message_id=f'ld:{LINE}:PHONEREPLY', message_type='text', text='I will check this for you',
        timestamp=str(int(datetime.now(timezone.utc).timestamp())),
        business_phone_number_id=LINE, business_display_phone_number='155500000',
    )
    assert await env.service.sync_linked_device_outbound(phone_reply)
    stored = await env.repo.get_ticket(ticket['id'])
    assert stored['handling_mode'] == 'HUMAN'
    assert stored['ai_escalation_reason'] == 'STAFF_REPLIED_IN_WHATSAPP_APP'
    assert env.cache.saved[-1] == ('human', str(ticket['id']))
    # It is now an ordinary ticket answered in the WhatsApp app (Active, not Unassigned).
    assert ticket['id'] in await _ids(env.repo, 'active', SUPERVISOR)
    assert ticket['id'] not in await _ids(env.repo, 'unassigned')
    assert env.sent == []


@pytest.mark.asyncio
async def test_usage_report_counts_ai_ticket_outcomes_and_ticket_cost(service_env, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.ai_usage import AIUsageRepository
    from app.config import settings
    env = service_env
    monkeypatch.setattr(settings, 'support_ai_ticket_quiet_resolve_minutes', 120)
    storage = AIUsageRepository()
    storage._pool = env.repo._pool
    resolved = await _open_ai_ticket(env, source='wamid.a')
    await env.service.send_ai_reply(resolved, body='Done.', source_message_id='wamid.a')
    await _age_ai_ticket(env.sql, resolved['id'], 121)
    assert await env.service.auto_resolve_idle_ai_tickets() == 1
    # A second customer on the same line: escalated. A third: still with the AI.
    for phone, source in (('15550009002', 'wamid.b'), ('15550009003', 'wamid.c')):
        conversation = Conversation(phone_number=phone, conversation_key=f'{LINE}:{phone}', business_phone_number_id=LINE)
        ticket, _ = await env.service.open_ai_ticket(conversation=conversation, source_whatsapp_message_id=source,
                                                     business_phone_number_id=LINE)
        if source == 'wamid.b':
            await env.service.escalate_ai_ticket(ticket, reason_code='CHANGE_REQUIRES_HUMAN')
    now = datetime.now(timezone.utc)
    outcomes = await storage.ai_ticket_outcomes(now - timedelta(days=1), now + timedelta(days=1), LINE)
    assert {key: int(value) for key, value in outcomes.items()} == {
        'tickets': 3, 'resolved_by_ai': 1, 'handed_to_staff': 1, 'still_open': 1,
    }
    # Human tickets and other lines are not AI tickets.
    assert int((await storage.ai_ticket_outcomes(now - timedelta(days=1), now + timedelta(days=1), 'other'))['tickets']) == 0
    assert int((await storage.ai_ticket_outcomes(now + timedelta(days=1), now + timedelta(days=2), None))['tickets']) == 0

    await env.sql("""INSERT INTO ai_usage_events (reservation_id, purpose, tier, requested_model, estimate_micros, cost_micros,
                       cost_basis, outcome, phone_number_id, ticket_id, budget_day, budget_month, created_at)
                     VALUES ('r1','turn','primary','gpt-example',5000,4200,'actual','success',%s,%s,'2026-11-22','2026-11',
                             UTC_TIMESTAMP(6))""", (LINE, resolved['id']))
    usage = await storage.ticket_usage(resolved['id'])
    assert int(usage['requests']) == 1 and int(usage['cost_micros']) == 4200 and int(usage['pending']) == 0
    indexes = await env.sql("SHOW INDEX FROM ai_usage_events WHERE Key_name='idx_ai_usage_ticket'")
    assert indexes


@pytest.mark.asyncio
async def test_ticket_detail_offers_take_over_and_admin_cost(service_env, monkeypatch):
    import app.support_service as service
    env = service_env
    monkeypatch.setattr(service.ai_usage_service.repository, '_pool', env.repo._pool)
    ticket = await _open_ai_ticket(env)
    agent_view = await env.service.ticket_detail(ticket['id'], AGENT)
    assert agent_view['permissions']['can_take_over'] is True
    assert agent_view['permissions']['can_claim'] is False and agent_view['permissions']['can_reply'] is False
    assert agent_view['ai_usage'] is None
    admin_view = await env.service.ticket_detail(ticket['id'], SUPERVISOR)
    assert admin_view['ai_usage'] == {'estimated': True, 'requests': 0, 'pending': 0, 'cost_usd': 0.0}
    assert admin_view['ticket']['handling_mode'] == 'AI'
    await env.service.take_over(ticket['id'], AGENT)
    taken = await env.service.ticket_detail(ticket['id'], AGENT)
    assert taken['permissions']['can_take_over'] is False and taken['permissions']['can_reply'] is True
