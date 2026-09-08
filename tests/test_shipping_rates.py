from __future__ import annotations

import asyncio
import sys
import types

# The production dependency is not installed in this artifact-validation runtime.
# The tests exercise deterministic matching/calculation and replace DB I/O.
fake_aiomysql = types.ModuleType("aiomysql")
setattr(fake_aiomysql, "Pool", object)
setattr(fake_aiomysql, "DictCursor", object)
sys.modules.setdefault("aiomysql", fake_aiomysql)

from app.shipment_client import ShipmentClient


RATE_ROW = {
    "id": 3,
    "destination_id": 1,
    "origin": "UAE",
    "destination": "Lebanon",
    "shipping_method": "Air (Daily)",
    "goods_type": "Cosmetics",
    "price": "Pickup: $12.30/kg, Delivery: $12.80/kg",
    "transit_time": "2-3 business days",
}


def _client_with_rows(rows: list[dict]) -> ShipmentClient:
    client = ShipmentClient()

    async def _load() -> list[dict]:
        return rows

    client._load_shipping_rates = _load  # type: ignore[method-assign]
    return client


def test_cosmetics_rate_calculates_pickup_and_delivery_totals() -> None:
    async def run() -> None:
        result = await _client_with_rows([RATE_ROW]).get_shipping_price(
            origin="UAE",
            destination="Lebanon",
            weight_kg=20,
            goods_type="cosmetics",
            shipping_method="air",
        )
        assert result["found"] is True
        assert result["matched_requested_filters"] is True
        assert result["calculated_totals"] == {
            "pickup": "$246.00",
            "delivery": "$256.00",
        }
        assert result["transit_time"] == "2-3 business days"

    asyncio.run(run())


def test_multilingual_and_dialect_aliases_match_the_same_rate() -> None:
    cases = [
        ("Dubai", "Liban", "produits cosmétiques", "par avion"),
        ("الامارات", "لبنان", "مستحضرات تجميل", "شحن جوي"),
        ("uae", "lebnen", "cosmetics", "bl jaw"),
    ]

    async def run() -> None:
        client = _client_with_rows([RATE_ROW])
        for origin, destination, goods, method in cases:
            result = await client.get_shipping_price(
                origin=origin,
                destination=destination,
                weight_kg=20,
                goods_type=goods,
                shipping_method=method,
            )
            assert result["matched_requested_filters"] is True
            assert result["calculated_totals"]["pickup"] == "$246.00"
            assert result["calculated_totals"]["delivery"] == "$256.00"

    asyncio.run(run())


def test_route_options_are_returned_when_requested_goods_do_not_match() -> None:
    async def run() -> None:
        result = await _client_with_rows([RATE_ROW]).get_shipping_price(
            origin="UAE",
            destination="Lebanon",
            weight_kg=20,
            goods_type="documents",
            shipping_method="air",
        )
        assert result["found"] is True
        assert result["matched_requested_filters"] is False
        assert result["available_rates"][0]["calculated_totals"]["delivery"] == "$256.00"

    asyncio.run(run())

USA_ROUTE_ROWS = [
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
]


def test_arabic_usa_route_question_returns_catalog_categories_without_weight() -> None:
    async def run() -> None:
        result = await _client_with_rows(USA_ROUTE_ROWS).get_route_shipping_options(
            origin="اميركا",
            destination="لبنان",
        )
        assert result["found"] is True
        assert result["matched_requested_filters"] is True
        assert result["origin"] == "USA"
        assert result["destination"] == "Lebanon"
        assert result["rate_catalog_categories"] == [
            "Normal (General)",
            "Cosmetics",
            "Electronics",
        ]
        assert result["shipping_methods"] == ["Air (Tues & Fri)"]
        assert result["options"][0]["price"] == (
            "Pickup: $23.00/kg, Delivery: $23.50/kg"
        )
        assert "not an exhaustive" in result["catalog_scope"]

    asyncio.run(run())


def test_usa_route_aliases_work_in_english_french_arabic_and_arabizi() -> None:
    cases = [
        ("America", "Lebanon", None),
        ("États-Unis", "Liban", "par avion"),
        ("أمريكا", "لبنان", "بالجو"),
        ("amerka", "lebnen", "bl jaw"),
    ]

    async def run() -> None:
        client = _client_with_rows(USA_ROUTE_ROWS)
        for origin, destination, method in cases:
            result = await client.get_route_shipping_options(
                origin=origin,
                destination=destination,
                shipping_method=method,
            )
            assert result["found"] is True
            assert result["matched_requested_filters"] is True
            assert len(result["options"]) == 3
            assert result["rate_catalog_categories"][-1] == "Electronics"

    asyncio.run(run())


def test_route_options_fall_back_to_route_when_method_does_not_match() -> None:
    async def run() -> None:
        result = await _client_with_rows(USA_ROUTE_ROWS).get_route_shipping_options(
            origin="USA",
            destination="Lebanon",
            shipping_method="sea",
        )
        assert result["found"] is True
        assert result["matched_requested_filters"] is False
        assert len(result["options"]) == 3
        assert "all available" in result["note"]

    asyncio.run(run())
