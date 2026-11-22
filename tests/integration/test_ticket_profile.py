"""Counts-only ticket profiling (Step 4b) on synthetic data: correct counts, no text leaves."""
import importlib.util
import json
from pathlib import Path

import pytest


def _profiler():
    spec = importlib.util.spec_from_file_location('profile_support_tickets', Path('scripts/profile_support_tickets.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_profile_reports_only_aggregates(infrastructure):
    repo, sql, _ = infrastructure
    profiler = _profiler()
    await sql("UPDATE support_tickets SET reason='MEDIA_UNSUPPORTED_BY_AI' WHERE id=41")
    await sql("UPDATE support_tickets SET reason='Customer Jana at 15550000042 wants a refund' WHERE id=42")
    await sql("UPDATE support_tickets SET conversation_origin='STAFF' WHERE id=43")
    for ticket_id, sender, direction, kind, body in [
        (41, 'CUSTOMER', 'INBOUND', 'text', 'secret customer sentence 15559998888'),
        (41, 'CUSTOMER', 'INBOUND', 'audio', 'Voice note'),
        (41, 'AGENT', 'OUTBOUND', 'text', 'agent reply text'),
        (42, 'CUSTOMER', 'INBOUND', 'image', 'Image'),
    ]:
        await repo.add_ticket_message(ticket_id=ticket_id, sender_type=sender, sender_id='7' if sender == 'AGENT' else None,
                                      direction=direction, message_type=kind, body=body)
    rows = []
    async with repo._pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("START TRANSACTION READ ONLY")
            for query in profiler.QUERIES.values():
                await cur.execute(query)
                rows.extend(await cur.fetchall())
            await conn.rollback()
    profile = profiler.parse_rows(rows)
    dumped = json.dumps(profile)
    for private in ('secret customer', '15559998888', 'agent reply', 'Jana', '15550000042', '1555000041'):
        assert private not in dumped
    assert profile['totals']['tickets'] == 2  # the staff chat is excluded
    assert profile['reason_codes'] == {'(free text)': 1, 'MEDIA_UNSUPPORTED_BY_AI': 1}
    assert profile['messages_by_sender'] == {'AGENT:OUTBOUND': 1, 'CUSTOMER:INBOUND': 3}
    assert profile['tickets_with_inbound_media'] == {'image': 1, 'voice_note': 1}
    assert profile['messages_per_ticket'] == {'01': 1, '02-05': 1}
    assert profile['first_staff_reply_time'] == {'0-5m': 1}
    assert set(profile) >= {'status', 'tickets_per_month', 'language', 'style', 'resolution_time',
                            'tickets_per_customer', 'created_hour_utc', 'average_text_length'} - {'resolution_time'}


def test_sql_script_is_read_only():
    script = _profiler().sql_script()
    assert script.startswith('SET SESSION TRANSACTION READ ONLY;\nSTART TRANSACTION READ ONLY;')
    assert script.rstrip().endswith('ROLLBACK;')
    statements = [part.strip() for part in script.split(';') if part.strip()]
    assert all(statement.split()[0].upper() in {'SELECT', 'SET', 'START', 'ROLLBACK'} for statement in statements)
    assert 'm.body' not in script.replace('CHAR_LENGTH(m.body)', '').replace("m.body = 'Voice note'", '')
