"""Read-only, aggregate Redis shutdown gate. Never clear keys or extend TTLs."""
from __future__ import annotations

import argparse
import json
import time


PATTERNS = {
    "pending": "conversation:pending-inbound:*",
    "inflight": "conversation:inflight-inbound:*",
    "leases": "conversation:processing:*",
}


def inspect(client) -> dict:
    result = {"pending": 0, "inflight": 0, "leases": 0, "invalid": 0}
    for kind, pattern in PATTERNS.items():
        for key in set(client.scan_iter(match=pattern, count=100)):
            key_type = client.type(key)
            if isinstance(key_type, bytes):
                key_type = key_type.decode("ascii")
            if key_type == "none":
                continue
            if kind == "leases":
                result[kind] += 1
                result["invalid"] += int(key_type != "string")
            elif key_type != "list":
                result["invalid"] += 1
            else:
                result[kind] += client.llen(key)
    return result


def require_drained(client, stable_seconds=10, *, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + stable_seconds
    while True:
        result = inspect(client)
        if any(result.values()):
            # Fail immediately. Waiting for a 15-minute legacy queue/lease to
            # expire is NOT evidence of successful processing.
            raise RuntimeError("INBOUND_DRAIN_UNRESOLVED")
        if clock() >= deadline:
            return result
        sleep(min(1, max(0, deadline - clock())))


def main():
    import redis
    parser = argparse.ArgumentParser(description=__doc__)
    address = parser.add_mutually_exclusive_group(required=True)
    address.add_argument("--socket")
    address.add_argument("--port", type=int)
    parser.add_argument("--stable-seconds", type=int, default=10)
    parser.add_argument("--inspect-only", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.stable_seconds <= 60:
        parser.error("Stable interval must be between 0 and 60 seconds")
    client = redis.Redis(unix_socket_path=args.socket, socket_timeout=5) if args.socket else redis.Redis(
        host="127.0.0.1", port=args.port, socket_timeout=5)
    try:
        result = inspect(client) if args.inspect_only else require_drained(client, args.stable_seconds)
        print(json.dumps({"drained": not any(result.values()), **result}))
    except Exception:
        print('{"drained":false,"error":"INBOUND_DRAIN_UNRESOLVED_OR_UNAVAILABLE"}')
        raise SystemExit(1) from None
    finally:
        client.close()


if __name__ == "__main__":
    main()
