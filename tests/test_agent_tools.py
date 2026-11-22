"""Authorization, ownership, freshness and deterministic output with fake sources."""
from test_ai_ticket_turns import main  # dependency doubles

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agent import business_data as data, tools
from app.agent.contracts import ToolCall
from app.customer_ai_identity import AIIdentity
from app import ai_tools


def action(name, args=None):
    return ToolCall(kind="tool_call", name=name, arguments_json=json.dumps(args or {}))


def identity(level="L1"):
    return AIIdentity(level=level, reason="TEST", customer_id="replica:9")


def run(call, level="L1", style="en"):
    return asyncio.run(tools.execute_tool(call, identity(level), ticket={"id": 1}, style=style))


@pytest.fixture
def source(monkeypatch):
    state = SimpleNamespace(calls=[], stamp=datetime.now(timezone.utc), payload={
        "found": True, "count": 1, "shipments": [{"order_id": "ORD-42", "tracking_number": "TR-42", "status": "In transit"}],
    })

    async def read(name, args, owner):
        state.calls.append((name, args, owner))
        return data.BusinessResult(name, state.stamp, "test_replica", state.payload)

    monkeypatch.setattr(tools, "business_data_source", SimpleNamespace(read=read))
    return state


@pytest.mark.parametrize("level,name", [("L0", "get_customer_shipments"), ("L0", "track_shipment"),
    ("L1", "get_customer_balance"), ("L1", "get_customer_transactions"), ("L1", "get_previous_ticket_context")])
def test_identity_gate_precedes_lookup(source, level, name):
    with pytest.raises(data.BusinessDataError, match="IDENTITY_VERIFICATION_REQUIRED"):
        run(action(name), level)
    assert source.calls == []


@pytest.mark.parametrize("args", [{"customer_id": "victim"}, {"limit": 999}, {"sql": "SELECT secret"}])
def test_no_extra_arguments_or_owner_override(source, args):
    with pytest.raises(data.BusinessDataError, match="AI_TOOL_UNAVAILABLE"):
        run(action("get_customer_shipments", args))
    assert source.calls == []


@pytest.mark.parametrize("style", ["en", "fr", "ar", "leb_ar", "leb_arabizi", "mixed"])
@pytest.mark.parametrize("name", ["get_customer_shipments", "track_shipment", "get_order_status"])
def test_deterministic_owned_shipments_all_styles(source, style, name):
    reply = run(action(name, {} if name == "get_customer_shipments" else {"reference": "ORD-42"}), style=style)
    assert "ORD-42" in reply.text and "TR-42" in reply.text
    assert reply.language == style and reply.awaiting_customer == (name == "get_customer_shipments")
    assert source.calls[0][2].customer_id == "replica:9"


@pytest.mark.parametrize("style", ["en", "fr", "ar", "leb_ar", "leb_arabizi", "mixed"])
def test_wallet_and_transactions_l2(source, style):
    source.payload = {"balance": "12.30", "currency": "USD"}
    reply = run(action("get_customer_balance"), "L2", style)
    assert "12.30 USD" in reply.text
    source.payload = {"transactions": [{"reference": "TX-42", "amount": "-2.30", "currency": "USD", "occurred_at": "2026-11-22"}]}
    assert "TX-42" in run(action("get_customer_transactions"), "L2", style).text


@pytest.mark.parametrize("stamp", [None, datetime.now(timezone.utc) - timedelta(days=1), datetime.now(timezone.utc) + timedelta(hours=1)])
def test_missing_old_or_future_watermarks_fail_closed(source, stamp):
    source.stamp = stamp
    with pytest.raises(data.BusinessDataError, match="BUSINESS_DATA_STALE"):
        run(action("get_customer_shipments"))


def test_mutation_is_unavailable_even_to_verified_customer(source, monkeypatch):
    with pytest.raises(data.BusinessDataError, match="CHANGE_REQUIRES_HUMAN"):
        run(action("update_shipment_mode"), "L2")
    assert source.calls == []
    assert all(item["name"] != "update_shipment_mode" for item in ai_tools.TOOLS)
    result = asyncio.run(ai_tools.dispatch_tool_call("update_shipment_mode", {}, verified_customer_id="9"))
    assert result["handoff"] and result["reason"] == "CHANGE_REQUIRES_HUMAN"


@pytest.fixture
def replica(monkeypatch):
    instance = data.ReplicaBusinessDataSource()
    state = SimpleNamespace(queries=[], rows=[[{"order_id": "ORD-42", "tracking_number": "TR-42", "shipment_id": None, "status": "Transit"}]])

    async def account(customer_id):
        assert customer_id == "replica:9"
        return {"uuid": "stable-owner-uuid"}

    async def snapshot(dataset, queries):
        state.queries.append((dataset, queries))
        return datetime.now(timezone.utc), state.rows

    monkeypatch.setattr(data.customer_users, "get_by_identity", account)
    monkeypatch.setattr(instance, "_snapshot", snapshot)
    state.instance = instance
    return state


def test_replica_uses_uuid_scoped_parameterized_lookup(replica):
    reference = "' OR 1=1 --"
    result = asyncio.run(replica.instance.read("track_shipment", {"reference": reference}, identity()))
    sql, params = replica.queries[0][1][0]
    assert sql.startswith("SELECT ") and "customer_uuid = %s" in sql
    assert reference not in sql and params == ("stable-owner-uuid", reference, reference, reference)
    assert set(result.payload["shipments"][0]) == {"order_id", "tracking_number", "shipment_id", "status"}
    assert result.source == "business_replica" and result.as_of.tzinfo


@pytest.mark.parametrize("rows", [[], [{"balance": "0", "currency": "USD"}] * 2])
def test_missing_or_ambiguous_wallet_never_becomes_zero(replica, rows):
    replica.rows = [rows]
    with pytest.raises(data.BusinessDataError, match="BUSINESS_DATA_UNAVAILABLE"):
        asyncio.run(replica.instance.read("get_customer_balance", {}, identity("L2")))


def test_replica_itself_checks_identity_before_sql(replica):
    with pytest.raises(data.BusinessDataError, match="IDENTITY_VERIFICATION_REQUIRED"):
        asyncio.run(replica.instance.read("get_customer_balance", {}, identity("L1")))
    assert replica.queries == []


def test_unconfigured_source_never_uses_legacy_database(monkeypatch):
    monkeypatch.setattr(data.settings, "ai_business_db_host", "")
    with pytest.raises(data.BusinessDataError, match="BUSINESS_DATA_UNAVAILABLE"):
        asyncio.run(data.ReplicaBusinessDataSource()._ensure_pool())


def test_injected_financial_fields_at_l1_fail_closed(replica):
    replica.rows[0][0]["balance"] = "123"
    with pytest.raises(data.BusinessDataError, match="BUSINESS_DATA_UNAVAILABLE"):
        asyncio.run(replica.instance.read("get_customer_shipments", {}, identity()))


def test_missing_uuid_never_falls_back_to_numeric_id(replica, monkeypatch):
    async def account(_):
        return {"id": 9}
    monkeypatch.setattr(data.customer_users, "get_by_identity", account)
    with pytest.raises(data.BusinessDataError):
        asyncio.run(replica.instance.read("get_customer_shipments", {}, identity()))
    assert replica.queries == []


@pytest.mark.parametrize("style", ["en", "fr", "ar", "leb_ar", "leb_arabizi", "mixed"])
@pytest.mark.parametrize("name", ["get_shipping_price", "get_delivery_duration", "get_route_shipping_options"])
def test_public_catalog_reuses_matching_math_and_renderers(replica, style, name):
    replica.rows = [[{"id": 1, "destination_id": 1, "origin": "China", "destination": "Lebanon",
                     "shipping_method": "Air", "goods_type": "General goods", "price": "$5/kg", "transit_time": "5 days"}],
                    [{"id": 1, "slug": "lebanon", "label": "Lebanon"}]]
    args = {"origin": "China", "destination": "Lebanon", "shipping_method": "Air"}
    if name != "get_route_shipping_options":
        args["goods_type"] = "General goods"
    if name == "get_shipping_price":
        args["weight_kg"] = 2.0
    result = asyncio.run(replica.instance.read(name, args, identity("L0")))
    reply = tools.render_result(result, style)
    assert reply.text and reply.language == style
    assert replica.queries[0][0] == "catalog"
    if name == "get_shipping_price":
        assert "10" in reply.text


def test_legacy_stale_mutation_call_reaches_human_without_continuation(monkeypatch):
    from app import openai_service
    from app.conversation_store import Conversation
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output=[SimpleNamespace(type="function_call", name="update_shipment_mode",
                               call_id="call-1", arguments='{"tracking_number":"T-42","mode":"delivery"}')])

    monkeypatch.setattr(openai_service, "_create_response", create)
    monkeypatch.setattr(openai_service, "_dump_output_items", lambda response: [])
    conversation = Conversation(phone_number="15550000000")
    result = asyncio.run(openai_service.get_ai_reply(conversation, "Change delivery"))
    assert result.handoff.reason == "CHANGE_REQUIRES_HUMAN" and len(calls) == 1
