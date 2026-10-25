"""Read-only manager investigation and projections over the existing local audit."""
from collections import defaultdict
from datetime import datetime
import json

import aiomysql
from fastapi import HTTPException
from app.support_timing import LIFECYCLE_EVENTS, responsibility_periods, period_in_range, review_indicators, timing_summary, validate_current_projection
from app.support_staff_names import attach_current_staff_names

AUDIT_EVENTS = (*LIFECYCLE_EVENTS, 'CONVERSATION_VIEWED', 'INCOMING_MARKED_READ')


async def ticket_batch(cur, start, end, before=None, limit=250):
    await cur.execute("""SELECT id,ticket_reference,customer_name,status,active_key,conversation_origin,current_assignee_id
        FROM support_tickets t WHERE t.id<%s AND t.created_at<%s AND
        (t.active_key IS NOT NULL OR t.updated_at>=%s OR EXISTS
         (SELECT 1 FROM support_ticket_events e WHERE e.ticket_id=t.id AND e.created_at>=%s AND e.created_at<%s))
        ORDER BY t.id DESC LIMIT %s""", (before or 9223372036854775807, end, start, start, end, limit))
    return list(await cur.fetchall())


async def load_evidence(cur, tickets, as_of, *, include_messages=True, activity_start=None):
    ids = [t['id'] for t in tickets]
    if not ids:
        return {}, {}
    slots = ','.join(['%s'] * len(ids))
    selected_kinds = AUDIT_EVENTS if include_messages else LIFECYCLE_EVENTS
    kinds = ','.join(['%s'] * len(selected_kinds))
    lifecycle_slots = ','.join(['%s'] * len(LIFECYCLE_EVENTS))
    await cur.execute(f"""SELECT id,ticket_id,event_type,actor_type,actor_id,actor_name,details_json,created_at
        FROM support_ticket_events WHERE ticket_id IN ({slots}) AND created_at<=%s
        AND event_type IN ({kinds}) AND (%s IS NULL OR created_at>=%s OR event_type IN ({lifecycle_slots}))
        ORDER BY ticket_id,created_at,id""", (*ids, as_of, *selected_kinds, activity_start, activity_start, *LIFECYCLE_EVENTS))
    events = defaultdict(list)
    for e in await cur.fetchall():
        events[e['ticket_id']].append(e)
    if not include_messages:
        return events, {}
    await cur.execute(f"""SELECT id,ticket_id,sender_id,send_status,whatsapp_message_id,statistics_sent_at AS sent_at
        FROM support_ticket_messages WHERE ticket_id IN ({slots}) AND sender_type='AGENT'
        AND direction='OUTBOUND' AND sender_id IS NOT NULL AND statistics_sent_at<=%s""", (*ids, as_of))
    messages = defaultdict(list)
    for m in await cur.fetchall():
        messages[m['ticket_id']].append(m)
    return events, messages


async def timing_report(cur, start, end, staff_ids):
    await cur.execute('SELECT UTC_TIMESTAMP(6) AS now')
    now = (await cur.fetchone())['now']
    as_of = min(now, end)
    samples = {str(i): ([], []) for i in staff_ids}
    incomplete = 0
    if start >= as_of:
        return {key: timing_summary([], []) for key in samples}, timing_summary([], []), 0
    before = None
    while True:
        tickets = await ticket_batch(cur, start, end, before)
        if not tickets:
            break
        events, _ = await load_evidence(cur, tickets, as_of, include_messages=False)
        for ticket in tickets:
            result = responsibility_periods(events[ticket['id']], as_of)
            if as_of == now:
                validate_current_projection(result, ticket)
            incomplete += bool(result['issues'] or not result['periods'])
            for period in result['periods']:
                if period['agent_id'] not in samples or not period_in_range(period, start, end):
                    continue
                handling, resolution = samples[period['agent_id']]
                handling.append(period['seconds'])
                if period['resolution_seconds'] is not None:
                    resolution.append(period['resolution_seconds'])
        before = tickets[-1]['id']
    rows = {key: timing_summary(*values) for key, values in samples.items()}
    totals = timing_summary([v for pair in samples.values() for v in pair[0]],
                            [v for pair in samples.values() for v in pair[1]])
    return rows, totals, incomplete


class InvestigationRepository:
    def __init__(self, statistics):
        self.statistics = statistics

    async def tickets(self, start, end, staff_id, before, flagged_only):
        pool = await self.statistics.pool()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute('SELECT UTC_TIMESTAMP(6) AS now')
                    now = (await cur.fetchone())['now']
                    as_of = min(now, end)
                    await cur.execute("SELECT id,name FROM staff WHERE deleted_at IS NULL AND role IN ('agent','supervisor','admin')")
                    names = {str(row['id']): row['name'] for row in await cur.fetchall()}
                    if staff_id is not None and str(staff_id) not in names:
                        raise HTTPException(404, 'Staff account not found.')
                    tickets = await ticket_batch(cur, start, end, before, limit=50) if start < as_of else []
                    events, messages = await load_evidence(cur, tickets, as_of, activity_start=start)
                    result = []
                    for ticket in tickets:
                        audit = events[ticket['id']]
                        projection = responsibility_periods(audit, as_of)
                        if as_of == now:
                            validate_current_projection(projection, ticket)
                        matching = [p for p in projection['periods'] if (staff_id is None or p['agent_id'] == str(staff_id))
                                    and (period_in_range(p, start, end) or (p['ended_at'] is None and p['started_at'] < end))]
                        acted = any(str(e.get('actor_id')) == str(staff_id) and start <= e['created_at'] < end for e in audit)
                        if staff_id is not None and not matching and not acted:
                            continue
                        flags = review_indicators(projection['periods'], audit, messages[ticket['id']], start, as_of, staff_id)
                        flags = [dict(flag, agent_name=names[flag['agent_id']]) for flag in flags if flag['agent_id'] in names]
                        if flagged_only and not flags:
                            continue
                        result.append(dict(ticket, flags=flags, **projection))
                    await cur.execute('SELECT ticket_activity_tracking_started_at FROM support_statistics_metadata WHERE id=1')
                    tracking = (await cur.fetchone())['ticket_activity_tracking_started_at']
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return dict(items=result, next_before=tickets[-1]['id'] if len(tickets) == 50 else None,
                    activity_tracking_started_at=tracking, as_of=as_of)

    async def investigate(self, ticket_id, agent, before_message=None, before_event=None):
        # Do not call ticket_detail: that operational path can enrich/update identity.
        from app.support_service import _serialize_support_messages, _safe_ticket_row, _safe_context_value, _normalize_row
        pool = await self.statistics.pool()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute('SELECT * FROM support_tickets WHERE id=%s', (ticket_id,))
                    ticket = await cur.fetchone()
                    if not ticket:
                        raise HTTPException(404, 'Ticket not found.')
                    await cur.execute('SELECT UTC_TIMESTAMP(6) AS now')
                    now = (await cur.fetchone())['now']
                    evidence, _ = await load_evidence(cur, [ticket], now, include_messages=False)
                    projection = validate_current_projection(responsibility_periods(evidence[ticket_id], now), ticket)
                    await cur.execute("""SELECT * FROM support_ticket_messages m WHERE ticket_id=%s AND id<%s
                        AND NOT EXISTS (SELECT 1 FROM support_message_user_deletions d WHERE d.message_id=m.id AND d.user_id=%s)
                        ORDER BY id DESC LIMIT 101""", (ticket_id, before_message or 9223372036854775807, agent.id))
                    messages = list(await cur.fetchall())
                    await cur.execute('SELECT * FROM support_ticket_events WHERE ticket_id=%s AND id<%s ORDER BY id DESC LIMIT 101',
                                      (ticket_id, before_event or 9223372036854775807))
                    events = list(await cur.fetchall())
                    await attach_current_staff_names(cur, [ticket, *messages, *events])
                    await cur.execute("SELECT id,name FROM staff WHERE deleted_at IS NULL")
                    names = {str(r['id']): r['name'] for r in await cur.fetchall()}
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        messages = await self.statistics.repository._attach_message_reactions(messages)
        for period in projection['periods']:
            period['agent_name'] = names.get(period['agent_id'], 'Former staff #' + period['agent_id'])
        return dict(ticket=_safe_ticket_row(ticket), **projection,
                    original_claimant_name=names.get(projection['original_claimant_id']),
                    messages=_serialize_support_messages(ticket_id, list(reversed(messages[:100]))),
                    events=[_safe_context_value(_normalize_row(e)) for e in reversed(events[:100])],
                    next_message_before=messages[99]['id'] if len(messages)>100 else None,
                    next_event_before=events[99]['id'] if len(events)>100 else None,
                    read_only=True, as_of=now)

    async def record_view(self, ticket_id, agent, request_id):
        from app.support_service import _can_view_ticket
        repository = self.statistics.repository
        ticket = await repository.get_ticket(ticket_id)
        if not ticket or not await _can_view_ticket(ticket, agent):
            raise HTTPException(404, 'Conversation not found.')
        pool = await self.statistics.pool()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute('SELECT id FROM support_tickets WHERE id=%s FOR UPDATE', (ticket_id,))
                    if not await cur.fetchone():
                        raise HTTPException(404, 'Conversation not found.')
                    await cur.execute("""INSERT INTO support_ticket_events
                        (ticket_id,event_type,actor_type,actor_id,actor_name,details_json,created_at,audit_key)
                        VALUES (%s,'CONVERSATION_VIEWED','AGENT',%s,%s,%s,UTC_TIMESTAMP(6),%s)
                        ON DUPLICATE KEY UPDATE audit_key=VALUES(audit_key)""",
                        (ticket_id, agent.id, agent.name, json.dumps({'source':'dashboard', 'session_id':agent.session_id}),
                         f'view:{ticket_id}:{agent.id}:{request_id}'))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
