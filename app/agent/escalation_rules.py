"""Deterministic must-escalate safety net for change requests and complaints.

The model alone missed many of these, especially in Arabizi, in the local
benchmark. These patterns run after the model's decision. When the customer's
latest messages clearly ask for a change or make a complaint, a reply or tool call
becomes a hand-off to staff. An escalation chosen by the model keeps its own reason.
Patterns are deliberately specific: false positives only hand a turn to staff.
"""
from __future__ import annotations

import re
import unicodedata

_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ـ": None})
_TASHKEEL = re.compile(r"[ً-ْٰ]")

_EN_FIELDS = r"(?:address|name|phone|number|mode|method|destination|delivery|recipient|receiver|order)"
_AR_FIELDS = r"(?:العنوان|عنوان|الاسم|اسم|الرقم|رقم|طريقه|طريقة|الشحن|جوي|بحري|المستلم|التسليم)"
_ARABIZI_FIELDS = r"(?:l?address|l?3enwen|l?3inwen|l?3enwan|l?esm|l?ism|l?ra2m|l?ra2em|number|l?mode|jawwe|jawwi|jawi|ba7re|ba7ri|air|sea)"
_ITEM = r"(?:shipment|parcel|package|order|delivery)"

CHANGE = re.compile("|".join((
    rf"\b(?:change|modify|edit)\b(?:\W+\w+){{0,5}}?\W+(?:the\s+|my\s+)?{_EN_FIELDS}\b",
    rf"\bupdate\s+(?:my|the)\s+(?:delivery\s+)?{_EN_FIELDS}\b",
    r"\bcancel(?:l?ed|l?ing|lation)?\b",
    r"\bswitch\b(?:\W+\w+){0,4}?\W+(?:to\s+)?(?:air|sea|land|express)\b",
    rf"\b(?:stop|hold)\b(?:\W+\w+){{0,2}}?\W+{_ITEM}\b",
    rf"\breturn\s+(?:my|the|this)\s+(?:{_ITEM}|item|items)\b",
    r"\b(?:changer|modifier)\b(?:\W+\w+){0,4}?\W+(?:l'|la\s+|le\s+|mon\s+|ma\s+)?(?:adresse|nom|num[ée]ro|t[ée]l[ée]phone|mode|livraison|destinataire)\b",
    r"\bannul(?:er|ez|ation)\b",
    rf"(?:غير|غيرو|غيرولي|بدل|بدلو|عدل|عدلو|تعديل|تغيير)\s+(?:\S+\s+){{0,3}}?{_AR_FIELDS}",
    r"\b(?:الغاء|الغي|الغو|الغيه|الغيها|كنسل|كنسلو)\b",
    r"(?:وقف|وقفو|وقفوا)\s+(?:\S+\s+){0,2}?(?:الشحنه|الطلب|الطرد)",
    rf"\b(?:ghayyer|ghayer|ghayir|8ayer|8ayyer|8ayyir|ghayro|ghayrouli|ghayrili|baddel|badel|bdel|3adel|3adlo|3addel)\b(?:\W+\w+){{0,4}}?\W+{_ARABIZI_FIELDS}\b",
    r"\b(?:kansel|kanslo|kansilo|lghe|elghe|ilghe|el8e|l8e|l8iya)\b",
    r"\b(?:wa2ef|wa22ef|w2ef|wa2fo|wa2efa|wa22fo)\b(?:\W+\w+){0,2}?\W+(?:l\s+)?(?:shi7na|she7ne|shi7ne|tard|order|talab)\b",
)), re.I)

COMPLAINT = re.compile("|".join((
    r"\b(?:broken|damaged|smashed|cracked)\b",
    r"\b(?:missing|wrong)\s+(?:item|items|piece|pieces|product|products|parcel|package)\b",
    r"\bcharged\b(?:\W+\w+){0,3}?\W+(?:too\s+much|extra|twice|more)\b",
    r"\b(?:overcharg\w*|refund|complain\w*|scam|fraud|stolen)\b",
    rf"\blost\s+(?:my\s+|the\s+)?{_ITEM}\b|\b{_ITEM}\s+(?:is|was|got)\s+lost\b",
    r"\b(?:cass[ée]e?|endommag[ée]e?|ab[iî]m[ée]e?|manquante?|remboursement|r[ée]clamation|plainte|perdue?)\b",
    r"(?:مكسور|مكسوره|متضرر|تالف|ناقص|ناقصه|مفقود|ضايع|ضايعه|شكوى|شكوي|استرداد|استرجاع المبلغ|نصب|حراميه)",
    r"\b(?:mkassar|mkassra|mkasra|mkassara|maksour|maksoura|na2es|na2sa|na2sin|da3et|day3a|day3in|daya3to|shakwa|shakwe|nasab|nasabin|7aramiye)\b",
    r"\bzyede\s+(?:bl|bel|b)\s*wazn\b",
)), re.I)


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _TASHKEEL.sub("", folded).translate(_ARABIC_FOLD)


def customer_burst(messages: list[dict]) -> list[str]:
    """Customer messages after the last assistant/staff message."""
    burst = []
    for message in reversed(messages):
        if message.get("role") != "user":
            break
        burst.append(message.get("text") or "")
    return list(reversed(burst))


def must_escalate_reason(texts: list[str]) -> str | None:
    text = _normalize(" \n ".join(texts))
    if COMPLAINT.search(text):
        return "COMPLAINT_DISPUTE"
    if CHANGE.search(text):
        return "CHANGE_REQUIRES_HUMAN"
    return None
