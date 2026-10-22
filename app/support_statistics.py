"""Local activity interval accounting and actor-attributed support reports."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiomysql
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from uuid import UUID

from app.support_auth import SupportAgent, get_current_agent, require_support_csrf
from app.support_repository import support_repository

router = APIRouter(prefix="/statistics", tags=["support-statistics"])
IDLE_SECONDS = 60
MAX_GAP_SECONDS = 45
METRICS = ("active_seconds", "tickets_claimed", "tickets_resolved", "messages_sent")


def date_bounds(start: date, end: date, zone: str):
    if end < start or (end - start).days >= 366:
        raise HTTPException(422, "Choose a date range of 1–366 days.")
    try:
        tz = ZoneInfo(zone)
        lower = datetime.combine(start, time.min, tz)
        upper = datetime.combine(end + timedelta(days=1), time.min, tz)
    except (ZoneInfoNotFoundError, ValueError, OverflowError):
        raise HTTPException(422, "Invalid reporting timezone or date range.") from None
    return tuple(value.astimezone(timezone.utc).replace(tzinfo=None) for value in (lower, upper))


class ActivityHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tab_id: UUID
    sequence: int = Field(ge=1, le=9_007_199_254_740_991)
    active: bool = Field(strict=True)
    reset: bool = Field(default=False, strict=True)
    activity_age_seconds: float = Field(ge=0, le=86400, allow_inf_nan=False)


class StatisticsRepository:
    def __init__(self, repository=None):
        self.repository = repository or support_repository

    async def pool(self):
        if self.repository._pool is None:
            await self.repository.connect()
        return self.repository._pool

    async def heartbeat(self, agent: SupportAgent, pulse: ActivityHeartbeat):
        pool = await self.pool()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    # One lock per human, shared by ALL their tabs and sessions.
                    # No provider calls, Redis, or remote database access here.
                    await cur.execute("SELECT id FROM staff WHERE id=%s AND is_active=1 FOR UPDATE", (agent.id,))
                    if not await cur.fetchone():
                        raise HTTPException(401, "Staff session is no longer active.")
                    await cur.execute("""SELECT id FROM staff_sessions WHERE id=%s AND staff_id=%s
                        AND revoked_at IS NULL AND expires_at>UTC_TIMESTAMP(6)""", (agent.session_id, agent.id))
                    if not await cur.fetchone():
                        raise HTTPException(401, "Staff session is no longer active.")
                    # Read time after obtaining the lock so concurrent pulses cannot reverse time.
                    now = await self._now(cur)
                    key = (agent.id, agent.session_id, str(pulse.tab_id))
                    await cur.execute("""SELECT * FROM support_agent_heartbeats
                        WHERE staff_id=%s AND session_id=%s AND tab_id=%s""", key)
                    previous = await cur.fetchone()
                    if previous and pulse.sequence <= previous["sequence_number"]:
                        await conn.commit()
                        return
                    if previous and previous["active_until"] and not pulse.reset:
                        start = previous["last_seen_at"]
                        end = min(now, previous["active_until"])
                        if timedelta(0) <= now - start <= timedelta(seconds=MAX_GAP_SECONDS):
                            await self._merge_interval(cur, agent.id, start, end)
                    until = (now + timedelta(seconds=IDLE_SECONDS - pulse.activity_age_seconds)
                             if pulse.active and pulse.activity_age_seconds < IDLE_SECONDS else None)
                    await cur.execute("""INSERT INTO support_agent_heartbeats
                        (staff_id,session_id,tab_id,sequence_number,last_seen_at,active_until)
                        VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE
                        sequence_number=VALUES(sequence_number),last_seen_at=VALUES(last_seen_at),active_until=VALUES(active_until)""",
                        (*key, pulse.sequence, now, until))
                    await cur.execute("""DELETE FROM support_agent_heartbeats WHERE staff_id=%s
                        AND last_seen_at<%s LIMIT 100""", (agent.id, now - timedelta(days=2)))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    @staticmethod
    async def _now(cur):
        await cur.execute("SELECT UTC_TIMESTAMP(6) AS now")
        return (await cur.fetchone())["now"]

    @staticmethod
    async def _merge_interval(cur, staff_id, start, end):
        while start < end:
            day = start.date()
            boundary = min(end, datetime.combine(day + timedelta(days=1), time.min))
            await cur.execute("""SELECT started_at,ended_at FROM support_agent_activity
                WHERE staff_id=%s AND activity_day=%s AND started_at<=%s AND ended_at>=%s""",
                (staff_id, day, boundary, start))
            overlaps = await cur.fetchall()
            merged_start = min([start, *(row["started_at"] for row in overlaps)])
            merged_end = max([boundary, *(row["ended_at"] for row in overlaps)])
            if overlaps:
                await cur.execute("""DELETE FROM support_agent_activity WHERE staff_id=%s
                    AND activity_day=%s AND started_at<=%s AND ended_at>=%s""", (staff_id, day, boundary, start))
            await cur.execute("INSERT INTO support_agent_activity VALUES (%s,%s,%s,%s)",
                              (staff_id, day, merged_start, merged_end))
            start = boundary

    async def report(self, start, end, staff_id, rank_by, descending):
        pool = await self.pool()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute("SELECT id,name,role,is_active FROM staff WHERE deleted_at IS NULL AND role IN ('agent','supervisor','admin') ORDER BY name,id")
                    agents = list(await cur.fetchall())
                    rows = {str(a["id"]): dict(a, **{metric: 0 for metric in METRICS})
                            for a in agents if staff_id is None or a["id"] == staff_id}
                    if staff_id is not None and not rows:
                        raise HTTPException(404, "Staff account not found.")
                    optional = " AND actor_id=%s" if staff_id is not None else ""
                    args = (start, end, str(staff_id)) if staff_id is not None else (start, end)
                    await cur.execute(f"""SELECT actor_id,event_type,COUNT(DISTINCT ticket_id) AS count
                        FROM support_ticket_events WHERE event_type IN ('TICKET_CLAIMED','TICKET_RESOLVED')
                        AND actor_type='AGENT' AND created_at>=%s AND created_at<%s {optional}
                        GROUP BY actor_id,event_type""", args)
                    for event in await cur.fetchall():
                        if event["actor_id"] in rows:
                            metric = "tickets_claimed" if event["event_type"] == "TICKET_CLAIMED" else "tickets_resolved"
                            rows[event["actor_id"]][metric] = event["count"]
                    optional = " AND sender_id=%s" if staff_id is not None else ""
                    await cur.execute(f"""SELECT sender_id,COUNT(*) AS count FROM support_ticket_messages
                        WHERE sender_type='AGENT' AND direction='OUTBOUND' AND send_status='SENT'
                        AND whatsapp_message_id IS NOT NULL AND sender_id IS NOT NULL
                        AND statistics_sent_at>=%s AND statistics_sent_at<%s {optional} GROUP BY sender_id""", args)
                    for message in await cur.fetchall():
                        if message["sender_id"] in rows:
                            rows[message["sender_id"]]["messages_sent"] = message["count"]
                    optional = " AND staff_id=%s" if staff_id is not None else ""
                    activity_args = (start, end, start.date(), end.date(), start, end)
                    if staff_id is not None:
                        activity_args += (staff_id,)
                    await cur.execute(f"""SELECT staff_id,SUM(TIMESTAMPDIFF(MICROSECOND,
                        GREATEST(started_at,%s),LEAST(ended_at,%s)))/1000000 AS seconds
                        FROM support_agent_activity WHERE activity_day>=%s AND activity_day<=%s
                        AND ended_at>%s AND started_at<%s {optional} GROUP BY staff_id""",
                        activity_args)
                    for activity in await cur.fetchall():
                        if str(activity["staff_id"]) in rows:
                            rows[str(activity["staff_id"])]["active_seconds"] = round(float(activity["seconds"]), 3)
                    await cur.execute("SELECT activity_tracking_started_at FROM support_statistics_metadata WHERE id=1")
                    tracking = (await cur.fetchone())["activity_tracking_started_at"]
                    await cur.execute("""SELECT MIN(created_at) AS since FROM support_ticket_events
                        WHERE event_type IN ('TICKET_CLAIMED','TICKET_RESOLVED') AND actor_type='AGENT'""")
                    tickets_since = (await cur.fetchone())["since"]
                    await cur.execute("""SELECT MIN(statistics_sent_at) AS since FROM support_ticket_messages
                        WHERE sender_type='AGENT' AND direction='OUTBOUND' AND send_status='SENT'
                        AND sender_id IS NOT NULL AND whatsapp_message_id IS NOT NULL""")
                    messages_since = (await cur.fetchone())["since"]
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        ordered = sorted(rows.values(), key=lambda r: ((-1 if descending else 1) * r[rank_by], r["name"].casefold(), r["id"]))
        rank = 0
        for index, row in enumerate(ordered, 1):
            if index == 1 or row[rank_by] != ordered[index-2][rank_by]:
                rank = index
            row["rank"] = rank
        stamp = lambda value: value.isoformat() + "Z" if value else None
        return {"agents": agents, "rows": ordered, "totals": {m: round(sum(r[m] for r in ordered), 3) for m in METRICS},
                "rank_by": rank_by, "direction": "desc" if descending else "asc",
                "activity_tracking_started_at": stamp(tracking), "ticket_history_since": stamp(tickets_since),
                "message_history_since": stamp(messages_since), "idle_timeout_seconds": IDLE_SECONDS}


statistics_repository = StatisticsRepository()


@router.post("/heartbeat")
async def activity_heartbeat(pulse: ActivityHeartbeat, agent: SupportAgent = Depends(require_support_csrf)):
    if agent.session_id is None:
        raise HTTPException(401, "A current staff session is required.")
    await statistics_repository.heartbeat(agent, pulse)
    return {"status": "recorded"}


@router.get("")
async def agent_statistics(
    start: date, end: date, timezone_name: str = "Asia/Beirut",
    agent_id: int | None = Query(default=None, ge=1),
    rank_by: Literal["active_seconds", "tickets_claimed", "tickets_resolved", "messages_sent"] = "tickets_resolved",
    direction: Literal["asc", "desc"] = "desc",
    agent: SupportAgent = Depends(get_current_agent),
):
    if not agent.is_supervisor:
        raise HTTPException(403, "Agent statistics are available to admins and Super Admins only.")
    lower, upper = date_bounds(start, end, timezone_name)
    result = await statistics_repository.report(lower, upper, agent_id, rank_by, direction == "desc")
    return {**result, "start": start.isoformat(), "end": end.isoformat(), "timezone": timezone_name, "agent_id": agent_id}


@router.get("/pdf")
async def download_statistics_pdf(report: dict = Depends(agent_statistics)):
    from starlette.concurrency import run_in_threadpool
    from fastapi.responses import Response
    from app.support_statistics_pdf import statistics_pdf

    content = await run_in_threadpool(statistics_pdf, report)
    filename = f'agent-statistics-{report["start"]}-to-{report["end"]}.pdf'
    return Response(content, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })
