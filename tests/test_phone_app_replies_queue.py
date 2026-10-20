"""Tickets answered in the linked WhatsApp app leave Unassigned for Active."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import subprocess
import sys
import types
from typing import Any

import pytest

fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {})
)
sys.modules["aiomysql"] = fake_aiomysql

fake_bcrypt = sys.modules.get("bcrypt") or types.ModuleType("bcrypt")
fake_bcrypt.checkpw = getattr(fake_bcrypt, "checkpw", lambda *_args, **_kwargs: False)
fake_bcrypt.hashpw = getattr(fake_bcrypt, "hashpw", lambda *_args, **_kwargs: b"hash")
fake_bcrypt.gensalt = getattr(fake_bcrypt, "gensalt", lambda *_args, **_kwargs: b"salt")
sys.modules["bcrypt"] = fake_bcrypt

fake_openai = sys.modules.get("openai") or types.ModuleType("openai")
if not hasattr(fake_openai, "AsyncOpenAI"):
    class _AsyncOpenAI:
        def __init__(self, *_args, **_kwargs) -> None:
            pass
    fake_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules["openai"] = fake_openai

from app.support_repository import SupportRepository
from app.support_ui import _HTML


class _Cursor:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.calls.append((sql, tuple(params)))

    async def fetchone(self) -> dict[str, Any]:
        return {"total": 0}

    async def fetchall(self) -> list[dict[str, Any]]:
        return []


class _Connection:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls

    async def __aenter__(self) -> "_Connection":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        return _Cursor(self.calls)


class _Pool:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls

    def acquire(self) -> _Connection:
        return _Connection(self.calls)


def _queries(view: str, *, can_view_all: bool, agent_id: str = "7") -> list[tuple[str, tuple[Any, ...]]]:
    calls: list[tuple[str, tuple[Any, ...]]] = []
    repository = SupportRepository()
    repository._pool = _Pool(calls)  # type: ignore[assignment]
    asyncio.run(repository.list_tickets(view=view, agent_id=agent_id, can_view_all=can_view_all))
    return calls


def _database() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.create_function("JSON_UNQUOTE", 1, lambda value: value)
    database.execute(
        "CREATE TABLE support_tickets (id INTEGER, conversation_origin TEXT, status TEXT, active_key TEXT, "
        "current_assignee_id TEXT, initiated_by_staff_id TEXT)"
    )
    database.execute(
        "CREATE TABLE support_ticket_messages (ticket_id INTEGER, direction TEXT, sender_type TEXT, sender_id TEXT)"
    )
    database.execute(
        "CREATE TABLE support_ticket_events (ticket_id INTEGER, event_type TEXT, actor_id TEXT, details_json TEXT)"
    )
    database.executemany(
        "INSERT INTO support_tickets VALUES (?, ?, ?, ?, ?, NULL)",
        [
            (1, "CUSTOMER", "NEW", "line:1", None),  # nobody has answered yet
            (2, "CUSTOMER", "NEW", "line:2", None),  # answered in the WhatsApp app
            (3, "CUSTOMER", "NEW", "line:3", None),  # only the customer and the bot wrote
            (4, "CUSTOMER", "ASSIGNED", "line:4", "5"),  # claimed by another agent
            (5, "CUSTOMER", "IN_PROGRESS", "line:5", "7"),  # claimed by this agent
            (6, "CUSTOMER", "RESOLVED", None, None),  # closed, even though the app answered
        ],
    )
    database.executemany(
        "INSERT INTO support_ticket_messages VALUES (?, ?, ?, ?)",
        [
            (1, "INBOUND", "CUSTOMER", None),
            (2, "INBOUND", "CUSTOMER", None),
            (2, "OUTBOUND", "AGENT", None),
            (3, "INBOUND", "CUSTOMER", None),
            (3, "OUTBOUND", "SYSTEM", None),
            (4, "OUTBOUND", "AGENT", "5"),
            (5, "OUTBOUND", "AGENT", "7"),
            (6, "OUTBOUND", "AGENT", None),
        ],
    )
    return database


def _ids(database: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[int]:
    query = sql.replace("SELECT COUNT(*) AS total", "SELECT id").replace("%s", "?")
    return sorted(row[0] for row in database.execute(query, params))


def test_unassigned_hides_tickets_answered_in_whatsapp_app() -> None:
    database = _database()
    for can_view_all in (True, False):
        count_sql, params = _queries("unassigned", can_view_all=can_view_all)[0]
        assert _ids(database, count_sql, params) == [1, 3]


@pytest.mark.parametrize(("can_view_all", "expected"), [(True, [2, 4, 5]), (False, [2, 5])])
def test_active_lists_tickets_answered_in_whatsapp_app(can_view_all: bool, expected: list[int]) -> None:
    database = _database()
    count_sql, params = _queries("active", can_view_all=can_view_all)[0]
    assert count_sql.count("%s") == len(params)
    assert _ids(database, count_sql, params) == expected


def test_queue_rows_report_whatsapp_app_replies() -> None:
    _count_sql, list_sql = [sql for sql, _params in _queries("active", can_view_all=True)]
    assert "AS phone_app_replied" in list_sql
    database = _database()
    select = re.search(r"(EXISTS \(.*?\)) AS phone_app_replied", list_sql, re.S)
    assert select is not None
    rows = dict(database.execute(f"SELECT id, {select.group(1)} FROM support_tickets"))
    assert rows == {1: 0, 2: 1, 3: 0, 4: 0, 5: 0, 6: 1}


def _ticket_row_meta(ticket: dict[str, Any]) -> list[str]:
    start = _HTML.index("function createTicketRow(")
    end = _HTML.index("\nfunction ", start + 1)
    script = r"""
function element(tag) {
  return {tag, children: [], dataset: {}, attributes: {}, textContent: '', className: '',
    append(...items) { this.children.push(...items); },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener() {}};
}
global.document = {createElement: element, createTextNode: text => ({textContent: text})};
function createIcon(name) { return {icon: name}; }
function createMetaChip(label, iconName) { return {chip: label, icon: iconName}; }
function conversationStatusBadge(ticket) { return {chip: 'status:' + ticket.status}; }
function createSupportNumberLabel() { return 'line'; }
function initials() { return 'C'; }
function isStaffChat() { return false; }
function fmt() { return 'now'; }
function relativeTime() { return 'now'; }
function parseServerDate() { return new Date(); }
const WAITING_AGE_VIEWS = [];
const WAITING_AGE_WARN_MINUTES = 5;
const WAITING_AGE_ALERT_MINUTES = 15;
function openTicket() {}
const state = {ticketId: null, view: 'active'};
""" + _HTML[start:end] + """
const row = createTicketRow(JSON.parse(process.argv[1]));
const found = [];
(function walk(node) {
  if (!node || typeof node !== 'object') return;
  if (node.className === 'ticket-row-meta') found.push(...node.children.map(child => child.chip));
  for (const child of node.children || []) walk(child);
})(row);
console.log(JSON.stringify(found));
"""
    result = subprocess.run(
        ["node", "-e", script, json.dumps(ticket)], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def test_ticket_row_marks_unclaimed_tickets_answered_in_whatsapp_app() -> None:
    base = {"id": 2, "ticket_reference": "CS-2", "status": "NEW", "customer_name": "Maya"}
    assert "Answered in WhatsApp app" in _ticket_row_meta({**base, "phone_app_replied": 1})
    assert "Answered in WhatsApp app" not in _ticket_row_meta({**base, "phone_app_replied": 0})
    claimed = _ticket_row_meta(
        {**base, "status": "ASSIGNED", "current_assignee_name": "Rana", "phone_app_replied": 1}
    )
    assert "Answered in WhatsApp app" not in claimed and "Rana" in claimed


def test_conversations_started_in_whatsapp_app_do_not_raise_unassigned_alert() -> None:
    start = _HTML.index("if (type === 'ticket_created' && payload.phone_initiated === true) {")
    alert = _HTML.index("notifyNewCustomerActivity(notificationText);")
    assert start < alert
    assert "return;" in _HTML[start:alert]
    assert "New unassigned WhatsApp app conversation" not in _HTML
