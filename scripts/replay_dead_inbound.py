"""List or replay inbound messages parked in the dead-letter lists.

A message is parked only after repeated failures while MariaDB and Redis were
healthy. Listing is read-only and prints metadata only, never message text.
Replaying requires ``--confirm``; it moves the parked messages of ONE customer
queue (identified by its hashed key) back to the front of that queue, marked so
that they are routed to human support without AI. The API's inbound sweeper
then processes them.

    venv/bin/python scripts/replay_dead_inbound.py --socket /run/redis/redis-server.sock
    venv/bin/python scripts/replay_dead_inbound.py --socket ... --replay <digest> --confirm
"""
from __future__ import annotations

import argparse
import json
import re

DEAD_PREFIX = "conversation:dead-inbound:"
PENDING_PREFIX = "conversation:pending-inbound:"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_METADATA_FIELDS = (
    "message_id", "message_type", "attempts", "last_failure_status",
    "dead_letter_reason", "dead_lettered_at", "first_failed_at", "enqueued_at",
)

_REPLAY_SCRIPT = """
if redis.call('LREM', KEYS[1], 1, ARGV[1]) == 0 then return 0 end
redis.call('LPUSH', KEYS[2], ARGV[2]); redis.call('PERSIST', KEYS[2])
return 1
"""


def _text(value) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, (bytes, bytearray)) else str(value)


def _decode(raw) -> dict | None:
    try:
        item = json.loads(_text(raw))
    except (TypeError, ValueError):
        return None
    return item if isinstance(item, dict) else None


def list_dead_letters(client) -> list[dict]:
    queues = []
    for key in sorted({_text(k) for k in client.scan_iter(match=DEAD_PREFIX + "*", count=200)}):
        items = []
        for raw in client.lrange(key, 0, -1):
            item = _decode(raw)
            items.append(
                {field: item.get(field) for field in _METADATA_FIELDS if field in item}
                if item is not None else {"unreadable": True}
            )
        queues.append({"digest": key[len(DEAD_PREFIX):], "count": len(items), "items": items})
    return queues


def replay(client, digest: str) -> int:
    if not _DIGEST_RE.match(digest):
        raise SystemExit("Digest must be the 64-character hex suffix shown by the listing.")
    dead_key, pending_key = DEAD_PREFIX + digest, PENDING_PREFIX + digest
    moved = 0
    # Reverse so the original order is preserved at the front of the queue.
    for raw in reversed(client.lrange(dead_key, 0, -1)):
        item = _decode(raw)
        if item is None:
            continue  # unreadable payloads stay parked for manual inspection
        for field in ("dead_lettered_at", "dead_letter_reason", "retry_after", "first_failed_at"):
            item.pop(field, None)
        item["attempts"] = 0
        item["recovered"] = True  # human support only, never AI
        payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        moved += int(client.eval(_REPLAY_SCRIPT, 2, dead_key, pending_key, raw, payload) or 0)
    return moved


def main() -> None:
    import redis

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    address = parser.add_mutually_exclusive_group(required=True)
    address.add_argument("--socket")
    address.add_argument("--port", type=int)
    parser.add_argument("--replay", metavar="DIGEST", help="replay one customer queue's parked messages")
    parser.add_argument("--confirm", action="store_true", help="required with --replay")
    args = parser.parse_args()
    if args.replay and not args.confirm:
        parser.error("--replay changes Redis state; add --confirm after reviewing the listing")
    client = redis.Redis(unix_socket_path=args.socket, socket_timeout=5) if args.socket else redis.Redis(
        host="127.0.0.1", port=args.port, socket_timeout=5)
    try:
        if args.replay:
            print(json.dumps({"replayed": replay(client, args.replay), "digest": args.replay}))
        else:
            print(json.dumps({"dead_letter_queues": list_dead_letters(client)}, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
