"""Backend-owned multilingual shipping-catalog coordination.

Every request related to ``shipping_db.destinations`` or
``shipping_db.shipping_rates`` follows one path:

    customer text (any supported style)
      -> deterministic extraction + structured English normalization
      -> explicit-current-turn fields merged with safe quote context
      -> live catalogue validation
      -> MariaDB lookup
      -> Decimal calculation
      -> deterministic response in the customer's current style

The generative customer-service path is not allowed to answer a recognized catalogue
request from stale conversation prose. This is especially important when a customer
names a new, unsupported destination: that explicit destination must replace the prior
route instead of falling back to an old quote.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import logging
import re
import time
from typing import Any

from app.client_chat_auth import normalize_decimal_digits
from app.config import settings
from app.conversation_store import Conversation
from app.customer_reply_guard import (
    render_shipping_duration,
    render_shipping_price,
    render_shipping_route_options,
    validate_customer_reply,
)
from app.shipment_client import ShipmentDBError, shipment_client
from app.shipping_intent_normalizer import (
    NormalizedShippingIntent,
    normalize_shipping_intent,
)


logger = logging.getLogger("shipment-bot")


@dataclass(frozen=True)
class ShippingQuoteOutcome:
    reply_text: str
    status: str


_SHIPPING_INTENT_RE = re.compile(
    r"(?:\bship(?:ping)?\b|\bfreight\b|\bquote\b|\brate\b|"
    r"\bexp[eé]di(?:er|tion)\b|\benvoy(?:er|ez)\b|\bcolis\b|\blivraison\b|"
    r"(?:^|\s)(?:esh7an|eshhan|she7n|she7ne|sh7n|tesh7an|nsh7an)(?:\s|$)|"
    r"شحن|اشحن|أشحن|شحني|شحنة|الشحنة)",
    re.IGNORECASE,
)
_PRICE_INTENT_RE = re.compile(
    r"(?:\bprice\b|\bcost\b|\bhow\s+much\b|\bquote\b|\brate\b|"
    r"\bcombien\b|\bprix\b|\btarif\b|\bco[uû]te\b|"
    r"سعر|السعر|كلفة|الكلفة|تكلف|بتكلف|قديش|كم|"
    r"\badesh\b|\baddeh\b|\b2adde\b|\b2addesh\b|\bse3er\b|"
    r"\bbetkallef\b|\bbtkallef\b|\bteklefe\b)",
    re.IGNORECASE,
)
_TRANSIT_INTENT_RE = re.compile(
    r"(?:\btransit\b|\bduration\b|\bhow\s+long\b|\bwhen\b|"
    r"\bcombien\s+de\s+temps\b|\bd[eé]lai\b|\bdur[eé]e\b|"
    r"مدة|المدة|وقت|الوقت|قديش\s+بتاخد|امتى|إمتى|"
    r"\b2adde\s+wa2et\b|\badesh\s+wa2et\b|\bemta\b|\bimta\b)",
    re.IGNORECASE,
)
_OPTIONS_INTENT_RE = re.compile(
    r"(?:\bwhat\s+can\s+i\s+ship\b|\bcan\s+i\s+ship\b|"
    r"\bwhere\s+can\s+i\s+ship\b|\bavailable\s+(?:route|destination|service)s?\b|"
    r"\bdestinations?\b|\bqu['’]?est[- ]ce\s+que\s+je\s+peux\s+exp[eé]dier\b|"
    r"\bpuis[- ]je\s+exp[eé]dier\b|\bdestinations?\s+disponibles?\b|"
    r"شو\s+فيني\s+اشحن|شو\s+فيني\s+إشحن|فيني\s+اشحن|فيني\s+إشحن|"
    r"وين\s+فيني\s+اشحن|المتاح|الوجهات|"
    r"\bfine\s+esh7an\b|\bwen\s+fine\s+esh7an\b|\bshu\s+fine\s+esh7an\b)",
    re.IGNORECASE,
)
_FOLLOWUP_RE = re.compile(
    r"(?:\bwhat\s+about\b|\bhow\s+about\b|\band\s+if\b|"
    r"\bif\s+(?:it\s+is\s+)?from\b|\bet\s+si\b|\bsinon\b|"
    r"\beza\b|\biza\b|\btyb\b|\btayeb\b|طيب|طب|واذا|وإذا|إذا|اذا)",
    re.IGNORECASE,
)

_WEIGHT_WITH_UNIT_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>"
    r"kg|kgs|kilograms?|kilogrammes?|kilo(?:s)?|كيلو(?:غرام)?|كغ|"
    r"g|grams?|grammes?|غرام|غ|"
    r"lb|lbs|pounds?|livres?|"
    r"ton(?:s)?|tonnes?|طن"
    r")\b",
    re.IGNORECASE,
)
_BARE_NUMBER_RE = re.compile(r"^\s*(?P<value>\d+(?:[.,]\d+)?)\s*$")

_ALLOWED_STATE_KEYS = {
    "origin",
    "destination",
    "goods_type",
    "shipping_method",
    "shipping_method_explicit",
    "weight_kg",
}


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def extract_weight_kg(text: str | None, *, allow_bare_number: bool) -> str | None:
    """Extract a positive weight and normalize it to kilograms.

    A bare number is accepted only while a quote is waiting for weight. This keeps an
    unrelated order/tracking number from becoming a shipment quantity.
    """
    value = normalize_decimal_digits(str(text or ""))
    value = value.replace("٫", ".").replace("٬", "")
    match = _WEIGHT_WITH_UNIT_RE.search(value)
    unit = None
    if match is None and allow_bare_number:
        match = _BARE_NUMBER_RE.fullmatch(value)
    if match is None:
        return None

    raw_number = match.group("value").replace(",", ".")
    try:
        amount = Decimal(raw_number)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None

    if "unit" in match.groupdict():
        unit = str(match.groupdict().get("unit") or "").casefold()
    if unit in {"g", "gram", "grams", "gramme", "grammes", "غرام", "غ"}:
        amount /= Decimal("1000")
    elif unit in {"lb", "lbs", "pound", "pounds", "livre", "livres"}:
        amount *= Decimal("0.45359237")
    elif unit in {"ton", "tons", "tonne", "tonnes", "طن"}:
        amount *= Decimal("1000")

    amount = amount.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    if amount > Decimal("1000000"):
        return None
    return _decimal_text(amount)


def _safe_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    clean: dict[str, Any] = {}
    for key in _ALLOWED_STATE_KEYS:
        item = value.get(key)
        if key == "shipping_method_explicit":
            clean[key] = bool(item)
        elif item is not None:
            rendered = str(item).strip()
            if rendered:
                clean[key] = rendered[:191]
    return clean


def _context_is_fresh(updated_at: float | None) -> bool:
    if updated_at is None:
        # Backwards compatibility during a rolling deployment.
        return True
    ttl = max(60, int(settings.shipping_quote_context_ttl_seconds))
    age = time.time() - float(updated_at)
    return 0 <= age <= ttl


def _mark_pending(conversation: Conversation, slots: dict[str, Any]) -> None:
    conversation.pending_shipping_quote = _safe_state(slots)
    conversation.pending_shipping_quote_updated_at = time.time()


def _clear_pending(conversation: Conversation) -> None:
    conversation.pending_shipping_quote = None
    conversation.pending_shipping_quote_updated_at = None


def _mark_last(conversation: Conversation, slots: dict[str, Any]) -> None:
    conversation.last_shipping_quote = _safe_state(slots)
    conversation.last_shipping_quote_updated_at = time.time()


def _missing_prompt(missing: str, style: str | None) -> str:
    messages = {
        "weight_kg": {
            "fr": "Quel est le poids de l’envoi en kg ?",
            "ar": "ما وزن الشحنة بالكيلوغرام؟",
            "leb_ar": "قديش وزن الشحنة بالكيلو؟",
            "leb_arabizi": "2adde wazna bel kg?",
            "mixed": "2adde l weight bel kg?",
            "en": "What is the shipment weight in kg?",
        },
        "origin": {
            "fr": "Depuis quel pays souhaitez-vous expédier ?",
            "ar": "من أي بلد تريد الشحن؟",
            "leb_ar": "من أي بلد بدك تشحن؟",
            "leb_arabizi": "Mn ayya balad baddak tesh7an?",
            "mixed": "Mn ayya country baddak ship?",
            "en": "Which country are you shipping from?",
        },
        "destination": {
            "fr": "Vers quel pays souhaitez-vous expédier ?",
            "ar": "إلى أي بلد تريد الشحن؟",
            "leb_ar": "على أي بلد بدك تشحن؟",
            "leb_arabizi": "3a ayya balad baddak tesh7an?",
            "mixed": "3a ayya country baddak ship?",
            "en": "Which country are you shipping to?",
        },
        "goods_type": {
            "fr": "Quel type de marchandise souhaitez-vous expédier ?",
            "ar": "ما نوع الأغراض التي تريد شحنها؟",
            "leb_ar": "شو نوع الأغراض اللي بدك تشحنها؟",
            "leb_arabizi": "Shu naw3 l aghrad li baddak tesh7anon?",
            "mixed": "Shu type l goods li baddak ship?",
            "en": "What type of goods are you shipping?",
        },
    }
    group = messages.get(missing, messages["goods_type"])
    return group.get(style or "en", group["en"])


def _validate_template(text: str, style: str | None) -> str:
    validation = validate_customer_reply(text, style)
    if validation.safe:
        return text
    return {
        "fr": "Veuillez fournir l’information manquante sur l’envoi.",
        "ar": "يرجى تزويدي بالمعلومة الناقصة عن الشحنة.",
        "leb_ar": "بعثلي المعلومة الناقصة عن الشحنة.",
        "leb_arabizi": "B3atle l ma3loume l na2sa 3an l she7ne.",
        "mixed": "B3atle the missing shipment info.",
        "en": "Please provide the missing shipment information.",
    }.get(style or "en", "Please provide the missing shipment information.")


def _local_request_kind(
    text: str,
    *,
    explicit_slots: dict[str, Any],
    weight: str | None,
    has_context: bool,
    has_pending: bool,
    has_price_context: bool,
) -> str:
    if _PRICE_INTENT_RE.search(text):
        return "price_quote"
    if _TRANSIT_INTENT_RE.search(text):
        return "transit_time"
    if _OPTIONS_INTENT_RE.search(text):
        return "route_options"
    if (
        has_price_context
        and _FOLLOWUP_RE.search(text)
        and any(
            explicit_slots.get(key)
            for key in ("origin", "destination", "goods_type", "shipping_method")
        )
    ):
        # "tyb eza mn el su3udiye" after a completed priced quote changes the route
        # while retaining the known weight/category; it is not a generic route-listing
        # request merely because the customer repeated the verb "ship".
        return "price_quote"
    if _SHIPPING_INTENT_RE.search(text):
        return "price_quote" if weight else "route_options"
    if has_pending and weight:
        return "price_quote"
    if has_context and (weight or any(explicit_slots.get(key) for key in (
        "origin", "destination", "goods_type", "shipping_method"
    ))):
        # A short route/category/weight fragment after a completed quote continues that
        # quote unless the current turn explicitly asks for options or transit time.
        return "price_quote"
    return "none"


def _candidate_catalog_turn(
    text: str,
    *,
    local_kind: str,
    explicit_slots: dict[str, Any],
    weight: str | None,
    has_context: bool,
) -> bool:
    if local_kind != "none":
        return True
    if _SHIPPING_INTENT_RE.search(text) or _PRICE_INTENT_RE.search(text):
        return True
    return has_context and bool(
        weight
        or _FOLLOWUP_RE.search(text)
        or any(
            explicit_slots.get(key)
            for key in ("origin", "destination", "goods_type", "shipping_method")
        )
    )


def _is_followup(
    text: str,
    *,
    has_context: bool,
    explicit_slots: dict[str, Any],
    semantic: NormalizedShippingIntent | None,
    has_pending: bool,
    weight: str | None,
) -> bool:
    if has_pending and weight:
        return True
    if not has_context:
        return False
    if semantic is not None and semantic.is_followup_fragment:
        return True
    if _FOLLOWUP_RE.search(text):
        return True
    return len(text.split()) <= 10 and any(
        explicit_slots.get(key)
        for key in ("origin", "destination", "goods_type", "shipping_method")
    )


def _merge_explicit_slots(
    local: dict[str, Any],
    semantic: NormalizedShippingIntent | None,
    local_weight: str | None,
) -> tuple[dict[str, Any], set[str]]:
    slots: dict[str, Any] = {}
    explicit: set[str] = set()

    for key in ("origin", "destination", "goods_type", "shipping_method"):
        value = local.get(key)
        if value:
            slots[key] = str(value)
            explicit.add(key)

    if local_weight is not None:
        slots["weight_kg"] = local_weight
        explicit.add("weight_kg")

    if semantic is not None:
        semantic_fields = (
            ("origin", semantic.origin, semantic.explicit_origin),
            ("destination", semantic.destination, semantic.explicit_destination),
            ("goods_type", semantic.goods_type, semantic.explicit_goods_type),
            (
                "shipping_method",
                semantic.shipping_method,
                semantic.explicit_shipping_method,
            ),
            ("weight_kg", semantic.weight_kg, semantic.explicit_weight),
        )
        for key, value, is_explicit in semantic_fields:
            if is_explicit and value:
                # Deterministic extraction wins whenever it recognized the field. The
                # structured model fills gaps (unusual spelling, unsupported places,
                # mixed-language goods wording); it may never replace a locally
                # recognized current-turn country/category/method/weight.
                if key in explicit:
                    continue
                slots[key] = str(value)
                explicit.add(key)

    if "shipping_method" in explicit:
        slots["shipping_method_explicit"] = True
    else:
        slots["shipping_method_explicit"] = False
    return slots, explicit


async def _semantic_intent(
    *,
    text: str,
    context: dict[str, Any],
) -> NormalizedShippingIntent | None:
    if not settings.openai_api_key or not settings.shipping_semantic_normalizer_enabled:
        return None
    try:
        catalog = await shipment_client.get_shipping_catalog_summary()
        return await normalize_shipping_intent(
            user_text=text,
            catalog=catalog,
            previous_context=context,
        )
    except ShipmentDBError:
        # The deterministic DB call below will produce the normal data-source behavior.
        return None
    except Exception as exc:
        logger.warning(
            "Shipping semantic normalization failed: error_type=%s",
            type(exc).__name__,
        )
        return None


async def maybe_handle_shipping_quote(
    conversation: Conversation,
    user_text: str,
) -> ShippingQuoteOutcome | None:
    """Handle every recognized shipping-catalog request before the generic AI path."""
    text = str(user_text or "").strip()
    if not text:
        return None

    pending = _safe_state(getattr(conversation, "pending_shipping_quote", None))
    last = _safe_state(getattr(conversation, "last_shipping_quote", None))
    if pending and not _context_is_fresh(
        getattr(conversation, "pending_shipping_quote_updated_at", None)
    ):
        _clear_pending(conversation)
        pending = {}
    if last and not _context_is_fresh(
        getattr(conversation, "last_shipping_quote_updated_at", None)
    ):
        conversation.last_shipping_quote = None
        conversation.last_shipping_quote_updated_at = None
        last = {}

    try:
        local = await shipment_client.resolve_shipping_request_slots(text)
    except ShipmentDBError:
        local = {}

    local_weight = extract_weight_kg(text, allow_bare_number=bool(pending))
    has_context = bool(pending or last)
    local_kind = _local_request_kind(
        text,
        explicit_slots=local,
        weight=local_weight,
        has_context=has_context,
        has_pending=bool(pending),
        has_price_context=bool(
            (pending or last).get("weight_kg")
            and (pending or last).get("goods_type")
        ),
    )
    if not _candidate_catalog_turn(
        text,
        local_kind=local_kind,
        explicit_slots=local,
        weight=local_weight,
        has_context=has_context,
    ):
        return None

    context_for_normalizer = pending or last
    semantic = await _semantic_intent(text=text, context=context_for_normalizer)
    strong_local_kind = bool(
        _PRICE_INTENT_RE.search(text)
        or _TRANSIT_INTENT_RE.search(text)
        or _OPTIONS_INTENT_RE.search(text)
        or (
            local_kind == "price_quote"
            and bool(
                (pending or last).get("weight_kg")
                and (pending or last).get("goods_type")
            )
        )
    )
    if strong_local_kind:
        request_kind = local_kind
    elif semantic is not None and semantic.is_shipping_catalog_request:
        request_kind = semantic.request_kind
    else:
        request_kind = local_kind

    # A semantic "none" cannot override strong deterministic shipping intent. It can,
    # however, keep a weak context-only pleasantry such as "tamem ysallemon" out of the
    # catalogue pipeline.
    if request_kind == "none":
        return None

    explicit_slots, explicit_keys = _merge_explicit_slots(
        local,
        semantic,
        local_weight,
    )
    followup = _is_followup(
        text,
        has_context=has_context,
        explicit_slots=explicit_slots,
        semantic=semantic,
        has_pending=bool(pending),
        weight=local_weight,
    )

    logger.info(
        "Shipping catalogue request normalized: kind=%s followup=%s "
        "origin=%s destination=%s goods=%s method=%s weight=%s semantic=%s",
        request_kind,
        followup,
        "origin" in explicit_keys,
        "destination" in explicit_keys,
        "goods_type" in explicit_keys,
        "shipping_method" in explicit_keys,
        "weight_kg" in explicit_keys,
        semantic is not None,
    )

    if request_kind == "route_options":
        # Route discovery is based on the current turn only. In particular, an explicit
        # unsupported destination such as Iraq must never inherit Lebanon from an older
        # quote. Replace the route context with the new explicit fields after answering.
        _clear_pending(conversation)
        try:
            result = await shipment_client.get_route_shipping_options(
                origin=explicit_slots.get("origin"),
                destination=explicit_slots.get("destination"),
                goods_type=explicit_slots.get("goods_type"),
                shipping_method=(
                    explicit_slots.get("shipping_method")
                    if explicit_slots.get("shipping_method_explicit")
                    else None
                ),
            )
        except ShipmentDBError:
            return None
        rendered = render_shipping_route_options(
            result,
            conversation.communication_style,
        )
        if not rendered:
            return None
        route_context = {
            key: explicit_slots[key]
            for key in ("origin", "destination", "goods_type", "shipping_method")
            if explicit_slots.get(key)
        }
        if explicit_slots.get("shipping_method_explicit"):
            route_context["shipping_method_explicit"] = True
        if route_context:
            _mark_last(conversation, route_context)
        else:
            conversation.last_shipping_quote = None
            conversation.last_shipping_quote_updated_at = None
        return ShippingQuoteOutcome(rendered, "shipping_catalog_options")

    base = pending or (last if followup else {})
    slots = dict(base)
    for key in ("origin", "destination", "goods_type", "weight_kg"):
        if key in explicit_keys:
            slots[key] = explicit_slots[key]

    if "shipping_method" in explicit_keys:
        slots["shipping_method"] = explicit_slots["shipping_method"]
        slots["shipping_method_explicit"] = True
    elif not bool(base.get("shipping_method_explicit")):
        # A method displayed by the DB is a result, not a customer filter.
        slots.pop("shipping_method", None)
        slots["shipping_method_explicit"] = False

    if request_kind == "transit_time":
        missing = next(
            (key for key in ("origin", "destination") if not slots.get(key)),
            None,
        )
        if missing:
            _mark_pending(conversation, slots)
            reply = _validate_template(
                _missing_prompt(missing, conversation.communication_style),
                conversation.communication_style,
            )
            return ShippingQuoteOutcome(reply, f"shipping_transit_waiting_{missing}")
        try:
            result = await shipment_client.get_delivery_duration(
                origin=str(slots["origin"]),
                destination=str(slots["destination"]),
                goods_type=(str(slots["goods_type"]) if slots.get("goods_type") else None),
                shipping_method=(
                    str(slots["shipping_method"])
                    if slots.get("shipping_method")
                    and slots.get("shipping_method_explicit")
                    else None
                ),
            )
        except ShipmentDBError:
            return None
        rendered = render_shipping_duration(result, conversation.communication_style)
        if not rendered:
            return None
        _clear_pending(conversation)
        _mark_last(conversation, slots)
        return ShippingQuoteOutcome(rendered, "shipping_transit_completed")

    # price_quote
    required = ("origin", "destination", "goods_type", "weight_kg")
    missing = next((key for key in required if not slots.get(key)), None)
    if missing:
        _mark_pending(conversation, slots)
        reply = _validate_template(
            _missing_prompt(missing, conversation.communication_style),
            conversation.communication_style,
        )
        return ShippingQuoteOutcome(
            reply_text=reply,
            status=f"shipping_quote_waiting_{missing}",
        )

    try:
        result = await shipment_client.get_shipping_price(
            origin=str(slots["origin"]),
            destination=str(slots["destination"]),
            weight_kg=float(str(slots["weight_kg"])),
            goods_type=str(slots["goods_type"]),
            shipping_method=(
                str(slots["shipping_method"])
                if slots.get("shipping_method")
                and slots.get("shipping_method_explicit")
                else None
            ),
        )
    except (ShipmentDBError, ValueError, TypeError):
        return None

    rendered = render_shipping_price(result, conversation.communication_style)
    if not rendered:
        return None

    if result.get("found") is True and result.get("matched_requested_filters") is True:
        _clear_pending(conversation)
        _mark_last(
            conversation,
            {
                "origin": result.get("origin") or slots.get("origin"),
                "destination": result.get("destination") or slots.get("destination"),
                "goods_type": result.get("goods_type") or slots.get("goods_type"),
                "shipping_method": (
                    slots.get("shipping_method")
                    if slots.get("shipping_method_explicit")
                    else None
                ),
                "shipping_method_explicit": bool(
                    slots.get("shipping_method_explicit")
                ),
                "weight_kg": result.get("weight_kg") or slots.get("weight_kg"),
            },
        )
        status = "shipping_quote_completed"
    else:
        # Keep current canonical slots so the customer can correct only the unmatched
        # category/method on the next turn.
        _mark_pending(conversation, slots)
        status = "shipping_quote_options"

    return ShippingQuoteOutcome(reply_text=rendered, status=status)
