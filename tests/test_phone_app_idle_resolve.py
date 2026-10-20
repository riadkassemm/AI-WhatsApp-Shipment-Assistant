"""Unclaimed tickets answered in the WhatsApp app resolve themselves after going quiet."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
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

import app.main as main
import app.support_service as support_service_module
from app.conversation_store import Conversation
from app.support_repository import (
    _IDLE_PHONE_APP_TICKET,
    SupportRepository,
    auto_resolve_actor_name,
)
from app.support_service import support_service
from app.support_ui import _HTML


def _ago(minutes: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _idle_ticket_ids(minutes: int = 120) -> list[int]:
    database = sqlite3.connect(":memory:")
    database.execute(
        "CREATE TABLE support_tickets (id INTEGER, conversation_origin TEXT, status TEXT, active_key TEXT, "
        "current_assignee_id TEXT, created_at TEXT)"
    )
    database.execute(
        "CREATE TABLE support_ticket_messages (ticket_id INTEGER, direction TEXT, sender_type TEXT, "
        "sender_id TEXT, created_at TEXT)"
    )
    database.executemany(
        "INSERT INTO support_tickets VALUES (?, ?, 'NEW', ?, ?, ?)",
        [
            (1, "CUSTOMER", "line:1", None, _ago(300)),  # answered in the app, quiet for 3 hours
            (2, "CUSTOMER", "line:2", None, _ago(300)),  # answered in the app, customer wrote 30 min ago
            (3, "CUSTOMER", "line:3", None, _ago(300)),  # answered in the app 1 hour ago
            (4, "CUSTOMER", "line:4", None, _ago(300)),  # never answered from the app
            (5, "CUSTOMER", "line:5", "7", _ago(300)),  # claimed on the dashboard
            (6, "STAFF", "line:6", None, _ago(300)),  # staff-started chat
            (7, "CUSTOMER", None, None, _ago(300)),  # already closed
            (8, "CUSTOMER", "line:8", None, _ago(30)),  # ticket itself is new
        ],
    )
    old, recent = _ago(180), _ago(30)
    database.executemany(
        "INSERT INTO support_ticket_messages VALUES (?, ?, ?, ?, ?)",
        [
            (1, "INBOUND", "CUSTOMER", None, old),
            (1, "OUTBOUND", "AGENT", None, old),
            (2, "OUTBOUND", "AGENT", None, old),
            (2, "INBOUND", "CUSTOMER", None, recent),
            (3, "INBOUND", "CUSTOMER", None, old),
            (3, "OUTBOUND", "AGENT", None, _ago(60)),
            (4, "INBOUND", "CUSTOMER", None, old),
            (4, "OUTBOUND", "AI", None, old),
            (5, "OUTBOUND", "AGENT", None, old),
            (6, "OUTBOUND", "AGENT", None, old),
            (7, "OUTBOUND", "AGENT", None, old),
            (8, "OUTBOUND", "AGENT", None, _ago(200)),
        ],
    )
    sql = (
        f"SELECT id FROM support_tickets WHERE {_IDLE_PHONE_APP_TICKET} ORDER BY id"
        .replace("UTC_TIMESTAMP(6) - INTERVAL %s MINUTE", "datetime('now', printf('-%d minutes', ?))")
    )
    return [row[0] for row in database.execute(sql, (minutes, minutes))]


def test_only_quiet_unclaimed_phone_app_tickets_are_due() -> None:
    assert _idle_ticket_ids() == [1]
    assert _idle_ticket_ids(minutes=20) == [1, 2, 3, 8]


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.connection.calls.append((" ".join(sql.split()), tuple(params)))

    async def fetchone(self) -> dict[str, Any] | None:
        return {"id": 9} if self.connection.still_idle else None

    async def fetchall(self) -> list[dict[str, Any]]:
        return [{"id": 9}, {"id": 12}]


class _Connection:
    def __init__(self, *, still_idle: bool) -> None:
        self.still_idle = still_idle
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.log: list[str] = []

    async def __aenter__(self) -> "_Connection":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def begin(self) -> None:
        self.log.append("begin")

    async def commit(self) -> None:
        self.log.append("commit")

    async def rollback(self) -> None:
        self.log.append("rollback")

    def cursor(self, *_args: Any, **_kwargs: Any) -> _Cursor:
        return _Cursor(self)


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Connection:
        return self.connection


def _repository(connection: _Connection, monkeypatch: pytest.MonkeyPatch) -> SupportRepository:
    repository = SupportRepository()
    repository._pool = _Pool(connection)  # type: ignore[assignment]

    async def get_ticket(ticket_id: int) -> dict[str, Any]:
        return {"id": ticket_id, "status": "RESOLVED"}

    monkeypatch.setattr(repository, "get_ticket", get_ticket)
    return repository


def test_repository_resolves_after_rechecking_under_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection(still_idle=True)
    repository = _repository(connection, monkeypatch)

    resolved = asyncio.run(repository.auto_resolve_idle_phone_app_ticket(9, idle_minutes=120))

    assert resolved == {"id": 9, "status": "RESOLVED"}
    assert connection.log == ["begin", "commit"]
    recheck, update, *events = connection.calls
    assert recheck[0].startswith("SELECT id FROM support_tickets WHERE id = %s AND")
    assert recheck[0].endswith("FOR UPDATE") and recheck[1] == (9, 120, 120)
    assert "SET status = 'RESOLVED', active_key = NULL" in update[0]
    assert "resolved_by_staff_id = NULL" in update[0] and update[1] == (9,)
    assert [params[1] for _sql, params in events] == ["TICKET_RESOLVED", "RETURNED_TO_AI"]
    for sql, params in events:
        assert "'SYSTEM'" in sql
        assert params[2] == "Automatic (no messages for 2 hours)"
        assert json.loads(params[3]) == {"reason": "phone_app_idle", "idle_minutes": 120}


def test_repository_leaves_ticket_that_changed_since_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection(still_idle=False)
    repository = _repository(connection, monkeypatch)

    assert asyncio.run(repository.auto_resolve_idle_phone_app_ticket(9, idle_minutes=120)) is None
    assert connection.log == ["begin", "rollback"]
    assert len(connection.calls) == 1


def test_repository_lists_due_ticket_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection(still_idle=True)
    repository = _repository(connection, monkeypatch)

    assert asyncio.run(repository.list_idle_phone_app_tickets(idle_minutes=90)) == [9, 12]
    assert connection.calls[0][1] == (90, 90, 50)


def test_actor_name_follows_the_configured_window() -> None:
    assert auto_resolve_actor_name(60) == "Automatic (no messages for 1 hour)"
    assert auto_resolve_actor_name(180) == "Automatic (no messages for 3 hours)"
    assert auto_resolve_actor_name(45) == "Automatic (no messages for 45 minutes)"


class _ConversationStore:
    def __init__(self) -> None:
        self.saved: list[Conversation] = []

    async def get_or_create(self, phone: str, **kwargs: Any) -> Conversation:
        conversation = Conversation(phone_number=phone)
        conversation.mode = "human"
        conversation.active_ticket_id = "9"
        conversation.conversation_key = kwargs.get("conversation_key")
        return conversation

    async def save(self, conversation: Conversation) -> None:
        self.saved.append(conversation)


def _ticket(ticket_id: int) -> dict[str, Any]:
    return {
        "id": ticket_id, "status": "RESOLVED", "whatsapp_phone": "96170000000",
        "business_phone_number_id": "line-1", "business_display_phone_number": "+961 1 234 567",
    }


def test_service_resolves_quietly_and_returns_chat_to_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _ConversationStore()
    published: list[tuple[str, dict[str, Any]]] = []
    attempts: list[tuple[int, int]] = []

    async def list_due(*, idle_minutes: int) -> list[int]:
        return [9, 10, 11]

    async def resolve(ticket_id: int, *, idle_minutes: int) -> dict[str, Any] | None:
        attempts.append((ticket_id, idle_minutes))
        if ticket_id == 10:
            raise RuntimeError("database hiccup")
        if ticket_id == 11:
            return None  # claimed or answered after it was listed
        return _ticket(ticket_id)

    async def publish(event_type: str, **payload: Any) -> None:
        published.append((event_type, payload))

    async def no_customer_messages(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("auto-resolve must not message the customer")

    monkeypatch.setattr(support_service_module.settings, "support_phone_app_idle_resolve_minutes", 120)
    monkeypatch.setattr(support_service_module.support_repository, "list_idle_phone_app_tickets", list_due)
    monkeypatch.setattr(support_service_module.support_repository, "auto_resolve_idle_phone_app_ticket", resolve)
    monkeypatch.setattr(support_service_module, "conversation_store", store)
    monkeypatch.setattr(support_service_module, "publish_support_event", publish)
    monkeypatch.setattr(support_service, "_send_system_customer_message", no_customer_messages)

    assert asyncio.run(support_service.auto_resolve_idle_phone_app_tickets()) == 1

    assert attempts == [(9, 120), (10, 120), (11, 120)]
    assert published == [("ticket_updated", {"ticket_id": 9, "status": "RESOLVED"})]
    [conversation] = store.saved
    assert conversation.mode == "ai" and conversation.active_ticket_id is None
    assert conversation.conversation_key == "line-1:96170000000"
    assert "resolved automatically after the conversation went quiet" in conversation.messages[-1]["content"]


def test_service_does_nothing_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_due(**_kwargs: Any) -> list[int]:
        raise AssertionError("disabled sweep must not query")

    monkeypatch.setattr(support_service_module.settings, "support_phone_app_idle_resolve_minutes", 0)
    monkeypatch.setattr(support_service_module.support_repository, "list_idle_phone_app_tickets", list_due)

    assert asyncio.run(support_service.auto_resolve_idle_phone_app_tickets()) == 0


def test_sweeper_keeps_running_after_a_failed_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    sweeps: list[int] = []

    async def sweep() -> int:
        sweeps.append(1)
        if len(sweeps) == 1:
            raise RuntimeError("redis down")
        if len(sweeps) == 3:
            raise asyncio.CancelledError
        return 0

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(main.support_service, "auto_resolve_idle_phone_app_tickets", sweep)
    monkeypatch.setattr(main.asyncio, "sleep", no_wait)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main._sweep_idle_tickets_forever())
    assert len(sweeps) == 3


def test_sweeper_starts_and_stops_with_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []

    async def forever() -> None:
        started.append(True)
        await asyncio.Event().wait()

    async def lifecycle(minutes: int) -> bool:
        monkeypatch.setattr(main.settings, "support_phone_app_idle_resolve_minutes", minutes)
        await main._start_idle_ticket_sweeper()
        running = main._idle_ticket_sweeper is not None
        await asyncio.sleep(0)
        await main._stop_idle_ticket_sweeper()
        assert main._idle_ticket_sweeper is None
        return running

    monkeypatch.setattr(main, "_sweep_idle_tickets_forever", forever)
    assert asyncio.run(lifecycle(0)) is False
    assert asyncio.run(lifecycle(120)) is True
    assert started == [True]


def test_phone_app_chip_explains_auto_resolve() -> None:
    assert "Resolves automatically after 2 hours with no messages." in _HTML
