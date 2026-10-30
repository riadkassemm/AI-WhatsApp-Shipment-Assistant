"""Read-only recovery report for customer messages dropped as stale ("expired").

Before the delayed-inbound fix, a customer message that reached the API more
than ``WHATSAPP_MAX_INBOUND_AGE_SECONDS`` after it was sent was discarded, even
when a support conversation owned it. This report finds those messages and
proposes where each belongs. It never recovers anything:

- the API journal (``journalctl -o short-iso`` text) identifies dropped messages;
- the linked-device bridge store is read as files (no lock, no repair);
- the application database is queried through connections set READ ONLY.

The detailed manifest contains message/ticket identifiers (never bodies or full
phone numbers) and is written with mode 0600 to a path outside the repository.
Only identifier-free counts are printed.

    journalctl -u shipment-bot.service --since ... -o short-iso > journal.txt
    venv/bin/python scripts/delayed_inbound_recovery_report.py \\
        --journal journal.txt --bridge-data /var/lib/whatsapp-support/linked-device \\
        --manifest /root/recovery/delayed-inbound.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

BEIRUT = ZoneInfo("Asia/Beirut")
LINKED_DEVICE_ID = re.compile(r"ld:([0-9]{5,32}):([A-Za-z0-9_.-]+)")
_LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:[.,]\d+)?([+-]\d{2}:?\d{2}|Z)?")
_INCOMING = re.compile(r"Incoming WhatsApp message: .*?message_id=(\S+) type=(\S+)")
_STALE = re.compile(r"Stale inbound WhatsApp message ignored: .*?message_id=(\S+) reason=(\S+) timestamp=(\S+)")
# Message kinds the support timeline records from a customer.
RECORDABLE_KINDS = {
    "conversation": "text", "extendedTextMessage": "text", "imageMessage": "image",
    "videoMessage": "video", "audioMessage": "audio", "documentMessage": "document",
    "stickerMessage": "sticker", "locationMessage": "location",
    "contactMessage": "contacts", "contactsArrayMessage": "contacts",
}
MEDIA_KINDS = {"image", "video", "audio", "document", "sticker"}


@dataclass
class Candidate:
    message_id: str
    sent_ts: int | None
    arrivals: list[datetime] = field(default_factory=list)
    logged_type: str | None = None

    @property
    def line_id(self) -> str | None:
        match = LINKED_DEVICE_ID.fullmatch(self.message_id)
        return match.group(1) if match else None

    @property
    def native_id(self) -> str | None:
        match = LINKED_DEVICE_ID.fullmatch(self.message_id)
        return match.group(2) if match else None


def _log_time(line: str) -> datetime | None:
    match = _LOG_TIME.match(line)
    if not match:
        return None
    offset = (match.group(2) or "+00:00").replace("Z", "+00:00")
    if len(offset) == 5:  # +0100 -> +01:00 (Python 3.10 fromisoformat)
        offset = offset[:3] + ":" + offset[3:]
    return datetime.fromisoformat(match.group(1) + offset).astimezone(timezone.utc)


def parse_journal(lines: Iterable[str]) -> dict[str, Candidate]:
    """Unique messages dropped as ``expired``; repeated deliveries add arrivals."""
    types: dict[str, str] = {}
    candidates: dict[str, Candidate] = {}
    for line in lines:
        incoming = _INCOMING.search(line)
        if incoming:
            types[incoming.group(1)] = incoming.group(2)
            continue
        stale = _STALE.search(line)
        if not stale or stale.group(2) != "expired":
            continue
        message_id = stale.group(1)
        try:
            sent_ts = int(stale.group(3))
        except ValueError:
            sent_ts = None
        candidate = candidates.setdefault(message_id, Candidate(message_id, sent_ts))
        arrived = _log_time(line)
        if arrived is not None:
            candidate.arrivals.append(arrived)
        candidate.logged_type = types.get(message_id, candidate.logged_type)
    return candidates


def read_journal_store(filename: Path) -> dict[str, Any]:
    """Python equivalent of the bridge's readJournal(..., repairTail=false).

    Reads the snapshot (or legacy JSON) and applies complete, checksummed
    journal records in sequence. An incomplete final record is ignored, never
    truncated: this function opens files read-only.
    """
    seq = 0
    snapshot = Path(str(filename) + ".snapshot")
    if snapshot.exists():
        state = json.loads(snapshot.read_text())
        data, seq = state["data"], int(state["seq"])
    elif filename.exists():
        data = json.loads(filename.read_text())
    else:
        data = {}
    snapshot_seq = seq
    journal = Path(str(filename) + ".journal")
    if not journal.exists():
        return data
    raw = journal.read_bytes()
    complete = raw[: raw.rfind(b"\n") + 1]
    previous = None
    for line in complete.decode("utf-8").split("\n"):
        if not line:
            continue
        envelope = json.loads(line)
        if hashlib.sha256(envelope["record"].encode("utf-8")).hexdigest() != envelope["checksum"]:
            raise ValueError("JOURNAL_CHECKSUM_FAILED")
        record = json.loads(envelope["record"])
        if previous is not None and record["seq"] != previous + 1:
            raise ValueError("JOURNAL_SEQUENCE_GAP")
        previous = record["seq"]
        if record["seq"] <= snapshot_seq:
            continue
        if record["seq"] != seq + 1:
            raise ValueError("JOURNAL_SEQUENCE_GAP")
        for op in record["ops"]:
            parent = data
            for key in op["path"][:-1]:
                parent = parent[int(key)] if isinstance(parent, list) else parent[key]
            if op.get("remove"):
                if isinstance(parent, dict):
                    parent.pop(op["path"][-1], None)
            elif isinstance(parent, list) and op["path"][-1] == "length":
                del parent[int(op["value"]):]
            elif isinstance(parent, list):
                index = int(op["path"][-1])
                parent.extend([None] * (index + 1 - len(parent)))
                parent[index] = op.get("value")
            else:
                parent[op["path"][-1]] = op.get("value")
        seq = record["seq"]
    return data


def describe_source(store: dict[str, Any], native_id: str, line_dir: Path) -> dict[str, Any]:
    """What the bridge still holds for one message; no content is returned."""
    record = (store.get("messages") or {}).get(native_id)
    if not record:
        return {"present": False}
    content = (record.get("message") or {}).get("message") or {}
    raw_kind = next((key for key in content if key not in {"messageContextInfo", "senderKeyDistributionMessage"}), None)
    kind = RECORDABLE_KINDS.get(raw_kind or "", "unsupported")
    media_id = record.get("media_id")
    media = (store.get("media") or {}).get(media_id) if media_id else None
    media_file = None
    if media_id and ":" in str(media_id):
        media_file = line_dir / "media" / f"{str(media_id).rsplit(':', 1)[-1]}.bin"
    reactions = [
        key for key, other in (store.get("messages") or {}).items()
        if (((other.get("message") or {}).get("message") or {}).get("reactionMessage") or {}).get("key", {}).get("id") == native_id
    ]
    return {
        "present": True, "kind": kind, "raw_kind": raw_kind, "from_me": bool((record.get("message") or {}).get("key", {}).get("fromMe")),
        "deleted": bool(record.get("deleted")), "notified": bool(record.get("notified")),
        "phone": str(record.get("phone") or ""), "sent_ms": record.get("timestamp"),
        "media_reference": bool(media), "media_downloaded": bool(media and media.get("downloaded")),
        "media_file_present": bool(media_file and media_file.exists()),
        "dependent_reactions": len(reactions),
    }


def classify(source: dict[str, Any], existing: dict[str, Any] | None, matches: list[dict[str, Any]]) -> tuple[str, str]:
    """Recovery category for one dropped message (pure; covered by fixtures)."""
    if existing is not None:
        return "already_present", "a support message already has this provider ID"
    if not source.get("present"):
        return "unrecoverable", "source message is not retained (Cloud API, or absent from the bridge store)"
    if source.get("from_me"):
        return "excluded", "sent from the business phone, not a customer message"
    if source.get("deleted"):
        return "excluded", "the customer deleted the message for everyone"
    if source.get("kind") == "unsupported":
        return "excluded", "message kind is not recorded in support timelines"
    if source.get("kind") in MEDIA_KINDS and not source.get("media_reference"):
        return "unrecoverable", "media reference is not retained"
    if not matches:
        return "no_ticket", "no support conversation on this line was open when it was sent"
    if len(matches) > 1:
        return "ambiguous", "more than one conversation qualifies at the send time"
    return "confident", "exactly one conversation on this line was open when it was sent"


def _times(value: datetime | None) -> dict[str, str | None]:
    if value is None:
        return {"utc": None, "beirut": None}
    value = value.astimezone(timezone.utc)
    return {"utc": value.isoformat(timespec="seconds"), "beirut": value.astimezone(BEIRUT).isoformat(timespec="seconds")}


async def _read_only_repository():
    import aiomysql
    from app.config import settings
    from app.support_repository import SupportRepository

    repository = SupportRepository()
    # Every connection refuses writes at the server, whatever code runs on it.
    repository._pool = await aiomysql.create_pool(
        host=settings.db_host, port=settings.db_port, user=settings.db_user,
        password=settings.db_password, db=settings.db_name, minsize=1, maxsize=2,
        autocommit=True, charset="utf8mb4", init_command="SET SESSION TRANSACTION READ ONLY",
    )
    return repository


async def build_report(candidates: dict[str, Candidate], bridge_data: Path) -> list[dict[str, Any]]:
    repository = await _read_only_repository()
    stores: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for candidate in sorted(candidates.values(), key=lambda item: (item.sent_ts or 0, item.message_id)):
            line_id, native_id = candidate.line_id, candidate.native_id
            source: dict[str, Any] = {"present": False}
            if line_id and native_id:
                if line_id not in stores:
                    path = bridge_data / line_id / "state.json"
                    stores[line_id] = read_journal_store(path) if path.exists() or Path(str(path) + ".journal").exists() else {}
                source = describe_source(stores[line_id], native_id, bridge_data / line_id)
            existing = await repository.get_message_by_whatsapp_id(candidate.message_id)
            sent_at = (datetime.fromtimestamp(candidate.sent_ts, tz=timezone.utc).replace(tzinfo=None)
                       if candidate.sent_ts else None)
            arrival = min(candidate.arrivals) if candidate.arrivals else None
            phone = re.sub(r"\D", "", source.get("phone") or "")
            matches = (await repository.find_tickets_open_at_whatsapp_timestamp(phone, line_id, sent_at)
                       if phone and line_id and sent_at else [])
            at_arrival = (await repository.find_tickets_open_at_whatsapp_timestamp(
                phone, line_id, arrival.replace(tzinfo=None)) if phone and line_id and arrival else [])
            category, reason = classify(source, existing, matches)
            proposed = matches[0] if category == "confident" else None
            current = await repository.get_ticket(int(proposed["id"])) if proposed else None
            rows.append({
                "message_id": candidate.message_id, "line_id": line_id,
                "customer_phone_last4": phone[-4:] if phone else None,
                "kind": source.get("kind") or candidate.logged_type, "logged_type": candidate.logged_type,
                "sent": _times(sent_at.replace(tzinfo=timezone.utc) if sent_at else None),
                "api_arrivals": [_times(value) for value in sorted(candidate.arrivals)],
                "delivery_attempts_logged": len(candidate.arrivals),
                "delay_seconds": int((arrival - sent_at.replace(tzinfo=timezone.utc)).total_seconds()) if arrival and sent_at else None,
                "source": {key: value for key, value in source.items() if key != "phone"},
                "existing_support_message": (
                    {"message_id": existing["id"], "ticket_id": existing["ticket_id"]} if existing else None),
                "tickets_open_at_send": [int(ticket["id"]) for ticket in matches],
                "tickets_open_at_arrival": [int(ticket["id"]) for ticket in at_arrival],
                "proposed_ticket_id": int(proposed["id"]) if proposed else None,
                "proposed_ticket_now": ({
                    "status": current.get("status"), "active": current.get("active_key") is not None,
                    "same_as_open_at_arrival": int(proposed["id"]) in [int(t["id"]) for t in at_arrival],
                } if current else None),
                "category": category, "reason": reason,
                "report_generated": _times(now.replace(tzinfo=timezone.utc)),
            })
    finally:
        repository._pool.close()
        await repository._pool.wait_closed()
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    delays = [row["delay_seconds"] for row in rows if row["delay_seconds"] is not None]
    return {
        "unique_messages": len(rows),
        "logged_drops": sum(row["delivery_attempts_logged"] for row in rows),
        "by_category": dict(Counter(row["category"] for row in rows)),
        "by_day_beirut": dict(Counter((row["sent"]["beirut"] or "unknown")[:10] for row in rows)),
        "by_kind": dict(Counter(str(row["kind"]) for row in rows)),
        "confident_targets_closed_now": sum(1 for row in rows if row["proposed_ticket_now"] and not row["proposed_ticket_now"]["active"]),
        "confident_target_differs_from_arrival_ticket": sum(
            1 for row in rows if row["proposed_ticket_now"] and not row["proposed_ticket_now"]["same_as_open_at_arrival"]),
        "media_without_downloaded_file": sum(
            1 for row in rows if row["category"] == "confident" and row["kind"] in MEDIA_KINDS
            and not row["source"].get("media_file_present")),
        "dependent_reactions": sum(row["source"].get("dependent_reactions", 0) for row in rows),
        "delay_seconds": ({"min": min(delays), "median": int(statistics.median(delays)), "max": max(delays)} if delays else None),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--journal", required=True, help="journalctl -o short-iso text ('-' for stdin)")
    parser.add_argument("--bridge-data", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path, help="restricted output path outside the repository")
    args = parser.parse_args(argv)
    manifest = args.manifest.resolve()
    if Path(__file__).resolve().parents[1] in manifest.parents:
        parser.error("write the manifest outside the repository/release")
    lines = sys.stdin if args.journal == "-" else open(args.journal, encoding="utf-8", errors="replace")
    with lines:
        candidates = parse_journal(lines)
    rows = asyncio.run(build_report(candidates, args.bridge_data))
    summary = summarize(rows)
    manifest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({"summary": summary, "messages": rows}, handle, indent=2, default=str)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
