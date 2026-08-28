from __future__ import annotations

import asyncio
import json
import re
import sys
import types

import pytest

# Artifact-validation runtime stubs. Production installs the real dependencies.
fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
sys.modules["aiomysql"] = fake_aiomysql

fake_openai = sys.modules.get("openai") or types.ModuleType("openai")


class _AsyncOpenAI:
    pass


fake_openai.AsyncOpenAI = getattr(fake_openai, "AsyncOpenAI", _AsyncOpenAI)
sys.modules["openai"] = fake_openai

from app.config import settings
from app.conversation_store import Conversation
from app.language import detect_communication_style
from app.shipment_client import ShipmentClient, shipment_client
from app.shipping_intent_normalizer import (
    NormalizedShippingIntent,
    normalize_shipping_intent,
)
from app import shipping_intent_normalizer, shipping_quote_service
from app.shipping_quote_service import maybe_handle_shipping_quote


RATE_ROWS = [
    {
        "id": 1,
        "destination_id": 1,
        "origin": "UAE",
        "destination": "Lebanon",
        "shipping_method": "Air (Daily)",
        "goods_type": "Normal (General)",
        "price": "Pickup: $9.25/kg, Delivery: $9.75/kg",
        "transit_time": "2-3 business days",
    },
    {
        "id": 2,
        "destination_id": 1,
        "origin": "UAE",
        "destination": "Lebanon",
        "shipping_method": "Air (Daily)",
        "goods_type": "Electronics",
        "price": "Pickup: $12.25/kg, Delivery: $12.75/kg",
        "transit_time": "2-3 business days",
    },
    {
        "id": 3,
        "destination_id": 1,
        "origin": "UAE",
        "destination": "Lebanon",
        "shipping_method": "Air (Daily)",
        "goods_type": "Cosmetics",
        "price": "Pickup: $12.30/kg, Delivery: $12.80/kg",
        "transit_time": "2-3 business days",
    },
    {
        "id": 4,
        "destination_id": 1,
        "origin": "KSA",
        "destination": "Lebanon",
        "shipping_method": "Land (Every Thursday)",
        "goods_type": "Normal (General)",
        "price": "Pickup: $4.50/kg, Delivery: $5.00/kg",
        "transit_time": "20-25 days from departure",
    },
    {
        "id": 5,
        "destination_id": 1,
        "origin": "KSA",
        "destination": "Lebanon",
        "shipping_method": "Land (Every Thursday)",
        "goods_type": "Cosmetics",
        "price": "Pickup: $5.25/kg, Delivery: $5.75/kg",
        "transit_time": "20-25 days from departure",
    },
    {
        "id": 6,
        "destination_id": 1,
        "origin": "USA",
        "destination": "Lebanon",
        "shipping_method": "Air (Tues & Fri)",
        "goods_type": "Normal (General)",
        "price": "Pickup: $23.00/kg, Delivery: $23.50/kg",
        "transit_time": "12-15 business days",
    },
    {
        "id": 7,
        "destination_id": 1,
        "origin": "USA",
        "destination": "Lebanon",
        "shipping_method": "Air (Tues & Fri)",
        "goods_type": "Cosmetics",
        "price": "Pickup: $25.00/kg, Delivery: $25.50/kg",
        "transit_time": "10-15 business days",
    },
    {
        "id": 8,
        "destination_id": 1,
        "origin": "USA",
        "destination": "Lebanon",
        "shipping_method": "Air (Tues & Fri)",
        "goods_type": "Electronics",
        "price": "Pickup: $25.00/kg, Delivery: $25.50/kg",
        "transit_time": "12-15 business days",
    },
    {
        "id": 20,
        "destination_id": 2,
        "origin": "USA",
        "destination": "Syria",
        "shipping_method": "Air",
        "goods_type": "Clothes & Accessories",
        "price": "$35/kg",
        "transit_time": "15-20 business days",
    },
    {
        "id": 21,
        "destination_id": 2,
        "origin": "USA",
        "destination": "Syria",
        "shipping_method": "Air",
        "goods_type": "Makeup & Electronics",
        "price": "$40/kg",
        "transit_time": "15-20 business days",
    },
]

DESTINATION_ROWS = [
    {"id": 1, "slug": "lebanon", "label": "Lebanon"},
    {"id": 2, "slug": "syria", "label": "Syria"},
]


def _refresh_style(conversation: Conversation, text: str) -> None:
    if not any(ch.isalpha() for ch in text):
        return
    profile = detect_communication_style(
        text,
        previous_style=conversation.communication_style,
    )
    conversation.current_language = profile.language
    conversation.communication_style = profile.style


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    async def rates() -> list[dict]:
        return RATE_ROWS

    async def destinations() -> list[dict]:
        return DESTINATION_ROWS

    monkeypatch.setattr(shipment_client, "_load_shipping_rates", rates)
    monkeypatch.setattr(shipment_client, "_load_shipping_destinations", destinations)
    monkeypatch.setattr(settings, "openai_api_key", "")


def test_full_reported_followup_sequence_is_canonical_and_style_safe(
    fake_catalog: None,
) -> None:
    async def run() -> None:
        conversation = Conversation(
            phone_number="96170000020",
            current_language="ar-LB",
            communication_style="leb_arabizi",
        )

        async def turn(text: str):
            _refresh_style(conversation, text)
            return await maybe_handle_shipping_quote(conversation, text)

        first = await turn(
            "eza bade esh7an mawed tejmil mn el emarat 3a lebnen adesh betkallef"
        )
        assert first is not None
        assert first.reply_text == "2adde wazna bel kg?"

        uae = await turn("25")
        assert uae is not None
        assert "$307.50" in uae.reply_text
        assert "$320.00" in uae.reply_text
        assert conversation.communication_style == "leb_arabizi"

        ksa = await turn("tyb eza bade esh7an mn el su3udiye 3a lebnen")
        assert ksa is not None
        assert "$131.25" in ksa.reply_text
        assert "$143.75" in ksa.reply_text
        assert "bel barr" in ksa.reply_text

        thanks = await turn("tamem ysallemon")
        assert thanks is None
        assert conversation.communication_style == "leb_arabizi"

        missing_category = await turn("electronics adesh?")
        assert missing_category is not None
        assert "aghrad 3adiye" in missing_category.reply_text
        assert "mawed tejmil" in missing_category.reply_text
        assert "25 kg" in missing_category.reply_text

        usa = await turn("electronics men amerka")
        assert usa is not None
        assert "$625.00" in usa.reply_text
        assert "$637.50" in usa.reply_text
        assert conversation.communication_style == "leb_arabizi"
        assert not usa.reply_text.startswith("For ")
        assert not re.search(r"[\u0600-\u06FF]", usa.reply_text)

        accessories = await turn("ekseswar 3a souriya")
        assert accessories is not None
        assert "$875.00" in accessories.reply_text
        assert "$35.00/kg" in accessories.reply_text
        assert "tyeb w accessories" in accessories.reply_text

        accessories_50 = await turn("50kg ekseswar 3a souriya")
        assert accessories_50 is not None
        assert "$1750.00" in accessories_50.reply_text
        assert "$35.00/kg" in accessories_50.reply_text

        iraq = await turn("tyb 3al iraq fine esh7an?")
        assert iraq is not None
        assert iraq.status == "shipping_catalog_options"
        assert "Iraq" in iraq.reply_text
        assert "Lebnen" in iraq.reply_text
        assert "Souria" in iraq.reply_text
        assert "$1750.00" not in iraq.reply_text
        assert "Amerka 3a Lebnen" not in iraq.reply_text
        assert conversation.last_shipping_quote == {
            "destination": "iraq",
            "shipping_method_explicit": False,
        }

    asyncio.run(run())


def test_accessories_matches_combined_english_database_category() -> None:
    async def run() -> None:
        client = ShipmentClient()

        async def rates() -> list[dict]:
            return RATE_ROWS

        client._load_shipping_rates = rates  # type: ignore[method-assign]
        result = await client.get_shipping_price(
            origin="USA",
            destination="Syria",
            weight_kg=50,
            goods_type="ekseswar",
        )
        assert result["matched_requested_filters"] is True
        assert result["goods_type"] == "Clothes & Accessories"
        assert result["calculated_total"] == "$1750.00"

    asyncio.run(run())


def test_short_domain_fragments_keep_established_arabizi_style() -> None:
    style = "leb_arabizi"
    for text in (
        "electronics adesh?",
        "electronics men amerka",
        "ekseswar 3a souriya",
        "50kg ekseswar 3a souriya",
        "tyb 3al iraq fine esh7an?",
    ):
        profile = detect_communication_style(text, previous_style=style)
        assert profile.style == "leb_arabizi"
        style = profile.style

    switched = detect_communication_style(
        "What is the price from America to Lebanon?",
        previous_style="leb_arabizi",
    )
    assert switched.style == "en"


def test_semantic_normalizer_preserves_explicit_unsupported_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        output_text = json.dumps(
            {
                "is_shipping_catalog_request": True,
                "request_kind": "route_options",
                "is_followup_fragment": True,
                "origin": None,
                "destination": "Qatar",
                "goods_type": None,
                "shipping_method": None,
                "weight_value": None,
                "weight_unit": None,
                "explicit_origin": False,
                "explicit_destination": True,
                "explicit_goods_type": False,
                "explicit_shipping_method": False,
                "explicit_weight": False,
            }
        )

    class FakeResponses:
        async def create(self, **_kwargs):
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "shipping_semantic_normalizer_enabled", True)
    monkeypatch.setattr(shipping_intent_normalizer, "_get_client", lambda: FakeClient())

    async def run() -> None:
        normalized = await normalize_shipping_intent(
            user_text="طيب فيني اشحن ع قطر؟",
            catalog={
                "origins": ["USA", "UAE"],
                "destinations": ["Lebanon", "Syria"],
                "goods_types": ["Electronics"],
                "shipping_methods": ["Air"],
            },
            previous_context={"origin": "USA", "destination": "Lebanon"},
        )
        assert normalized is not None
        assert normalized.request_kind == "route_options"
        assert normalized.destination == "Qatar"
        assert normalized.explicit_destination is True
        assert normalized.origin is None

    asyncio.run(run())


def test_semantic_current_turn_destination_replaces_stale_route(
    fake_catalog: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def semantic(**_kwargs):
        return NormalizedShippingIntent(
            is_shipping_catalog_request=True,
            request_kind="route_options",
            is_followup_fragment=True,
            origin=None,
            destination="Qatar",
            goods_type=None,
            shipping_method=None,
            weight_kg=None,
            explicit_origin=False,
            explicit_destination=True,
            explicit_goods_type=False,
            explicit_shipping_method=False,
            explicit_weight=False,
        )

    monkeypatch.setattr(shipping_quote_service, "_semantic_intent", semantic)

    async def run() -> None:
        conversation = Conversation(
            phone_number="96170000021",
            current_language="fr",
            communication_style="fr",
            last_shipping_quote={
                "origin": "USA",
                "destination": "Lebanon",
                "goods_type": "Electronics",
                "weight_kg": "25",
                "shipping_method_explicit": False,
            },
        )
        outcome = await maybe_handle_shipping_quote(
            conversation,
            "Et vers le Qatar, je peux expédier ?",
        )
        assert outcome is not None
        assert "Qatar" in outcome.reply_text
        assert "Lebanon" not in outcome.reply_text
        assert "$625.00" not in outcome.reply_text
        assert conversation.last_shipping_quote == {
            "destination": "Qatar",
            "shipping_method_explicit": False,
        }

    asyncio.run(run())


def test_structured_normalizer_cannot_override_locally_recognized_fields(
    fake_catalog: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def semantic(**_kwargs):
        # Deliberately wrong structured output. The exact deterministic aliases in the
        # customer turn must remain authoritative.
        return NormalizedShippingIntent(
            is_shipping_catalog_request=True,
            request_kind="price_quote",
            is_followup_fragment=False,
            origin="USA",
            destination="Syria",
            goods_type="Electronics",
            shipping_method="air",
            weight_kg="99",
            explicit_origin=True,
            explicit_destination=True,
            explicit_goods_type=True,
            explicit_shipping_method=True,
            explicit_weight=True,
        )

    monkeypatch.setattr(shipping_quote_service, "_semantic_intent", semantic)

    async def run() -> None:
        conversation = Conversation(
            phone_number="96170000022",
            current_language="ar-LB",
            communication_style="leb_arabizi",
        )
        outcome = await maybe_handle_shipping_quote(
            conversation,
            "20kg mawed tejmil mn el emarat 3a lebnen adesh betkallef",
        )
        assert outcome is not None
        assert "$246.00" in outcome.reply_text
        assert "$256.00" in outcome.reply_text
        assert "l Emarat" in outcome.reply_text
        assert "Lebnen" in outcome.reply_text
        assert "Souria" not in outcome.reply_text
        assert "99 kg" not in outcome.reply_text

    asyncio.run(run())


def test_combined_catalogue_categories_match_each_supported_concept() -> None:
    async def run() -> None:
        client = ShipmentClient()

        async def rates() -> list[dict]:
            return [RATE_ROWS[-1]]

        client._load_shipping_rates = rates  # type: ignore[method-assign]
        for goods in ("makeup", "cosmetics", "electronics", "Makeup & Electronics"):
            result = await client.get_shipping_price(
                origin="USA",
                destination="Syria",
                weight_kg=25,
                goods_type=goods,
            )
            assert result["matched_requested_filters"] is True
            assert result["goods_type"] == "Makeup & Electronics"
            assert result["calculated_total"] == "$1000.00"

    asyncio.run(run())
