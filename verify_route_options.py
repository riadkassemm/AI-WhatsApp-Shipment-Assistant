"""Production smoke test for public route-catalog lookup.

Run from the project root after copying the patched files:
    python verify_route_options.py
"""
from __future__ import annotations

import asyncio
import json

from app.shipment_client import shipment_client


async def main() -> None:
    try:
        result = await shipment_client.get_route_shipping_options(
            origin="اميركا",
            destination="لبنان",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if not result.get("found"):
            raise SystemExit("USA -> Lebanon route was not found.")
        categories = result.get("rate_catalog_categories") or []
        expected = {"Normal (General)", "Cosmetics", "Electronics"}
        if not expected.issubset(set(categories)):
            raise SystemExit(
                "USA -> Lebanon route is missing expected categories: "
                + ", ".join(sorted(expected - set(categories)))
            )
        print("\nOK: public USA -> Lebanon route options are available.")
    finally:
        await shipment_client.close()


if __name__ == "__main__":
    asyncio.run(main())
