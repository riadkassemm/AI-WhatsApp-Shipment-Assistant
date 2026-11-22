"""Usage administration rejects non-managers before touching storage."""
import asyncio
import hashlib
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI

from test_ai_usage_budget import ADMIN, AGENT, FakeCounters, FakeRepository, NOW
from app import support_api
from app.ai_usage import AIUsageService, AIUsageValidationError
from app.support_auth import SupportAgent, get_current_agent


PRICE = {"model_prefix": "gpt-example", "input_per_million": "1", "cached_input_per_million": "0.1",
         "output_per_million": "5", "batch_factor": "0.5"}
BUDGET = {"monthly_limit_usd": "300", "daily_limit_usd": None, "warning_thresholds": [50, 80, 95],
          "enforcement_enabled": True, "version": 1}


class ReportRepository(FakeRepository):
    async def usage_by_day(self, start, end, line, model):
        self.filters = (start, end, line, model)
        return [{"day": "2026-11-21", "requests": 3, "cost_micros": 2_500_000, "pending_micros": 500_000}]

    async def usage_by(self, column, *filters):
        return [{"name": "eval" if column == "purpose" else "example", "requests": 1,
                 "input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 20,
                 "reasoning_tokens": 2, "cost_micros": 1_000_000, "failures": 0}]

    async def ticket_cost(self, *filters):
        return {"tickets": 2, "cost_micros": 1_500_000}


def test_report_uses_budget_timezone_fills_days_and_separates_offline_cost():
    repository = ReportRepository()
    # UTC is still November; Beirut has crossed into December.
    service = AIUsageService(repository, FakeCounters(), clock=lambda: datetime(2026, 11, 30, 23, tzinfo=timezone.utc))
    report = asyncio.run(service.usage_report(ADMIN))
    assert report["filters"] == {"from": "2026-12-01", "to": "2026-12-01", "line": None, "model": None}
    service = AIUsageService(repository, FakeCounters(), clock=lambda: NOW)
    report = asyncio.run(service.usage_report(ADMIN, start="2026-11-20", end="2026-11-22", line="line-a", model="example"))
    assert repository.filters == ("2026-11-20", "2026-11-22", "line-a", "example")
    assert [row["requests"] for row in report["daily"]] == [0, 3, 0]
    assert report["daily"][1]["pending_usd"] == .5
    assert report["summary"]["range_cost_usd"] == 2.5
    assert report["summary"]["offline_cost_usd"] == 1
    assert report["summary"]["average_cost_per_ticket_usd"] == .75
    assert report["summary"]["escalation_rate"] is None
    assert report["estimated"] is True


@pytest.mark.parametrize("start,end", [("invalid", "2026-11-22"), ("2026-11-23", "2026-11-22"),
                                       ("2024-01-01", "2026-11-22")])
def test_report_rejects_invalid_or_unbounded_ranges(start, end):
    service = AIUsageService(ReportRepository(), FakeCounters(), clock=lambda: NOW)
    with pytest.raises(AIUsageValidationError):
        asyncio.run(service.usage_report(ADMIN, start=start, end=end))


@pytest.mark.parametrize("field,value", [("input_per_million", "NaN"), ("input_per_million", "Infinity"),
    ("cached_input_per_million", "-1"), ("output_per_million", "0.0000001"),
    ("batch_factor", "0"), ("batch_factor", "1.1"), ("batch_factor", "0.12345"),
    ("model_prefix", "bad/name")])
def test_prices_reject_unrepresentable_or_invalid_values_before_storage(field, value):
    service = AIUsageService(object(), FakeCounters())
    with pytest.raises(AIUsageValidationError):
        asyncio.run(service.add_price(ADMIN, {**PRICE, field: value}))


ROUTES = [("GET", "/ai-usage", None), ("GET", "/ai-budget", None), ("GET", "/ai-prices", None),
          ("PUT", "/ai-budget", BUDGET), ("POST", "/ai-prices", PRICE),
          ("POST", "/ai-prices/1/verify", None), ("POST", "/ai-prices/1/retire", None)]


@pytest.mark.parametrize("method,path,payload", ROUTES)
def test_agents_get_403_on_every_usage_route(monkeypatch, method, path, payload):
    app = FastAPI()
    app.include_router(support_api.router)
    agent = SupportAgent(id=AGENT.id, name=AGENT.name, role="agent",
                         csrf_hash=hashlib.sha256(b"test-csrf").hexdigest())
    async def current_agent():
        return agent
    app.dependency_overrides[get_current_agent] = current_agent
    monkeypatch.setattr(support_api, "ai_usage_service", AIUsageService(object(), FakeCounters()))

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.request(method, "/api/support" + path, json=payload,
                                          headers={"X-CSRF-Token": "test-csrf"})
            assert result.status_code == 403, result.text
            assert "Admin" in result.json()["detail"]
    asyncio.run(request())


@pytest.mark.parametrize("method,path,payload", [row for row in ROUTES if row[0] != "GET"])
def test_manager_mutations_require_csrf(monkeypatch, method, path, payload):
    app = FastAPI()
    app.include_router(support_api.router)
    async def current_agent():
        return ADMIN
    app.dependency_overrides[get_current_agent] = current_agent
    monkeypatch.setattr(support_api, "ai_usage_service", AIUsageService(object(), FakeCounters()))

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.request(method, "/api/support" + path, json=payload)
            assert result.status_code == 403 and result.json()["detail"] == "Invalid CSRF token."
    asyncio.run(request())


def test_verifying_snapshot_does_not_unblock_unpriced_parent():
    class Repository(FakeRepository):
        async def mark_price(self, price_id, **kwargs):
            return {"model_prefix": "gpt-example-2026-11-01"}

    repository = Repository(prices=[])
    counters = FakeCounters()
    counters.blocked = {"gpt-example", "gpt-example-2026-11-01"}
    service = AIUsageService(repository, counters)

    async def price_for(model):
        return object() if model == "gpt-example-2026-11-01" else None

    async def price_list(agent):
        return {}

    service.price_for = price_for
    service.price_list = price_list
    asyncio.run(service.verify_price(ADMIN, 1))
    assert counters.blocked == {"gpt-example"}
