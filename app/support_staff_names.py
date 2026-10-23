"""Resolve display names by staff ID without rewriting historical snapshots."""
import json
import re
from typing import Any


async def attach_current_staff_names(cursor, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    references = []
    details = []

    def reference(row, id_key, name_key):
        staff_id = str(row.get(id_key) or '')
        # Avoid MariaDB numeric coercion of provider/customer IDs such as '7abc'.
        if re.fullmatch(r'[1-9][0-9]{0,19}', staff_id) and int(staff_id) <= 18446744073709551615:
            references.append((row, staff_id, name_key))

    for row in rows:
        reference(row, 'current_assignee_id', 'current_assignee_name')
        reference(row, 'initiated_by_staff_id', 'initiated_by_staff_name')
        reference(row, 'updated_by_staff_id', 'updated_by_staff_name')
        for prefix in ('sender', 'reactor', 'actor'):
            if row.get(prefix + '_type') == 'AGENT':
                reference(row, prefix + '_id', prefix + '_name')
        if row.get('event_type') == 'TICKET_TRANSFERRED' and row.get('details_json'):
            try:
                payload = json.loads(row['details_json'])
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                reference(payload, 'from_agent_id', 'from_agent_name')
                reference(payload, 'to_agent_id', 'to_agent_name')
                details.append((row, payload))
    if not references:
        return rows
    identifiers = sorted({staff_id for _, staff_id, _ in references})
    placeholders = ','.join(['%s'] * len(identifiers))
    await cursor.execute(f'SELECT id, name FROM staff WHERE deleted_at IS NULL AND id IN ({placeholders})', tuple(identifiers))
    names = {str(row['id']): row['name'] for row in await cursor.fetchall()}
    for row, staff_id, field in references:
        if staff_id in names:
            row[field] = names[staff_id]
    for row, payload in details:
        row['details_json'] = json.dumps(payload, ensure_ascii=False)
    return rows
