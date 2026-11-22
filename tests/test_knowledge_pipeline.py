"""Offline knowledge pipeline (Step 4c-2): export, batch format, parsing, taxonomy. No OpenAI calls."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from decimal import Decimal
from pathlib import Path

for _name in ("aiomysql", "bcrypt", "openai"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

import pytest  # noqa: E402

from app.knowledge import pipeline  # noqa: E402


def _fetcher(tickets, messages):
    async def fetch(query, args):
        if "FROM staff" in query:
            return [{"name": "Maya Staff"}]
        if "FROM support_tickets" in query:
            return tickets
        return [m for m in messages if m["ticket_id"] in args]
    return fetch


def test_export_deidentifies_and_holds_out_the_newest_tickets() -> None:
    tickets = [{"id": i, "created_at": i, "communication_style": "en", "customer_name": "Rami Haddad"} for i in (1, 2, 3)]
    messages = [
        {"ticket_id": 1, "sender_type": "CUSTOMER", "direction": "INBOUND", "message_type": "text",
         "body": "I'm Rami Haddad, my number is +961 70 123 456"},
        {"ticket_id": 1, "sender_type": "AGENT", "direction": "OUTBOUND", "message_type": "text",
         "body": "Hi, Maya Staff here. Refund of $20 approved"},
        {"ticket_id": 1, "sender_type": "CUSTOMER", "direction": "INBOUND", "message_type": "audio", "body": "Voice note"},
        {"ticket_id": 2, "sender_type": "AGENT", "direction": "OUTBOUND", "message_type": "text", "body": "agent only"},
        {"ticket_id": 3, "sender_type": "CUSTOMER", "direction": "INBOUND", "message_type": "image", "body": "Image"},
    ]
    training, gold = asyncio.run(pipeline.export_transcripts(_fetcher(tickets, messages), gold_count=1))
    # Ticket 2 has no customer message and is skipped; ticket 3 (newest) is the gold set.
    assert [r["ticket_id"] for r in training] == [1] and [r["ticket_id"] for r in gold] == [3]
    text = json.dumps(training)
    for private in ("Rami", "Haddad", "70 123", "Maya", "$20"):
        assert private not in text
    roles = [m["role"] for m in training[0]["messages"]]
    assert roles == ["customer", "agent", "customer"] and training[0]["messages"][2]["text"] == "[voice note]"
    assert gold[0]["messages"] == [{"role": "customer", "text": "[image]"}]


def test_long_conversations_are_bounded() -> None:
    rows = [{"sender_type": "CUSTOMER", "message_type": "text", "body": f"message {i} " + "x" * 300} for i in range(100)]
    record = pipeline.build_transcript({"id": 9}, rows, names=[], max_messages=10, max_chars=2000)
    assert len(record["messages"]) <= 11 and record["messages"][-1]["text"] == "[conversation shortened]"


def test_batch_request_and_cost_estimate() -> None:
    record = {"ticket_id": 7, "messages": [{"role": "customer", "text": "wen el parcel?"}]}
    request = pipeline.classification_request(record, model="gpt-6-luna")
    assert request["custom_id"] == "ticket-7" and request["url"] == "/v1/responses"
    body = request["body"]
    assert body["store"] is False and body["text"]["format"]["strict"] is True
    assert body["input"][0]["content"] == "customer: wen el parcel?"
    estimate = pipeline.estimate_cost([body] * 1000, input_per_million=Decimal("0.10"),
                                      output_per_million=Decimal("0.50"), max_output_tokens=400,
                                      batch_factor=Decimal("0.5"))
    assert estimate.requests == 1000 and estimate.output_tokens == 400_000
    assert Decimal("0.1") < estimate.usd < Decimal("1")


def _output(custom_id, data):
    return json.dumps({"custom_id": custom_id, "response": {"body": {"output": [
        {"content": [{"type": "output_text", "text": json.dumps(data)}]}]}}})


def test_batch_output_parsing_and_taxonomy() -> None:
    base = {"other_intents": [], "language_style": "en", "data_needed": ["tracking_number"],
            "resolution_type": "data_lookup", "needed_human": False, "policy_statements": ["Delivery takes 2-3 days"]}
    lines = [
        _output("ticket-1", {**base, "primary_intent": "Tracking Status"}),
        _output("ticket-2", {**base, "primary_intent": "where_is_my_parcel"}),
        _output("ticket-3", {**base, "primary_intent": "refund_request", "needed_human": True,
                             "policy_statements": ["Refunds above <AMOUNT_1> need a manager"]}),
        _output("ticket-4", {**base, "primary_intent": "spam"}),
        json.dumps({"custom_id": "ticket-5", "error": {"message": "failed"}}),
    ]
    results, failed = pipeline.parse_batch_output(lines)
    assert failed == 1 and [r["primary_intent"] for r in results][:2] == ["tracking_status", "where_is_my_parcel"]
    taxonomy = pipeline.build_taxonomy(results, mapping={"where_is_my_parcel": "tracking_status", "spam": "drop"})
    assert [(t["intent_key"], t["tickets"]) for t in taxonomy] == [("tracking_status", 2), ("refund_request", 1)]
    assert taxonomy[0]["labels_merged"] == ["tracking_status", "where_is_my_parcel"]
    assert taxonomy[1]["needed_human_percent"] == 100.0
    text, ids = pipeline.draft_input(taxonomy[0], results, mapping={"where_is_my_parcel": "tracking_status"})
    assert ids == [1, 2] and "(2x) Delivery takes 2-3 days" in text


def test_classify_dry_run_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    spec = importlib.util.spec_from_file_location("kb_pipeline", Path("scripts/kb_pipeline.py"))
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    (tmp_path / "transcripts.jsonl").write_text(json.dumps({"ticket_id": 1, "messages": [{"role": "customer", "text": "hi"}]}) + "\n")

    async def price(_model):
        return types.SimpleNamespace(input_per_million=Decimal("0.1"), output_per_million=Decimal("0.5"),
                                     batch_factor=Decimal("0.5"))

    monkeypatch.setattr(script, "_price", price)
    monkeypatch.setitem(sys.modules, "app.ai_provider", None)  # any OpenAI client use would fail
    args = types.SimpleNamespace(dir=tmp_path, limit=0, model="gpt-6-luna", submit=False, yes=False)
    asyncio.run(script.cmd_classify(args))
    out = capsys.readouterr().out
    assert "Dry run: nothing was sent" in out and '"requests": 1' in out
    assert (tmp_path / "classify_requests.jsonl").stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit, match="--yes"):
        asyncio.run(script.cmd_classify(types.SimpleNamespace(**{**vars(args), "submit": True})))
