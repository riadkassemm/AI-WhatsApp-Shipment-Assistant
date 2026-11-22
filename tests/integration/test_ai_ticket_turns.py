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
