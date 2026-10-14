"""Apply the additive WhatsApp number display-name migration.

Run with the app environment from the project root. Credentials are read from
the existing settings and are never printed.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aiomysql
from app.config import settings


async def apply() -> None:
    path = ROOT / "migrations/20261014_whatsapp_number_display_names.sql"
    statements = re.sub(r"(?m)^\s*--.*$", "", path.read_text()).split(";")
    connection = await aiomysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        db=settings.db_name,
        connect_timeout=10,
        autocommit=True,
    )
    try:
        async with connection.cursor() as cursor:
            for statement in statements:
                if statement.strip():
                    await cursor.execute(statement)
    finally:
        connection.close()
    print("WhatsApp number display-name migration applied.")


if __name__ == "__main__":
    try:
        asyncio.run(apply())
    except Exception as exc:
        code = exc.args[0] if isinstance(exc, aiomysql.Error) and exc.args else None
        if code in {1044, 1045, 1142, 1143}:
            print("Migration requires ALTER permission on the app database.", file=sys.stderr)
        else:
            print(f"Migration failed ({type(exc).__name__}). Check the database connection and schema.", file=sys.stderr)
        sys.exit(1)
