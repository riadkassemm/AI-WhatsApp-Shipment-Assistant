"""Delayed customer messages against real MariaDB/Redis: correct ticket, once, no AI."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import main
from app.conversation_store import InMemoryConversationStore
from app.whatsapp_client import IncomingWhatsAppMessage


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def routing(infrastructure, monkeypatch):
    repository, sql, redis = infrastructure
    monkeypatch.setattr(main, "conversation_store", InMemoryConversationStore())
    monkeypatch.setattr(main.settings, "whatsapp_max_inbound_age_seconds", 900)
    lookups = []
    original = main.support_service.ticket_for_delayed_inbound

    async def counted(**kwargs):
        lookups.append(kwargs)
        return await original(**kwargs)

    async def no_ai(*_args, **_kwargs):
        raise AssertionError("a delayed message reached the AI")

    monkeypatch.setattr(main.support_service, "ticket_for_delayed_inbound", counted)
    monkeypatch.setattr(main, "get_ai_reply", no_ai)

    async def ticket(ticket_id, phone, *, created, resolved=None, line="90005", assignee="7", status=None):
        active = resolved is None
        await sql(
            """INSERT INTO support_tickets (id,ticket_reference,active_key,whatsapp_phone,reason,ai_summary,status,
                   current_assignee_id,business_phone_number_id,created_at,updated_at,last_activity_at,resolved_at)
               VALUES (%s,%s,%s,%s,'test','test',%s,%s,%s,%s,%s,%s,%s)""",
            (ticket_id, f"LATE-{ticket_id}", f"{line}:{phone}" if active else None, phone,
             status or ("ASSIGNED" if active else "RESOLVED"), assignee, line, created, created, created, resolved),
        )

    async def event(ticket_id, event_type, at):
        await sql("INSERT INTO support_ticket_events (ticket_id,event_type,actor_type,created_at) VALUES (%s,%s,'SYSTEM',%s)",
                  (ticket_id, event_type, at))

    async def deliver(phone, message_id, sent, *, line="90005"):
        response = await main._process_incoming_message(IncomingWhatsAppMessage(
            phone, message_id, "text", "synthetic late message", str(int(sent.replace(tzinfo=timezone.utc).timestamp())),
            business_phone_number_id=line,
        ))
        return response.status_code, json.loads(response.body)

    async def rows(message_id):
        return await sql("SELECT ticket_id, whatsapp_timestamp, created_at FROM support_ticket_messages WHERE whatsapp_message_id=%s",
                         (message_id,))

    async def state(ticket_id):
        return (await sql("SELECT status, active_key, current_assignee_id, resolved_at FROM support_tickets WHERE id=%s",
                          (ticket_id,)))[0]

    return repository, ticket, event, deliver, rows, state, lookups


@pytest.mark.asyncio
async def test_message_delayed_past_the_close_of_its_ticket_is_recorded_there_unchanged(routing):
    _repo, ticket, _event, deliver, rows, state, _lookups = routing
    now = utcnow()
    await ticket(101, "15550000101", created=now - timedelta(hours=2), resolved=now - timedelta(minutes=10))
    before = await state(101)
    sent = now - timedelta(minutes=30)
    assert await deliver("15550000101", "ld:90005:LATE-101", sent) == (200, {"status": "delayed_message_recorded", "ticket_id": 101})
    [row] = await rows("ld:90005:LATE-101")
    assert row["ticket_id"] == 101 and int(row["whatsapp_timestamp"]) == int(sent.replace(tzinfo=timezone.utc).timestamp())
    assert row["created_at"] > sent + timedelta(minutes=29)  # receipt time kept separately
    assert await state(101) == before  # not reopened, reassigned or re-statused


@pytest.mark.asyncio
async def test_an_older_message_never_goes_to_a_newer_ticket(routing):
    _repo, ticket, _event, deliver, rows, state, _lookups = routing
    now = utcnow()
    await ticket(201, "15550000201", created=now - timedelta(hours=3), resolved=now - timedelta(minutes=40))
    await ticket(202, "15550000201", created=now - timedelta(minutes=20))
    assert (await deliver("15550000201", "ld:90005:LATE-201", now - timedelta(hours=1)))[1]["ticket_id"] == 201
    assert [row["ticket_id"] for row in await rows("ld:90005:LATE-201")] == [201]
    assert (await state(202))["status"] == "ASSIGNED"
    # Sent between the two tickets: no conversation owned it, so it is not attached.
    await ticket(203, "15550000203", created=now - timedelta(hours=3), resolved=now - timedelta(minutes=90))
    await ticket(204, "15550000203", created=now - timedelta(minutes=30))
    assert await deliver("15550000203", "ld:90005:GAP", now - timedelta(hours=1)) == (200, {"status": "stale_message_ignored", "reason": "expired"})
    assert await rows("ld:90005:GAP") == []


@pytest.mark.asyncio
async def test_business_lines_are_isolated_for_the_same_customer(routing):
    _repo, ticket, _event, deliver, rows, _state, _lookups = routing
    now = utcnow()
    await ticket(301, "15550000301", created=now - timedelta(hours=2), resolved=now - timedelta(minutes=5), line="90006")
    assert (await deliver("15550000301", "ld:90005:OTHER-LINE", now - timedelta(minutes=30)))[1]["status"] == "stale_message_ignored"
    assert await rows("ld:90005:OTHER-LINE") == []
    assert (await deliver("15550000301", "ld:90006:RIGHT-LINE", now - timedelta(minutes=30), line="90006"))[1]["ticket_id"] == 301


@pytest.mark.asyncio
async def test_reopened_ticket_owns_only_its_open_intervals(routing):
    _repo, ticket, event, deliver, rows, state, _lookups = routing
    now = utcnow()
    await ticket(401, "15550000401", created=now - timedelta(hours=5), status="WAITING_CUSTOMER")
    await event(401, "TICKET_RESOLVED", now - timedelta(hours=4))
    await event(401, "TICKET_REOPENED", now - timedelta(hours=2))
    assert (await deliver("15550000401", "ld:90005:IN-GAP", now - timedelta(hours=3)))[1]["status"] == "stale_message_ignored"
    assert await rows("ld:90005:IN-GAP") == []
    assert (await deliver("15550000401", "ld:90005:REOPENED", now - timedelta(hours=1)))[1]["ticket_id"] == 401
    # Still the active ticket: the normal customer-replied transition applies.
    assert (await state(401))["status"] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_delivery_records_one_message(routing):
    _repo, ticket, _event, deliver, rows, _state, _lookups = routing
    now = utcnow()
    await ticket(501, "15550000501", created=now - timedelta(hours=2), resolved=now - timedelta(minutes=5))
    sent = now - timedelta(minutes=45)
    results = await asyncio.gather(*(deliver("15550000501", "ld:90005:TWICE", sent) for _ in range(2)))
    assert sorted(body["status"] for _status, body in results) in (
        ["delayed_message_recorded", "duplicate_support_message"], ["delayed_message_recorded", "delayed_message_recorded"])
    assert (await deliver("15550000501", "ld:90005:TWICE", sent))[1]["status"] == "duplicate_support_message"
    assert len(await rows("ld:90005:TWICE")) == 1


@pytest.mark.asyncio
async def test_timely_messages_keep_the_live_route(routing):
    _repo, ticket, _event, deliver, rows, _state, lookups = routing
    now = utcnow()
    await ticket(601, "15550000601", created=now - timedelta(hours=1))
    status, _body = await deliver("15550000601", "ld:90005:ON-TIME", now - timedelta(seconds=30))
    assert status == 200 and [row["ticket_id"] for row in await rows("ld:90005:ON-TIME")] == [601]
    assert lookups == []
