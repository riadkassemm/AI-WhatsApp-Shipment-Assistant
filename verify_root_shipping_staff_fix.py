from __future__ import annotations

import asyncio
import re
from typing import Any

import aiomysql

from app.config import settings
from app.conversation_store import Conversation
from app.language import detect_communication_style
from app.shipment_client import shipment_client
from app.shipping_quote_service import maybe_handle_shipping_quote


async def _verify_staff_email_invariant() -> None:
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

            await cursor.execute(
                """
                SELECT LOWER(TRIM(email)) AS canonical_email,
                       COUNT(*) AS account_count,
                       GROUP_CONCAT(id ORDER BY id) AS staff_ids
                FROM staff
                GROUP BY LOWER(TRIM(email))
                HAVING COUNT(*) > 1
                """
            )
            duplicates = list(await cursor.fetchall())
            if duplicates:
                summary = "; ".join(
                    f"{row.get('canonical_email')} (ids {row.get('staff_ids')})"
                    for row in duplicates
                )
                raise RuntimeError(
                    "Duplicate staff emails still exist: "
                    + summary
                    + ". Resolve them in /support and run ensure_staff_email_uniqueness.py."
                )

            await cursor.execute(
                """
                SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = 'staff'
                ORDER BY INDEX_NAME, SEQ_IN_INDEX
                """,
                (settings.db_name,),
            )
            by_index: dict[str, dict[str, Any]] = {}
            for row in await cursor.fetchall():
                name = str(row.get("INDEX_NAME") or "")
                item = by_index.setdefault(
                    name,
                    {"non_unique": int(row.get("NON_UNIQUE", 1)), "columns": []},
                )
                item["columns"].append(str(row.get("COLUMN_NAME") or ""))
            exact_unique = any(
                item["non_unique"] == 0
                and item["columns"] == ["email_normalized"]
                for item in by_index.values()
            )
            if not exact_unique:
                raise RuntimeError(
                    "staff.email_normalized needs an exact single-column UNIQUE index. "
                    "Run: python ensure_staff_email_uniqueness.py"
                )
    finally:
        connection.close()


async def _verify_turkey_two_turn_quote() -> None:
    message = "bade esh7an tyeb mn terkiya 3a lebnen adesh betkallef"
    profile = detect_communication_style(message)
    if profile.style != "leb_arabizi":
        raise RuntimeError(f"Unexpected style detection: {profile}")

    conversation = Conversation(
        phone_number="verification-only",
        current_language=profile.language,
        communication_style=profile.style,
    )
    first = await maybe_handle_shipping_quote(conversation, message)
    if first is None or first.status != "shipping_quote_waiting_weight_kg":
        raise RuntimeError(f"The initial Turkey quote was not retained: {first}")

    second = await maybe_handle_shipping_quote(conversation, "50")
    if second is None or second.status != "shipping_quote_completed":
        raise RuntimeError(f"The 50 kg Turkey quote did not complete: {second}")
    if "$350.00" not in second.reply_text or "$7.00/kg" not in second.reply_text:
        raise RuntimeError(
            "The live Turkey clothes row did not produce $350.00 at $7.00/kg. "
            f"Reply was: {second.reply_text!r}"
        )
    if re.search(r"[\u0600-\u06FF]", second.reply_text):
        raise RuntimeError("The Lebanese Arabizi quote contains Arabic-script letters.")


async def _verify_ksa_followup() -> None:
    conversation = Conversation(
        phone_number="verification-followup",
        current_language="ar-LB",
        communication_style="leb_arabizi",
        last_shipping_quote={
            "origin": "UAE",
            "destination": "Lebanon",
            "goods_type": "Cosmetics",
            "weight_kg": "20",
            "shipping_method_explicit": False,
        },
    )
    outcome = await maybe_handle_shipping_quote(
        conversation,
        "tyb w eza mn el su3udiye",
    )
    if outcome is None:
        raise RuntimeError("KSA follow-up was not recognized.")
    for required in ("$105.00", "$115.00", "bel barr"):
        if required not in outcome.reply_text:
            raise RuntimeError(
                f"KSA follow-up is missing {required!r}: {outcome.reply_text!r}"
            )


async def main() -> None:
    try:
        await _verify_staff_email_invariant()
        await _verify_turkey_two_turn_quote()
        await _verify_ksa_followup()
    finally:
        await shipment_client.close()

    print(
        "OK: backend-owned multilingual quote context and full staff-account lifecycle are ready."
    )


if __name__ == "__main__":
    asyncio.run(main())
