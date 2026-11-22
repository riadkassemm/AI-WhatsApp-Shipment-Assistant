"""Deterministic de-identification of support transcripts (plan B2, step 1).

Runs inside our own infrastructure, before any historical text is used for
classification, knowledge drafting or evaluation. Nothing here calls a model.

Replaced with stable placeholders: passwords and other labelled secrets, email
addresses, URLs, phone numbers, known customer/contact names (passed in by the
caller), order/shipment/tracking style identifiers, and money amounts. Media
placeholders such as "[image]" are kept. The same value maps to the same
placeholder within one transcript (``<PHONE_1>`` twice means the same number),
so a classifier can still follow the conversation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.client_chat_auth import redact_sensitive_text

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
# Amount with a currency word/sign on either side (USD, $, LBP, L.L., ل.ل, دولار …).
_CURRENCY = r"(?:usd|\$|us\$|dollars?|lbp|l\.?l\.?|ل\.ل|ليرة|دولار|eur|€|aed|sar)"
_AMOUNT = re.compile(
    rf"(?:{_CURRENCY}\s?\d[\d,.]*(?:\s?[kK])?|\d[\d,.]*(?:\s?[kK])?\s?{_CURRENCY})",
    re.IGNORECASE,
)
# Phone numbers: optional +/00, 7-15 digits with common separators.
_PHONE = re.compile(r"(?<![\w<])(?:\+|00)?\d(?:[\s().-]?\d){6,14}(?![\w>])")
# Codes that mix letters and at least three digits (tracking numbers, order
# references, user IDs), or long digit runs that are not phone-like. Arabizi
# words use one or two digits as letters ("ba3atet", "7abibi") and are kept.
_IDENTIFIER = re.compile(
    r"(?<![\w<])(?=(?:[A-Za-z-]*\d){3})(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9-]{4,}(?![\w>])"
)
_LONG_NUMBER = re.compile(r"(?<![\w<])\d{5,6}(?![\w>])")
_MEDIA = re.compile(r"^\[[^\]]{1,60}\]$")


@dataclass
class Deidentifier:
    """One transcript's placeholder mapping. Create a new one per ticket."""

    names: list[str] = field(default_factory=list)
    _maps: dict[str, dict[str, str]] = field(default_factory=dict)

    def _placeholder(self, kind: str, value: str) -> str:
        mapping = self._maps.setdefault(kind, {})
        key = re.sub(r"\W", "", value).casefold() or value
        if kind == "PHONE":
            key = re.sub(r"^00", "", re.sub(r"\D", "", value))
        if key not in mapping:
            mapping[key] = f"<{kind}_{len(mapping) + 1}>"
        return mapping[key]

    def text(self, value: str | None) -> str:
        body = str(value or "")
        if not body.strip() or _MEDIA.match(body.strip()):
            return body
        body = re.sub(r"\*{3,}", "<SECRET>", redact_sensitive_text(body))
        for kind, pattern in (("EMAIL", _EMAIL), ("URL", _URL), ("AMOUNT", _AMOUNT)):
            body = pattern.sub(lambda m, k=kind: self._placeholder(k, m.group(0)), body)
        for name in sorted({n.strip() for n in self.names if n and len(n.strip()) >= 3}, key=len, reverse=True):
            body = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", lambda m: self._placeholder("NAME", m.group(0)),
                          body, flags=re.IGNORECASE)
        body = _PHONE.sub(lambda m: self._placeholder("PHONE", m.group(0)), body)
        body = _IDENTIFIER.sub(lambda m: self._placeholder("ID", m.group(0)), body)
        body = _LONG_NUMBER.sub(lambda m: self._placeholder("ID", m.group(0)), body)
        return body


def deidentify_transcript(messages: list[dict], *, names: list[str] | None = None) -> list[dict]:
    """De-identify ``[{"role": ..., "text": ...}]``; roles and order are kept."""
    tool = Deidentifier(names=list(names or []))
    return [{**message, "text": tool.text(message.get("text"))} for message in messages]
