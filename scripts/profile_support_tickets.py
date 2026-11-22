#!/usr/bin/env python3
"""Aggregate, counts-only profile of historical support tickets (plan B2, step 2).

Approved by the owner on 2026-11-22: read-only, counts only, no message bodies.
Every query returns rows of (section, key, value) built from counts, buckets and
codes. No message text, names, phone numbers or customer IDs are selected; the
only free-text column read is ``reason``, and only values that look like reason
codes (A-Z, 0-9, _) are reported by name.

Usage
  # Print the SQL (pipe it to `mysql --batch` on the database host, then parse):
  venv/bin/python scripts/profile_support_tickets.py --sql > profile.sql
  mysql --batch --skip-column-names "$DB_NAME" < profile.sql > profile.tsv
  venv/bin/python scripts/profile_support_tickets.py --parse < profile.tsv > profile.json

  # Or run it directly against the database configured in .env:
  venv/bin/python scripts/profile_support_tickets.py --run > profile.json

The SQL starts a READ ONLY transaction, so the database refuses any write.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Customer conversations only (not staff-started chats), deleted tickets excluded.
TICKETS = "support_tickets t WHERE COALESCE(t.conversation_origin, 'CUSTOMER') <> 'STAFF' AND t.deleted_at IS NULL"

_DURATION = """CASE
    WHEN m < 5 THEN '0-5m' WHEN m < 30 THEN '5-30m' WHEN m < 120 THEN '30m-2h'
    WHEN m < 480 THEN '2-8h' WHEN m < 1440 THEN '8-24h' WHEN m < 4320 THEN '1-3d' ELSE '3d+' END"""

QUERIES: dict[str, str] = {
    "totals": f"""
        SELECT 'totals', 'tickets', COUNT(*) FROM {TICKETS}
        UNION ALL SELECT 'totals', 'customers', COUNT(DISTINCT t.whatsapp_phone) FROM {TICKETS}
        UNION ALL SELECT 'totals', 'first_ticket_month', DATE_FORMAT(MIN(t.created_at), '%Y-%m') FROM {TICKETS}
        UNION ALL SELECT 'totals', 'last_ticket_month', DATE_FORMAT(MAX(t.created_at), '%Y-%m') FROM {TICKETS}""",
    "status": f"SELECT 'status', t.status, COUNT(*) FROM {TICKETS} GROUP BY t.status",
    "tickets_per_month": f"""
        SELECT 'tickets_per_month', DATE_FORMAT(t.created_at, '%Y-%m'), COUNT(*) FROM {TICKETS}
        GROUP BY DATE_FORMAT(t.created_at, '%Y-%m')""",
    "tickets_per_line": f"""
        SELECT 'tickets_per_line', COALESCE(t.business_phone_number_id, 'legacy'), COUNT(*) FROM {TICKETS}
        GROUP BY COALESCE(t.business_phone_number_id, 'legacy')""",
    "language": f"SELECT 'language', COALESCE(t.current_language, 'unknown'), COUNT(*) FROM {TICKETS} GROUP BY 1, 2",
    "style": f"SELECT 'style', COALESCE(t.communication_style, 'unknown'), COUNT(*) FROM {TICKETS} GROUP BY 1, 2",
    "reason_codes": f"""
        SELECT 'reason_codes', CASE WHEN t.reason REGEXP BINARY '^[A-Z0-9_]{{3,64}}$' THEN t.reason ELSE '(free text)' END,
               COUNT(*) FROM {TICKETS} GROUP BY 1, 2""",
    "messages_per_ticket": f"""
        SELECT 'messages_per_ticket', CASE WHEN n <= 1 THEN '01' WHEN n <= 5 THEN '02-05' WHEN n <= 10 THEN '06-10'
               WHEN n <= 20 THEN '11-20' WHEN n <= 50 THEN '21-50' ELSE '51+' END, COUNT(*)
        FROM (SELECT t.id, COUNT(m.id) AS n FROM {TICKETS.replace('WHERE', 'LEFT JOIN support_ticket_messages m ON m.ticket_id = t.id WHERE')}
              GROUP BY t.id) per_ticket GROUP BY 1, 2""",
    "messages_by_sender": f"""
        SELECT 'messages_by_sender', CONCAT(m.sender_type, ':', m.direction), COUNT(*)
        FROM support_ticket_messages m JOIN {TICKETS.replace('WHERE', 'ON t.id = m.ticket_id WHERE')} GROUP BY 1, 2""",
    "messages_by_type": f"""
        SELECT 'messages_by_type', CONCAT(m.direction, ':', COALESCE(m.message_type, 'text')), COUNT(*)
        FROM support_ticket_messages m JOIN {TICKETS.replace('WHERE', 'ON t.id = m.ticket_id WHERE')} GROUP BY 1, 2""",
    "average_text_length": f"""
        SELECT 'average_text_length', m.sender_type, ROUND(AVG(CHAR_LENGTH(m.body)))
        FROM support_ticket_messages m JOIN {TICKETS.replace('WHERE', 'ON t.id = m.ticket_id WHERE')}
        AND COALESCE(m.message_type, 'text') = 'text' GROUP BY 1, 2""",
    "tickets_with_inbound_media": f"""
        SELECT 'tickets_with_inbound_media', kind, COUNT(DISTINCT ticket_id) FROM (
            SELECT m.ticket_id, CASE WHEN m.message_type = 'audio' AND m.body = 'Voice note' THEN 'voice_note'
                                     ELSE m.message_type END AS kind
            FROM support_ticket_messages m JOIN {TICKETS.replace('WHERE', 'ON t.id = m.ticket_id WHERE')}
            AND m.direction = 'INBOUND' AND m.message_type IN ('image', 'audio', 'video', 'document', 'location', 'sticker', 'contacts')
        ) media GROUP BY 1, 2""",
    "resolution_time": f"""
        SELECT 'resolution_time', {_DURATION}, COUNT(*) FROM (
            SELECT TIMESTAMPDIFF(MINUTE, t.created_at, t.resolved_at) AS m FROM {TICKETS} AND t.resolved_at IS NOT NULL
        ) durations GROUP BY 1, 2""",
    "first_staff_reply_time": f"""
        SELECT 'first_staff_reply_time', {_DURATION}, COUNT(*) FROM (
            SELECT TIMESTAMPDIFF(MINUTE, t.created_at, MIN(m.created_at)) AS m
            FROM support_ticket_messages m JOIN {TICKETS.replace('WHERE', 'ON t.id = m.ticket_id WHERE')}
            AND m.sender_type = 'AGENT' AND m.direction = 'OUTBOUND' GROUP BY t.id, t.created_at
        ) replies GROUP BY 1, 2""",
    "tickets_per_customer": f"""
        SELECT 'tickets_per_customer', CASE WHEN n = 1 THEN '1' WHEN n <= 3 THEN '2-3' WHEN n <= 10 THEN '4-10'
               ELSE '11+' END, COUNT(*)
        FROM (SELECT t.whatsapp_phone, COUNT(*) AS n FROM {TICKETS} GROUP BY t.whatsapp_phone) per_customer GROUP BY 1, 2""",
    "created_hour_utc": f"SELECT 'created_hour_utc', LPAD(HOUR(t.created_at), 2, '0'), COUNT(*) FROM {TICKETS} GROUP BY 1, 2",
}


def sql_script() -> str:
    parts = ["SET SESSION TRANSACTION READ ONLY;", "START TRANSACTION READ ONLY;"]
    parts += [" ".join(query.split()) + ";" for query in QUERIES.values()]
    parts.append("ROLLBACK;")
    return "\n".join(parts) + "\n"


def _value(raw: str):
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return raw


def parse_rows(rows) -> dict:
    profile: dict[str, dict] = {}
    for row in rows:
        if len(row) != 3:
            continue
        section, key, value = (str(part) for part in row)
        profile.setdefault(section, {})[key] = _value(value)
    return {section: dict(sorted(values.items())) for section, values in profile.items()}


async def run() -> dict:
    import aiomysql
    from app.config import settings

    connection = await aiomysql.connect(
        host=settings.db_host, port=settings.db_port, user=settings.db_user, password=settings.db_password,
        db=settings.db_name, charset="utf8mb4", autocommit=False,
    )
    rows: list = []
    try:
        async with connection.cursor() as cur:
            await cur.execute("SET SESSION TRANSACTION READ ONLY")
            await cur.execute("START TRANSACTION READ ONLY")
            for query in QUERIES.values():
                await cur.execute(query)
                rows.extend(await cur.fetchall())
        await connection.rollback()
    finally:
        connection.close()
    return parse_rows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--sql", action="store_true", help="print the read-only SQL script")
    group.add_argument("--parse", action="store_true", help="turn `mysql --batch` output (stdin) into JSON")
    group.add_argument("--run", action="store_true", help="run against the database in .env")
    args = parser.parse_args()
    if args.sql:
        sys.stdout.write(sql_script())
    elif args.parse:
        rows = [line.rstrip("\n").split("\t") for line in sys.stdin if line.strip()]
        print(json.dumps(parse_rows(rows), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(asyncio.run(run()), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
