from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import types
from typing import Any

import pytest

os.environ["REDIS_URL"] = ""

fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {}))
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

from app.support_auth import SupportAgent
from app.support_service import SupportForbiddenError, SupportService
from app.support_ui import _HTML
import app.support_service as support_service_module


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is unavailable')
def test_customer_search_opens_conversation_and_message_search_jumps_to_message():
    helper = _HTML[_HTML.index('async function openSearchResult('):_HTML.index('// Only one queue row')]
    script = r"""
const assert = require('node:assert/strict');
const state = {};
const opened = [], searched = [], closed = [];
const closeConversationSearch = render => closed.push(render);
const openTicket = async (...args) => opened.push(args);
const searchCurrentConversation = async options => searched.push(options);
const $ = () => ({value: '', classList: {remove() {}}});
""" + helper + r"""
(async () => {
  await openSearchResult({ticket_id: 42, result_type: 'conversation', message_id: null, conversation_origin: 'STAFF'}, 'Maya');
  assert.deepEqual(opened[0], [42, {silent: false, conversationOrigin: 'STAFF'}]);
  assert.equal(searched.length, 0);
  assert.deepEqual(closed, [false]);
  await openSearchResult({ticket_id: 42, result_type: 'message', message_id: 99}, 'parcel');
  assert.equal(opened[1][1].aroundMessageId, 99);
  assert.equal(state.conversationSearchQuery, 'parcel');
  assert.deepEqual(searched, [{focusMessageId: 99, preserveQuery: true}]);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def _ticket(*, assignee: str | None = "7") -> dict[str, Any]:
    return {
        "id": 42,
        "ticket_reference": "SUP-42",
        "active_key": "business:96170123456",
        "status": "IN_PROGRESS",
        "current_assignee_id": assignee,
        "context_json": "{}",
        "customer_id": None,
        "tracking_reference": None,
    }


def test_ticket_message_search_enforces_ticket_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="5", name="Agent", role="agent")

        async def fake_get_ticket(_ticket_id: int) -> dict[str, Any]:
            return _ticket(assignee="7")

        async def fake_participated(_ticket_id: int, _agent_id: str) -> bool:
            return False

        async def should_not_search(**_kwargs: Any) -> list[dict[str, Any]]:
            raise AssertionError("repository search must not run before authorization")

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(support_service_module.support_repository, "agent_has_participated", fake_participated)
        monkeypatch.setattr(support_service_module.support_repository, "search_messages", should_not_search)

        with pytest.raises(SupportForbiddenError):
            await service.search_messages(query="invoice", agent=agent, ticket_id=42)

    asyncio.run(run())


def test_global_message_search_passes_role_scope_and_builds_safe_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        supervisor = SupportAgent(id="1", name="Supervisor", role="supervisor")
        captured: dict[str, Any] = {}

        async def fake_search(**kwargs: Any) -> list[dict[str, Any]]:
            captured.update(kwargs)
            return [{
                "message_id": 9,
                "ticket_id": 42,
                "message_type": "text",
                "body": "Tracking proof: ABC-123",
                "created_at": None,
            }]

        monkeypatch.setattr(support_service_module.support_repository, "search_messages", fake_search)
        payload = await service.search_messages(query="ABC-123", agent=supervisor)
        assert payload["count"] == 1
        assert payload["items"][0]["preview"] == "Tracking proof: ABC-123"
        assert "body" not in payload["items"][0]
        assert captured["can_view_all"] is True
        assert captured["agent_id"] == "1"

    asyncio.run(run())


def test_ticket_detail_can_load_context_around_search_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        service = SupportService()
        agent = SupportAgent(id="7", name="Agent", role="agent")
        calls: dict[str, Any] = {}

        async def fake_get_ticket(_ticket_id: int) -> dict[str, Any]:
            return _ticket(assignee="7")

        async def fake_around(ticket_id: int, message_id: int, **kwargs: Any) -> list[dict[str, Any]]:
            calls.update(ticket_id=ticket_id, message_id=message_id, **kwargs)
            return [{
                "id": message_id,
                "ticket_id": ticket_id,
                "sender_type": "CUSTOMER",
                "direction": "INBOUND",
                "message_type": "text",
                "body": "proof phrase",
                "created_at": None,
            }]

        async def fake_events(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            return []

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", fake_get_ticket)
        monkeypatch.setattr(support_service_module.support_repository, "list_messages_around", fake_around)
        monkeypatch.setattr(support_service_module.support_repository, "list_events", fake_events)

        detail = await service.ticket_detail(42, agent, around_message_id=99)
        assert detail["messages"][0]["id"] == 99
        assert calls["message_id"] == 99

    asyncio.run(run())


def test_support_ui_contains_queue_and_in_conversation_search() -> None:
    assert 'id="queueSearch"' in _HTML
    assert 'placeholder="Search conversations and messages"' in _HTML
    assert 'id="conversationSearchBtn"' in _HTML
    assert 'id="conversationSearchToolbar"' in _HTML
    assert "params = new URLSearchParams({page, page_size: pageSize, q: state.query.trim()})" in _HTML
    assert "ticket.search_message_id" in _HTML
    assert "/api/support/search/messages?q=" in _HTML
    assert "message-search-highlight" in _HTML
    assert "detailParams.set('around_message_id'" in _HTML
