from __future__ import annotations

import asyncio
import sys
import types

fake_aiomysql = types.ModuleType("aiomysql")
fake_aiomysql.Pool = object
fake_aiomysql.DictCursor = object
sys.modules.setdefault("aiomysql", fake_aiomysql)

from app.ai_config import CHATBOT_INSTRUCTIONS
from app.ai_tools import NEEDS_VERIFIED_IDENTITY, TOOLS, dispatch_tool_call, shipment_client


def test_route_options_tool_is_public_and_explicitly_available() -> None:
    names = {tool.get("name") for tool in TOOLS}
    assert "get_route_shipping_options" in names
    assert "get_route_shipping_options" not in NEEDS_VERIFIED_IDENTITY
    assert "get_route_shipping_options" in CHATBOT_INSTRUCTIONS
    assert "شو فيني اشحن" in CHATBOT_INSTRUCTIONS


def test_guest_can_dispatch_route_options_without_authentication_or_weight() -> None:
    async def fake_lookup(
        origin: str,
        destination: str,
        shipping_method: str | None = None,
    ) -> dict:
        assert origin == "اميركا"
        assert destination == "لبنان"
        assert shipping_method is None
        return {
            "found": True,
            "rate_catalog_categories": [
                "Normal (General)",
                "Cosmetics",
                "Electronics",
            ],
            "options": [],
        }

    async def run() -> None:
        original = shipment_client.get_route_shipping_options
        shipment_client.get_route_shipping_options = fake_lookup  # type: ignore[method-assign]
        try:
            result = await dispatch_tool_call(
                "get_route_shipping_options",
                {
                    "origin": "اميركا",
                    "destination": "لبنان",
                    "shipping_method": None,
                },
                verified_customer_id=None,
            )
        finally:
            shipment_client.get_route_shipping_options = original  # type: ignore[method-assign]

        assert result["found"] is True
        assert result["rate_catalog_categories"] == [
            "Normal (General)",
            "Cosmetics",
            "Electronics",
        ]
        assert "error" not in result

    asyncio.run(run())
