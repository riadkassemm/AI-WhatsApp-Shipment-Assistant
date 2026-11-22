"""AI budget on real Redis + MariaDB: atomic reservations, crashes, rebuilds."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.ai_usage import AIUsageBlocked, AIUsageRepository, AIUsageService, BudgetCounters
from app.support_auth import SupportAgent

ADMIN = SupportAgent(id='8', name='Manager', role='supervisor')
NOW = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)


async def service_for(infrastructure, *, monthly_micros=1_000_000, verify=('gpt-6.2-sol',)):
    repo, sql, redis_client = infrastructure
    from app.config import settings
    await redis_client.flushdb()
    await sql("UPDATE ai_budget_settings SET monthly_limit_micros=%s, daily_limit_micros=NULL, "
              "enforcement_enabled=1, override_until=NULL WHERE id=1", (monthly_micros,))
    for prefix in verify:
        await sql("UPDATE ai_model_prices SET verified_at=UTC_TIMESTAMP(6), verified_by_staff_id=8 "
                  "WHERE model_prefix=%s", (prefix,))
    storage = AIUsageRepository()
    storage._pool = repo._pool
    counters = BudgetCounters(redis_url=settings.redis_url)
    return AIUsageService(storage, counters, clock=lambda: NOW), sql, redis_client, counters


def response(output_tokens=100):
    return SimpleNamespace(model='gpt-6.2-sol-2026-11-01', usage=SimpleNamespace(
        input_tokens=100, input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens=output_tokens, output_tokens_details=SimpleNamespace(reasoning_tokens=0)))


@pytest.mark.asyncio
async def test_seeded_prices_are_unverified_and_block_until_an_admin_verifies(infrastructure):
    service, sql, _redis, counters = await service_for(infrastructure, verify=())
    rows = await sql("SELECT model_prefix, verified_at FROM ai_model_prices ORDER BY model_prefix")
    assert {r['model_prefix'] for r in rows} >= {'gpt-6-luna', 'gpt-6.2-sol', 'gpt-transcribe'}
    assert all(r['verified_at'] is None for r in rows)
    with pytest.raises(AIUsageBlocked) as blocked:
        await service.metered(purpose='turn', tier='primary', model='gpt-6.2-sol', call=lambda: None)
    assert blocked.value.reason == 'AI_MODEL_PRICING_UNCONFIGURED'
    await sql("UPDATE ai_model_prices SET verified_at=UTC_TIMESTAMP(6) WHERE model_prefix='gpt-6.2-sol'")
    price = await service.price_for('gpt-6.2-sol-2026-11-01')
    assert price is not None and price.model_prefix == 'gpt-6.2-sol'
    await counters.client().aclose()


@pytest.mark.asyncio
async def test_concurrent_calls_never_exceed_the_limit(infrastructure):
    # Each call reserves ~ (input estimate + 1,000 output tokens x $10/1M) ~ $0.0103.
    service, sql, _redis, counters = await service_for(infrastructure, monthly_micros=100_000)
    started = []

    async def call():
        started.append(1)
        await asyncio.sleep(0.05)
        return response(output_tokens=1000)

    async def one():
        try:
            await service.metered(purpose='turn', tier='primary', model='gpt-6.2-sol', call=call,
                                  input_payload='x' * 100, max_output_tokens=1000)
            return 'ok'
        except AIUsageBlocked as exc:
            return exc.reason

    results = await asyncio.gather(*(one() for _ in range(40)))
    assert results.count('ok') == len(started) < 40
    assert set(results) == {'ok', 'AI_BUDGET_EXCEEDED'}
    total = (await sql("SELECT SUM(cost_micros) AS total FROM ai_usage_events"))[0]['total']
    assert int(total) <= 100_000
    await counters.client().aclose()


@pytest.mark.asyncio
async def test_crash_after_pending_row_and_redis_flush_never_lower_spend(infrastructure):
    service, sql, redis_client, counters = await service_for(infrastructure, monthly_micros=50_000)

    async def crash():
        raise SystemExit('process died mid-call')

    with pytest.raises(SystemExit):
        await service.metered(purpose='turn', tier='primary', model='gpt-6.2-sol', call=crash,
                              input_payload='x' * 100, max_output_tokens=3000)
    row = (await sql("SELECT outcome, cost_micros, estimate_micros FROM ai_usage_events"))[0]
    assert row['outcome'] == 'failure' and row['cost_micros'] == row['estimate_micros']
    spent_before = await counters.used(counters.month_key('2026-11'))
    assert spent_before == row['estimate_micros']

    # Simulate a hard crash with the row still pending, then lose all of Redis.
    await sql("UPDATE ai_usage_events SET outcome='pending'")
    await redis_client.flushdb()
    restarted = AIUsageService(service.repository, BudgetCounters(redis_url=counters.redis_url), clock=lambda: NOW)
    await restarted.budget_state()
    assert await restarted.counters.used(restarted.counters.month_key('2026-11')) == spent_before

    # Reconciliation raises Redis to the durable total but never lowers it.
    await sql("UPDATE ai_usage_events SET cost_micros=cost_micros+10000")
    restarted._reconciled.clear()
    await restarted.budget_state()
    assert await restarted.counters.used(restarted.counters.month_key('2026-11')) == spent_before + 10_000
    await sql("UPDATE ai_usage_events SET cost_micros=1")
    restarted._reconciled.clear()
    await restarted.budget_state()
    assert await restarted.counters.used(restarted.counters.month_key('2026-11')) == spent_before + 10_000
    await counters.client().aclose()
    await restarted.counters.client().aclose()


@pytest.mark.asyncio
async def test_settled_costs_use_real_usage_and_settings_are_versioned(infrastructure):
    service, sql, _redis, counters = await service_for(infrastructure, monthly_micros=1_000_000)

    async def call():
        return response(output_tokens=200)

    await service.metered(purpose='turn', tier='primary', model='gpt-6.2-sol', call=call,
                          input_payload='hello', max_output_tokens=1000)
    row = (await sql("SELECT outcome, cost_basis, cost_micros, estimate_micros, model, input_tokens, output_tokens "
                     "FROM ai_usage_events"))[0]
    assert (row['outcome'], row['cost_basis'], row['model']) == ('success', 'computed', 'gpt-6.2-sol-2026-11-01')
    assert row['cost_micros'] == 100 * 2 + 200 * 10 and row['estimate_micros'] > row['cost_micros']

    version = (await sql("SELECT version FROM ai_budget_settings"))[0]['version']
    payload = {'monthly_limit_usd': '250', 'daily_limit_usd': None, 'warning_thresholds': [60, 90],
               'enforcement_enabled': True, 'version': version}
    saved = await service.update_settings(ADMIN, payload)
    assert saved['monthly_limit_usd'] == 250 and saved['warning_thresholds'] == [60, 90]
    with pytest.raises(Exception, match='changed in another session'):
        await service.update_settings(ADMIN, payload)
    audit = await sql("SELECT action FROM ai_control_settings_audit WHERE action='AI_BUDGET_UPDATED'")
    assert len(audit) == 1
    await counters.client().aclose()
