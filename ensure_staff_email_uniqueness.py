#!/usr/bin/env python3
"""Backfill and enforce one case-insensitive email per support staff account.

This script is deliberately non-destructive. If duplicate canonical emails already
exist, it reports the accounts and exits without changing rows or adding the unique
index. Resolve each duplicate in /support, then rerun.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import aiomysql

from app.config import settings
from app.support_auth import normalize_staff_email


INDEX_NAME = "uq_staff_email_normalized"


async def main() -> int:
    conn = await aiomysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        db=settings.db_name,
        autocommit=False,
        charset="utf8mb4",
    )
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT id, name, email, email_normalized, role, is_active
                FROM staff
                ORDER BY id
                """
            )
            rows = list(await cur.fetchall())

            invalid: list[dict[str, Any]] = []
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                canonical = normalize_staff_email(str(row.get("email") or ""))
                if not canonical or "@" not in canonical:
                    invalid.append(row)
                else:
                    groups[canonical].append(row)

            duplicates = {
                email: members for email, members in groups.items() if len(members) > 1
            }
            if invalid or duplicates:
                print("Staff email uniqueness was NOT changed.")
                if invalid:
                    print("\nAccounts with invalid/empty emails:")
                    for row in invalid:
                        print(
                            f"  id={row.get('id')} role={row.get('role')} "
                            f"active={row.get('is_active')} email={row.get('email')!r}"
                        )
                if duplicates:
                    print("\nDuplicate canonical emails:")
                    for email, members in sorted(duplicates.items()):
                        print(f"  {email}")
                        for row in members:
                            print(
                                f"    id={row.get('id')} name={row.get('name')!r} "
                                f"role={row.get('role')} active={row.get('is_active')}"
                            )
                print(
                    "\nResolve each duplicate group in /support, then rerun this script. "
                    "Keep one account for the email; change the other row's email to a "
                    "unique archival address before deactivating it. For future conflicts, "
                    "edit the existing account's role instead of creating a second row."
                )
                return 2

            for canonical, members in groups.items():
                row = members[0]
                await cur.execute(
                    """
                    UPDATE staff
                    SET email_normalized = %s,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s
                      AND (email_normalized IS NULL OR email_normalized <> %s)
                    """,
                    (canonical, int(row["id"]), canonical),
                )

            await cur.execute(
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
            for row in await cur.fetchall():
                name = str(row.get("INDEX_NAME") or "")
                item = by_index.setdefault(
                    name,
                    {"non_unique": int(row.get("NON_UNIQUE", 1)), "columns": []},
                )
                item["columns"].append(str(row.get("COLUMN_NAME") or ""))
            unique_exists = any(
                item["non_unique"] == 0
                and item["columns"] == ["email_normalized"]
                for item in by_index.values()
            )
            if not unique_exists:
                await cur.execute(
                    f"ALTER TABLE staff ADD UNIQUE KEY {INDEX_NAME} (email_normalized)"
                )

            await conn.commit()
            print(
                "OK: staff.email_normalized is backfilled and protected by a unique index."
            )
            return 0
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
