from __future__ import annotations

import asyncio
import json

from app.shipment_client import shipment_client


async def main() -> None:
    try:
        result = await shipment_client.get_shipping_price(
            origin="UAE",
            destination="Lebanon",
            weight_kg=20,
            goods_type="Cosmetics",
            shipping_method="Air",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        await shipment_client.close()


if __name__ == "__main__":
    asyncio.run(main())
