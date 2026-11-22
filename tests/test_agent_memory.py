from test_ai_ticket_turns import main  # dependency doubles

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.agent import memory
from app.agent.business_data import BusinessDataError
from app.customer_ai_identity import AIIdentity


def row(index, **extra):
    return {'id': index, 'sender_type': 'CUSTOMER', 'body': 'Where is my parcel?',
            'whatsapp_message_id': f'in.{index}', 'version': 1, 'deleted_for_everyone_at': None, **extra}


@pytest.fixture
def env(monkeypatch):
    state = SimpleNamespace(rows=[row(i) for i in range(1, 26)], cache={}, calls=[], saves=[], lookups=[])

    async def snapshot(ticket_id, source_message_id=None):
        rows = state.rows
        if source_message_id:
            rows = [r for r in rows if r['id'] <= int(source_message_id.split('.')[-1])]
        return deepcopy(state.cache), deepcopy(rows)

    async def save(ticket_id, **kwargs):
        state.saves.append(kwargs)
        state.cache = {'ai_memory_summary': kwargs['summary'], 'ai_memory_through_id': kwargs['through_id'],
                       'ai_memory_fingerprint': kwargs['fingerprint'], 'ai_memory_language': kwargs['language']}
        return True

    async def summarize(previous, rows, style):
        state.calls.append((previous, deepcopy(rows), style))
        if getattr(state, 'during_summary', None):
            state.during_summary()
        return 'The customer previously asked about a parcel.'

    async def previous(**kwargs):
        state.lookups.append(kwargs)
        return {'id': 7, 'ticket_reference': 'T-7'}

    async def identity(_):
        return AIIdentity(level=state.level, reason='TEST', customer_id='replica:9')

    state.level = 'L2'
    state.repo = SimpleNamespace(ai_memory_snapshot=snapshot, save_ai_memory=save, ai_previous_ticket=previous)
    monkeypatch.setattr(memory, 'summarize', summarize)
    monkeypatch.setattr(memory.customer_ai_identity_service, 'resolve_or_unknown', identity)
    state.run = lambda **kwargs: asyncio.run(memory.ticket_summary(state.repo, 7, source_message_id=kwargs.pop('source_message_id', None), style='en', **kwargs))
    return state


def test_short_ticket_has_no_summary_call(env):
    env.rows = env.rows[:20]
    assert env.run() is None
    assert env.calls == env.saves == []


def test_incremental_cache_excludes_last_twelve_and_waits_for_delta(env):
    assert env.run()
    assert [r['id'] for r in env.calls[0][1]] == list(range(1, 14))
    assert env.cache['ai_memory_through_id'] == 13
    env.rows.extend(row(i) for i in range(26, 32))
    assert env.run() and len(env.calls) == 1
    env.rows.extend(row(i) for i in range(32, 36))
    assert env.run() and len(env.calls) == 2
    assert env.calls[1][0] == env.cache['ai_memory_summary']
    assert [r['id'] for r in env.calls[1][1]] == list(range(13, 24))


@pytest.mark.parametrize('change', [{'body': 'Edited request', 'version': 2}, {'deleted_for_everyone_at': 'deleted'}])
def test_edited_or_deleted_source_invalidates_cache(env, change):
    env.run()
    env.rows[0].update(change)
    env.run()
    assert len(env.calls) == 2 and env.calls[1][0] == ''
    if 'deleted_for_everyone_at' in change:
        assert all(r['id'] != 1 for r in env.calls[1][1])


def test_summary_of_later_messages_is_not_reused_for_older_turn(env):
    env.run()
    assert env.run(source_message_id='in.21')
    assert len(env.calls) == 2 and env.calls[1][0] == ''
    assert max(r['id'] for r in env.calls[1][1]) == 9


def test_source_change_during_generation_prevents_cache_and_reply(env):
    env.during_summary = lambda: env.rows[0].update(deleted_for_everyone_at='deleted')
    with pytest.raises(BusinessDataError, match='AI_MEMORY_UNAVAILABLE'):
        env.run()
    assert env.saves == []


@pytest.mark.parametrize('text', ['Hello', 'What is my balance?', 'Track parcel'])
def test_previous_tool_requires_customer_reference(env, text):
    with pytest.raises(BusinessDataError, match='AI_TOOL_UNAVAILABLE'):
        asyncio.run(memory.previous_context(env.repo, ticket={'id': 9, 'whatsapp_phone': '15550000000'},
                    identity=AIIdentity(level='L2', reason='TEST', customer_id='replica:9'),
                    reference='latest', current_text=text, style='en'))
    assert env.lookups == env.calls == []


@pytest.mark.parametrize('text', ['My previous ticket', 'Le même problème', 'المشكلة السابقة', '2abel 7kina', 'Ticket T-7'])
def test_previous_tool_uses_one_lazy_cached_summary(env, text):
    async def get():
        return await memory.previous_context(env.repo, ticket={'id': 9, 'whatsapp_phone': '15550000000'},
            identity=AIIdentity(level='L2', reason='TEST', customer_id='replica:9'),
            reference='T-7' if text == 'Ticket T-7' else 'latest', current_text=text, style='en')
    assert asyncio.run(get()).text.startswith('Earlier conversation')
    assert asyncio.run(get()).text.startswith('Earlier conversation')
    assert len(env.calls) == 1 and len(env.calls[0][1]) == 25


def test_previous_verification_revoked_during_summary_blocks_disclosure(env):
    env.during_summary = lambda: setattr(env, 'level', 'L0')
    with pytest.raises(BusinessDataError, match='IDENTITY_VERIFICATION_REQUIRED'):
        asyncio.run(memory.previous_context(env.repo, ticket={'id': 9, 'whatsapp_phone': '15550000000'},
                    identity=AIIdentity(level='L2', reason='TEST', customer_id='replica:9'),
                    reference='latest', current_text='My previous issue', style='en'))


from test_agent_core import agent, reply, response


def test_summary_provider_is_metered_masked_and_has_no_retry(agent):
    rows = [row(1, body='password: private-pass'), row(2, body='Where is the parcel?')]
    agent.state['responses'] = [response(reply('The customer asked about a parcel.').model_dump())]
    result = asyncio.run(memory.summarize('', rows, 'en'))
    assert result == 'The customer asked about a parcel.'
    assert len(agent.calls) == 1 and agent.meters[0]['purpose'] == 'summary'
    assert 'private-pass' not in agent.calls[0]['input']
    assert agent.options == [{'max_retries': 0}]


def test_summary_fabrication_is_rejected_without_second_call(agent):
    agent.state['responses'] = [response(reply('The customer paid $999.').model_dump())]
    with pytest.raises(BusinessDataError, match='AI_MEMORY_UNAVAILABLE'):
        asyncio.run(memory.summarize('', [row(1)], 'en'))
    assert len(agent.calls) == 1
