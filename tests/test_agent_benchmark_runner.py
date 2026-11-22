"""Replay harness must use the real core/meter path with isolated business data."""
from test_ai_ticket_turns import main  # dependency doubles
from test_agent_core import agent, response, reply

import asyncio
import json

import pytest

from app.agent import core
from app.agent.benchmark.runner import replay_case, run_paid
from app.agent.benchmark.scoring import load_config, observation_digest
from app.agent.benchmark.files import private_directory


@pytest.mark.parametrize('configuration', list(load_config()['configurations']))
def test_all_configurations_capture_usage_without_real_customer_access(agent, monkeypatch, tmp_path, configuration):
    case = {'case_id': 'synthetic', 'split': 'gold', 'identity_level': 'L1', 'approved_knowledge': '',
            'context': [], 'text': 'Where is my parcel?', 'evidence_profile': 'unavailable-v1'}
    agent.state['responses'] = [response({'kind': 'tool_call', 'name': 'get_customer_shipments', 'arguments_json': '{}'})]
    queries = []
    async def fetch(query, args):
        queries.append((query, args))
        return [{'reservation_id': 'synthetic-reservation', 'requested_model': 'test', 'model': 'test',
                 'outcome': 'success', 'cost_basis': 'computed', 'cost_micros': 19,
                 'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5, 'price_id': 1}]
    monkeypatch.setattr(core.ai_usage_service.repository, '_fetch', fetch)
    original = core.support_repository
    output = private_directory(tmp_path / 'run')
    row = asyncio.run(replay_case(case, load_config()['configurations'][configuration],
                     configuration=configuration, run_id='synthetic-run', output=output, bundle_sha256='bundle'))
    assert core.support_repository is original
    assert row['reason_code'] == 'BUSINESS_DATA_UNAVAILABLE' and row['escalated']
    assert row['tools'] == [{'name': 'get_customer_shipments', 'arguments': {}}]
    assert row['cost_micros'] == 19 and row['usage_complete']
    assert row['review']['reviewed'] is False
    assert row['review']['observation_sha256'] == observation_digest(row)
    assert len(agent.calls) == 1 and agent.meters[0]['purpose'] == 'eval'
    assert agent.calls[0]['model'] == load_config()['configurations'][configuration]['model']
    assert 'synthetic-run' not in json.dumps(agent.calls)
    assert len(queries) == 1 and queries[0][1] == (row['batch_id'],)
    assert len(list(output.glob('*.json'))) == 1
    assert not agent.reads  # real repository collaborator never reached


def test_stepup_cost_and_calls_both_recorded(agent, monkeypatch, tmp_path):
    case = {'case_id': 'synthetic', 'split': 'redteam', 'identity_level': 'L1', 'approved_knowledge': '',
            'context': [], 'text': 'Hello', 'evidence_profile': 'unavailable-v1'}
    agent.state['responses'] = [response({'kind': 'escalate', 'reason_code': 'MULTIPLE_ISSUES'}), response()]
    async def fetch(*args):
        return [{'reservation_id': str(i), 'cost_micros': cost, 'outcome': 'success', 'cost_basis': 'computed',
                 'input_tokens': 12, 'cached_input_tokens': 0, 'output_tokens': 5} for i, cost in enumerate([10, 99])]
    monkeypatch.setattr(core.ai_usage_service.repository, '_fetch', fetch)
    row = asyncio.run(replay_case(case, load_config()['configurations']['luna→sol'], configuration='luna→sol',
                     run_id='synthetic-run', output=private_directory(tmp_path / 'run'), bundle_sha256='bundle'))
    assert row['cost_micros'] == 109 and len(row['usage']) == 2
    assert len(agent.calls) == 2 and [c['model'] for c in agent.calls] == ['gpt-6-luna', 'gpt-6.2-sol']
    assert [m['purpose'] for m in agent.meters] == ['eval', 'eval']


def test_l2_tool_is_blocked_by_real_registry_before_data_source(agent, monkeypatch, tmp_path):
    case = {'case_id': 'synthetic', 'split': 'redteam', 'identity_level': 'L1', 'approved_knowledge': '',
            'context': [], 'text': 'Show my balance', 'evidence_profile': 'unavailable-v1'}
    agent.state['responses'] = [response({'kind': 'tool_call', 'name': 'get_customer_balance', 'arguments_json': '{}'})]
    async def fetch(*args):
        return []
    monkeypatch.setattr(core.ai_usage_service.repository, '_fetch', fetch)
    row = asyncio.run(replay_case(case, load_config()['configurations']['luna-none'], configuration='luna-none',
                     run_id='synthetic-run', output=private_directory(tmp_path / 'run'), bundle_sha256='bundle'))
    assert row['reason_code'] == 'IDENTITY_VERIFICATION_REQUIRED'
    assert not row['usage_complete'] and row['cost_micros'] is None


def test_price_preflight_blocks_entire_run_without_any_provider_call(agent, monkeypatch, tmp_path):
    from app.agent.benchmark import runner
    async def no_price(model):
        return None
    monkeypatch.setattr(runner, 'require_paid_approval', lambda *args, **kwargs: None)
    monkeypatch.setattr(core.ai_usage_service, 'price_for', no_price)
    output = private_directory(tmp_path / 'run')
    with pytest.raises(ValueError, match='Verify every'):
        asyncio.run(run_paid([], load_config(), {}, output=output, bundle_sha256='bundle', configurations=['luna-none']))
    assert not agent.calls and not list(output.iterdir())
