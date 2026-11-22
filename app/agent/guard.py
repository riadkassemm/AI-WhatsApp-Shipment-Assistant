"""Conservative lexical grounding, in addition to the shared customer reply guard.

This checks exact factual tokens, not semantic truth. Customer text, previous AI
replies, and model-declared facts never become authoritative evidence.
"""
from __future__ import annotations

import re
import unicodedata

from app.agent.contracts import Reply
from app.agent.context import _PASSWORD
from app.client_chat_auth import redact_sensitive_text
from app.customer_reply_guard import validate_customer_reply

_TOKEN = re.compile(r"[\w]+(?:[.,:/-][\w]+)*", re.UNICODE)
_ARABIZI_WORDS = frozenset("mar7aba ahla wsa7la shu chou kifak kifek 7abibi 7abibti ma3ak ma3ik ba3d ba3den 3andak 3andik 3ande 3am 3al 3ala fiya fiye 7a ra7 2abel 2abl 2oul 2ele 3afwan 3anjad 7elo 7elwe".split())
_DATES = frozenset((
    "today tomorrow yesterday monday tuesday wednesday thursday friday saturday sunday "
    "january february march april may june july august september october november december "
    "aujourd'hui demain hier lundi mardi mercredi jeudi vendredi samedi dimanche "
    "janvier février mars avril mai juin juillet août septembre octobre novembre décembre "
    "اليوم غدا غداً بكرا مبارح الاثنين الثلاثاء الأربعاء الخميس الجمعة السبت الأحد "
    "يناير فبراير مارس أبريل مايو يونيو يوليو أغسطس سبتمبر أكتوبر نوفمبر ديسمبر"
).split())
_MONEY_WORDS = re.compile(
    r"\b(?:[\w-]+\s+){1,4}(?:dollars?|euros?|USD|LBP|دولار|ليرة)\b", re.I,
)
_CURRENCY = r"(?:US\$|\$|€|USD|EUR|LBP|AED|SAR|dollars?|euros?|دولار|ليرة)"
_AMOUNT = r"[-+]?\d[\d,.]*(?:\s?[kKmM])?"
_MONEY = re.compile(rf"{_CURRENCY}\s*{_AMOUNT}|{_AMOUNT}\s*{_CURRENCY}", re.I)
_LABELLED_ID = re.compile(
    r"\b(?:order|shipment|tracking|account|user)(?:\s+(?:id|number|code|reference|ref))?"
    r"\s*[:#=]\s*([\w-]+)", re.I,
)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def factual_tokens(text: str) -> set[str]:
    normalized = _normalize(text)
    tokens = set()
    for match in _TOKEN.finditer(normalized):
        word = match.group()
        if (any(c.isdigit() for c in word) and word not in _ARABIZI_WORDS) or word in _DATES:
            tokens.add(word)
        # A credit and a debit with the same digits are different facts.
        if word[0].isdigit() and match.start() and normalized[match.start() - 1] in "-+":
            tokens.add(normalized[match.start() - 1:match.end()])
    tokens.update("id:" + match.group(1) for match in _LABELLED_ID.finditer(normalized))
    tokens.update("amount:" + re.sub(r"\s+", "", match.group()) for match in _MONEY.finditer(normalized))
    return tokens


def reply_violations(reply: Reply, *, style: str, evidence: tuple[str, ...]) -> tuple[str, ...]:
    reasons = list(validate_customer_reply(reply.text, style).reasons)
    if reply.language != style:
        reasons.append("language_mismatch")
    if redact_sensitive_text(reply.text) != reply.text:
        reasons.append("credential_disclosure")
    if _PASSWORD.search(reply.text):
        reasons.append("password_reference")
    # Do not let the model launder an invented value through facts_used.
    sources = tuple(_normalize(source) for source in evidence)
    if any(not fact.strip() or not any(_normalize(fact) in source for source in sources)
           for fact in reply.facts_used):
        reasons.append("unsupported_fact_reference")
    supported = set().union(*(factual_tokens(source) for source in evidence))
    if not factual_tokens(reply.text) <= supported:
        reasons.append("ungrounded_value")
    # Spelled-out money must also be quoted from an authoritative source.
    for match in _MONEY_WORDS.finditer(reply.text):
        if not any(c.isdigit() for c in match.group()) and not any(
            _normalize(match.group()) in source for source in sources
        ):
            reasons.append("ungrounded_amount")
    return tuple(dict.fromkeys(reasons))
