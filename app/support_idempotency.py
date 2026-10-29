"""Payload identity is independent of transport IDs and scoped by repository keys."""
import hashlib
import json


def send_fingerprint(kind: str, **fields) -> str:
    serialized = json.dumps(
        {'kind': kind, **fields}, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def verify_send_fingerprint(row: dict, fingerprint: str | None) -> None:
    if fingerprint is not None and row.get('request_fingerprint') != fingerprint:
        from app.support_repository import SupportConflictError
        raise SupportConflictError('This message ID belongs to another request or a legacy request whose payload cannot be verified. Check the original message.')
