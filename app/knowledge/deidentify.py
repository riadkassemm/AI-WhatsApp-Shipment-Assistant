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
# Amounts written without a currency, with thousands separators: 4,000,000 / 1.500.000.
_GROUPED_NUMBER = re.compile(r"(?<![\w<.,])\d{1,3}(?:[,.]\d{3})+(?![\w>])")
# Any other standalone 3-6 digit number (short IDs, codes, amounts) unless it is
# clearly a weight, size, duration or percentage.
_UNIT = r"(?:kg|kgs|kilos?|g|gr|grams?|cm|mm|m|days?|hours?|hrs?|h|min|mins|minutes?|weeks?|%|كيلو|كغ|غ|يوم|أيام|ايام|ساعة|ساعات|سم)"
_NUMBER = re.compile(
    rf"(?<![\w<])(?<!\d[.,:/])\d{{3,6}}(?![\w>])(?![.,:/]\d)(?!\s?{_UNIT}\b)", re.IGNORECASE,
)
# Street addresses: from an address word to the end of the clause.
_ADDRESS_WORDS = (
    r"street|st\.|road|rd\.|avenue|ave\.|highway|building|bldg|bldg\.|floor|apartment|apt|block|near|next to|"
    r"behind|opposite|facing|cheri3|chere3|share3|sharee3|bineye|binaye|benaye|tabe2|ta2|2orb|hadd|"
    r"شارع|بناية|بنايه|مبنى|طابق|الطابق|حي|قرب|بجانب|مقابل|خلف|جنب"
)
# Up to three capitalised or Arabic-script words before the address word are part
# of it ("Hamra street", "Mar Elias road").
_ADDRESS = re.compile(
    rf"(?:(?:[A-Z][\w'-]*|[\u0600-\u06FF]+)\s+){{0,3}}(?<!\w)(?i:{_ADDRESS_WORDS})(?!\w)[^.,;!?\n]{{0,60}}",
)
# A name the writer introduces: "my name is X", "ana X", "esme X", "اسمي X".
_NAME_INTRO = re.compile(
    r"(?<!\w)(?:my name is|my name's|name:|this is|i am|i'm|ana|esme|esmi|ismi|اسمي|انا|أنا|معك|mr\.?|mrs\.?|ms\.?|miss)"
    r"\s+((?:[A-Z][a-zA-Z'-]{1,20}|[\u0600-\u06FF]{2,20})(?:\s+(?:[A-Z][a-zA-Z'-]{1,20}|[\u0600-\u06FF]{2,20}))?)",
)


def name_pattern(names) -> re.Pattern | None:
    """One compiled pattern for many known names (accounts, contacts, staff), longest first."""
    cleaned = sorted({" ".join(str(n).split()) for n in names if n and len(" ".join(str(n).split())) >= 3},
                     key=len, reverse=True)
    if not cleaned:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in cleaned) + r")(?!\w)", re.IGNORECASE)
_MEDIA = re.compile(r"^\[[^\]]{1,60}\]$")


@dataclass
class Deidentifier:
    """One transcript's placeholder mapping. Create a new one per ticket."""

    names: list[str] = field(default_factory=list)
    # A shared pattern from name_pattern() for large name lists (built once per export).
    known_names: re.Pattern | None = None
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
        for pattern in (name_pattern(self.names), self.known_names):
            if pattern is not None:
                body = pattern.sub(lambda m: self._placeholder("NAME", m.group(0)), body)
        body = _NAME_INTRO.sub(
            lambda m: m.group(0)[: m.start(1) - m.start(0)] + self._placeholder("NAME", m.group(1)), body,
        )
        body = _ADDRESS.sub(lambda m: self._placeholder("ADDRESS", m.group(0)), body)
        body = _PHONE.sub(lambda m: self._placeholder("PHONE", m.group(0)), body)
        body = _IDENTIFIER.sub(lambda m: self._placeholder("ID", m.group(0)), body)
        body = _GROUPED_NUMBER.sub(lambda m: self._placeholder("AMOUNT", m.group(0)), body)
        body = _NUMBER.sub(lambda m: self._placeholder("NUMBER", m.group(0)), body)
        return body


def deidentify_transcript(messages: list[dict], *, names: list[str] | None = None) -> list[dict]:
    """De-identify ``[{"role": ..., "text": ...}]``; roles and order are kept."""
    tool = Deidentifier(names=list(names or []))
    return [{**message, "text": tool.text(message.get("text"))} for message in messages]
