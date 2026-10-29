"""Queue predicates and per-staff dashboard reads; independent of WhatsApp receipts."""
from typing import Any

import aiomysql


def search_pattern(query: str) -> str:
    return '%' + query.strip().replace('=', '==').replace('%', '=%').replace('_', '=_') + '%'


def queue_filters(
    view: str, staff_id: str | None, support_number: str | None,
    chat_status: str, query: str, agent_id: str,
) -> tuple[str, list[Any]]:
    clauses, values = ['1=1'], []
    if support_number:
        clauses.append('support_tickets.business_phone_number_id=%s')
        values.append(support_number)
    if staff_id and view in {'chats', 'active', 'waiting', 'history'}:
        clauses.append('support_tickets.current_assignee_id=%s')
        values.append(staff_id)
    if view == 'chats' and chat_status != 'all':
        closed = "(support_tickets.active_key IS NULL OR support_tickets.status IN ('CLOSED','RESOLVED'))"
        clauses.append(closed if chat_status == 'closed' else f'NOT {closed}')
    if query.strip():
        # Literal substring search, including hidden/deleted-message boundaries.
        term = search_pattern(query)
        columns = ['customer_name', 'customer_userid', 'whatsapp_phone', 'ticket_reference',
                   'tracking_reference', 'current_assignee_name', 'business_display_phone_number']
        predicates = [f"support_tickets.{column} LIKE %s ESCAPE '='" for column in columns]
        values.extend([term] * len(columns))
        predicates.append("""EXISTS (SELECT 1 FROM whatsapp_phone_numbers qn
            WHERE qn.phone_number_id=support_tickets.business_phone_number_id AND qn.display_name LIKE %s ESCAPE '=')""")
        values.append(term)
        predicates.append("""EXISTS (SELECT 1 FROM support_ticket_messages qm
            WHERE qm.ticket_id=support_tickets.id AND qm.deleted_for_everyone_at IS NULL
            AND qm.body LIKE %s ESCAPE '=' AND NOT EXISTS (SELECT 1 FROM support_message_user_deletions qd
                WHERE qd.message_id=qm.id AND qd.user_id=%s))""")
        values.extend([term, agent_id])
        clauses.append('(' + ' OR '.join(predicates) + ')')
    return ' AND '.join(clauses), values


async def attach_dashboard_unread(cur: Any, rows: list[dict[str, Any]], staff_id: str) -> None:
    if not rows:
        return
    ids = [row['id'] for row in rows]
    await cur.execute(f"""SELECT m.ticket_id, COUNT(*) AS unread
        FROM support_ticket_messages m
        LEFT JOIN support_dashboard_reads r ON r.ticket_id=m.ticket_id AND r.staff_id=%s
        WHERE m.ticket_id IN ({','.join(['%s'] * len(ids))}) AND m.direction='INBOUND'
          AND m.id>COALESCE(r.last_message_id,0) AND m.deleted_for_everyone_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM support_message_user_deletions d WHERE d.message_id=m.id AND d.user_id=%s)
        GROUP BY m.ticket_id""", (staff_id, *ids, staff_id))
    counts = {str(row['ticket_id']): int(row['unread']) for row in await cur.fetchall()}
    for row in rows:
        row['dashboard_unread_count'] = counts.get(str(row['id']), 0)


class DashboardRepositoryMixin:
    async def queue_filter_options(self, cur: Any, where: str, params: tuple) -> dict:
        # Options come from this user's visible queue, including historical lines
        # and inactive staff. No staff credentials or unrelated conversations leak.
        await cur.execute(f"""SELECT DISTINCT support_tickets.business_phone_number_id AS id,
            COALESCE(NULLIF(n.display_name,''),NULLIF(n.display_phone_number,''),
                     support_tickets.business_display_phone_number,support_tickets.business_phone_number_id) AS name
            FROM support_tickets LEFT JOIN whatsapp_phone_numbers n
              ON n.phone_number_id=support_tickets.business_phone_number_id
            WHERE ({where}) AND support_tickets.business_phone_number_id IS NOT NULL ORDER BY name,id""", params)
        numbers = list(await cur.fetchall())
        await cur.execute(f"""SELECT DISTINCT support_tickets.current_assignee_id AS id,
            COALESCE(s.name,support_tickets.current_assignee_name,support_tickets.current_assignee_id) AS name
            FROM support_tickets LEFT JOIN staff s ON CAST(s.id AS BINARY)=CAST(support_tickets.current_assignee_id AS BINARY)
            WHERE ({where}) AND support_tickets.current_assignee_id IS NOT NULL ORDER BY name,id""", params)
        return {'support_numbers': numbers, 'staff': list(await cur.fetchall())}

    async def attach_queue_search_matches(self, cur: Any, rows: list[dict], query: str, staff_id: str) -> None:
        if not rows or not query.strip():
            return
        ids = [row['id'] for row in rows]
        await cur.execute(f"""SELECT m.ticket_id,m.id,LEFT(m.body,220) AS preview
            FROM support_ticket_messages m JOIN (
                SELECT qm.ticket_id, MIN(qm.id) AS id FROM support_ticket_messages qm
                WHERE qm.ticket_id IN ({','.join(['%s'] * len(ids))})
                  AND qm.deleted_for_everyone_at IS NULL AND qm.body LIKE %s ESCAPE '='
                  AND NOT EXISTS (SELECT 1 FROM support_message_user_deletions d WHERE d.message_id=qm.id AND d.user_id=%s)
                GROUP BY qm.ticket_id
            ) first_match ON first_match.id=m.id""", (*ids, search_pattern(query), staff_id))
        matches = {str(row['ticket_id']): row for row in await cur.fetchall()}
        for row in rows:
            match = matches.get(str(row['id']))
            if match:
                row['search_message_id'] = match['id']
                row['search_preview'] = match['preview']

    async def mark_dashboard_read(self, ticket_id: int, staff_id: str, message_id: int) -> dict | None:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    # Same ticket lock as inbound insertion: a submitted boundary
                    # never includes a newer message arriving during the request.
                    await cur.execute('SELECT id FROM support_tickets WHERE id=%s FOR UPDATE', (ticket_id,))
                    await cur.fetchone()
                    await cur.execute("""SELECT id FROM support_ticket_messages m
                        WHERE id=%s AND ticket_id=%s AND direction='INBOUND' AND deleted_for_everyone_at IS NULL
                        AND NOT EXISTS (SELECT 1 FROM support_message_user_deletions d WHERE d.message_id=m.id AND d.user_id=%s)""",
                        (message_id, ticket_id, staff_id))
                    if not await cur.fetchone():
                        await conn.rollback()
                        return None
                    await cur.execute("SELECT last_message_id FROM support_dashboard_reads WHERE ticket_id=%s AND staff_id=%s", (ticket_id, staff_id))
                    previous = await cur.fetchone()
                    if not previous or int(previous['last_message_id']) < message_id:
                        await cur.execute("""INSERT INTO support_ticket_events
                            (ticket_id,event_type,actor_type,actor_id,actor_name,details_json,created_at,audit_key)
                            SELECT %s,'INCOMING_MARKED_READ','AGENT',id,name,
                                JSON_OBJECT('mode','dashboard','message_id',%s),UTC_TIMESTAMP(6),%s FROM staff WHERE id=%s
                            ON DUPLICATE KEY UPDATE audit_key=VALUES(audit_key)""",
                            (ticket_id, message_id, f'dashboard-read:{ticket_id}:{staff_id}:{message_id}', staff_id))
                    await cur.execute("""INSERT INTO support_dashboard_reads (ticket_id,staff_id,last_message_id,updated_at)
                        VALUES (%s,%s,%s,UTC_TIMESTAMP(6)) ON DUPLICATE KEY UPDATE
                        last_message_id=GREATEST(last_message_id,VALUES(last_message_id)), updated_at=UTC_TIMESTAMP(6)""",
                        (ticket_id, staff_id, message_id))
                    rows = [{'id': ticket_id}]
                    await attach_dashboard_unread(cur, rows, staff_id)
                await conn.commit()
                return rows[0]
            except BaseException:
                await conn.rollback()
                raise
