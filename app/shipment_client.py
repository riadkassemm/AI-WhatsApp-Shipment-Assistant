"""
Direct MariaDB access layer for the WhatsApp shipment assistant.

Security boundary:
    WhatsApp -> chatbot backend -> this class -> MariaDB

OpenAI never receives database credentials and never executes SQL directly.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from difflib import SequenceMatcher
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

import aiomysql

from app.config import settings


class ShipmentDBError(Exception):
    """Database/application-level error."""


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_MONEY_RATE_RE = re.compile(
    r"""
    (?:(?P<label>[^,:;\n]+?)\s*:\s*)?
    (?P<prefix>US\$|USD|AED|EUR|GBP|\$|€|£)?\s*
    (?P<amount>\d+(?:[.,]\d+)?)\s*
    (?P<suffix>USD|AED|EUR|GBP|US\$|\$|€|£)?\s*
    (?:/|\bper\s+)
    (?P<unit>kg|kgs|kilogram(?:me)?s?|كيلو(?:غرام)?|كغ)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _fold(value: str | None) -> str:
    """Normalize multilingual lookup text without changing returned DB values."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.translate(
        str.maketrans(
            {
                "أ": "ا",
                "إ": "ا",
                "آ": "ا",
                "ة": "ه",
                "ى": "ي",
                "ؤ": "و",
                "ئ": "ي",
            }
        )
    )
    text = re.sub(r"[^0-9a-z\u0600-\u06ff]+", " ", text)
    return " ".join(text.split())


def _alias_index(groups: dict[str, set[str]]) -> dict[str, str]:
    index: dict[str, str] = {}
    for canonical, aliases in groups.items():
        for value in {canonical, *aliases}:
            normalized = _fold(value)
            if normalized:
                index[normalized] = canonical
    return index


_PLACE_ALIASES = _alias_index(
    {
        "uae": {
            "UAE",
            "U.A.E.",
            "United Arab Emirates",
            "Emirates",
            "Al Emarat",
            "Emarat",
            "El Emarat",
            "L Emarat",
            "Imarat",
            "l emarat",
            "el emarat",
            "Dubai",
            "Abu Dhabi",
            "les emirats",
            "emirats arabes unis",
            "الامارات",
            "الإمارات",
            "الامارات العربية المتحدة",
            "دبي",
            "ابو ظبي",
            "أبو ظبي",
        },
        "usa": {
            "USA",
            "U.S.A.",
            "US",
            "U.S.",
            "United States",
            "United States of America",
            "America",
            "Etats Unis",
            "États-Unis",
            "Amerique",
            "Amérique",
            "Americaine",
            "Américaine",
            "amrika",
            "amerka",
            "amerika",
            "amerka",
            "اميركا",
            "أميركا",
            "امريكا",
            "أمريكا",
            "الولايات المتحدة",
            "الولايات المتحدة الامريكية",
            "الولايات المتحدة الأميركية",
        },
        "ksa": {
            "KSA",
            "Saudi Arabia",
            "Saudi",
            "Arabie Saoudite",
            "Saoudite",
            "Saudiye",
            "Sa3oudiye",
            "S3oudiye",
            "Su3udiye",
            "So3oudiye",
            "Sou3oudiye",
            "Sou3oudiyye",
            "Saoudiye",
            "Saoudiyye",
            "El Su3udiye",
            "L Su3udiye",
            "Al Su3udiye",
            "السعودية",
            "السعوديه",
            "المملكة العربية السعودية",
            "saoudi",
        },
        "china": {
            "China",
            "PRC",
            "Chine",
            "الصين",
            "sine",
            "sin",
        },
        "turkey": {
            "Turkey",
            "Türkiye",
            "Turkiye",
            "Turquie",
            "تركيا",
            "turkiya",
            "terkiya",
            "torkiya",
            "turkya",
        },
        "syria": {
            "Syria",
            "Syrian Arab Republic",
            "Syrie",
            "سوريا",
            "souria",
            "souriya",
            "suriya",
        },
        "lebanon": {
            "Lebanon",
            "Lebnen",
            "El Lebnen",
            "L Lebnen",
            "Lebnan",
            "Libnan",
            "Libnen",
            "Lubnan",
            "Liban",
            "لبنان",
        },
        "iraq": {
            "Iraq",
            "Irak",
            "3ira2",
            "3iraq",
            "l 3ira2",
            "el 3ira2",
            "العراق",
            "عراق",
        },
    }
)

_GOODS_ALIASES = _alias_index(
    {
        "cosmetics": {
            "cosmetic",
            "cosmetics",
            "makeup",
            "make up",
            "beauty products",
            "produit cosmetique",
            "produits cosmetiques",
            "cosmetique",
            "cosmetiques",
            "maquillage",
            "مكياج",
            "مستحضرات تجميل",
            "مستحضرات التجميل",
            "مواد تجميل",
            "مواد التجميل",
            "تجميل",
            "tajmil",
            "tejmil",
            "mawed tejmil",
            "mawed tajmil",
            "mawad tejmil",
            "mawad tajmil",
            "mawedet tejmil",
            "mawedet tajmil",
        },
        "normal general": {
            "normal",
            "general",
            "normal general",
            "general goods",
            "ordinary goods",
            "marchandise generale",
            "marchandises generales",
            "produits generaux",
            "بضاعة عادية",
            "بضاعة عامة",
            "اغراض عامة",
            "أغراض عامة",
            "شحن عادي",
            "عادي",
        },
        "clothes": {
            "clothes",
            "clothing",
            "garments",
            "apparel",
            "clothes and accessories",
            "clothing and accessories",
            "vetements",
            "vêtements",
            "vetements et accessoires",
            "ملابس",
            "ملابس واكسسوارات",
            "ثياب",
            "تياب",
            "tyeb",
            "accessory",
            "accessories",
            "accessoire",
            "accessoires",
            "ekseswar",
            "exeswar",
            "akseswar",
            "اكسيزوار",
            "اكسسوارات",
            "إكسسوارات",
        },
        "electronics": {
            "electronics",
            "electronic",
            "electronic devices",
            "electrical goods",
            "electronique",
            "électronique",
            "appareils electroniques",
            "الكترونيات",
            "إلكترونيات",
            "اجهزة الكترونية",
            "أجهزة إلكترونية",
        },
    }
)

_METHOD_ALIASES = _alias_index(
    {
        "air": {
            "air",
            "air freight",
            "air cargo",
            "by air",
            "air daily",
            "avion",
            "aerien",
            "aerienne",
            "par avion",
            "jaw",
            "bel jaw",
            "bl jaw",
            "bil jaw",
            "بالجو",
            "جوا",
            "جواً",
            "جوي",
            "شحن جوي",
        },
        "sea": {
            "sea",
            "sea freight",
            "ocean",
            "by sea",
            "maritime",
            "bateau",
            "par bateau",
            "ba7er",
            "bel ba7er",
            "bl ba7er",
            "بحر",
            "بحري",
            "بالبحر",
            "شحن بحري",
        },
        "land": {
            "land",
            "road",
            "truck",
            "ground",
            "terrestre",
            "par route",
            "barr",
            "bel barr",
            "bl barr",
            "بر",
            "بري",
            "بالبر",
            "شحن بري",
        },
    }
)

_SERVICE_ALIASES = _alias_index(
    {
        "pickup": {
            "pickup",
            "pick up",
            "collect",
            "collection",
            "retrait",
            "استلام",
            "استلام من الفرع",
        },
        "delivery": {
            "delivery",
            "deliver",
            "door delivery",
            "livraison",
            "توصيل",
            "دليفري",
        },
    }
)


def _fuzzy_ratio(left: str, right: str) -> float:
    """Conservative typo/transliteration similarity for normalized phrases."""
    if len(left) < 4 or len(right) < 4:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _canonical(value: str | None, aliases: dict[str, str]) -> str:
    normalized = _fold(value)
    if not normalized:
        return ""
    exact = aliases.get(normalized)
    if exact:
        return exact

    # Database labels commonly add qualifiers, e.g. "Air (Daily)". Resolve an
    # alias only when it is a complete token phrase, not an arbitrary substring.
    padded = f" {normalized} "
    matches: list[tuple[int, str]] = []
    for alias, canonical in aliases.items():
        if f" {alias} " in padded:
            matches.append((len(alias), canonical))
    if matches:
        matches.sort(reverse=True)
        return matches[0][1]

    # Last-resort typo/transliteration matching. Require one clear best candidate.
    fuzzy: list[tuple[float, int, str]] = []
    for alias, canonical in aliases.items():
        ratio = _fuzzy_ratio(normalized, alias)
        threshold = 0.86 if max(len(normalized), len(alias)) <= 6 else 0.80
        if ratio >= threshold:
            fuzzy.append((ratio, len(alias), canonical))
    if fuzzy:
        fuzzy.sort(reverse=True)
        best = fuzzy[0]
        if len(fuzzy) == 1 or best[0] - fuzzy[1][0] >= 0.04 or best[2] == fuzzy[1][2]:
            return best[2]
    return normalized


def detect_shipping_method_in_text(value: str | None) -> str | None:
    """Return an explicitly mentioned broad transport method, if any.

    The active customer turn is the only trustworthy source for whether a method was
    requested. A method that appeared in an earlier database result (for example,
    ``Air (Daily)``) must not silently become a filter on a later follow-up quote.
    """
    normalized = _fold(value)
    if not normalized:
        return None
    padded = f" {normalized} "
    matches: list[tuple[int, str]] = []
    for alias, canonical in _METHOD_ALIASES.items():
        if f" {alias} " in padded:
            matches.append((len(alias), canonical))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][1]


def canonical_shipping_place(value: str | None) -> str:
    return _canonical(value, _PLACE_ALIASES)


def canonical_shipping_goods(value: str | None) -> str:
    return _canonical(value, _GOODS_ALIASES)


def canonical_shipping_method(value: str | None) -> str:
    return _canonical(value, _METHOD_ALIASES)


def _extended_aliases(
    base: dict[str, str],
    catalogue_values: Iterable[str | None],
) -> dict[str, str]:
    extended = dict(base)
    for raw in catalogue_values:
        folded = _fold(raw)
        if not folded:
            continue
        known = _canonical(raw, base)
        # New English-labelled DB values remain matchable without a code deployment.
        extended.setdefault(folded, known or folded)
    return extended


def _find_alias_mentions(value: str, aliases: dict[str, str]) -> list[dict[str, Any]]:
    """Locate exact and conservative fuzzy aliases in normalized customer text."""
    normalized = _fold(value)
    if not normalized:
        return []

    mentions: list[dict[str, Any]] = []
    for alias, canonical in aliases.items():
        pattern = re.compile(
            rf"(?<![0-9a-z\u0600-\u06ff]){re.escape(alias)}(?![0-9a-z\u0600-\u06ff])"
        )
        for match in pattern.finditer(normalized):
            mentions.append(
                {
                    "canonical": canonical,
                    "alias": alias,
                    "start": match.start(),
                    "end": match.end(),
                    "score": 1.0 + min(len(alias), 50) / 1000.0,
                }
            )

    # Fuzzy matching is token based only. It catches spelling/transliteration drift,
    # not arbitrary semantic guesses. Exact phrase hits always outrank it.
    single_token_aliases = [
        (alias, canonical)
        for alias, canonical in aliases.items()
        if " " not in alias and len(alias) >= 4
    ]
    for token_match in re.finditer(r"[0-9a-z\u0600-\u06ff]+", normalized):
        token = token_match.group(0)
        if len(token) < 4:
            continue
        best: tuple[float, str, str] | None = None
        second = 0.0
        for alias, canonical in single_token_aliases:
            ratio = _fuzzy_ratio(token, alias)
            threshold = 0.86 if max(len(token), len(alias)) <= 6 else 0.80
            if ratio < threshold:
                continue
            if best is None or ratio > best[0]:
                if best is not None:
                    second = max(second, best[0])
                best = (ratio, alias, canonical)
            else:
                second = max(second, ratio)
        if best is not None and (best[0] - second >= 0.04 or second == 0.0):
            mentions.append(
                {
                    "canonical": best[2],
                    "alias": best[1],
                    "start": token_match.start(),
                    "end": token_match.end(),
                    "score": best[0],
                }
            )

    mentions.sort(
        key=lambda item: (
            int(item["start"]),
            -float(item["score"]),
            -len(str(item["alias"])),
        )
    )
    selected: list[dict[str, Any]] = []
    for mention in mentions:
        duplicate = False
        for existing in selected:
            overlap = not (
                mention["end"] <= existing["start"]
                or mention["start"] >= existing["end"]
            )
            if overlap and mention["canonical"] == existing["canonical"]:
                duplicate = True
                break
        if not duplicate:
            selected.append(mention)
    return selected


_ORIGIN_MARKER_RE = re.compile(
    r"(?:^|\s)(?:from|mn|men|min|de|depuis|من|عن)(?:\s|$)", re.IGNORECASE
)
_DESTINATION_MARKER_RE = re.compile(
    r"(?:^|\s)(?:to|towards?|vers|au|aux|3a|3al|3ala|aal|ala|la|الى|الي|إلى|على)(?:\s|$)",
    re.IGNORECASE,
)


def extract_shipping_request_slots(
    value: str | None,
    *,
    catalogue_origins: Iterable[str | None] = (),
    catalogue_destinations: Iterable[str | None] = (),
    catalogue_goods: Iterable[str | None] = (),
    catalogue_methods: Iterable[str | None] = (),
) -> dict[str, Any]:
    """Extract route/category/method slots from supported multilingual wording.

    Values are canonical lookup keys. Exact row labels and prices still come from the
    database, and no customer-facing amount is inferred here.
    """
    text = str(value or "")
    normalized = _fold(text)
    place_aliases = _extended_aliases(
        _PLACE_ALIASES, [*catalogue_origins, *catalogue_destinations]
    )
    goods_aliases = _extended_aliases(_GOODS_ALIASES, catalogue_goods)
    method_aliases = _extended_aliases(_METHOD_ALIASES, catalogue_methods)

    places = _find_alias_mentions(text, place_aliases)
    goods_mentions = _find_alias_mentions(text, goods_aliases)
    method_mentions = _find_alias_mentions(text, method_aliases)

    unique_places: list[dict[str, Any]] = []
    seen_places: set[str] = set()
    for mention in sorted(places, key=lambda item: int(item["start"])):
        canonical = str(mention["canonical"])
        if canonical not in seen_places:
            seen_places.add(canonical)
            unique_places.append(mention)

    origin: str | None = None
    destination: str | None = None
    role_by_position: dict[int, str] = {}
    for index, mention in enumerate(unique_places):
        left = normalized[max(0, int(mention["start"]) - 32) : int(mention["start"])]
        left_tail = " ".join(left.split()[-5:])
        if _ORIGIN_MARKER_RE.search(left_tail):
            role_by_position[index] = "origin"
        elif _DESTINATION_MARKER_RE.search(left_tail):
            role_by_position[index] = "destination"

    for index, mention in enumerate(unique_places):
        role = role_by_position.get(index)
        if role == "origin" and origin is None:
            origin = str(mention["canonical"])
        elif role == "destination" and destination is None:
            destination = str(mention["canonical"])

    if len(unique_places) >= 2:
        origin = origin or str(unique_places[0]["canonical"])
        destination = destination or next(
            (
                str(item["canonical"])
                for item in unique_places
                if str(item["canonical"]) != origin
            ),
            None,
        )
    elif len(unique_places) == 1:
        only = str(unique_places[0]["canonical"])
        role = role_by_position.get(0)
        if role == "origin":
            origin = only
        elif role == "destination":
            destination = only

    goods_type = None
    if goods_mentions:
        best_goods = max(
            goods_mentions,
            key=lambda item: (float(item["score"]), len(str(item["alias"]))),
        )
        goods_type = str(best_goods["canonical"])

    shipping_method = None
    if method_mentions:
        best_method = max(
            method_mentions,
            key=lambda item: (float(item["score"]), len(str(item["alias"]))),
        )
        shipping_method = str(best_method["canonical"])

    return {
        "origin": origin,
        "destination": destination,
        "goods_type": goods_type,
        "shipping_method": shipping_method,
        "shipping_method_explicit": shipping_method is not None,
        "place_mentions": [str(item["canonical"]) for item in unique_places],
    }


def _match_score(requested: str | None, candidate: str | None, aliases: dict[str, str]) -> int:
    request_folded = _fold(requested)
    candidate_folded = _fold(candidate)
    if not request_folded or not candidate_folded:
        return 0

    request_canonical = _canonical(requested, aliases)
    candidate_canonical = _canonical(candidate, aliases)
    if request_canonical == candidate_canonical:
        return 100
    if request_folded == candidate_folded:
        return 95

    request_tokens = set(request_folded.split())
    candidate_tokens = set(candidate_folded.split())
    if request_tokens and request_tokens <= candidate_tokens:
        return 80
    if candidate_tokens and candidate_tokens <= request_tokens:
        return 75
    if len(request_folded) >= 3 and request_folded in candidate_folded:
        return 65
    if len(candidate_folded) >= 3 and candidate_folded in request_folded:
        return 60
    return 0


def _goods_concepts(value: str | None) -> set[str]:
    """Return every supported goods concept explicitly present in a label.

    Some catalogue rows intentionally combine categories, for example
    ``Makeup & Electronics`` or ``Clothes, Accessories, Electronics``. Choosing only
    the longest alias collapses those rows to one category and makes the other valid
    category impossible to match. This function keeps all represented concepts.
    """
    text = str(value or "")
    mentions = _find_alias_mentions(text, _GOODS_ALIASES)
    concepts = {str(item["canonical"]) for item in mentions}

    # ``Normal`` is frequently a service/price qualifier rather than an assertion that
    # the row is general goods (for example ``Normal (Clothes & Accessories)``). Keep
    # normal/general alongside another concept only when the label actually says
    # ``general`` or a multi-word general-goods alias.
    if "normal general" in concepts and len(concepts) > 1:
        folded = _fold(text)
        explicit_general = bool(
            re.search(r"(?:^|\s)general(?:\s|$)", folded)
            or "general goods" in folded
            or "normal general" in folded
        )
        if not explicit_general:
            concepts.discard("normal general")

    if not concepts:
        fallback = _canonical(text, _GOODS_ALIASES)
        if fallback:
            concepts.add(fallback)
    return concepts


def _goods_match_score(requested: str | None, candidate: str | None) -> int:
    request_folded = _fold(requested)
    candidate_folded = _fold(candidate)
    if not request_folded or not candidate_folded:
        return 0
    if request_folded == candidate_folded:
        return 110

    requested_concepts = _goods_concepts(requested)
    candidate_concepts = _goods_concepts(candidate)
    if requested_concepts and requested_concepts <= candidate_concepts:
        # A single requested category may validly match a combined catalogue row. An
        # exact concept set still outranks a broader combined row when both exist.
        return 105 if requested_concepts == candidate_concepts else 100

    return _match_score(requested, candidate, _GOODS_ALIASES)


def _decimal(value: float | Decimal | str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ShipmentDBError("Invalid shipment weight.") from exc
    if not result.is_finite() or result <= 0:
        raise ShipmentDBError("Invalid shipment weight.")
    return result


def _money_text(amount: Decimal, prefix: str, suffix: str) -> str:
    rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    number = format(rounded, ".2f")
    marker = prefix or suffix
    if marker in {"$", "€", "£", "US$"}:
        return f"{marker}{number}"
    if marker:
        return f"{number} {marker.upper()}"
    return number


def _parse_per_kg_rates(price: Any, weight_kg: Decimal) -> dict[str, Any]:
    raw = str(price or "").strip()
    options: list[dict[str, Any]] = []

    for index, match in enumerate(_MONEY_RATE_RE.finditer(raw), start=1):
        amount_text = match.group("amount")
        if "," in amount_text and "." not in amount_text:
            amount_text = amount_text.replace(",", ".")
        try:
            rate = Decimal(amount_text)
        except InvalidOperation:
            continue
        if rate < 0:
            continue

        label = str(match.group("label") or "").strip()
        service = _canonical(label, _SERVICE_ALIASES) if label else "rate"
        if service not in {"pickup", "delivery"}:
            service = _fold(label) or ("rate" if index == 1 else f"rate_{index}")

        prefix = str(match.group("prefix") or "").upper()
        suffix = str(match.group("suffix") or "").upper()
        total = rate * weight_kg
        option = {
            "service": service,
            "source_label": label or None,
            "currency": prefix or suffix or None,
            "rate_per_kg": format(rate, "f"),
            "rate_display": _money_text(rate, prefix, suffix) + "/kg",
            "weight_kg": format(weight_kg, "f"),
            "total": format(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f"),
            "total_display": _money_text(total, prefix, suffix),
        }
        options.append(option)

    # Do not return duplicate captures for the same service/rate/currency.
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for option in options:
        key = (option["service"], option["rate_per_kg"], option["currency"])
        if key not in seen:
            seen.add(key)
            deduped.append(option)

    totals = {
        option["service"]: option["total_display"]
        for option in deduped
        if option["service"] in {"pickup", "delivery"}
    }
    return {
        "available": bool(deduped),
        "basis": "per_kg" if deduped else None,
        "weight_kg": format(weight_kg, "f"),
        "options": deduped,
        "calculated_totals": totals,
        "rounding": "currency totals rounded to 0.01 using ROUND_HALF_UP" if deduped else None,
    }


class ShipmentClient:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None
        schema = str(settings.shipping_db_schema or "shipping_db").strip()
        if not _IDENTIFIER_RE.fullmatch(schema):
            raise RuntimeError("SHIPPING_DB_SCHEMA contains invalid characters.")
        self._shipping_rates_table = f"`{schema}`.`shipping_rates`"
        self._shipping_destinations_table = f"`{schema}`.`destinations`"
        self._catalog_lock = asyncio.Lock()
        self._rates_cache: list[dict[str, Any]] | None = None
        self._destinations_cache: list[dict[str, Any]] | None = None
        self._rates_cache_at = 0.0
        self._destinations_cache_at = 0.0

    async def connect(self) -> None:
        """Create the MariaDB connection pool."""
        if self._pool is not None:
            return
        try:
            self._pool = await aiomysql.create_pool(
                host=settings.db_host,
                port=settings.db_port,
                user=settings.db_user,
                password=settings.db_password,
                db=settings.db_name,
                minsize=1,
                maxsize=10,
                autocommit=True,
                charset="utf8mb4",
            )
        except Exception as exc:
            raise ShipmentDBError("Could not connect to the shipment database.") from exc

    async def close(self) -> None:
        """Close the MariaDB connection pool."""
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
        self._rates_cache = None
        self._destinations_cache = None
        self._rates_cache_at = 0.0
        self._destinations_cache_at = 0.0

    async def _fetchone(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> dict[str, Any] | None:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute(query, params)
                    return await cursor.fetchone()
        except ShipmentDBError:
            raise
        except Exception as exc:
            raise ShipmentDBError("Shipment database query failed.") from exc

    async def _fetchall(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute(query, params)
                    return await cursor.fetchall()
        except ShipmentDBError:
            raise
        except Exception as exc:
            raise ShipmentDBError("Shipment database query failed.") from exc

    async def _execute(
        self,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> int:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(query, params)
                    return cursor.rowcount
        except ShipmentDBError:
            raise
        except Exception as exc:
            raise ShipmentDBError("Shipment database operation failed.") from exc

    # ------------------------------------------------------------------
    # GENERAL SHIPPING INFORMATION
    # ------------------------------------------------------------------

    async def _load_shipping_rates(self) -> list[dict[str, Any]]:
        # Fetching the small rate catalogue and ranking in Python makes matching
        # deterministic across English, French, Arabic, and Arabizi aliases. It also
        # avoids exact SQL equality failures such as "Air" vs "Air (Daily)".
        ttl = max(0, int(settings.shipping_catalog_cache_seconds))
        now = time.monotonic()
        if (
            ttl > 0
            and self._rates_cache is not None
            and now - self._rates_cache_at <= ttl
        ):
            return [dict(row) for row in self._rates_cache]
        async with self._catalog_lock:
            now = time.monotonic()
            if (
                ttl > 0
                and self._rates_cache is not None
                and now - self._rates_cache_at <= ttl
            ):
                return [dict(row) for row in self._rates_cache]
            rows = await self._fetchall(
                f"""
                SELECT
                    id,
                    destination_id,
                    origin,
                    destination,
                    shipping_method,
                    goods_type,
                    price,
                    transit_time
                FROM {self._shipping_rates_table}
                ORDER BY id
                """
            )
            self._rates_cache = [dict(row) for row in rows]
            self._rates_cache_at = time.monotonic()
            return [dict(row) for row in rows]

    async def _load_shipping_destinations(self) -> list[dict[str, Any]]:
        """Load destination labels from the catalogue's destination table.

        ``shipping_rates`` remains the authority for an actually priced route. The
        destination table is used for route discovery and for a precise distinction
        between "destination is listed" and "a priced route row exists".
        """
        ttl = max(0, int(settings.shipping_catalog_cache_seconds))
        now = time.monotonic()
        if (
            ttl > 0
            and self._destinations_cache is not None
            and now - self._destinations_cache_at <= ttl
        ):
            return [dict(row) for row in self._destinations_cache]
        async with self._catalog_lock:
            now = time.monotonic()
            if (
                ttl > 0
                and self._destinations_cache is not None
                and now - self._destinations_cache_at <= ttl
            ):
                return [dict(row) for row in self._destinations_cache]
            rows = await self._fetchall(
                f"""
                SELECT id, slug, label
                FROM {self._shipping_destinations_table}
                ORDER BY id
                """
            )
            self._destinations_cache = [dict(row) for row in rows]
            self._destinations_cache_at = time.monotonic()
            return [dict(row) for row in rows]

    async def get_shipping_catalog_summary(self) -> dict[str, list[str]]:
        """Return compact English catalogue labels for semantic normalization.

        Prices are intentionally excluded. The semantic layer may translate customer
        wording into these labels, but it never receives authority to calculate or
        select a price.
        """
        rows = await self._load_shipping_rates()
        try:
            destinations = await self._load_shipping_destinations()
        except ShipmentDBError:
            # A rolling deployment may temporarily grant the app access to rates before
            # destinations. Pricing remains usable, and destination labels can be
            # derived conservatively from the rate rows until permissions are aligned.
            destinations = []

        def unique(values: Iterable[Any]) -> list[str]:
            output: list[str] = []
            seen: set[str] = set()
            for value in values:
                text = str(value or "").strip()
                folded = text.casefold()
                if not text or folded in seen:
                    continue
                seen.add(folded)
                output.append(text)
            return output

        destination_values: list[Any] = [
            *(row.get("label") for row in destinations),
            *(row.get("slug") for row in destinations),
            *(row.get("destination") for row in rows),
        ]
        return {
            "origins": unique(row.get("origin") for row in rows),
            "destinations": unique(destination_values),
            "goods_types": unique(row.get("goods_type") for row in rows),
            "shipping_methods": unique(row.get("shipping_method") for row in rows),
        }

    async def resolve_shipping_request_slots(self, text: str | None) -> dict[str, Any]:
        """Resolve customer wording against aliases plus the live rate catalogue."""
        rows = await self._load_shipping_rates()
        return extract_shipping_request_slots(
            text,
            catalogue_origins=(row.get("origin") for row in rows),
            catalogue_destinations=(row.get("destination") for row in rows),
            catalogue_goods=(row.get("goods_type") for row in rows),
            catalogue_methods=(row.get("shipping_method") for row in rows),
        )

    @staticmethod
    def _rank_route_rows(
        rows: Iterable[dict[str, Any]],
        *,
        origin: str | None,
        destination: str | None,
        goods_type: str | None,
        shipping_method: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        route_rows: list[dict[str, Any]] = []
        full_matches: list[dict[str, Any]] = []

        for row in rows:
            origin_score = (
                _match_score(origin, row.get("origin"), _PLACE_ALIASES)
                if origin
                else 0
            )
            destination_score = (
                _match_score(destination, row.get("destination"), _PLACE_ALIASES)
                if destination
                else 0
            )
            if origin and not origin_score:
                continue
            if destination and not destination_score:
                continue

            goods_score = (
                _goods_match_score(goods_type, row.get("goods_type"))
                if goods_type
                else 0
            )
            method_score = (
                _match_score(shipping_method, row.get("shipping_method"), _METHOD_ALIASES)
                if shipping_method
                else 0
            )
            ranked = dict(row)
            ranked["_score"] = origin_score + destination_score + goods_score + method_score
            ranked["_origin_match"] = not origin or origin_score > 0
            ranked["_destination_match"] = not destination or destination_score > 0
            ranked["_goods_match"] = not goods_type or goods_score > 0
            ranked["_method_match"] = not shipping_method or method_score > 0
            route_rows.append(ranked)
            if ranked["_goods_match"] and ranked["_method_match"]:
                full_matches.append(ranked)

        route_rows.sort(key=lambda row: (-int(row["_score"]), int(row.get("id") or 0)))
        full_matches.sort(key=lambda row: (-int(row["_score"]), int(row.get("id") or 0)))
        return route_rows, full_matches

    @staticmethod
    def _public_route_option(row: dict[str, Any]) -> dict[str, Any]:
        """Return only customer-facing rate-catalog fields for one route row."""
        return {
            "origin": row.get("origin"),
            "destination": row.get("destination"),
            "shipping_method": row.get("shipping_method"),
            "goods_type": row.get("goods_type"),
            "price": row.get("price"),
            "transit_time": row.get("transit_time"),
        }

    async def get_route_shipping_options(
        self,
        origin: str | None = None,
        destination: str | None = None,
        goods_type: str | None = None,
        shipping_method: str | None = None,
    ) -> dict[str, Any]:
        """List public rate-catalog categories and services.

        At least one route/category/method filter is useful but not mandatory. This
        allows precise answers to all of these without inventing a route:

        - what can I ship from USA to Lebanon?
        - can I ship to Iraq?
        - which destinations are currently listed?
        - are electronics listed for shipments to Syria?

        Returned categories are rows with company rate entries, not an exhaustive
        customs, dangerous-goods, or prohibited-items policy.
        """
        origin = str(origin or "").strip() or None
        destination = str(destination or "").strip() or None
        goods_type = str(goods_type or "").strip() or None
        shipping_method = str(shipping_method).strip() if shipping_method else None

        rows = await self._load_shipping_rates()
        try:
            destination_rows = await self._load_shipping_destinations()
        except ShipmentDBError:
            destination_rows = []

        route_rows, full_matches = self._rank_route_rows(
            rows,
            origin=origin,
            destination=destination,
            goods_type=goods_type,
            shipping_method=shipping_method,
        )

        supported_destinations: list[str] = []
        seen_destinations: set[str] = set()
        for raw in [
            *(row.get("label") for row in destination_rows),
            *(row.get("destination") for row in rows),
        ]:
            text = str(raw or "").strip()
            if text and text.casefold() not in seen_destinations:
                seen_destinations.add(text.casefold())
                supported_destinations.append(text)

        destination_listed: bool | None = None
        if destination:
            destination_listed = any(
                _match_score(destination, candidate, _PLACE_ALIASES) > 0
                for candidate in supported_destinations
            )

        if not route_rows:
            return {
                "found": False,
                "query": {
                    "origin": origin,
                    "destination": destination,
                    "goods_type": goods_type,
                    "shipping_method": shipping_method,
                },
                "destination_listed": destination_listed,
                "supported_destinations": supported_destinations,
                "message": "No shipping-catalog rows matched the requested filters.",
                "source_table": self._shipping_rates_table.replace("`", ""),
                "destination_source_table": self._shipping_destinations_table.replace(
                    "`", ""
                ),
            }

        has_secondary_filter = bool(goods_type or shipping_method)
        matched_requested_filters = not has_secondary_filter or bool(full_matches)
        selected = full_matches if has_secondary_filter and full_matches else route_rows
        options = [self._public_route_option(row) for row in selected]

        rate_catalog_categories: list[str] = []
        shipping_methods: list[str] = []
        available_origins: list[str] = []
        available_destinations: list[str] = []
        for option in options:
            category = str(option.get("goods_type") or "").strip()
            method = str(option.get("shipping_method") or "").strip()
            option_origin = str(option.get("origin") or "").strip()
            option_destination = str(option.get("destination") or "").strip()
            if category and category not in rate_catalog_categories:
                rate_catalog_categories.append(category)
            if method and method not in shipping_methods:
                shipping_methods.append(method)
            if option_origin and option_origin not in available_origins:
                available_origins.append(option_origin)
            if option_destination and option_destination not in available_destinations:
                available_destinations.append(option_destination)

        result: dict[str, Any] = {
            "found": True,
            "matched_requested_filters": matched_requested_filters,
            "query": {
                "origin": origin,
                "destination": destination,
                "goods_type": goods_type,
                "shipping_method": shipping_method,
            },
            "origin": options[0].get("origin") if origin else None,
            "destination": options[0].get("destination") if destination else None,
            "available_origins": available_origins,
            "available_destinations": available_destinations,
            "supported_destinations": supported_destinations,
            "destination_listed": destination_listed,
            "rate_catalog_categories": rate_catalog_categories,
            "shipping_methods": shipping_methods,
            "options": options,
            "catalog_scope": (
                "These are goods categories with active company rate entries for the "
                "route. They are not an exhaustive customs, dangerous-goods, or "
                "prohibited-items policy."
            ),
            "source_table": self._shipping_rates_table.replace("`", ""),
            "destination_source_table": self._shipping_destinations_table.replace(
                "`", ""
            ),
        }
        if has_secondary_filter and not full_matches:
            result["note"] = (
                "The requested category/method did not match exactly; all available "
                "broader rate-catalog options are returned."
            )
        return result

    @staticmethod
    def _public_rate(row: dict[str, Any], weight_kg: Decimal) -> dict[str, Any]:
        calculation = _parse_per_kg_rates(row.get("price"), weight_kg)
        result = {
            "id": row.get("id"),
            "origin": row.get("origin"),
            "destination": row.get("destination"),
            "shipping_method": row.get("shipping_method"),
            "goods_type": row.get("goods_type"),
            "price": row.get("price"),
            "transit_time": row.get("transit_time"),
            "weight_kg": format(weight_kg, "f"),
            "calculation": calculation,
            "calculated_totals": calculation["calculated_totals"],
        }
        if len(calculation["options"]) == 1:
            result["calculated_total"] = calculation["options"][0]["total_display"]
        return result

    async def get_shipping_price(
        self,
        origin: str,
        destination: str,
        weight_kg: float,
        goods_type: str | None = None,
        shipping_method: str | None = None,
    ) -> dict[str, Any]:
        """Look up a public company rate and calculate explicit per-kg totals."""
        origin = str(origin or "").strip()
        destination = str(destination or "").strip()
        goods_type = str(goods_type).strip() if goods_type else None
        shipping_method = str(shipping_method).strip() if shipping_method else None
        if not origin or not destination:
            raise ShipmentDBError("Origin and destination are required.")
        weight = _decimal(weight_kg)

        route_rows, full_matches = self._rank_route_rows(
            await self._load_shipping_rates(),
            origin=origin,
            destination=destination,
            goods_type=goods_type,
            shipping_method=shipping_method,
        )

        query = {
            "origin": origin,
            "destination": destination,
            "goods_type": goods_type,
            "shipping_method": shipping_method,
            "weight_kg": format(weight, "f"),
        }
        if full_matches:
            best = self._public_rate(full_matches[0], weight)
            return {
                "found": True,
                "matched_requested_filters": True,
                **best,
                "query": query,
                "source_table": self._shipping_rates_table.replace("`", ""),
            }

        if route_rows:
            available = [self._public_rate(row, weight) for row in route_rows]
            unmatched_filters: list[str] = []
            if goods_type and not any(bool(row.get("_goods_match")) for row in route_rows):
                unmatched_filters.append("goods_type")
            if shipping_method and not any(
                bool(row.get("_method_match")) for row in route_rows
            ):
                unmatched_filters.append("shipping_method")
            if not unmatched_filters and goods_type and shipping_method:
                # Each filter may exist somewhere on the route without a single row
                # satisfying both together.
                unmatched_filters.append("filter_combination")
            return {
                "found": True,
                "matched_requested_filters": False,
                "query": query,
                "unmatched_filters": unmatched_filters,
                "available_rates": available,
                "note": (
                    "The route exists, but no single row matched every requested goods "
                    "type/method filter. Calculated route options are returned instead."
                ),
                "source_table": self._shipping_rates_table.replace("`", ""),
            }

        return {
            "found": False,
            "query": query,
            "message": f"No shipping rate was found for {origin} to {destination}.",
            "source_table": self._shipping_rates_table.replace("`", ""),
        }

    async def get_delivery_duration(
        self,
        origin: str,
        destination: str,
        goods_type: str | None = None,
        shipping_method: str | None = None,
    ) -> dict[str, Any]:
        """Get authoritative transit time using the same multilingual matcher."""
        origin = str(origin or "").strip()
        destination = str(destination or "").strip()
        goods_type = str(goods_type).strip() if goods_type else None
        shipping_method = str(shipping_method).strip() if shipping_method else None
        if not origin or not destination:
            raise ShipmentDBError("Origin and destination are required.")

        query = {
            "origin": origin,
            "destination": destination,
            "goods_type": goods_type,
            "shipping_method": shipping_method,
        }

        route_rows, full_matches = self._rank_route_rows(
            await self._load_shipping_rates(),
            origin=origin,
            destination=destination,
            goods_type=goods_type,
            shipping_method=shipping_method,
        )
        selected = full_matches or route_rows
        if not selected:
            return {
                "found": False,
                "query": query,
                "message": f"No delivery-duration information was found for {origin} to {destination}.",
            }

        options = [
            {
                "origin": row.get("origin"),
                "destination": row.get("destination"),
                "shipping_method": row.get("shipping_method"),
                "goods_type": row.get("goods_type"),
                "transit_time": row.get("transit_time"),
                "price": row.get("price"),
            }
            for row in selected
        ]
        return {
            "found": True,
            "matched_requested_filters": bool(full_matches),
            "query": query,
            "origin": options[0]["origin"],
            "destination": options[0]["destination"],
            "shipping_method": options[0]["shipping_method"],
            "goods_type": options[0]["goods_type"],
            "transit_time": options[0]["transit_time"],
            "price": options[0]["price"],
            "available_options": options,
        }

    # ------------------------------------------------------------------
    # CUSTOMER / SHIPMENT DATA
    # ------------------------------------------------------------------

    async def get_customer_balance(
        self,
        customer_id: str,
    ) -> dict[str, Any]:
        user = await self._fetchone(
            """
            SELECT id, userid, name, mobile, email
            FROM users
            WHERE id = %s
            LIMIT 1
            """,
            (customer_id,),
        )

        if not user:
            raise ShipmentDBError("Customer account not found.")

        wallet = await self._fetchone(
            """
            SELECT id, user_id, ballance, ballancet
            FROM user_wallet
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (customer_id,),
        )

        return {
            "customer_id": str(user["id"]),
            "userid": user["userid"],
            "name": user["name"],
            "balance": wallet["ballance"] if wallet else 0,
            "balance_type": wallet["ballancet"] if wallet else "0",
        }

    async def track_shipment(
        self,
        customer_id: str,
        tracking_number: str,
    ) -> dict[str, Any]:
        row = await self._fetchone(
            """
            SELECT
                o.id, o.uid, o.trackid, o.orderid, o.shipmentid, o.number,
                o.description, o.weight, o.qty, o.pod, o.branch, o.receiving_location,
                o.paidamount, o.paidway, o.payment_way, o.paid, o.received,
                o.warehousereceive, o.express_assigned, o.deliveryid, o.status, o.assigneddate,
                o.completedate, o.date,
                u.id AS customer_id, u.userid AS customer_userid, u.name AS customer_name
            FROM orders_new o
            INNER JOIN users u ON o.uid = u.userid
            WHERE u.id = %s
              AND (o.trackid = %s OR o.shipmentid = %s OR o.orderid = %s)
            ORDER BY o.id DESC
            LIMIT 1
            """,
            (customer_id, tracking_number, tracking_number, tracking_number),
        )

        if not row:
            return {
                "found": False,
                "authorized": False,
                "message": "No shipment with this tracking number was found for the authenticated customer.",
            }

        shipment = {
            "found": True,
            "authorized": True,
            "id": row["id"],
            "tracking_number": row["trackid"],
            "order_id": row["orderid"],
            "shipment_id": row["shipmentid"],
            "number": row["number"],
            "description": row["description"],
            "weight": row["weight"],
            "quantity": row["qty"],
            "mode": "pickup" if str(row["pod"]) == "1" else "delivery" if str(row["pod"]) == "2" else str(row["pod"]),
            "branch": row["branch"],
            "receiving_location": row["receiving_location"],
            "paid_amount": row["paidamount"],
            "paid_way": row["paidway"],
            "payment_way": row["payment_way"],
            "paid": row["paid"],
            "received": row["received"],
            "warehouse_received": row["warehousereceive"],
            "express_assigned": row["express_assigned"],
            "delivery_id": row["deliveryid"],
            "status": row["status"],
            "assigned_date": row["assigneddate"],
            "completed_date": row["completedate"],
            "date": row["date"],
        }

        if row["shipmentid"]:
            shipment_row = await self._fetchone(
                """
                SELECT id, branch, scode, sid, sway, pickprice, deliveryprice,
                       driverprice, dubaidate, complete, shipped, reportdate, date
                FROM shipments
                WHERE sid = %s
                LIMIT 1
                """,
                (row["shipmentid"],),
            )

            if shipment_row:
                shipment["shipment"] = {
                    "id": shipment_row["id"],
                    "branch": shipment_row["branch"],
                    "scode": shipment_row["scode"],
                    "sid": shipment_row["sid"],
                    "sway": shipment_row["sway"],
                    "pick_price": shipment_row["pickprice"],
                    "delivery_price": shipment_row["deliveryprice"],
                    "driver_price": shipment_row["driverprice"],
                    "dubai_date": shipment_row["dubaidate"],
                    "complete": shipment_row["complete"],
                    "shipped": shipment_row["shipped"],
                    "report_date": shipment_row["reportdate"],
                    "date": shipment_row["date"],
                }

        return shipment


    async def get_customer_shipments(
        self,
        customer_id: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Return a compact, ownership-scoped shipment list for ambiguity resolution."""
        limit = max(1, min(int(limit), 20))
        rows = await self._fetchall(
            f"""
            SELECT
                o.id, o.trackid, o.orderid, o.shipmentid, o.number,
                o.description, o.status, o.pod, o.branch, o.receiving_location,
                o.assigneddate, o.completedate, o.date
            FROM orders_new o
            INNER JOIN users u ON o.uid = u.userid
            WHERE u.id = %s
            ORDER BY o.id DESC
            LIMIT {limit}
            """,
            (customer_id,),
        )
        return {
            "found": bool(rows),
            "count": len(rows),
            "shipments": [
                {
                    "tracking_number": row.get("trackid"),
                    "order_id": row.get("orderid"),
                    "shipment_id": row.get("shipmentid"),
                    "number": row.get("number"),
                    "description": row.get("description"),
                    "status": row.get("status"),
                    "mode": (
                        "pickup" if str(row.get("pod")) == "1"
                        else "delivery" if str(row.get("pod")) == "2"
                        else str(row.get("pod") or "")
                    ),
                    "branch": row.get("branch"),
                    "receiving_location": row.get("receiving_location"),
                    "assigned_date": row.get("assigneddate"),
                    "completed_date": row.get("completedate"),
                    "date": row.get("date"),
                }
                for row in rows
            ],
        }

    async def update_shipment_mode(
        self,
        customer_id: str,
        tracking_number: str,
        mode: str,
    ) -> dict[str, Any]:
        mode = mode.lower().strip()
        mode_map = {"pickup": "1", "delivery": "2"}

        if mode not in mode_map:
            raise ShipmentDBError("Invalid shipment mode. Use pickup or delivery.")

        new_pod = mode_map[mode]

        existing = await self._fetchone(
            """
            SELECT o.id, o.trackid, o.pod, u.id AS customer_id
            FROM orders_new o
            INNER JOIN users u ON o.uid = u.userid
            WHERE u.id = %s AND o.trackid = %s
            ORDER BY o.id DESC
            LIMIT 1
            """,
            (customer_id, tracking_number),
        )

        if not existing:
            return {
                "updated": False,
                "authorized": False,
                "message": "Shipment not found for the authenticated customer.",
            }

        old_pod = str(existing["pod"])

        if old_pod == new_pod:
            return {
                "updated": False,
                "authorized": True,
                "message": f"Shipment is already set to {mode}.",
                "tracking_number": tracking_number,
                "mode": mode,
            }

        affected = await self._execute(
            """
            UPDATE orders_new o
            INNER JOIN users u ON o.uid = u.userid
            SET o.pod = %s
            WHERE u.id = %s AND o.trackid = %s
            """,
            (new_pod, customer_id, tracking_number),
        )

        return {
            "updated": affected > 0,
            "authorized": True,
            "tracking_number": tracking_number,
            "mode": mode,
            "previous_mode": "pickup" if old_pod == "1" else "delivery" if old_pod == "2" else old_pod,
        }


shipment_client = ShipmentClient()