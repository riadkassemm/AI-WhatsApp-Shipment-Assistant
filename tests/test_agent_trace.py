from test_ai_ticket_turns import main  # dependency doubles

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.agent import trace
from app.ai_usage import usage_scope
from test_agent_core import agent, reply, response


@pytest.fixture
def repo():
    state = SimpleNamespace(started=[], finished=[], duplicate=False, fail_finish=False)
    async def begin(turn_id, ticket_id):
        state.started.append((turn_id, ticket_id))
        return not state.duplicate
    async def finish(turn_id, **kwargs):
        if state.fail_finish:
            raise RuntimeError('private database detail')
        state.finished.append((turn_id, deepcopy(kwargs)))
    state.begin_agent_trace, state.finish_agent_trace = begin, finish
    return state


def test_trace_records_only_safe_labels_and_restores_scope(repo, agent):
    agent.state['responses'] = [response(reply('It costs $999.').model_dump()), response()]
    async def run():
        token = usage_scope.set({'phone_number_id': 'test-line'})
        try:
            async with trace.trace_turn(repo, ticket_id=47, source_message_id='private-message-id'):
                assert usage_scope.get()['agent_turn_id'] == repo.started[0][0]
                assert await agent.run()
                trace.record_tool('private model output', {'track_shipment'})
                trace.record_outcome('replied')
            assert usage_scope.get() == {'phone_number_id': 'test-line'}
            assert trace.active_trace.get() is None
        finally:
            usage_scope.reset(token)
    asyncio.run(run())
    encoded = json.dumps(repo.finished)
    assert '999' not in encoded and 'private model output' not in encoded and 'private-message-id' not in encoded
    assert repo.finished[0][1]['tools'] == ['unknown']
    assert repo.finished[0][1]['guards'][0]['verdicts'] == ['ungrounded_value']
    assert repo.finished[0][1]['outcome'] == 'replied'


def test_duplicate_trace_never_enters_provider_work(repo):
    repo.duplicate = True
    async def run():
        async with trace.trace_turn(repo, ticket_id=47, source_message_id='same'):
            raise AssertionError('Must not enter repeated turn')
    with pytest.raises(RuntimeError, match='already recorded'):
        asyncio.run(run())
    assert repo.finished == []


def test_trace_finalization_failure_preserves_result_and_no_payload_log(repo, caplog):
    repo.fail_finish = True
    async def run():
        async with trace.trace_turn(repo, ticket_id=47, source_message_id='one'):
            trace.record_outcome('replied')
            return 'sent once'
    assert asyncio.run(run()) == 'sent once'
    assert 'private database detail' not in caplog.text
    assert trace.active_trace.get() is None


def test_concurrent_turn_metadata_does_not_mix(repo):
    async def run():
        entered = asyncio.Event()
        async def one():
            async with trace.trace_turn(repo, ticket_id=1, source_message_id='one'):
                trace.record_tool('track_shipment', {'track_shipment'})
                entered.set()
                await finished.wait()
                trace.record_outcome('replied')
        async def two():
            await entered.wait()
            async with trace.trace_turn(repo, ticket_id=2, source_message_id='two'):
                trace.record_tool('get_customer_balance', {'get_customer_balance'})
                trace.record_outcome('escalated', 'IDENTITY_VERIFICATION_REQUIRED')
            finished.set()
        finished = asyncio.Event()
        await asyncio.gather(one(), two())
    asyncio.run(run())
    assert repo.finished[0][1]['tools'] == ['get_customer_balance']
    assert repo.finished[1][1]['tools'] == ['track_shipment']


def test_exception_records_failure_without_exception_text(repo):
    async def run():
        async with trace.trace_turn(repo, ticket_id=1, source_message_id='one'):
            raise ValueError('private reply')
    with pytest.raises(ValueError):
        asyncio.run(run())
    assert repo.finished[0][1]['reason_code'] == 'AI_TURN_FAILED'
    assert 'private reply' not in json.dumps(repo.finished)
