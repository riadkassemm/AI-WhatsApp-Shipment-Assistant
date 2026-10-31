from __future__ import annotations

import asyncio
import sys
import types

# Keep imports lightweight in artifact validation environments.
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


class _AsyncOpenAI:
    def __init__(self, *_args, **_kwargs) -> None:
        pass


fake_openai.AsyncOpenAI = getattr(fake_openai, "AsyncOpenAI", _AsyncOpenAI)
sys.modules["openai"] = fake_openai

from app.support_auth import SupportAgent
from app.support_dashboard_repository import UNREAD_EXISTS, queue_filters
from app.support_service import SupportService


def test_unread_filter_uses_the_staff_read_boundary_for_mine_and_chats():
    for view in ("mine", "chats"):
        where, values = queue_filters(view, None, None, "all", "", "7", unread=True)
        assert UNREAD_EXISTS in where
        assert "support_tickets.id=%s" not in where
        assert values == ["7", "7"]


def test_unread_filter_keeps_the_open_conversation_listed():
    where, values = queue_filters("mine", None, "90005", "all", "", "7", unread=True, keep_ticket_id=42)
    assert f"({UNREAD_EXISTS} OR support_tickets.id=%s)" in where
    assert values == ["90005", "7", "7", 42]


def test_unread_filter_is_ignored_outside_mine_and_chats():
    for view in ("unassigned", "watching", "active", "waiting", "history"):
        where, values = queue_filters(view, None, None, "all", "", "7", unread=True, keep_ticket_id=42)
        assert UNREAD_EXISTS not in where and values == []
    where, values = queue_filters("mine", None, None, "all", "", "7", keep_ticket_id=42)
    assert where == "1=1" and values == []


def test_service_forwards_unread_filter(monkeypatch):
    calls = []

    async def fake_list_tickets(**kwargs):
        calls.append(kwargs)
        return {"items": [], "filter_options": None, "page": 1, "page_size": 30, "total": 0}

    monkeypatch.setattr("app.support_service.support_repository.list_tickets", fake_list_tickets)
    agent = SupportAgent(id="7", name="Agent", role="agent", session_id=1)
    asyncio.run(SupportService().list_tickets(
        view="chats", agent=agent, page=1, page_size=30, unread=True, keep_ticket_id=42,
    ))
    assert calls[0]["unread"] is True and calls[0]["keep_ticket_id"] == 42
    assert calls[0]["agent_id"] == "7"
