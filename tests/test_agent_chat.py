"""Local terminal chat: refuses non-disposable setups and isolates every collaborator."""
from test_ai_ticket_turns import main  # dependency doubles
from test_agent_core import agent, response, reply

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent import core
from scripts import agent_chat


def setup_preflight(monkeypatch, *, host="127.0.0.1", v2=False, tickets=0, price=object()):
    monkeypatch.setattr(agent_chat, "LOCAL_HOSTS", {"127.0.0.1", "localhost"})
    from app.config import settings
    monkeypatch.setattr(settings, "db_host", host)
    monkeypatch.setattr(settings, "ai_agent_v2_enabled", v2)

    async def fetch(query, args=(), *, one=False):
        assert "support_tickets" in query
        return {"n": tickets}

    async def price_for(model):
        return price

    monkeypatch.setattr(core.ai_usage_service.repository, "_fetch", fetch)
    monkeypatch.setattr(core.ai_usage_service, "price_for", price_for)


def test_preflight_accepts_local_empty_database(monkeypatch):
    setup_preflight(monkeypatch)
    asyncio.run(agent_chat.preflight("gpt-6-luna"))


@pytest.mark.parametrize("change,message", [
    ({"host": "10.0.0.5"}, "DB_HOST"),
    ({"v2": True}, "AI_AGENT_V2_ENABLED"),
    ({"tickets": 3}, "support tickets"),
    ({"price": None}, "Verify the price"),
])
def test_preflight_refuses_unsafe_setups(monkeypatch, change, message):
    setup_preflight(monkeypatch, **change)
    with pytest.raises(agent_chat.ChatRefused, match=message):
        asyncio.run(agent_chat.preflight("gpt-6-luna"))


def test_turn_uses_metered_core_with_isolated_identity_and_no_business_data(agent, monkeypatch):
    agent.state["responses"] = [response({"kind": "tool_call", "name": "get_customer_shipments", "arguments_json": "{}"})]
    batches = []

    async def fetch(query, args=(), *, one=False):
        batches.append(args)
        return [{"outcome": "success", "cost_basis": "computed", "cost_micros": 41}]

    monkeypatch.setattr(core.ai_usage_service.repository, "_fetch", fetch)
    original = core.support_repository
    history = [{"sender_type": "CUSTOMER", "body": "Where is my parcel?"}]
    outcome, attempted, style, seconds, cost, complete = asyncio.run(agent_chat.run_turn(
        history, identity_level="L1", knowledge="", options={"model": "gpt-6-luna", "reasoning_effort": "none", "step_up": False}))
    assert core.support_repository is original
    assert outcome.reply is None and outcome.reason_code == "BUSINESS_DATA_UNAVAILABLE"
    assert attempted == ["get_customer_shipments"]
    assert style == "en" and cost == 41 and complete
    assert len(agent.calls) == 1 and agent.meters[0]["purpose"] == "eval"
    assert agent.calls[0]["model"] == "gpt-6-luna"
    assert "Where is my parcel?" in json.dumps(agent.calls[0], ensure_ascii=False)
    assert batches and len(batches[0][0]) == 32


def test_reply_is_added_to_history_for_the_next_turn(agent, monkeypatch, capsys):
    agent.state["responses"] = [response(reply("Hello! How can I help?").model_dump()),
                                response(reply("Please send your tracking number.").model_dump())]

    async def fetch(query, args=(), *, one=False):
        if "support_tickets" in query:
            return {"n": 0}
        return [{"outcome": "success", "cost_basis": "computed", "cost_micros": 10}]

    async def price_for(model):
        return object()

    from app.config import settings
    monkeypatch.setattr(settings, "db_host", "127.0.0.1")
    monkeypatch.setattr(settings, "ai_agent_v2_enabled", False)
    monkeypatch.setattr(core.ai_usage_service.repository, "_fetch", fetch)
    monkeypatch.setattr(core.ai_usage_service, "price_for", price_for)
    inputs = iter(["Hi", "Where is my parcel?", "/cost", "/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    args = SimpleNamespace(model="gpt-6-luna", reasoning="none", identity="L1", knowledge=None)
    assert asyncio.run(agent_chat.chat(args)) == 0
    out = capsys.readouterr().out
    assert "bot> Hello! How can I help?" in out and "bot> Please send your tracking number." in out
    assert "spent $0.0000 over 2 turns" in out
    assert "Hello! How can I help?" in json.dumps(agent.calls[1], ensure_ascii=False)


def test_queued_messages_reach_one_turn_together(agent, monkeypatch, capsys):
    agent.state["responses"] = [response(reply("Thanks, checking.").model_dump())]

    async def fetch(query, args=(), *, one=False):
        if "support_tickets" in query:
            return {"n": 0}
        return [{"outcome": "success", "cost_basis": "computed", "cost_micros": 10}]

    async def price_for(model):
        return object()

    from app.config import settings
    monkeypatch.setattr(settings, "db_host", "127.0.0.1")
    monkeypatch.setattr(settings, "ai_agent_v2_enabled", False)
    monkeypatch.setattr(core.ai_usage_service.repository, "_fetch", fetch)
    monkeypatch.setattr(core.ai_usage_service, "price_for", price_for)
    inputs = iter(["+Hello", "+I sent a parcel", "Is it delivered?", "/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    args = SimpleNamespace(model="gpt-6-luna", reasoning="none", identity="L1", knowledge=None)
    assert asyncio.run(agent_chat.chat(args)) == 0
    assert len(agent.calls) == 1
    sent = json.dumps(agent.calls[0], ensure_ascii=False)
    assert "Hello" in sent and "I sent a parcel" in sent and "Is it delivered?" in sent
    assert capsys.readouterr().out.count("(queued, no bot turn yet)") == 2
