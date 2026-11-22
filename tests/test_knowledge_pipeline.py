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


@pytest.fixture
def import_cli(tmp_path, monkeypatch):
    from app import ai_knowledge

    spec = importlib.util.spec_from_file_location("kb_import_pipeline", Path("scripts/kb_pipeline.py"))
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    calls = []

    class DraftService:
        async def create_tool_draft(self, article, **source):
            calls.append((article, source))
            return len(calls)

    monkeypatch.setattr(ai_knowledge, "ai_knowledge_service", DraftService())
    monkeypatch.setitem(sys.modules, "app.ai_provider", None)
    monkeypatch.setitem(sys.modules, "app.ai_usage", None)
    row = {"intent_key": "tracking_status", "title": "Tracking status",
           "approved_policy": "[STAFF TO CONFIRM: when to request the tracking number]",
           "data_needed": "Tracking number", "escalate_when": "Missing tracking data",
           "examples": [{"style": "en", "customer": "Where is my parcel?", "reply": "Let me check."}],
           "observed_practice": "Agents checked the warehouse.",
           "source_ticket_count": 35, "source_ticket_ids": [1, 2, 3]}
    args = types.SimpleNamespace(dir=tmp_path, submit=False, yes=False)
    return script, row, args, calls


def _draft_file(args, *rows):
    (args.dir / "drafts.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def test_import_drafts_defaults_to_validation_without_database_or_provider(import_cli, monkeypatch, capsys):
    from app.ai_knowledge import AIKnowledgeRepository

    script, row, args, calls = import_cli
    _draft_file(args, row)

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Dry run must not open a database connection")

    monkeypatch.setattr(AIKnowledgeRepository, "connect", forbidden)
    monkeypatch.setattr(sys, "argv", ["kb_pipeline.py", "import-drafts", "--dir", str(args.dir)])
    assert script.main() == 0
    assert calls == []
    assert '"drafts_validated": 1' in capsys.readouterr().out
    args.submit = True
    with pytest.raises(SystemExit, match="--yes"):
        asyncio.run(script.cmd_import_drafts(args))
    assert calls == []


def test_import_drafts_passes_validated_articles_and_provenance_only(import_cli, capsys):
    script, row, args, calls = import_cli
    _draft_file(args, row)
    args.submit = args.yes = True
    asyncio.run(script.cmd_import_drafts(args))
    assert len(calls) == 1
    article, source = calls[0]
    assert article == {k: v for k, v in row.items() if not k.startswith("source_")}
    assert source == {"source_ticket_count": 35, "source_ticket_ids": [1, 2, 3]}
    assert '"drafts_created": 1' in capsys.readouterr().out


@pytest.mark.parametrize("change", [
    {"intent_key": "bad key"}, {"approved_policy": ""}, {"status": "APPROVED"},
    {"examples": [{"style": "invalid", "customer": "a", "reply": "b"}]},
    {"source_ticket_count": True}, {"source_ticket_count": 0}, {"source_ticket_count": "3"},
    {"source_ticket_count": 2}, {"source_ticket_ids": []}, {"source_ticket_ids": [1, 1]},
    {"source_ticket_ids": [True]}, {"source_ticket_ids": [0]}, {"source_ticket_ids": ["1"]},
    {"source_ticket_ids": None},
])
def test_import_drafts_preflights_all_rows_before_any_write(import_cli, change):
    script, row, args, calls = import_cli
    _draft_file(args, row, {**row, "intent_key": "shipping_rates", **change})
    args.submit = args.yes = True
    with pytest.raises(SystemExit, match="line 2"):
        asyncio.run(script.cmd_import_drafts(args))
    assert calls == []


def test_import_drafts_rejects_duplicate_normalized_topics(import_cli):
    script, row, args, calls = import_cli
    _draft_file(args, row, {**row, "intent_key": " TRACKING_STATUS "})
    args.submit = args.yes = True
    with pytest.raises(SystemExit, match="line 2"):
        asyncio.run(script.cmd_import_drafts(args))
    assert calls == []


@pytest.mark.parametrize("content", ["", "\n  \n", "[]", "null", '{"title": "private sentinel",'])
def test_import_drafts_rejects_bad_files_without_echoing_input(import_cli, content, capsys):
    script, _row, args, calls = import_cli
    (args.dir / "drafts.jsonl").write_text(content)
    args.submit = args.yes = True
    with pytest.raises(SystemExit) as error:
        asyncio.run(script.cmd_import_drafts(args))
    assert "private sentinel" not in str(error.value)
    assert "private sentinel" not in capsys.readouterr().out
    assert calls == []


def test_import_drafts_missing_and_non_utf8_files_fail_safely(import_cli):
    script, _row, args, calls = import_cli
    for content in (None, b"\xff"):
        if content:
            (args.dir / "drafts.jsonl").write_bytes(content)
        with pytest.raises(SystemExit, match="Cannot read"):
            asyncio.run(script.cmd_import_drafts(args))
    assert calls == []


def test_import_drafts_skips_conflicts_without_overwriting_staff_work(import_cli, monkeypatch, capsys):
    from app import ai_knowledge

    script, row, args, calls = import_cli
    _draft_file(args, row, {**row, "intent_key": "shipping_rates"})

    async def create(article, **source):
        if article["intent_key"] == "tracking_status":
            raise ai_knowledge.AIKnowledgeConflictError("private sentinel")
        calls.append((article, source))

    monkeypatch.setattr(ai_knowledge.ai_knowledge_service, "create_tool_draft", create)
    args.submit = args.yes = True
    asyncio.run(script.cmd_import_drafts(args))
    assert [article["intent_key"] for article, _ in calls] == ["shipping_rates"]
    out = capsys.readouterr().out
    assert '"drafts_created": 1, "drafts_skipped": 1' in out
    assert "private sentinel" not in out


def test_import_drafts_reports_partial_failure_without_database_payload(import_cli, monkeypatch):
    from app import ai_knowledge

    script, row, args, calls = import_cli
    _draft_file(args, row, {**row, "intent_key": "shipping_rates"}, {**row, "intent_key": "delivery_time"})

    async def create(article, **source):
        if calls:
            raise RuntimeError("private database payload")
        calls.append((article, source))

    monkeypatch.setattr(ai_knowledge.ai_knowledge_service, "create_tool_draft", create)
    args.submit = args.yes = True
    with pytest.raises(SystemExit, match="1 created and 0 skipped") as error:
        asyncio.run(script.cmd_import_drafts(args))
    assert "private database payload" not in str(error.value)
    assert len(calls) == 1
