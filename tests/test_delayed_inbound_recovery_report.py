"""Recovery-report selection logic, with sanitized fixtures only (no services)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.delayed_inbound_recovery_report import (
    classify, describe_source, parse_journal, read_journal_store, summarize,
)

JOURNAL = """\
2026-10-29T13:17:12+0100 vmi python[1]: INFO:shipment-bot:Incoming WhatsApp message: customer_ref=wa_x message_id=ld:90015:AAA type=text text_chars=10
2026-10-29T13:17:12+0100 vmi python[1]: WARNING:shipment-bot:Stale inbound WhatsApp message ignored: customer_ref=wa_x message_id=ld:90015:AAA reason=expired timestamp=1761740880 last_timestamp=1761740000
2026-10-29T13:19:34+0100 vmi python[1]: INFO:shipment-bot:Incoming WhatsApp message: customer_ref=wa_x message_id=ld:90015:BBB type=image text_chars=0
2026-10-29T13:19:34+0100 vmi python[1]: WARNING:shipment-bot:Stale inbound WhatsApp message ignored: customer_ref=wa_x message_id=ld:90015:BBB reason=expired timestamp=1761741000 last_timestamp=None
2026-10-29T13:25:00+0100 vmi python[1]: WARNING:shipment-bot:Stale inbound WhatsApp message ignored: customer_ref=wa_x message_id=ld:90015:BBB reason=expired timestamp=1761741000 last_timestamp=None
2026-10-29T13:26:00+0100 vmi python[1]: WARNING:shipment-bot:Stale inbound WhatsApp message ignored: customer_ref=wa_x message_id=ld:90015:CCC reason=recent_duplicate timestamp=1761741100 last_timestamp=None
2026-10-29T13:27:00+0100 vmi python[1]: WARNING:shipment-bot:Stale inbound WhatsApp message ignored: customer_ref=wa_y message_id=wamid.CLOUD reason=expired timestamp=1761741200 last_timestamp=None
"""


def test_journal_parsing_counts_unique_messages_and_repeated_deliveries():
    candidates = parse_journal(JOURNAL.splitlines())
    assert sorted(candidates) == ["ld:90015:AAA", "ld:90015:BBB", "wamid.CLOUD"]  # recent_duplicate excluded
    assert [str(value) for value in candidates["ld:90015:BBB"].arrivals] == [
        "2026-10-29 12:19:34+00:00", "2026-10-29 12:25:00+00:00"]
    assert candidates["ld:90015:BBB"].logged_type == "image"
    assert candidates["ld:90015:AAA"].sent_ts == 1761740880
    assert (candidates["ld:90015:AAA"].line_id, candidates["ld:90015:AAA"].native_id) == ("90015", "AAA")
    assert candidates["wamid.CLOUD"].line_id is None


def _journal_line(seq: int, ops: list[dict], *, corrupt: bool = False) -> str:
    record = json.dumps({"version": 1, "seq": seq, "ops": ops})
    checksum = hashlib.sha256(record.encode()).hexdigest()
    return json.dumps({"record": record, "checksum": "0" * 64 if corrupt else checksum}) + "\n"


def test_journal_store_reader_is_read_only_and_matches_bridge_semantics(tmp_path: Path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"messages": {"OLD": {"phone": "1"}}, "media": {}, "list": [1, 2, 3]}))
    journal = Path(str(state) + ".journal")
    journal.write_text(
        _journal_line(1, [{"path": ["messages", "NEW"], "value": {"phone": "96170123456", "notified": True}}])
        + _journal_line(2, [{"path": ["messages", "OLD"], "remove": True}, {"path": ["list", "1"], "value": 9}])
        + _journal_line(3, [{"path": ["list", "length"], "value": 2}])
        + '{"record": "incomplete'  # torn final write: ignored, never truncated
    )
    before = (state.read_bytes(), journal.read_bytes())
    data = read_journal_store(state)
    assert data == {"messages": {"NEW": {"phone": "96170123456", "notified": True}}, "media": {}, "list": [1, 9]}
    assert (state.read_bytes(), journal.read_bytes()) == before
    journal.write_text(_journal_line(1, [], corrupt=True))
    with pytest.raises(ValueError, match="CHECKSUM"):
        read_journal_store(state)
    journal.write_text(_journal_line(1, []) + _journal_line(3, []))
    with pytest.raises(ValueError, match="SEQUENCE"):
        read_journal_store(state)


def _store():
    item = lambda kind, **extra: {"message": {"key": {"id": "x", "fromMe": False}, "message": {kind: {}}},
                                  "phone": "96170123456", "timestamp": 1761740880000, "notified": True, **extra}
    return {
        "messages": {
            "TEXT": item("conversation"),
            "IMG": item("imageMessage", media_id="ldmedia:90015:1111"),
            "IMG-NOREF": item("imageMessage"),
            "GONE": item("conversation", deleted=True),
            "ODD": item("pollCreationMessage"),
            "REACT": {"message": {"key": {"id": "r", "fromMe": True}, "message": {"reactionMessage": {"key": {"id": "TEXT"}, "text": "👍"}}}},
        },
        "media": {"ldmedia:90015:1111": {"downloaded": True}},
    }


def test_source_description_contains_no_content_and_finds_dependent_reactions(tmp_path: Path):
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "1111.bin").write_bytes(b"jpeg")
    store = _store()
    text = describe_source(store, "TEXT", tmp_path)
    assert text["kind"] == "text" and text["dependent_reactions"] == 1 and "message" not in text
    image = describe_source(store, "IMG", tmp_path)
    assert image["media_reference"] and image["media_downloaded"] and image["media_file_present"]
    assert describe_source(store, "MISSING", tmp_path) == {"present": False}


@pytest.mark.parametrize("native_id, existing, matches, expected", [
    ("TEXT", None, [{"id": 7}], "confident"),
    ("TEXT", {"id": 1, "ticket_id": 7}, [{"id": 7}], "already_present"),
    ("TEXT", None, [], "no_ticket"),
    ("TEXT", None, [{"id": 7}, {"id": 8}], "ambiguous"),
    ("IMG", None, [{"id": 7}], "confident"),
    ("IMG-NOREF", None, [{"id": 7}], "unrecoverable"),
    ("GONE", None, [{"id": 7}], "excluded"),
    ("ODD", None, [{"id": 7}], "excluded"),
    ("MISSING", None, [{"id": 7}], "unrecoverable"),
])
def test_classification(tmp_path: Path, native_id, existing, matches, expected):
    source = describe_source(_store(), native_id, tmp_path)
    assert classify(source, existing, matches)[0] == expected


def test_summary_has_counts_but_no_identifiers():
    row = {"category": "confident", "kind": "text", "delivery_attempts_logged": 2, "delay_seconds": 1700,
           "sent": {"beirut": "2026-10-29T14:48:00+02:00"}, "source": {"dependent_reactions": 1},
           "proposed_ticket_now": {"active": False, "same_as_open_at_arrival": False},
           "message_id": "ld:90015:AAA", "proposed_ticket_id": 7}
    summary = summarize([row])
    assert summary["unique_messages"] == 1 and summary["logged_drops"] == 2
    assert summary["confident_targets_closed_now"] == 1 and summary["dependent_reactions"] == 1
    assert "ld:90015" not in json.dumps(summary)
