"""Automated turns on AI tickets (Step 3b) against real MariaDB; no provider calls."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import main
import app.support_service as service
from app.ai_rate_limit import AIQuotaDecision
from app.conversation_store import InMemoryConversationStore
from app.support_auth import SupportAgent
from app.support_models import AIReplyResult, HandoffRequest
from app.whatsapp_client import IncomingWhatsAppMessage


AGENT = SupportAgent(id='7', name='Agent', role='agent')
LINE = '90005'
PHONE = '15550009101'


@pytest.fixture
def turns(infrastructure, monkeypatch):
    repo, sql, _redis = infrastructure
    store = InMemoryConversationStore()
    sent = []
    ai_calls = []
    replies = {'next': AIReplyResult(reply_text='Your parcel is in Beirut.')}

    async def send_text(**kwargs):
        sent.append(kwargs)
        return {'messages': [{'id': f'wamid.out.{len(sent)}'}]}

    async def availability(**_kwargs):
        return SimpleNamespace(effective_ai_enabled=True, effective_reason='MANUAL_ON',
                               configuration=SimpleNamespace(control_mode='manual'))

    async def allowed(_phone):
        return AIQuotaDecision(allowed=True, scope=None)

    async def no_quote(*_args, **_kwargs):
        return None

    async def fake_ai(conversation, user_message, **_kwargs):
        ai_calls.append(user_message)
        before = replies.get('before_reply')
        if before:
            await before()
        result = replies['next']
        conversation.messages.append({'role': 'assistant', 'content': result.reply_text})
        return result

    async def no_identity(_phone):
        return None

    async def no_notify(**_kwargs):
        return None

    monkeypatch.setattr(main, 'conversation_store', store)
    monkeypatch.setattr(service, 'conversation_store', store)
    monkeypatch.setattr(service, 'whatsapp_client', SimpleNamespace(send_text=send_text))
    monkeypatch.setattr(main, 'whatsapp_client', SimpleNamespace(send_text=send_text))
    monkeypatch.setattr(main.ai_availability_service, 'get_effective_ai_availability', availability)
    monkeypatch.setattr(main.ai_rate_limiter, 'consume', allowed)
    monkeypatch.setattr(main, 'maybe_handle_shipping_quote', no_quote)
    monkeypatch.setattr(main, 'get_ai_reply', fake_ai)
    monkeypatch.setattr(service, 'customer_directory', SimpleNamespace(resolve=no_identity))
    monkeypatch.setattr(service, 'notify_human_agents', no_notify)

    async def deliver(message_id, text='Where is my parcel?', **extra):
        incoming = IncomingWhatsAppMessage(
            PHONE, message_id, extra.pop('message_type', 'text'), text,
            str(int(datetime.now(timezone.utc).timestamp())), business_phone_number_id=LINE, **extra,
        )
        response = await main._process_incoming_message(incoming)
        return response.status_code, json.loads(response.body)

    async def messages(ticket_id):
        return list(await sql(
            "SELECT sender_type, direction, body, send_status, whatsapp_message_id FROM support_ticket_messages "
            "WHERE ticket_id=%s ORDER BY id", (ticket_id,)))

    async def only_ticket():
        rows = await sql("SELECT * FROM support_tickets WHERE whatsapp_phone=%s", (PHONE,))
        assert len(rows) == 1
        return rows[0]

    return SimpleNamespace(repo=repo, sql=sql, sent=sent, ai_calls=ai_calls, replies=replies,
                           deliver=deliver, messages=messages, only_ticket=only_ticket)


@pytest.mark.asyncio
async def test_an_automated_conversation_is_an_ai_ticket(turns):
    assert await turns.deliver('wamid.in.1') == (200, {'status': 'handled'})
    ticket = await turns.only_ticket()
    assert ticket['handling_mode'] == 'AI' and ticket['status'] == 'NEW' and ticket['current_assignee_id'] is None
    rows = await turns.messages(ticket['id'])
    assert [(r['sender_type'], r['direction']) for r in rows] == [('CUSTOMER', 'INBOUND'), ('AI', 'OUTBOUND')]
    assert rows[0]['whatsapp_message_id'] == 'wamid.in.1'
    assert rows[1]['body'] == 'Your parcel is in Beirut.' and rows[1]['send_status'] == 'SENT'
    assert len(turns.sent) == 1 and turns.sent[0]['to'] == PHONE and turns.sent[0]['phone_number_id'] == LINE
    assert ticket['ai_last_reply_at'] is not None

    # The next message continues the same ticket.
    turns.replies['next'] = AIReplyResult(reply_text='It arrives tomorrow.')
    assert (await turns.deliver('wamid.in.2', 'When?'))[1]['status'] == 'handled'
    ticket = await turns.only_ticket()
    assert [r['sender_type'] for r in await turns.messages(ticket['id'])] == ['CUSTOMER', 'AI', 'CUSTOMER', 'AI']
    assert len(turns.sent) == 2


@pytest.mark.asyncio
async def test_after_take_over_the_ai_stays_silent(turns):
    await turns.deliver('wamid.in.1')
    ticket = await turns.only_ticket()
    await service.support_service.take_over(ticket['id'], AGENT)
    status, body = await turns.deliver('wamid.in.2', 'Hello?')
    assert status == 200 and body['status'] == 'held_for_human'
    assert turns.ai_calls == ['Where is my parcel?'] and len(turns.sent) == 1
    assert [r['sender_type'] for r in await turns.messages(ticket['id'])] == ['CUSTOMER', 'AI', 'CUSTOMER']


@pytest.mark.asyncio
async def test_take_over_during_the_ai_call_stops_the_reply(turns):
    async def staff_takes_over():
        ticket = await turns.only_ticket()
        await service.support_service.take_over(ticket['id'], AGENT)

    turns.replies['before_reply'] = staff_takes_over
    status, body = await turns.deliver('wamid.in.1')
    assert status == 200 and body['status'] == 'handed_off_after_failed_turn'
    assert turns.sent == []
    ticket = await turns.only_ticket()
    assert ticket['handling_mode'] == 'HUMAN' and ticket['current_assignee_id'] == '7'
    assert [r['sender_type'] for r in await turns.messages(ticket['id'])] == ['CUSTOMER']


@pytest.mark.asyncio
async def test_ai_handoff_tells_the_customer_then_escalates(turns):
    turns.replies['next'] = AIReplyResult(
        reply_text='A colleague will help you shortly.',
        handoff=HandoffRequest(reason='CHANGE_REQUIRES_HUMAN', summary='Customer wants to change the address.'),
    )
    status, body = await turns.deliver('wamid.in.1', 'Please change my delivery address')
    assert status == 200 and body['status'] == 'handed_off'
    ticket = await turns.only_ticket()
    assert ticket['handling_mode'] == 'HUMAN' and ticket['ai_escalation_reason'] == 'CHANGE_REQUIRES_HUMAN'
    assert ticket['ai_summary'] == 'Customer wants to change the address.'
    rows = await turns.messages(ticket['id'])
    assert [(r['sender_type'], r['send_status']) for r in rows] == [('CUSTOMER', 'RECEIVED'), ('AI', 'SENT')]
    assert len(turns.sent) == 1 and turns.sent[0]['body'] == 'A colleague will help you shortly.'
    unassigned = await turns.repo.list_tickets(view='unassigned', agent_id='7')
    assert ticket['id'] in [int(row['id']) for row in unassigned['items']]


@pytest.mark.asyncio
async def test_media_on_an_ai_ticket_escalates_it(turns):
    await turns.deliver('wamid.in.1')
    status, body = await turns.deliver('wamid.in.loc', None, message_type='location',
                                       location_latitude=33.89, location_longitude=35.5)
    assert status == 200 and body['status'] == 'handed_off_location'
    ticket = await turns.only_ticket()
    assert ticket['handling_mode'] == 'HUMAN' and ticket['ai_escalation_reason'] == 'LOCATION_UNSUPPORTED_BY_AI'
    assert [r['sender_type'] for r in await turns.messages(ticket['id'])] == ['CUSTOMER', 'AI', 'CUSTOMER']
    assert len(turns.ai_calls) == 1


@pytest.mark.asyncio
async def test_ai_usage_is_attributed_to_the_ticket(turns, monkeypatch):
    seen = []

    async def recording_ai(conversation, user_message, **_kwargs):
        seen.append(main.usage_scope.get())
        conversation.messages.append({'role': 'assistant', 'content': 'Noted.'})
        return AIReplyResult(reply_text='Noted.')

    monkeypatch.setattr(main, 'get_ai_reply', recording_ai)
    await turns.deliver('wamid.in.1')
    ticket = await turns.only_ticket()
    assert seen == [{'phone_number_id': LINE, 'ticket_id': int(ticket['id'])}]


@pytest.fixture
def v2_turns(turns, monkeypatch):
    from app.agent import core
    from app.customer_ai_identity import AIIdentity
    import app.support_auth as auth

    turns.prompts = []
    turns.action = {'kind': 'reply', 'text': 'How can I help?', 'language': 'en',
                    'facts_used': [], 'awaiting_customer': True}

    async def knowledge():
        return ''

    async def identity(_phone):
        return AIIdentity(level='L0', reason='NO_MATCH')

    async def call(prefix, tail, **kwargs):
        turns.prompts.append(tail)
        before = turns.replies.get('before_reply')
        if before:
            await before()
        action = dict(turns.action)
        if action['kind'] == 'reply':
            action['language'] = tail['language_style']
        return SimpleNamespace(status='completed', output=[{
            'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'action': action})}],
        }])

    monkeypatch.setattr(main.settings, 'ai_agent_v2_enabled', True)
    monkeypatch.setattr(main, 'support_repository', turns.repo)
    monkeypatch.setattr(service, 'staff_repository', auth.staff_repository)
    monkeypatch.setattr(core, 'support_repository', turns.repo)
    monkeypatch.setattr(core.ai_knowledge_service, 'prompt_text', knowledge)
    monkeypatch.setattr(core.customer_ai_identity_service, 'resolve_or_unknown', identity)
    monkeypatch.setattr(core.agent_service, '_call', call)
    return turns


@pytest.mark.asyncio
async def test_v2_records_reply_and_awaiting_customer(v2_turns):
    env = v2_turns
    assert await env.deliver('wamid.v2.1', 'Hello') == (200, {'status': 'agent_replied'})
    ticket = await env.only_ticket()
    assert ticket['ai_awaiting_customer'] == 1 and ticket['ai_last_reply_at'] is not None
    assert env.prompts[0]['messages'] == [{'role': 'user', 'text': 'Hello'}]
    rows = await env.messages(ticket['id'])
    assert rows[-1]['sender_type'] == 'AI' and rows[-1]['send_status'] == 'SENT'
    assert env.ai_calls == []

    traces = await env.sql('SELECT * FROM ai_agent_turns WHERE ticket_id=%s', (ticket['id'],))
    assert len(traces) == 1 and traces[0]['outcome'] == 'replied' and traces[0]['finished_at']
    assert json.loads(traces[0]['guards_json']) == [{'stage': 'reply', 'verdicts': ['pass']}]


@pytest.mark.asyncio
async def test_v2_takeover_during_generation_wins_send_fence(v2_turns):
    env = v2_turns

    async def take_over():
        ticket = await env.only_ticket()
        await service.support_service.take_over(ticket['id'], AGENT)

    env.replies['before_reply'] = take_over
    assert (await env.deliver('wamid.v2.1', 'Hello'))[1]['status'] == 'handed_off_after_failed_turn'
    assert env.sent == []
    ticket = await env.only_ticket()
    assert ticket['handling_mode'] == 'HUMAN' and ticket['current_assignee_id'] == '7'


@pytest.mark.asyncio
async def test_v2_human_and_disabled_routes_never_call_model(v2_turns, monkeypatch):
    env = v2_turns

    async def disabled(**kwargs):
        return SimpleNamespace(effective_ai_enabled=False, effective_reason='LINE_AI_DISABLED',
                               configuration=SimpleNamespace(control_mode='manual'))

    monkeypatch.setattr(main.ai_availability_service, 'get_effective_ai_availability', disabled)
    assert (await env.deliver('wamid.v2.1', 'Hello'))[1]['status'] == 'handed_off_ai_disabled'
    assert (await env.deliver('wamid.v2.2', 'Hello again'))[1]['status'] == 'held_for_human'
    assert env.prompts == []


@pytest.mark.asyncio
async def test_v2_media_routes_to_staff_without_model(v2_turns):
    env = v2_turns
    result = await env.deliver('wamid.v2.loc', None, message_type='location',
                               location_latitude=33.89, location_longitude=35.5)
    assert result[1]['status'] == 'handed_off_location' and env.prompts == []


@pytest.mark.asyncio
async def test_v2_new_ticket_does_not_inherit_previous_ticket_or_redis_context(v2_turns):
    env = v2_turns
    assert (await env.deliver('wamid.v2.1', 'Old topic'))[1]['status'] == 'agent_replied'
    old = await env.only_ticket()
    await service.support_service.take_over(old['id'], AGENT)
    await env.repo.resolve_ticket(old['id'], agent_id='7', agent_name='Agent')
    assert (await env.deliver('wamid.v2.2', 'New topic'))[1]['status'] == 'agent_replied', env.prompts
    assert env.prompts[-1]['messages'] == [{'role': 'user', 'text': 'New topic'}]


@pytest.mark.asyncio
async def test_agent_context_query_excludes_notes_deleted_unsent_future_and_other_ticket(infrastructure):
    repo, sql, _redis = infrastructure
    await sql("UPDATE support_tickets SET handling_mode='AI' WHERE id=41")

    async def add(body, *, sender='CUSTOMER', status='RECEIVED', ticket=41, message_id=None):
        row, _ = await repo.add_ticket_message(
            ticket_id=ticket, body=body, sender_type=sender,
            direction='INBOUND' if sender == 'CUSTOMER' else 'OUTBOUND',
            send_status=status, whatsapp_message_id=message_id,
        )
        return row

    await add('other ticket', ticket=42)
    await add('staff note', sender='SYSTEM', status='SENT')
    await add('draft', sender='AI', status='PENDING')
    await add('failed', sender='AI', status='FAILED')
    deleted = await add('deleted')
    await sql('UPDATE support_ticket_messages SET deleted_for_everyone_at=UTC_TIMESTAMP(6) WHERE id=%s',
              (deleted['id'],))
    await add('sent reply', sender='AI', status='SENT')
    await add('current question', message_id='wamid.current')
    await add('future question', message_id='wamid.future')
    rows = await repo.list_ai_context_messages(41, source_message_id='wamid.current')
    assert [row['body'] for row in rows] == ['sent reply', 'current question']
    assert await repo.list_ai_context_messages(42, source_message_id='wamid.current') == []
    for i in range(20):
        await add(f'bounded {i}')
    assert len(await repo.list_ai_context_messages(41, source_message_id=None)) == 13


@pytest.mark.asyncio
async def test_v2_image_reaches_same_call_and_notes_are_visible(v2_turns, monkeypatch):
    from app.agent import media
    from app.support_media import StoredSupportMedia
    from pathlib import Path
    from tempfile import TemporaryDirectory
    env = v2_turns
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'synthetic.png'
        path.write_bytes(b'synthetic image')

        async def capture(**kwargs):
            return None

        async def image_call(prefix, tail, **kwargs):
            env.prompts.append(tail)
            return SimpleNamespace(status='completed', output=[{'type': 'message', 'content': [{
                'type': 'output_text', 'text': json.dumps({'action': env.action, 'media_notes': 'A sealed parcel.'}),
            }]}])

        monkeypatch.setattr(service.support_service, 'capture_inbound_media', capture)
        monkeypatch.setattr(media.support_media_store, 'get', lambda ticket, message: StoredSupportMedia(
            ticket_id=ticket, message_id=message, path=path, mime_type='image/png', size_bytes=path.stat().st_size, filename='synthetic.png'))
        from app.agent import core
        monkeypatch.setattr(core.agent_service, '_call', image_call)
        assert (await env.deliver('wamid.image', None, message_type='image', media_id='synthetic'))[1]['status'] == 'agent_replied'
        ticket = await env.only_ticket()
        rows = await env.sql("SELECT * FROM support_ticket_messages WHERE whatsapp_message_id='wamid.image'")
        row = rows[0]
        assert row['ai_media_text'] == 'A sealed parcel.' and row['ai_media_kind'] == 'image_notes'
        assert env.prompts[0]['_image_url'].startswith('data:image/png;base64,')
        assert not await env.repo.save_ai_media_annotation(ticket['id'], row['id'], kind='image_notes', text='overwrite')
        assert not await env.repo.save_ai_media_annotation(999, row['id'], kind='image_notes', text='other ticket')
        context = await env.repo.list_ai_context_messages(ticket['id'], source_message_id=None)
        assert context[0]['ai_media_text'] == 'A sealed parcel.'
        await env.sql("UPDATE support_ticket_messages SET deleted_for_everyone_at=UTC_TIMESTAMP() WHERE id=%s", (row['id'],))
        deleted = (await env.sql('SELECT * FROM support_ticket_messages WHERE id=%s', (row['id'],)))[0]
        visible = service._serialize_support_messages(ticket['id'], [deleted])[0]
        assert visible['ai_media_text'] is None
        assert len(await env.repo.list_ai_context_messages(ticket['id'], source_message_id=None)) == 1


@pytest.mark.asyncio
async def test_v2_disabled_audio_makes_no_transcription_call(v2_turns, monkeypatch):
    env = v2_turns
    async def disabled(**kwargs):
        return SimpleNamespace(effective_ai_enabled=False, effective_reason='LINE_AI_DISABLED',
                               configuration=SimpleNamespace(control_mode='manual'))
    async def capture(**kwargs):
        return None
    monkeypatch.setattr(main.ai_availability_service, 'get_effective_ai_availability', disabled)
    monkeypatch.setattr(service.support_service, 'capture_inbound_media', capture)
    result = await env.deliver('wamid.audio', None, message_type='audio', media_id='synthetic', is_voice=True)
    assert result[1]['status'] == 'handed_off_ai_disabled' and env.prompts == []


@pytest.mark.asyncio
async def test_v2_escalation_is_traced_without_reply_payload(v2_turns):
    env = v2_turns
    env.action = {'kind': 'escalate', 'reason_code': 'COMPLAINT_DISPUTE'}
    assert (await env.deliver('wamid.complaint', 'I want to make a complaint'))[1]['status'] == 'agent_escalated'
    ticket = await env.only_ticket()
    traces = await env.sql('SELECT * FROM ai_agent_turns WHERE ticket_id=%s', (ticket['id'],))
    assert len(traces) == 1 and traces[0]['outcome'] == 'escalated'
    assert traces[0]['reason_code'] == 'COMPLAINT_DISPUTE'
    assert 'I want to make a complaint' not in json.dumps(traces, default=str)
    assert len(env.prompts) == 1 and env.sent == []
