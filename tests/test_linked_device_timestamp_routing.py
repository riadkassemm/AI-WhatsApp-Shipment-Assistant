from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.support_repository import SupportRepository


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, sql, params):
        if "FROM support_tickets" in sql:
            peer, line, cutoff = params
            self.connection.cutoff = cutoff
            self.rows = sorted(
                (
                    ticket for ticket in self.connection.tickets
                    if ticket["whatsapp_phone"] == peer
                    and ticket["business_phone_number_id"] == line
                    and ticket["created_at"] < cutoff
                ),
                key=lambda ticket: ticket["id"], reverse=True,
            )
        elif "FROM support_ticket_events" in sql:
            ticket_id, cutoff = params
            self.rows = sorted(
                (
                    event for event in self.connection.events
                    if event["ticket_id"] == ticket_id
                    and event["created_at"] < cutoff
                    and event["event_type"] in {"CHAT_STARTED", "CHAT_REOPENED", "CHAT_CLOSED"}
                ),
                key=lambda event: (event["created_at"], event["id"]), reverse=True,
            )[:1]
        else:
            raise AssertionError(sql)

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, tickets, events=()):
        self.tickets = tickets
        self.events = events
        self.cutoff = None

    def cursor(self, *_args):
        return _Cursor(self)


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def _ticket(ticket_id, created_at, *, resolved_at=None, origin="CUSTOMER"):
    return {
        "id": ticket_id, "whatsapp_phone": "96171111111",
        "business_phone_number_id": "90001", "created_at": created_at,
        "resolved_at": resolved_at, "closed_at": None,
        "conversation_origin": origin,
    }


def test_provider_second_includes_ticket_opened_later_that_second():
    sent_at = datetime(2026, 10, 12, 10, 30, 20)
    historical = _ticket(1, sent_at + timedelta(milliseconds=400),
                         resolved_at=sent_at + timedelta(minutes=2))
    newer = _ticket(2, sent_at + timedelta(minutes=3))
    connection = _Connection([historical, newer])
    repository = SupportRepository()
    repository._pool = _Pool(connection)

    found = asyncio.run(repository.find_ticket_at_whatsapp_timestamp(
        "96171111111", "90001", sent_at,
    ))

    assert found == historical
    assert connection.cutoff == sent_at + timedelta(seconds=1)


def test_delayed_message_does_not_attach_to_chat_closed_before_send():
    sent_at = datetime(2026, 10, 12, 10, 30, 20)
    chat = _ticket(1, sent_at - timedelta(days=1), origin="STAFF")
    connection = _Connection([chat], [
        {"id": 1, "ticket_id": 1, "event_type": "CHAT_STARTED", "created_at": sent_at - timedelta(hours=1)},
        {"id": 2, "ticket_id": 1, "event_type": "CHAT_CLOSED", "created_at": sent_at - timedelta(seconds=1)},
    ])
    repository = SupportRepository()
    repository._pool = _Pool(connection)

    found = asyncio.run(repository.find_ticket_at_whatsapp_timestamp(
        "96171111111", "90001", sent_at,
    ))

    assert found is None
