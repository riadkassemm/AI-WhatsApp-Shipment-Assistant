from __future__ import annotations

import asyncio

import aiomysql

from app.config import settings
from app.customer_reply_guard import render_shipping_price, validate_customer_reply
from app.language import detect_communication_style
from app.shipment_client import shipment_client


async def _verify_staff_schema() -> None:
    connection = await aiomysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        db=settings.db_name,
        autocommit=True,
        charset="utf8mb4",
    )
    try:
        async with connection.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("SHOW COLUMNS FROM staff")
            columns = {str(row["Field"]) for row in await cursor.fetchall()}
            required = {
                "id",
                "name",
                "email",
                "email_normalized",
                "password_hash",
                "role",
                "is_active",
                "created_at",
                "updated_at",
            }
            missing = sorted(required - columns)
            if missing:
                raise RuntimeError(
                    "staff table is missing required columns: " + ", ".join(missing)
                )

            await cursor.execute("SHOW INDEX FROM staff")
            indexes = list(await cursor.fetchall())
            has_unique_email = any(
                str(row.get("Column_name") or "") == "email_normalized"
                and int(row.get("Non_unique") or 0) == 0
                for row in indexes
            )
            if not has_unique_email:
                raise RuntimeError(
                    "staff.email_normalized needs a UNIQUE index before enabling "
                    "concurrent web account creation."
                )
    finally:
        connection.close()


async def _verify_ksa_quote() -> None:
    message = "tyb w eza mn el su3udiye"
    profile = detect_communication_style(message)
    if profile.style != "leb_arabizi":
        raise RuntimeError(f"Unexpected style detection: {profile}")

    result = await shipment_client.get_shipping_price(
        origin="el su3udiye",
        destination="lebnen",
        weight_kg=20,
        goods_type="mawed tejmil",
        shipping_method=None,
    )
    if result.get("found") is not True or result.get("matched_requested_filters") is not True:
        raise RuntimeError(f"KSA cosmetics rate did not match: {result}")
    expected = {"pickup": "$105.00", "delivery": "$115.00"}
    if result.get("calculated_totals") != expected:
        raise RuntimeError(
            f"Unexpected KSA 20 kg totals: {result.get('calculated_totals')}"
        )

    rendered = render_shipping_price(result, "leb_arabizi")
    if not rendered or not validate_customer_reply(rendered, "leb_arabizi").safe:
        raise RuntimeError("KSA quote could not be rendered safely in Lebanese Arabizi.")
    if "$105.00" not in rendered or "$115.00" not in rendered:
        raise RuntimeError("Rendered KSA quote is missing calculated totals.")


async def main() -> None:
    try:
        await _verify_staff_schema()
        await _verify_ksa_quote()
    finally:
        await shipment_client.close()

    print("OK: supervisor staff management schema and KSA Arabizi quote are ready.")


if __name__ == "__main__":
    asyncio.run(main())
