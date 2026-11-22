#!/usr/bin/env python3
"""Build the AI knowledge base from historical tickets (plan B2). Offline, run by a developer.

Only de-identified text is sent to OpenAI (owner decision 2026-11-22). Nothing is
sent unless a command is given --submit and --yes; every billable request goes
through the AI budget metering (purpose kb_build). Files are written with 0600
permissions to --dir, outside the public repository.

  # 1. Export de-identified transcripts (read-only database access):
  venv/bin/python scripts/kb_pipeline.py export --dir ~/kb
  # 2. See the cost, then submit the classification batch (Batch API, 50% off):
  venv/bin/python scripts/kb_pipeline.py classify --dir ~/kb
  venv/bin/python scripts/kb_pipeline.py classify --dir ~/kb --submit --yes
  # 3. Download the results when the batch is done (usually within hours):
  venv/bin/python scripts/kb_pipeline.py collect --dir ~/kb
  # 4. Topic counts for staff review; merge labels in mapping.json ({"label": "topic" or "drop"}):
  venv/bin/python scripts/kb_pipeline.py taxonomy --dir ~/kb [--mapping ~/kb/mapping.json]
  # 5. See the cost, then draft articles for topics with enough tickets; they appear as drafts in
  #    AI Assistant > Knowledge for staff to correct and approve:
  venv/bin/python scripts/kb_pipeline.py draft --dir ~/kb --min-tickets 30
  venv/bin/python scripts/kb_pipeline.py draft --dir ~/kb --min-tickets 30 --submit --yes
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge import pipeline  # noqa: E402

CLASSIFY_MODEL = "gpt-6-luna"
DRAFT_MODEL = "gpt-6.2-sol"


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.writelines(line + "\n" for line in lines)


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


async def _price(model: str):
    from app.ai_usage import ai_usage_service
    price = await ai_usage_service.price_for(model)
    if price is None:
        raise SystemExit(f"No verified price for {model}. Verify it in AI Assistant > Usage & Costs first.")
    return price


async def _db_fetch():
    import aiomysql
    from app.config import settings
    connection = await aiomysql.connect(
        host=settings.db_host, port=settings.db_port, user=settings.db_user, password=settings.db_password,
        db=settings.db_name, charset="utf8mb4", autocommit=False,
    )
    async with connection.cursor() as cur:
        await cur.execute("SET SESSION TRANSACTION READ ONLY")
        await cur.execute("START TRANSACTION READ ONLY")

    async def fetch(query: str, args: tuple):
        async with connection.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, args)
            return list(await cur.fetchall())
    return connection, fetch


async def cmd_export(args) -> None:
    connection, fetch = await _db_fetch()
    try:
        training, gold = await pipeline.export_transcripts(fetch, gold_count=args.gold)
    finally:
        await connection.rollback()
        connection.close()
    _write(args.dir / "transcripts.jsonl", [json.dumps(r, ensure_ascii=False) for r in training])
    _write(args.dir / "gold.jsonl", [json.dumps(r, ensure_ascii=False) for r in gold])
    print(json.dumps({"classification_set": len(training), "gold_set": len(gold), "dir": str(args.dir)}))


async def cmd_classify(args) -> None:
    records = _read_jsonl(args.dir / "transcripts.jsonl")
    if args.limit:
        records = records[: args.limit]
    requests = [pipeline.classification_request(r, model=args.model) for r in records]
    price = await _price(args.model)
    estimate = pipeline.estimate_cost(
        (r["body"] for r in requests), input_per_million=price.input_per_million,
        output_per_million=price.output_per_million, max_output_tokens=pipeline.CLASSIFY_MAX_OUTPUT_TOKENS,
        batch_factor=price.batch_factor,
    )
    _write(args.dir / "classify_requests.jsonl", [json.dumps(r, ensure_ascii=False) for r in requests])
    print(json.dumps({"model": args.model, **estimate.to_dict()}))
    if not args.submit:
        print("Dry run: nothing was sent. Add --submit --yes to create the batch.")
        return
    if not args.yes:
        raise SystemExit("Refusing to submit without --yes.")
    from app.ai_provider import ai_provider_service
    from app.ai_usage import ai_usage_service, usage_scope
    client = await ai_provider_service.get_client("kb_build")
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests).encode("utf-8")

    async def submit():
        uploaded = await client.files.create(file=("classify_requests.jsonl", io.BytesIO(payload)), purpose="batch")
        return await client.batches.create(input_file_id=uploaded.id, endpoint="/v1/responses",
                                           completion_window="24h")

    token = usage_scope.set({"batch_id": "kb-classify"})
    try:
        # The batch is charged at its full estimate when submitted (no usage comes back
        # with the submission), so spending can never be under-counted.
        batch = await ai_usage_service.metered(
            purpose="kb_build", tier="batch", model=args.model, call=submit,
            input_payload=[r["body"] for r in requests],
            max_output_tokens=pipeline.CLASSIFY_MAX_OUTPUT_TOKENS * len(requests), batch=True,
        )
    finally:
        usage_scope.reset(token)
    _write(args.dir / "batch.json", [json.dumps({"batch_id": batch.id, "model": args.model})])
    print(json.dumps({"batch_id": batch.id, "status": batch.status}))


async def cmd_collect(args) -> None:
    from app.ai_provider import ai_provider_service
    batch_id = json.loads((args.dir / "batch.json").read_text())["batch_id"]
    client = await ai_provider_service.get_client("kb_build")
    batch = await client.batches.retrieve(batch_id)
    if batch.status != "completed":
        print(json.dumps({"batch_id": batch_id, "status": batch.status}))
        return
    content = await client.files.content(batch.output_file_id)
    raw = content.text if hasattr(content, "text") else content.read().decode("utf-8")
    results, failed = pipeline.parse_batch_output(raw.splitlines())
    _write(args.dir / "classifications.jsonl", [json.dumps(r, ensure_ascii=False) for r in results])
    print(json.dumps({"classified": len(results), "failed": failed}))


def _mapping(args) -> dict:
    return json.loads(Path(args.mapping).read_text()) if args.mapping else {}


async def cmd_taxonomy(args) -> None:
    taxonomy = pipeline.build_taxonomy(_read_jsonl(args.dir / "classifications.jsonl"), mapping=_mapping(args))
    _write(args.dir / "taxonomy.json", [json.dumps(taxonomy, ensure_ascii=False, indent=2)])
    for row in taxonomy[: args.top]:
        print(f"{row['tickets']:6d}  {row['share_percent']:5.1f}%  human {row['needed_human_percent']:5.1f}%  "
              f"{row['intent_key']}  (merged: {', '.join(row['labels_merged'])})")
    print(f"{len(taxonomy)} topics. Written to {args.dir / 'taxonomy.json'}")


async def cmd_draft(args) -> None:
    classifications = _read_jsonl(args.dir / "classifications.jsonl")
    mapping = _mapping(args)
    topics = [t for t in pipeline.build_taxonomy(classifications, mapping=mapping)
              if t["tickets"] >= args.min_tickets and t["intent_key"] != "other"]
    inputs = [(topic, *pipeline.draft_input(topic, classifications, mapping=mapping)) for topic in topics]
    price = await _price(args.model)
    estimate = pipeline.estimate_cost(
        ({"instructions": pipeline.DRAFT_INSTRUCTIONS, "input": text} for _t, text, _ids in inputs),
        input_per_million=price.input_per_million, output_per_million=price.output_per_million,
        max_output_tokens=pipeline.DRAFT_MAX_OUTPUT_TOKENS,
    )
    print(json.dumps({"model": args.model, "topics": [t["intent_key"] for t in topics], **estimate.to_dict()}))
    if not args.submit:
        print("Dry run: nothing was sent. Add --submit --yes to draft these articles.")
        return
    if not args.yes:
        raise SystemExit("Refusing to submit without --yes.")
    from app.ai_knowledge import AIKnowledgeConflictError, ai_knowledge_service
    from app.ai_provider import ai_provider_service
    from app.ai_usage import ai_usage_service
    client = await ai_provider_service.get_client("kb_build")
    created = 0
    for topic, text, ticket_ids in inputs:
        response = await ai_usage_service.metered(
            purpose="kb_build", tier="strong", model=args.model,
            input_payload=[pipeline.DRAFT_INSTRUCTIONS, text], max_output_tokens=pipeline.DRAFT_MAX_OUTPUT_TOKENS,
            call=lambda text=text: client.responses.create(
                model=args.model, instructions=pipeline.DRAFT_INSTRUCTIONS,
                input=[{"role": "user", "content": text}], reasoning={"effort": "low"},
                text={"format": {"type": "json_schema", "name": "kb_article", "strict": True,
                                 "schema": pipeline.draft_schema()}},
                max_output_tokens=pipeline.DRAFT_MAX_OUTPUT_TOKENS, store=False,
            ),
        )
        article = json.loads(response.output_text)
        try:
            await ai_knowledge_service.create_tool_draft(
                {**article, "intent_key": topic["intent_key"]},
                source_ticket_count=topic["tickets"], source_ticket_ids=ticket_ids,
            )
            created += 1
        except AIKnowledgeConflictError:
            print(f"Skipped {topic['intent_key']}: it already has a draft.")
    print(json.dumps({"drafts_created": created}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    default_dir = Path.home() / ".local/share/whatsapp_chatbot/kb"
    for name in ("export", "classify", "collect", "taxonomy", "draft"):
        command = sub.add_parser(name)
        command.add_argument("--dir", type=Path, default=default_dir)
        if name == "export":
            command.add_argument("--gold", type=int, default=400, help="newest tickets kept out as the gold set")
        if name in ("classify", "draft"):
            command.add_argument("--model", default=CLASSIFY_MODEL if name == "classify" else DRAFT_MODEL)
            command.add_argument("--submit", action="store_true")
            command.add_argument("--yes", action="store_true")
        if name == "classify":
            command.add_argument("--limit", type=int, default=0, help="only the first N tickets (trial run)")
        if name in ("taxonomy", "draft"):
            command.add_argument("--mapping", default=None)
        if name == "taxonomy":
            command.add_argument("--top", type=int, default=60)
        if name == "draft":
            command.add_argument("--min-tickets", type=int, default=30)
    args = parser.parse_args()
    args.dir = args.dir.expanduser()
    asyncio.run(globals()["cmd_" + args.command](args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
