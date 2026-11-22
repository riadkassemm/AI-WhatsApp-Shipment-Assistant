import json

import pytest

from app.ai_usage import AIUsageRepository


@pytest.mark.asyncio
async def test_durable_trace_links_and_aggregates_usage_including_pending(infrastructure):
    repo, sql, _ = infrastructure
    turn_id = 'a' * 64
    assert await repo.begin_agent_trace(turn_id, 41)
    assert not await repo.begin_agent_trace(turn_id, 41)
    usage = AIUsageRepository()
    usage._pool = repo._pool
    price = (await sql('SELECT id FROM ai_model_prices LIMIT 1'))[0]['id']
    for index in (1, 2):
        await usage.insert_pending({'reservation_id': str(index) * 32, 'purpose': 'turn', 'tier': 'primary',
            'requested_model': 'gpt-6-luna', 'modalities': 'text', 'estimate_micros': 100, 'price_id': price,
            'phone_number_id': '90005', 'ticket_id': 41, 'batch_id': None, 'budget_day': '2026-11-23',
            'budget_month': '2026-11', 'agent_turn_id': turn_id})
    await usage.settle('1' * 32, {'duration_ms': 25, 'model': 'gpt-6-luna', 'input_tokens': 10,
        'cached_input_tokens': 4, 'output_tokens': 3, 'cost_micros': 65, 'cost_basis': 'computed', 'outcome': 'success'})
    await repo.finish_agent_trace(turn_id, outcome='escalated', reason_code='AI_UNCERTAIN',
        tools=['track_shipment'], guards=[{'stage': 'reply', 'verdicts': ['pass']}], latency_ms=55)
    result = (await sql('SELECT * FROM ai_agent_turns WHERE turn_id=%s', (turn_id,)))[0]
    assert result['cost_micros'] == 165 and result['pending_usage_count'] == 1
    assert result['input_tokens'] is None and result['output_tokens'] is None
    assert result['latency_ms'] == 55 and result['finished_at']
    assert json.loads(result['reservation_ids_json']) == ['1' * 32, '2' * 32]
    assert json.loads(result['models_json']) == ['gpt-6-luna']
    assert json.loads(result['tools_json']) == ['track_shipment']
    # A failed finalization would leave this second start row and its usage links
    # durable, without inventing a delivery outcome or replaying the turn.
    assert await repo.begin_agent_trace('b' * 64, 42)
    pending = (await sql('SELECT * FROM ai_agent_turns WHERE turn_id=%s', ('b' * 64,)))[0]
    assert pending['outcome'] == 'started' and pending['finished_at'] is None
