"""Fail-closed validation for AI-generated customer-facing WhatsApp replies.

The model is allowed to reason internally, but only a clean final message may cross the
WhatsApp boundary.  This module deliberately contains no OpenAI client code so the
same deterministic checks can be applied both immediately after generation and again
at the final send boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Iterable


MAX_WHATSAPP_TEXT_CHARS = 4096


@dataclass(frozen=True)
class ReplyValidation:
    safe: bool
    reasons: tuple[str, ...] = ()


# These patterns target high-confidence drafting / self-correction leakage rather than
# ordinary customer-service prose.  They intentionally match the exact family of text
# seen in the incident ("Wait Arabic forbidden", "Need all Latin", etc.) plus common
# prompt/tool-plumbing disclosures.
_INTERNAL_NOTE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:arabic|latin)\s+(?:is\s+)?forbidden\b"),
    re.compile(r"(?i)\bneed\s+(?:all\s+)?latin\b"),
    re.compile(r"(?i)\bneed\s+no\s+arabic\b"),
    re.compile(r"(?i)\binclude\s+(?:the\s+)?(?:identifiers?|order\s+ids?)\b"),
    re.compile(r"(?i)\bmaybe\s+(?:include\s+)?order\s+ids?\b"),
    re.compile(
        r"(?i)\bwait[\s,!:\-]+(?:arabic|latin|language|script|style|reply|response|need)\b"
    ),
    re.compile(
        r"(?i)\b(?:note\s+to\s+self|internal\s+note|draft\s+(?:reply|response)|"
        r"rewrite\s+(?:the\s+)?(?:reply|response)|customer-facing\s+only)\b"
    ),
    re.compile(r"(?i)\bcommunication_style\b"),
    re.compile(r"(?i)\bAUTHORITATIVE_RESULT_JSON\b"),
    re.compile(r"(?i)\b(?:system|developer)\s+(?:prompt|instructions?)\b"),
    re.compile(r"(?i)\b(?:hidden|internal)\s+(?:reasoning|instructions?|prompt|state)\b"),
    re.compile(
        r"(?im)(?:^|(?<=[.!?]))\s*(?:need|maybe|include|rewrite|remove|avoid|use)\s+"
        r"(?:all\s+)?(?:arabic|latin|identifiers?|order\s+ids?|the\s+list|the\s+reply)\b"
    ),
)


def _contains_arabic_letter(value: str) -> bool:
    for char in value:
        if not unicodedata.category(char).startswith("L"):
            continue
        if "ARABIC" in unicodedata.name(char, ""):
            return True
    return False


def _contains_unsafe_control_character(value: str) -> bool:
    for char in value:
        if char in {"\n", "\r", "\t"}:
            continue
        if unicodedata.category(char) == "Cc":
            return True
    return False


def validate_customer_reply(text: str | None, style: str | None) -> ReplyValidation:
    """Validate one final customer-visible message.

    The validator is intentionally fail-closed for known internal-note signatures and
    for Arabic-script leakage in a Lebanese Arabizi conversation.  It does not attempt
    to decide whether shipment facts are correct; authoritative tools remain responsible
    for factual correctness.
    """
    value = str(text or "").strip()
    reasons: list[str] = []

    if not value:
        reasons.append("empty")
    if len(value) > MAX_WHATSAPP_TEXT_CHARS:
        reasons.append("too_long")
    if _contains_unsafe_control_character(value):
        reasons.append("control_character")

    if style == "leb_arabizi" and _contains_arabic_letter(value):
        reasons.append("arabic_script_in_arabizi")

    if any(pattern.search(value) for pattern in _INTERNAL_NOTE_PATTERNS):
        reasons.append("internal_note_leak")

    # Preserve deterministic ordering while avoiding duplicate reason labels.
    unique = tuple(dict.fromkeys(reasons))
    return ReplyValidation(safe=not unique, reasons=unique)


def style_repair_instruction(style: str | None) -> str:
    if style == "fr":
        return "Write natural French only."
    if style == "ar":
        return "Write formal Arabic prose in Arabic script."
    if style == "leb_ar":
        return "Write natural Lebanese Arabic prose in Arabic script."
    if style == "leb_arabizi":
        return (
            "Write natural Lebanese Arabizi using Latin letters and digits only. "
            "Do not use any Arabic-script letters. English logistics nouns such as "
            "order, tracking, shipment, status, user ID, and password are acceptable."
        )
    if style == "mixed":
        return "Match the customer's existing Latin-script mixed Lebanese/English/French style."
    return "Write natural English only."


def reply_guard_fallback(style: str | None) -> str:
    """Safe deterministic text used only when generation and repair both fail."""
    if style == "fr":
        return "Je n’ai pas pu formater la réponse correctement. Réessayez dans un instant."
    if style == "ar":
        return "تعذّر تنسيق الرد بشكل صحيح حالياً. حاول مرة أخرى بعد قليل."
    if style == "leb_ar":
        return "ما قدرت نسّق الجواب بشكل صحيح هلّق. جرّب كمان شوي."
    if style == "leb_arabizi":
        return "Ma 2dert na2el l jawab bi siyagha mazbouta halla2. Jarreb ba3d shway."
    if style == "mixed":
        return "Ma 2dert format l reply mazbout halla2. Jarreb again ba3d shway."
    return "I couldn’t format the reply correctly just now. Please try again shortly."


def _display_value(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    if not rendered or rendered.casefold() in {"none", "null"}:
        return None
    return rendered


def _field(label: str, value: Any, *, style: str | None) -> str | None:
    rendered = _display_value(value)
    if rendered is None:
        return None
    # In an Arabizi reply, omit optional prose values written in Arabic script rather
    # than violating the customer's requested script. Exact identifiers are normally
    # ASCII and therefore remain available.
    if style == "leb_arabizi" and _contains_arabic_letter(rendered):
        return None
    return f"{label}: {rendered}"


def _shipment_lines(shipments: Iterable[dict[str, Any]], style: str | None) -> list[str]:
    lines: list[str] = []
    for index, shipment in enumerate(shipments, start=1):
        if not isinstance(shipment, dict):
            continue

        if style in {"ar", "leb_ar"}:
            labels = {
                "order_id": "رقم الطلب",
                "tracking_number": "رقم التتبع",
                "shipment_id": "رقم الشحنة",
                "number": "الرقم",
                "status": "الحالة",
            }
        elif style == "fr":
            labels = {
                "order_id": "Commande",
                "tracking_number": "Suivi",
                "shipment_id": "Expédition",
                "number": "Numéro",
                "status": "Statut",
            }
        else:
            labels = {
                "order_id": "Order ID",
                "tracking_number": "Tracking",
                "shipment_id": "Shipment ID",
                "number": "Number",
                "status": "Status",
            }

        parts = [
            _field(labels["order_id"], shipment.get("order_id"), style=style),
            _field(labels["tracking_number"], shipment.get("tracking_number"), style=style),
            _field(labels["shipment_id"], shipment.get("shipment_id"), style=style),
            _field(labels["number"], shipment.get("number"), style=style),
            _field(labels["status"], shipment.get("status"), style=style),
        ]
        visible = [part for part in parts if part]
        if visible:
            lines.append(f"{index}) " + " | ".join(visible))
    return lines


def render_customer_shipments(result: dict[str, Any], style: str | None) -> str | None:
    """Render a compact order list directly from authoritative tool output.

    This is used as a deterministic fallback when a model-generated rendering is not
    safe.  It intentionally includes identifiers and avoids free-form descriptions,
    which may use a different script from the customer's current message.
    """
    if not isinstance(result, dict) or result.get("error"):
        return None

    raw_shipments = result.get("shipments")
    shipments = raw_shipments if isinstance(raw_shipments, list) else []
    lines = _shipment_lines(shipments, style)

    try:
        count = int(result.get("count", len(shipments)))
    except (TypeError, ValueError):
        count = len(shipments)
    count = max(count, len(lines))

    if not lines:
        if result.get("found") is False or count == 0:
            if style == "fr":
                return "Je n’ai trouvé aucune commande récente sur votre compte."
            if style == "ar":
                return "لم أجد أي طلبات حديثة على حسابك."
            if style == "leb_ar":
                return "ما لقيت طلبات حديثة على حسابك."
            if style == "leb_arabizi":
                return "Ma l2et orders jdod 3a account taba3ak."
            if style == "mixed":
                return "Ma l2et recent orders 3a account taba3ak."
            return "I couldn’t find any recent orders on your account."
        return None

    shown = len(lines)
    if style == "fr":
        header = f"J’ai trouvé {shown} commande(s) récente(s) :"
        footer = "Envoyez-moi l’identifiant de la commande que vous voulez vérifier."
    elif style == "ar":
        header = f"وجدت {shown} من أحدث الطلبات:"
        footer = "أرسل رقم الطلب الذي تريد التحقق منه."
    elif style == "leb_ar":
        header = f"لقيت {shown} من أحدث الطلبات:"
        footer = "بعثلي رقم الطلب اللي بدك شوف تفاصيله."
    elif style == "leb_arabizi":
        header = f"L2et {shown} men a7das l orders:"
        footer = "B3atle l order ID aw l tracking taba3 l order li baddak shouf details taba3a."
    elif style == "mixed":
        header = f"L2et {shown} recent orders:"
        footer = "B3atle l order ID aw tracking taba3 li baddak check."
    else:
        header = f"I found {shown} recent orders:"
        footer = "Send me the order ID or tracking number you want me to check."

    output_lines = [header]
    for line in lines:
        candidate = "\n".join([*output_lines, line, footer])
        if len(candidate) > MAX_WHATSAPP_TEXT_CHARS:
            break
        output_lines.append(line)
    output_lines.append(footer)
    rendered = "\n".join(output_lines)

    validation = validate_customer_reply(rendered, style)
    return rendered if validation.safe else None


def _compact_number(value: Any) -> str:
    rendered = _display_value(value) or ""
    if re.fullmatch(r"-?\d+\.0+", rendered):
        return rendered.split(".", 1)[0]
    if re.fullmatch(r"-?\d+\.\d+", rendered):
        return rendered.rstrip("0").rstrip(".")
    return rendered


def _localized_place(value: Any, style: str | None) -> str:
    raw = _display_value(value) or ""
    key = raw.casefold().strip()
    aliases = {
        "uae": {
            "en": "UAE",
            "fr": "les Émirats",
            "ar": "الإمارات",
            "leb_ar": "الإمارات",
            "leb_arabizi": "l Emarat",
            "mixed": "l Emarat",
        },
        "ksa": {
            "en": "KSA",
            "fr": "l'Arabie saoudite",
            "ar": "السعودية",
            "leb_ar": "السعودية",
            "leb_arabizi": "l Su3oudiye",
            "mixed": "l Su3oudiye",
        },
        "usa": {
            "en": "USA",
            "fr": "les États-Unis",
            "ar": "الولايات المتحدة",
            "leb_ar": "أميركا",
            "leb_arabizi": "Amerka",
            "mixed": "Amerka",
        },
        "lebanon": {
            "en": "Lebanon",
            "fr": "le Liban",
            "ar": "لبنان",
            "leb_ar": "لبنان",
            "leb_arabizi": "Lebnen",
            "mixed": "Lebnen",
        },
        "china": {
            "en": "China",
            "fr": "la Chine",
            "ar": "الصين",
            "leb_ar": "الصين",
            "leb_arabizi": "China",
            "mixed": "China",
        },
        "turkey": {
            "en": "Turkey",
            "fr": "la Turquie",
            "ar": "تركيا",
            "leb_ar": "تركيا",
            "leb_arabizi": "Turkey",
            "mixed": "Turkey",
        },
        "syria": {
            "en": "Syria",
            "fr": "la Syrie",
            "ar": "سوريا",
            "leb_ar": "سوريا",
            "leb_arabizi": "Souria",
            "mixed": "Souria",
        },
        "iraq": {
            "en": "Iraq",
            "fr": "l'Irak",
            "ar": "العراق",
            "leb_ar": "العراق",
            "leb_arabizi": "Iraq",
            "mixed": "Iraq",
        },
    }
    entry = aliases.get(key)
    if not entry:
        return raw
    return entry.get(style or "en", entry["en"])


def _localized_goods(value: Any, style: str | None) -> str:
    raw = _display_value(value) or "goods"
    folded = raw.casefold()
    has_cosmetics = "cosmetic" in folded or "makeup" in folded
    has_electronics = "electronic" in folded
    has_clothes = "clothes" in folded or "accessor" in folded

    if has_cosmetics and has_electronics:
        labels = {
            "fr": "maquillage et produits électroniques",
            "ar": "مكياج وإلكترونيات",
            "leb_ar": "مكياج وإلكترونيات",
            "leb_arabizi": "makeup w electronics",
            "mixed": "makeup w electronics",
            "en": "makeup and electronics",
        }
    elif has_clothes and has_electronics:
        labels = {
            "fr": "vêtements, accessoires et produits électroniques",
            "ar": "ملابس وإكسسوارات وإلكترونيات",
            "leb_ar": "تياب وإكسسوارات وإلكترونيات",
            "leb_arabizi": "tyeb, accessories w electronics",
            "mixed": "clothes, accessories w electronics",
            "en": "clothes, accessories and electronics",
        }
    elif has_cosmetics:
        labels = {
            "fr": "produits cosmétiques",
            "ar": "مواد تجميل",
            "leb_ar": "مواد تجميل",
            "leb_arabizi": "mawed tejmil",
            "mixed": "mawed tejmil",
            "en": "cosmetics",
        }
    elif has_electronics:
        labels = {
            "fr": "produits électroniques",
            "ar": "إلكترونيات",
            "leb_ar": "إلكترونيات",
            "leb_arabizi": "electronics",
            "mixed": "electronics",
            "en": "electronics",
        }
    elif has_clothes:
        labels = {
            "fr": "vêtements et accessoires",
            "ar": "ملابس وإكسسوارات",
            "leb_ar": "تياب وإكسسوارات",
            "leb_arabizi": "tyeb w accessories",
            "mixed": "clothes w accessories",
            "en": "clothes and accessories",
        }
    elif "normal" in folded or "general" in folded:
        labels = {
            "fr": "marchandises générales",
            "ar": "أغراض عامة",
            "leb_ar": "أغراض عامة",
            "leb_arabizi": "aghrad 3adiye",
            "mixed": "general goods",
            "en": "general goods",
        }
    else:
        return raw
    return labels.get(style or "en", labels["en"])


def _localized_method(value: Any, style: str | None) -> str:
    raw = _display_value(value) or ""
    folded = raw.casefold()
    if "express air" in folded:
        mode = "express_air"
    elif "air" in folded:
        mode = "air"
    elif "sea" in folded:
        mode = "sea"
    elif "land" in folded:
        mode = "land"
    else:
        return raw

    mode_labels = {
        "air": {
            "fr": "par avion",
            "ar": "جوي",
            "leb_ar": "بالجو",
            "leb_arabizi": "bel jaw",
            "mixed": "bel jaw",
            "en": "air",
        },
        "express_air": {
            "fr": "express par avion",
            "ar": "جوي سريع",
            "leb_ar": "سريع بالجو",
            "leb_arabizi": "express bel jaw",
            "mixed": "express bel jaw",
            "en": "express air",
        },
        "sea": {
            "fr": "par mer",
            "ar": "بحري",
            "leb_ar": "بالبحر",
            "leb_arabizi": "bel ba7er",
            "mixed": "bel ba7er",
            "en": "sea",
        },
        "land": {
            "fr": "par voie terrestre",
            "ar": "بري",
            "leb_ar": "بالبر",
            "leb_arabizi": "bel barr",
            "mixed": "bel barr",
            "en": "land",
        },
    }
    label = mode_labels[mode].get(style or "en", mode_labels[mode]["en"])

    schedule = ""
    if "daily" in folded:
        schedule = {
            "fr": "tous les jours",
            "ar": "يومياً",
            "leb_ar": "كل يوم",
            "leb_arabizi": "kel yom",
            "mixed": "daily",
            "en": "daily",
        }.get(style or "en", "daily")
    elif "every thursday" in folded:
        schedule = {
            "fr": "chaque jeudi",
            "ar": "كل خميس",
            "leb_ar": "كل خميس",
            "leb_arabizi": "kel khamis",
            "mixed": "every Thursday",
            "en": "every Thursday",
        }.get(style or "en", "every Thursday")
    elif "tues & fri" in folded or "tues and fri" in folded:
        schedule = {
            "fr": "mardi et vendredi",
            "ar": "الثلاثاء والجمعة",
            "leb_ar": "الثلاثا والجمعة",
            "leb_arabizi": "kel taleta w jem3a",
            "mixed": "Tues w Fri",
            "en": "Tuesdays and Fridays",
        }.get(style or "en", "Tuesdays and Fridays")
    elif "every friday" in folded:
        schedule = {
            "fr": "chaque vendredi",
            "ar": "كل جمعة",
            "leb_ar": "كل جمعة",
            "leb_arabizi": "kel jem3a",
            "mixed": "every Friday",
            "en": "every Friday",
        }.get(style or "en", "every Friday")
    elif "2-3 weekly" in folded:
        schedule = {
            "fr": "2-3 fois par semaine",
            "ar": "2-3 مرات أسبوعياً",
            "leb_ar": "2-3 مرات بالأسبوع",
            "leb_arabizi": "2-3 marrat bel osbou3",
            "mixed": "2-3 times weekly",
            "en": "2-3 times weekly",
        }.get(style or "en", "2-3 times weekly")
    elif "1 trip weekly" in folded or "1 weekly" in folded:
        schedule = {
            "fr": "une fois par semaine",
            "ar": "مرة أسبوعياً",
            "leb_ar": "مرة بالأسبوع",
            "leb_arabizi": "marra bel osbou3",
            "mixed": "once weekly",
            "en": "once weekly",
        }.get(style or "en", "once weekly")

    return f"{label} ({schedule})" if schedule else label


def _localized_transit(value: Any, style: str | None) -> str:
    raw = _display_value(value) or ""
    match = re.fullmatch(r"\s*(\d+(?:-\d+)?)\s+business\s+days\s*", raw, re.I)
    if match:
        number = match.group(1)
        return {
            "fr": f"{number} jours ouvrables",
            "ar": f"{number} أيام عمل",
            "leb_ar": f"{number} أيام عمل",
            "leb_arabizi": f"{number} iyem 3amal",
            "mixed": f"{number} business days",
            "en": f"{number} business days",
        }.get(style or "en", raw)
    match = re.fullmatch(r"\s*(\d+(?:-\d+)?)\s+days\s+from\s+departure\s*", raw, re.I)
    if match:
        number = match.group(1)
        return {
            "fr": f"{number} jours à partir du départ",
            "ar": f"{number} يوماً من تاريخ الانطلاق",
            "leb_ar": f"{number} يوم من وقت الانطلاق",
            "leb_arabizi": f"{number} yom mn wa2et l departure",
            "mixed": f"{number} days from departure",
            "en": f"{number} days from departure",
        }.get(style or "en", raw)
    match = re.fullmatch(r"\s*Approx\s+(\d+(?:-\d+)?)\s+business\s+days\s*", raw, re.I)
    if match:
        number = match.group(1)
        return {
            "fr": f"environ {number} jours ouvrables",
            "ar": f"حوالي {number} يوم عمل",
            "leb_ar": f"تقريباً {number} يوم عمل",
            "leb_arabizi": f"ta2riban {number} yom 3amal",
            "mixed": f"approx. {number} business days",
            "en": f"approximately {number} business days",
        }.get(style or "en", raw)
    return raw


def _shipping_rate_rows(result: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    if result.get("matched_requested_filters") is True:
        return result, False
    available = result.get("available_rates")
    if isinstance(available, list):
        for row in available:
            if isinstance(row, dict):
                return row, True
    return None, False


def _rate_option_lines(calculation: Any, style: str | None) -> list[str]:
    if not isinstance(calculation, dict):
        return []
    options = calculation.get("options")
    if not isinstance(options, list):
        return []

    labels = {
        "pickup": {
            "fr": "Retrait",
            "ar": "استلام",
            "leb_ar": "استلام",
            "leb_arabizi": "Pickup",
            "mixed": "Pickup",
            "en": "Pickup",
        },
        "delivery": {
            "fr": "Livraison",
            "ar": "توصيل",
            "leb_ar": "توصيل",
            "leb_arabizi": "Delivery",
            "mixed": "Delivery",
            "en": "Delivery",
        },
        "rate": {
            "fr": "Tarif",
            "ar": "السعر",
            "leb_ar": "السعر",
            "leb_arabizi": "Rate",
            "mixed": "Rate",
            "en": "Rate",
        },
    }
    lines: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        service = str(option.get("service") or "rate")
        label_set = labels.get(service, labels["rate"])
        label = label_set.get(style or "en", label_set["en"])
        total = _display_value(option.get("total_display"))
        per_kg = _display_value(option.get("rate_display"))
        if total and per_kg:
            lines.append(f"- {label}: {total} ({per_kg})")
        elif total:
            lines.append(f"- {label}: {total}")
        elif per_kg:
            lines.append(f"- {label}: {per_kg}")
    return lines


def _inline_rate_totals(row: dict[str, Any], style: str | None) -> str:
    calculation = row.get("calculation")
    if not isinstance(calculation, dict):
        return _display_value(row.get("price")) or ""
    options = calculation.get("options")
    if not isinstance(options, list):
        return _display_value(row.get("price")) or ""
    labels = {
        "pickup": {
            "fr": "retrait",
            "ar": "استلام",
            "leb_ar": "استلام",
            "leb_arabizi": "Pickup",
            "mixed": "Pickup",
            "en": "Pickup",
        },
        "delivery": {
            "fr": "livraison",
            "ar": "توصيل",
            "leb_ar": "توصيل",
            "leb_arabizi": "Delivery",
            "mixed": "Delivery",
            "en": "Delivery",
        },
        "rate": {
            "fr": "tarif",
            "ar": "السعر",
            "leb_ar": "السعر",
            "leb_arabizi": "Rate",
            "mixed": "Rate",
            "en": "Rate",
        },
    }
    parts: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        service = str(option.get("service") or "rate")
        label_set = labels.get(service, labels["rate"])
        label = label_set.get(style or "en", label_set["en"])
        total = _display_value(option.get("total_display"))
        rate = _display_value(option.get("rate_display"))
        if total and rate:
            parts.append(f"{label} {total} ({rate})")
        elif total:
            parts.append(f"{label} {total}")
        elif rate:
            parts.append(f"{label} {rate}")
    return ", ".join(parts) or (_display_value(row.get("price")) or "")


def _render_available_rate_options(
    result: dict[str, Any],
    style: str | None,
) -> str | None:
    rows = result.get("available_rates")
    if not isinstance(rows, list) or not rows:
        return None
    first = next((row for row in rows if isinstance(row, dict)), None)
    if first is None:
        return None
    query = result.get("query") if isinstance(result.get("query"), dict) else {}
    weight = _compact_number(first.get("weight_kg") or query.get("weight_kg"))
    origin = _localized_place(first.get("origin") or query.get("origin"), style)
    destination = _localized_place(
        first.get("destination") or query.get("destination"), style
    )
    requested_goods = _localized_goods(query.get("goods_type"), style)

    if style == "fr":
        header = (
            f"Je n'ai pas trouvé de tarif correspondant exactement à {requested_goods}. "
            f"Voici les options enregistrées pour {weight} kg de {origin} vers {destination} :"
        )
    elif style == "ar":
        header = (
            f"لم أجد سعراً يطابق فئة {requested_goods} تماماً. هذه الخيارات المسجلة "
            f"لوزن {weight} كغ من {origin} إلى {destination}:"
        )
    elif style == "leb_ar":
        header = (
            f"ما لقيت سعر مطابق تماماً لفئة {requested_goods}. هيدي الخيارات المسجّلة "
            f"لـ {weight} كغ من {origin} على {destination}:"
        )
    elif style == "leb_arabizi":
        header = (
            f"Ma l2et rate byetabe2 category {requested_goods} 100%. Hayde l options "
            f"l msajjalin la {weight} kg mn {origin} 3a {destination}:"
        )
    elif style == "mixed":
        header = (
            f"Ma l2et exact rate lal category {requested_goods}. Hayde the registered "
            f"options la {weight} kg mn {origin} 3a {destination}:"
        )
    else:
        header = (
            f"I couldn't find an exact rate for {requested_goods}. These are the "
            f"registered options for {weight} kg from {origin} to {destination}:"
        )

    lines = [header]
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        goods = _localized_goods(row.get("goods_type"), style)
        method = _localized_method(row.get("shipping_method"), style)
        totals = _inline_rate_totals(row, style)
        transit = _localized_transit(row.get("transit_time"), style)
        line = f"- {goods} | {method}"
        if totals:
            line += f" | {totals}"
        if transit:
            line += f" | {transit}"
        candidate = "\n".join([*lines, line])
        if len(candidate) > MAX_WHATSAPP_TEXT_CHARS:
            break
        lines.append(line)

    rendered = "\n".join(lines).strip()
    validation = validate_customer_reply(rendered, style)
    return rendered if validation.safe else None


def _localized_list(values: Any, style: str | None) -> str:
    if not isinstance(values, list):
        return ""
    rendered: list[str] = []
    seen: set[str] = set()
    for value in values:
        label = _localized_place(value, style)
        folded = label.casefold()
        if label and folded not in seen:
            seen.add(folded)
            rendered.append(label)
    return ", ".join(rendered)


def render_shipping_route_options(
    result: dict[str, Any],
    style: str | None,
) -> str | None:
    """Render route/category discovery directly from the shipping catalogue."""
    if not isinstance(result, dict) or result.get("error"):
        return None

    query = result.get("query") if isinstance(result.get("query"), dict) else {}
    origin = _localized_place(query.get("origin"), style) if query.get("origin") else ""
    destination = (
        _localized_place(query.get("destination"), style)
        if query.get("destination")
        else ""
    )
    requested_goods = (
        _localized_goods(query.get("goods_type"), style)
        if query.get("goods_type")
        else ""
    )
    supported = _localized_list(result.get("supported_destinations"), style)

    if result.get("found") is False:
        if destination:
            if style == "fr":
                rendered = f"Je n'ai trouvé aucune destination ni aucun tarif enregistré vers {destination}."
                if supported:
                    rendered += f" Destinations actuellement enregistrées: {supported}."
            elif style == "ar":
                rendered = f"لم أجد وجهة أو سعراً مسجلاً حالياً إلى {destination}."
                if supported:
                    rendered += f" الوجهات المسجلة حالياً: {supported}."
            elif style == "leb_ar":
                rendered = f"ما لقيت وجهة أو سعر مسجّل حالياً على {destination}."
                if supported:
                    rendered += f" الوجهات المسجّلة هلّق: {supported}."
            elif style == "leb_arabizi":
                rendered = f"Ma l2et destination aw rate msajjal 7aliyan 3a {destination}."
                if supported:
                    rendered += f" L destinations l msajjalin halla2: {supported}."
            elif style == "mixed":
                rendered = f"Ma l2et destination aw shipping rate msajjal 7aliyan 3a {destination}."
                if supported:
                    rendered += f" Registered destinations: {supported}."
            else:
                rendered = f"I couldn't find a registered destination or shipping rate to {destination}."
                if supported:
                    rendered += f" Currently registered destinations: {supported}."
        elif origin:
            if style == "fr":
                rendered = f"Je n'ai trouvé aucun tarif enregistré au départ de {origin}."
            elif style == "ar":
                rendered = f"لم أجد سعراً مسجلاً حالياً للشحن من {origin}."
            elif style == "leb_ar":
                rendered = f"ما لقيت سعر مسجّل حالياً للشحن من {origin}."
            elif style == "leb_arabizi":
                rendered = f"Ma l2et rate msajjal 7aliyan lal shipping mn {origin}."
            elif style == "mixed":
                rendered = f"Ma l2et shipping rate msajjal 7aliyan mn {origin}."
            else:
                rendered = f"I couldn't find a registered shipping rate from {origin}."
        else:
            if style == "fr":
                rendered = "Je n'ai trouvé aucune option dans le catalogue des tarifs."
            elif style == "ar":
                rendered = "لم أجد أي خيارات في جدول أسعار الشحن حالياً."
            elif style == "leb_ar":
                rendered = "ما لقيت خيارات بجدول أسعار الشحن هلّق."
            elif style == "leb_arabizi":
                rendered = "Ma l2et options b shipping rates table halla2."
            elif style == "mixed":
                rendered = "Ma l2et options in the shipping-rates table halla2."
            else:
                rendered = "I couldn't find any options in the shipping-rate catalogue."
        validation = validate_customer_reply(rendered, style)
        return rendered if validation.safe else None

    rows = result.get("options")
    if not isinstance(rows, list) or not rows:
        return None

    if origin and destination:
        headers = {
            "fr": f"Voici les catégories et services enregistrés de {origin} vers {destination} :",
            "ar": f"هذه الفئات والخدمات المسجلة من {origin} إلى {destination}:",
            "leb_ar": f"هيدي الفئات والخدمات المسجّلة من {origin} على {destination}:",
            "leb_arabizi": f"Hayde l categories w services l msajjalin mn {origin} 3a {destination}:",
            "mixed": f"Hayde the registered categories and services mn {origin} 3a {destination}:",
            "en": f"These are the registered categories and services from {origin} to {destination}:",
        }
    elif destination:
        headers = {
            "fr": f"Voici les options enregistrées vers {destination} :",
            "ar": f"هذه خيارات الشحن المسجلة إلى {destination}:",
            "leb_ar": f"هيدي خيارات الشحن المسجّلة على {destination}:",
            "leb_arabizi": f"Hayde l shipping options l msajjalin 3a {destination}:",
            "mixed": f"Hayde the registered shipping options 3a {destination}:",
            "en": f"These are the registered shipping options to {destination}:",
        }
    elif origin:
        headers = {
            "fr": f"Voici les options enregistrées au départ de {origin} :",
            "ar": f"هذه خيارات الشحن المسجلة من {origin}:",
            "leb_ar": f"هيدي خيارات الشحن المسجّلة من {origin}:",
            "leb_arabizi": f"Hayde l shipping options l msajjalin mn {origin}:",
            "mixed": f"Hayde the registered shipping options mn {origin}:",
            "en": f"These are the registered shipping options from {origin}:",
        }
    else:
        headers = {
            "fr": "Voici les options actuellement enregistrées dans le catalogue :",
            "ar": "هذه الخيارات المسجلة حالياً في جدول أسعار الشحن:",
            "leb_ar": "هيدي الخيارات المسجّلة هلّق بجدول أسعار الشحن:",
            "leb_arabizi": "Hayde l options l msajjalin halla2 b shipping rates table:",
            "mixed": "Hayde the options currently registered in the shipping-rates table:",
            "en": "These are the options currently registered in the shipping-rate catalogue:",
        }

    lines = [headers.get(style or "en", headers["en"])]
    if requested_goods and result.get("matched_requested_filters") is False:
        mismatch = {
            "fr": f"Aucune ligne ne correspond exactement à {requested_goods}; voici les options les plus proches du catalogue.",
            "ar": f"لا يوجد صف يطابق فئة {requested_goods} تماماً؛ هذه الخيارات الأقرب في الجدول.",
            "leb_ar": f"ما في صف مطابق تماماً لفئة {requested_goods}؛ هيدي أقرب خيارات بالجدول.",
            "leb_arabizi": f"Ma fi row byetabe2 category {requested_goods} 100%; hayde a2rab options bel table.",
            "mixed": f"Ma fi exact row lal category {requested_goods}; hayde the closest table options.",
            "en": f"No row exactly matches {requested_goods}; these are the closest catalogue options.",
        }
        lines.append(mismatch.get(style or "en", mismatch["en"]))

    include_origin = not bool(query.get("origin"))
    include_destination = not bool(query.get("destination"))
    seen_rows: set[tuple[str, str, str, str]] = set()
    for row in rows[:12]:
        if not isinstance(row, dict):
            continue
        raw_key = (
            str(row.get("origin") or ""),
            str(row.get("destination") or ""),
            str(row.get("goods_type") or ""),
            str(row.get("shipping_method") or ""),
        )
        if raw_key in seen_rows:
            continue
        seen_rows.add(raw_key)
        parts: list[str] = []
        if include_origin:
            parts.append(_localized_place(row.get("origin"), style))
        if include_destination:
            parts.append(_localized_place(row.get("destination"), style))
        parts.append(_localized_goods(row.get("goods_type"), style))
        parts.append(_localized_method(row.get("shipping_method"), style))
        price = _display_value(row.get("price"))
        transit = _localized_transit(row.get("transit_time"), style)
        if price:
            parts.append(price)
        if transit:
            parts.append(transit)
        line = "- " + " | ".join(part for part in parts if part)
        candidate = "\n".join([*lines, line])
        if len(candidate) > MAX_WHATSAPP_TEXT_CHARS - 220:
            break
        lines.append(line)

    scope = {
        "fr": "Ces catégories proviennent du tableau des tarifs; ce n'est pas une liste douanière complète des articles interdits.",
        "ar": "هذه الفئات مأخوذة من جدول الأسعار وليست قائمة جمركية كاملة بالممنوعات.",
        "leb_ar": "هيدي الفئات من جدول الأسعار، مش لائحة جمركية كاملة بالممنوعات.",
        "leb_arabizi": "Hayde l categories mn rate table, mesh le2i7a jemrokiye kemle lal mamnou3at.",
        "mixed": "Hayde categories from the rate table, mesh a complete customs/prohibited-items list.",
        "en": "These categories come from the rate table; they are not a complete customs or prohibited-items list.",
    }
    lines.append(scope.get(style or "en", scope["en"]))
    rendered = "\n".join(lines).strip()
    validation = validate_customer_reply(rendered, style)
    return rendered if validation.safe else None


def render_shipping_duration(
    result: dict[str, Any],
    style: str | None,
) -> str | None:
    """Render transit-time lookup without a generative paraphrasing step."""
    if not isinstance(result, dict) or result.get("error"):
        return None
    query = result.get("query") if isinstance(result.get("query"), dict) else {}
    if result.get("found") is False:
        origin = _localized_place(query.get("origin"), style)
        destination = _localized_place(query.get("destination"), style)
        messages = {
            "fr": f"Je n'ai trouvé aucun délai enregistré de {origin} vers {destination}.",
            "ar": f"لم أجد مدة شحن مسجلة من {origin} إلى {destination}.",
            "leb_ar": f"ما لقيت مدة شحن مسجّلة من {origin} على {destination}.",
            "leb_arabizi": f"Ma l2et transit time msajjal mn {origin} 3a {destination}.",
            "mixed": f"Ma l2et registered transit time mn {origin} 3a {destination}.",
            "en": f"I couldn't find a registered transit time from {origin} to {destination}.",
        }
        rendered = messages.get(style or "en", messages["en"])
        validation = validate_customer_reply(rendered, style)
        return rendered if validation.safe else None

    rows = result.get("available_options")
    if not isinstance(rows, list) or not rows:
        rows = [result]
    lines: list[str] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        origin = _localized_place(row.get("origin"), style)
        destination = _localized_place(row.get("destination"), style)
        goods = _localized_goods(row.get("goods_type"), style)
        method = _localized_method(row.get("shipping_method"), style)
        transit = _localized_transit(row.get("transit_time"), style)
        line = f"- {origin} -> {destination} | {goods} | {method} | {transit}"
        if len("\n".join([*lines, line])) > MAX_WHATSAPP_TEXT_CHARS:
            break
        lines.append(line)
    if not lines:
        return None
    headers = {
        "fr": "Délais enregistrés :",
        "ar": "مدد الشحن المسجلة:",
        "leb_ar": "مدد الشحن المسجّلة:",
        "leb_arabizi": "L transit times l msajjalin:",
        "mixed": "Registered transit times:",
        "en": "Registered transit times:",
    }
    rendered = "\n".join([headers.get(style or "en", headers["en"]), *lines])
    validation = validate_customer_reply(rendered, style)
    return rendered if validation.safe else None


def render_shipping_price(result: dict[str, Any], style: str | None) -> str | None:
    """Render a shipping quote directly from authoritative rate-table output.

    This removes a second generative interpretation step from price calculations, so
    multilingual wording cannot alter the matched row, the per-kg multiplication, or
    the customer's script style.
    """
    if not isinstance(result, dict) or result.get("error"):
        return None

    query = result.get("query") if isinstance(result.get("query"), dict) else {}
    if result.get("found") is False:
        origin = _localized_place(query.get("origin"), style)
        destination = _localized_place(query.get("destination"), style)
        if style == "fr":
            rendered = f"Je n'ai trouvé aucun tarif enregistré de {origin} vers {destination}."
        elif style == "ar":
            rendered = f"لم أجد سعراً مسجلاً للشحن من {origin} إلى {destination}."
        elif style == "leb_ar":
            rendered = f"ما لقيت سعر مسجّل للشحن من {origin} على {destination}."
        elif style == "leb_arabizi":
            rendered = f"Ma l2et se3er shipping msajjal mn {origin} 3a {destination}."
        elif style == "mixed":
            rendered = f"Ma l2et shipping rate msajjal mn {origin} 3a {destination}."
        else:
            rendered = f"I couldn't find a registered shipping rate from {origin} to {destination}."
        validation = validate_customer_reply(rendered, style)
        return rendered if validation.safe else None

    unmatched_filters = {
        str(value) for value in (result.get("unmatched_filters") or []) if value
    }
    if unmatched_filters & {"goods_type", "filter_combination"}:
        return _render_available_rate_options(result, style)

    row, used_fallback = _shipping_rate_rows(result)
    if row is None:
        return None

    weight = _compact_number(row.get("weight_kg") or query.get("weight_kg"))
    origin = _localized_place(row.get("origin") or query.get("origin"), style)
    destination = _localized_place(row.get("destination") or query.get("destination"), style)
    goods = _localized_goods(row.get("goods_type") or query.get("goods_type"), style)
    method = _localized_method(row.get("shipping_method"), style)
    transit = _localized_transit(row.get("transit_time"), style)

    if style == "fr":
        header = f"Pour {weight} kg de {goods}, de {origin} vers {destination}, le service disponible est {method} :"
        transit_line = f"Délai: {transit}." if transit else ""
        mismatch = "Le mode demandé n'a pas de tarif correspondant ; voici l'option disponible pour cet itinéraire."
        raw_price_prefix = "Tarif enregistré"
    elif style == "ar":
        header = f"لشحنة {weight} كغ من {goods} من {origin} إلى {destination}، الخدمة المتاحة هي {method}:"
        transit_line = f"المدة: {transit}." if transit else ""
        mismatch = "لا يوجد سعر مطابق لطريقة الشحن المطلوبة؛ هذه هي الخدمة المتاحة لهذا المسار."
        raw_price_prefix = "السعر المسجل"
    elif style == "leb_ar":
        header = f"لـ {weight} كغ {goods} من {origin} على {destination}، الخدمة المتاحة هي {method}:"
        transit_line = f"الوقت: {transit}." if transit else ""
        mismatch = "ما في سعر مطابق لطريقة الشحن المطلوبة؛ هيدي الخدمة المتاحة لهالمسار."
        raw_price_prefix = "السعر المسجّل"
    elif style == "leb_arabizi":
        header = f"La {weight} kg {goods} mn {origin} 3a {destination}, l service l available howwe {method}:"
        transit_line = f"L wa2et: {transit}." if transit else ""
        mismatch = "Ma fi rate msajjal 3a l shipping method li talabto; hayde l option l available lal route."
        raw_price_prefix = "L se3er l msajjal"
    elif style == "mixed":
        header = f"La {weight} kg {goods} mn {origin} 3a {destination}, the available service is {method}:"
        transit_line = f"Transit time: {transit}." if transit else ""
        mismatch = "Ma fi matching rate lal requested shipping method; hayde the available route option."
        raw_price_prefix = "Registered rate"
    else:
        header = f"For {weight} kg of {goods} from {origin} to {destination}, the available service is {method}:"
        transit_line = f"Transit time: {transit}." if transit else ""
        mismatch = "No rate matches the requested shipping method; this is the available route option."
        raw_price_prefix = "Registered rate"

    lines = [header]
    # With the dispatcher boundary, a non-null query method means the active customer
    # turn explicitly requested it. Only then is a mismatch notice customer-relevant.
    if used_fallback and query.get("shipping_method"):
        lines.append(mismatch)

    rate_lines = _rate_option_lines(row.get("calculation"), style)
    if rate_lines:
        lines.extend(rate_lines)
    else:
        raw_price = _display_value(row.get("price"))
        if raw_price:
            lines.append(f"- {raw_price_prefix}: {raw_price}")
    if transit_line:
        lines.append(transit_line)

    rendered = "\n".join(line for line in lines if line).strip()
    validation = validate_customer_reply(rendered, style)
    return rendered if validation.safe else None


def render_authoritative_tool_result(
    tool_name: str | None,
    result: dict[str, Any] | None,
    style: str | None,
) -> str | None:
    if tool_name == "get_route_shipping_options" and isinstance(result, dict):
        return render_shipping_route_options(result, style)
    if tool_name == "get_shipping_price" and isinstance(result, dict):
        return render_shipping_price(result, style)
    if tool_name == "get_delivery_duration" and isinstance(result, dict):
        return render_shipping_duration(result, style)
    if tool_name == "get_customer_shipments" and isinstance(result, dict):
        return render_customer_shipments(result, style)
    return None
