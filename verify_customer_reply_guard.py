from __future__ import annotations

import os
import re
import sys
from types import ModuleType, SimpleNamespace

# The verifier performs no network/database work. Make it runnable in a lightweight
# validation environment while leaving real production packages untouched when present.
os.environ["REDIS_URL"] = ""

try:
    import aiomysql  # type: ignore
except ModuleNotFoundError:
    aiomysql = ModuleType("aiomysql")
    aiomysql.Pool = object  # type: ignore[attr-defined]
    aiomysql.DictCursor = object  # type: ignore[attr-defined]
    sys.modules["aiomysql"] = aiomysql

try:
    import openai  # type: ignore
except ModuleNotFoundError:
    openai = ModuleType("openai")
if not hasattr(openai, "AsyncOpenAI"):
    class _AsyncOpenAI:
        def __init__(self, *_args, **_kwargs) -> None:
            pass
    openai.AsyncOpenAI = _AsyncOpenAI  # type: ignore[attr-defined]
sys.modules["openai"] = openai

from app.customer_reply_guard import render_customer_shipments, validate_customer_reply
from app.openai_service import _text_from_response


INCIDENT_TEXT = (
    'L2et 10 orders. 2elle ayya wa7ad بدك? Wait Arabic forbidden! '
    'Need all Latin. "baddak". list. Need no Arabic. Include identifiers. '
    'Maybe order IDs.'
)


validation = validate_customer_reply(INCIDENT_TEXT, "leb_arabizi")
assert not validation.safe
assert "arabic_script_in_arabizi" in validation.reasons
assert "internal_note_leak" in validation.reasons

response = SimpleNamespace(
    output_text=INCIDENT_TEXT + "\nunsafe aggregate",
    output=[
        SimpleNamespace(
            type="message",
            phase="commentary",
            content=[SimpleNamespace(text=INCIDENT_TEXT)],
        ),
        SimpleNamespace(
            type="message",
            phase="final_answer",
            content=[SimpleNamespace(text="L2et 2 recent orders.")],
        ),
    ],
)
assert _text_from_response(response) == "L2et 2 recent orders."

rendered = render_customer_shipments(
    {
        "found": True,
        "count": 2,
        "shipments": [
            {
                "order_id": "ORD-77",
                "tracking_number": "TRK-77",
                "shipment_id": "SHP-77",
                "status": "IN_TRANSIT",
            },
            {
                "order_id": "ORD-88",
                "tracking_number": "TRK-88",
                "shipment_id": "SHP-88",
                "status": "RECEIVED",
            },
        ],
    },
    "leb_arabizi",
)
assert rendered
assert "ORD-77" in rendered and "TRK-88" in rendered and "SHP-88" in rendered
assert not re.search(r"[\u0600-\u06FF]", rendered)
assert validate_customer_reply(rendered, "leb_arabizi").safe

print("OK: customer reply phase filtering, internal-note blocking, and Arabizi order rendering are configured.")
