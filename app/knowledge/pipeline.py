"""Offline knowledge-building pipeline (plan B2, steps 3-5 and 7).

1. export    Resolved customer tickets, de-identified (app.knowledge.deidentify),
             split into a classification set and a newer held-out gold set
             (the gold set is never classified or used for drafting; it is for
             the Step 5 benchmark).
2. classify  One OpenAI Batch API request per ticket (structured output: topic,
             style, data needed, resolution, whether staff were needed, general
             policy statements). Batch pricing is 50% off.
3. taxonomy  Counts per topic label, for staff to review and merge.
4. draft     One drafting call per kept topic, from aggregated de-identified
             policy statements only. Results become DRAFT knowledge articles
             (never approved automatically).

Only de-identified text leaves our infrastructure (owner decision 2026-11-22).
Nothing in this module calls OpenAI by itself; scripts/kb_pipeline.py does, only
with explicit flags, and every billable call goes through ai_usage metering.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Iterable

from app.knowledge.deidentify import Deidentifier

STYLES = ["en", "fr", "ar", "leb_ar", "leb_arabizi", "mixed"]
DATA_NEEDED = ["none", "tracking_number", "order_id", "account", "wallet", "payment", "address",
               "shipment_details", "rates", "other"]
RESOLUTIONS = ["answered_info", "data_lookup", "action_by_staff", "escalated_internal", "no_resolution",
               "customer_abandoned", "other"]
CLASSIFY_MAX_OUTPUT_TOKENS = 400
DRAFT_MAX_OUTPUT_TOKENS = 1600

_MEDIA_LABELS = {
    "image": "[image]", "video": "[video]", "document": "[document]", "location": "[location]",
    "sticker": "[sticker]", "contacts": "[shared contact]", "unsupported": "[unsupported message]",
}
_ROLES = {"CUSTOMER": "customer", "AGENT": "agent", "AI": "assistant", "SYSTEM": "system"}
_LABEL = re.compile(r"[^a-z0-9]+")


def normalize_label(value: str | None) -> str:
    label = _LABEL.sub("_", str(value or "").strip().lower()).strip("_")[:64]
    return label or "other"


def _message_text(row: dict[str, Any]) -> str:
    kind = str(row.get("message_type") or "text")
    if kind == "audio":
        return "[voice note]" if str(row.get("body") or "") == "Voice note" else "[audio]"
    if kind in _MEDIA_LABELS:
        return _MEDIA_LABELS[kind]
    return str(row.get("body") or "")


def build_transcript(ticket: dict[str, Any], messages: list[dict[str, Any]], *, names: list[str],
                     max_messages: int = 40, max_chars: int = 6000) -> dict[str, Any]:
    """One de-identified ticket record. Keeps the first and the latest messages within the limits."""
    tool = Deidentifier(names=[*names, str(ticket.get("customer_name") or "")])
    rows = [m for m in messages if str(m.get("message_type") or "text") != "reaction"]
    if len(rows) > max_messages:
        rows = rows[:4] + rows[-(max_messages - 4):]
    transcript = []
    used = 0
    for row in rows:
        text = tool.text(_message_text(row)).strip()
        if not text:
            continue
        text = text[:800]
        if used + len(text) > max_chars:
            transcript.append({"role": "system", "text": "[conversation shortened]"})
            break
        used += len(text)
        transcript.append({"role": _ROLES.get(str(row.get("sender_type") or ""), "system"), "text": text})
    return {
        "ticket_id": int(ticket["id"]),
        "style": ticket.get("communication_style") or None,
        "messages": transcript,
    }


async def export_transcripts(
    fetch: Callable[[str, tuple], Awaitable[list[dict[str, Any]]]], *, gold_count: int = 400,
    max_messages: int = 40, max_chars: int = 6000, chunk: int = 500,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(classification set, gold set) from resolved customer tickets, oldest first.

    ``fetch`` runs one read-only SELECT. The newest ``gold_count`` tickets are
    the held-out gold set.
    """
    names = [str(row["name"]) for row in await fetch("SELECT name FROM staff WHERE name IS NOT NULL", ())]
    tickets = await fetch(
        """
        SELECT id, created_at, communication_style, customer_name FROM support_tickets
        WHERE COALESCE(conversation_origin, 'CUSTOMER') <> 'STAFF' AND deleted_at IS NULL
          AND status IN ('RESOLVED', 'CLOSED')
        ORDER BY created_at, id
        """, (),
    )
    records = []
    for start in range(0, len(tickets), chunk):
        part = tickets[start:start + chunk]
        placeholders = ",".join(["%s"] * len(part))
        rows = await fetch(
            f"""
            SELECT ticket_id, sender_type, direction, message_type, body FROM support_ticket_messages
            WHERE ticket_id IN ({placeholders}) AND deleted_for_everyone_at IS NULL
            ORDER BY ticket_id, id
            """, tuple(int(t["id"]) for t in part),
        )
        by_ticket: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_ticket[int(row["ticket_id"])].append(row)
        for ticket in part:
            record = build_transcript(ticket, by_ticket.get(int(ticket["id"]), []), names=names,
                                      max_messages=max_messages, max_chars=max_chars)
            if any(m["role"] == "customer" for m in record["messages"]):
                records.append(record)
    split = max(0, len(records) - max(0, gold_count))
    return records[:split], records[split:]


CLASSIFY_INSTRUCTIONS = """You label historical customer-support conversations of a Lebanese shipping company
(WhatsApp). Personal data was replaced with placeholders such as <PHONE_1> or <AMOUNT_1>; never try to
recover it. Return only the requested JSON.
- primary_intent: a short snake_case label for the customer's main need, reusable across many
  conversations (for example tracking_status, shipping_rates, lost_parcel, customs_fees, refund_request,
  change_address, delivery_time, account_login, wallet_balance). Prefer general labels over specific ones.
- other_intents: further needs in the same conversation (may be empty).
- language_style: how the customer writes.
- data_needed: what the agent needed to answer.
- resolution_type: how the conversation ended.
- needed_human: true if answering required a person (a decision, an exception, a change, a complaint).
- policy_statements: up to 5 general rules or facts the agent stated (for example "Delivery to Beirut takes
  2-3 working days"). Keep placeholders as they are; leave out anything specific to this one customer."""


def classification_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["primary_intent", "other_intents", "language_style", "data_needed", "resolution_type",
                     "needed_human", "policy_statements"],
        "properties": {
            "primary_intent": {"type": "string"},
            "other_intents": {"type": "array", "items": {"type": "string"}},
            "language_style": {"type": "string", "enum": STYLES},
            "data_needed": {"type": "array", "items": {"type": "string", "enum": DATA_NEEDED}},
            "resolution_type": {"type": "string", "enum": RESOLUTIONS},
            "needed_human": {"type": "boolean"},
            "policy_statements": {"type": "array", "items": {"type": "string"}},
        },
    }


def classification_request(record: dict[str, Any], *, model: str) -> dict[str, Any]:
    """One Batch API line (/v1/responses) for one de-identified ticket."""
    conversation = "\n".join(f"{m['role']}: {m['text']}" for m in record["messages"])
    return {
        "custom_id": f"ticket-{record['ticket_id']}",
        "method": "POST",
        "url": "/v1/responses",
        "body": {
            "model": model,
            "instructions": CLASSIFY_INSTRUCTIONS,
            "input": [{"role": "user", "content": conversation}],
            "reasoning": {"effort": "none"},
            "text": {"format": {"type": "json_schema", "name": "ticket_classification", "strict": True,
                                "schema": classification_schema()}},
            "max_output_tokens": CLASSIFY_MAX_OUTPUT_TOKENS,
            "store": False,
        },
    }


@dataclass(frozen=True)
class CostEstimate:
    requests: int
    input_tokens: int
    output_tokens: int
    usd: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {"requests": self.requests, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "estimated_usd": str(self.usd.quantize(Decimal("0.01")))}


def estimate_cost(bodies: Iterable[dict[str, Any]], *, input_per_million: Decimal, output_per_million: Decimal,
                  max_output_tokens: int, batch_factor: Decimal = Decimal("1")) -> CostEstimate:
    """Worst-case estimate: about four characters per token, full output allowance."""
    requests = input_tokens = 0
    for body in bodies:
        requests += 1
        input_tokens += (len(json.dumps(body, ensure_ascii=False)) + 3) // 4
    output_tokens = requests * max_output_tokens
    usd = (Decimal(input_tokens) * input_per_million + Decimal(output_tokens) * output_per_million) / Decimal(1_000_000)
    return CostEstimate(requests, input_tokens, output_tokens, usd * batch_factor)


def parse_batch_output(lines: Iterable[str]) -> tuple[list[dict[str, Any]], int]:
    """(classifications, failed count) from a Batch API output file."""
    results, failed = [], 0
    for line in lines:
        if not line.strip():
            continue
        item = json.loads(line)
        body = ((item.get("response") or {}).get("body")) or {}
        text = body.get("output_text")
        if text is None:
            for output in body.get("output") or []:
                for content in output.get("content") or []:
                    if content.get("type") == "output_text":
                        text = content.get("text")
        try:
            data = json.loads(text or "")
            ticket_id = int(str(item.get("custom_id") or "").removeprefix("ticket-"))
        except (TypeError, ValueError):
            failed += 1
            continue
        data["ticket_id"] = ticket_id
        data["primary_intent"] = normalize_label(data.get("primary_intent"))
        data["other_intents"] = [normalize_label(label) for label in data.get("other_intents") or []]
        results.append(data)
    return results, failed


def build_taxonomy(classifications: list[dict[str, Any]], *, mapping: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Topic counts for staff review. ``mapping`` merges labels (label -> topic key; "drop" removes)."""
    mapping = {normalize_label(k): normalize_label(v) for k, v in (mapping or {}).items()}
    total = len(classifications) or 1
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in classifications:
        topic = mapping.get(item["primary_intent"], item["primary_intent"])
        if topic != "drop":
            groups[topic].append(item)
    rows = []
    for topic, items in groups.items():
        data = Counter(value for item in items for value in item.get("data_needed") or [] if value != "none")
        rows.append({
            "intent_key": topic,
            "tickets": len(items),
            "share_percent": round(len(items) * 100 / total, 1),
            "needed_human_percent": round(sum(1 for i in items if i.get("needed_human")) * 100 / len(items), 1),
            "top_data_needed": [name for name, _count in data.most_common(3)],
            "resolutions": dict(Counter(i.get("resolution_type") for i in items).most_common()),
            "labels_merged": sorted({i["primary_intent"] for i in items}),
        })
    return sorted(rows, key=lambda row: (-row["tickets"], row["intent_key"]))


DRAFT_INSTRUCTIONS = """You draft one short knowledge-base article for a customer-support AI of a Lebanese shipping
company. You get aggregated, de-identified facts from many past conversations about one topic. Staff will review
and correct the draft before the AI may use it.
- approved_policy: what the assistant should tell the customer and do, as clear rules. Use only what the
  statements support. Where statements disagree or a fact is missing, write [STAFF TO CONFIRM: ...] instead of
  guessing. Keep placeholders such as <AMOUNT_1> visible so staff replace them.
- data_needed: what to ask the customer for (short).
- escalate_when: when to hand over to staff.
- examples: up to 3 short example exchanges in different writing styles (en, ar, leb_arabizi, ...), generic.
- observed_practice: a short neutral summary of what agents actually did, including inconsistencies."""


def draft_schema() -> dict[str, Any]:
    example = {
        "type": "object", "additionalProperties": False, "required": ["style", "customer", "reply"],
        "properties": {"style": {"type": "string", "enum": STYLES}, "customer": {"type": "string"},
                       "reply": {"type": "string"}},
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["title", "approved_policy", "data_needed", "escalate_when", "examples", "observed_practice"],
        "properties": {
            "title": {"type": "string"}, "approved_policy": {"type": "string"}, "data_needed": {"type": "string"},
            "escalate_when": {"type": "string"}, "examples": {"type": "array", "items": example},
            "observed_practice": {"type": "string"},
        },
    }


def draft_input(topic: dict[str, Any], classifications: list[dict[str, Any]], *,
                mapping: dict[str, str] | None = None, max_statements: int = 60) -> tuple[str, list[int]]:
    """(drafting prompt input, source ticket IDs) for one taxonomy topic."""
    mapping = {normalize_label(k): normalize_label(v) for k, v in (mapping or {}).items()}
    items = [c for c in classifications if mapping.get(c["primary_intent"], c["primary_intent"]) == topic["intent_key"]]
    statements = Counter(
        " ".join(str(s).split())[:300] for item in items for s in item.get("policy_statements") or [] if str(s).strip()
    )
    lines = [
        f"Topic: {topic['intent_key']} ({topic['tickets']} conversations, {topic['share_percent']}% of all).",
        f"Needed a person: {topic['needed_human_percent']}% of these conversations.",
        f"Data usually needed: {', '.join(topic['top_data_needed']) or 'none recorded'}.",
        f"How they ended: {json.dumps(topic['resolutions'])}.",
        "Agent statements (with how many conversations contained each):",
        *[f"- ({count}x) {text}" for text, count in statements.most_common(max_statements)],
    ]
    return "\n".join(lines), [int(item["ticket_id"]) for item in items]
