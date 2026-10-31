from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {})
)
sys.modules["aiomysql"] = fake_aiomysql

from app.config import settings

settings.redis_url = ""

import app.support_api as support_api
import app.support_service as support_service_module
from app.support_auth import SupportAgent, require_support_csrf
from app.support_repository import SupportConflictError, SupportNotFoundError, SupportRepository
from app.support_service import SupportForbiddenError, SupportService
from app.support_ui import _HTML


def _chat(**overrides: Any) -> dict[str, Any]:
    chat = {
        "id": 85,
        "ticket_reference": "CHAT-85",
        "conversation_origin": "STAFF",
        "status": "CLOSED",
        "active_key": None,
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "business",
        "current_assignee_id": None,
        "customer_name": "Rana",
        "customer_id": None,
        "tracking_reference": None,
        "context_json": "{}",
    }
    chat.update(overrides)
    return chat


SUPERVISOR = SupportAgent(id="2", name="Supervisor", role="supervisor")
AGENT = SupportAgent(id="7", name="Agent", role="agent")


# --- service rules -----------------------------------------------------------

def _patch_repo(monkeypatch, chat: dict[str, Any] | None, calls: list[str]) -> None:
    async def get_ticket(_chat_id: int):
        calls.append("get")
        return dict(chat) if chat else None

    async def delete_staff_chat(chat_id: int, **_actor):
        calls.append(f"delete:{chat_id}")
        return dict(chat or {})

    def delete_media(chat_id: int) -> None:
        calls.append(f"media:{chat_id}")

    monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(support_service_module.support_repository, "delete_staff_chat", delete_staff_chat)
    monkeypatch.setattr(support_service_module.support_media_store, "delete_ticket_media", delete_media)


def test_agent_cannot_delete_a_chat(monkeypatch) -> None:
    calls: list[str] = []
    _patch_repo(monkeypatch, _chat(), calls)
    with pytest.raises(SupportForbiddenError, match="Super Admins and Admins"):
        asyncio.run(SupportService().delete_staff_chat(85, agent=AGENT))
    assert calls == []


@pytest.mark.parametrize("role", ["supervisor", "admin"])
@pytest.mark.parametrize("status", ["CLOSED", "RESOLVED"])
def test_manager_deletes_closed_chat_and_its_media(monkeypatch, role: str, status: str) -> None:
    calls: list[str] = []
    _patch_repo(monkeypatch, _chat(status=status), calls)
    result = asyncio.run(SupportService().delete_staff_chat(85, agent=SupportAgent(id="2", name="M", role=role)))
    assert result == {"deleted": True, "chat_id": 85}
    assert calls == ["get", "delete:85", "media:85"]


def test_open_chat_must_be_closed_before_deleting(monkeypatch) -> None:
    calls: list[str] = []
    _patch_repo(monkeypatch, _chat(status="IN_PROGRESS", active_key="business:96170123456"), calls)
    with pytest.raises(SupportConflictError, match="Close this chat"):
        asyncio.run(SupportService().delete_staff_chat(85, agent=SUPERVISOR))
    assert calls == ["get"]


@pytest.mark.parametrize("chat", [None, _chat(conversation_origin="CUSTOMER")])
def test_customer_ticket_or_missing_row_is_not_a_chat(monkeypatch, chat) -> None:
    calls: list[str] = []
    _patch_repo(monkeypatch, chat, calls)
    with pytest.raises(SupportNotFoundError, match="Chat not found"):
        asyncio.run(SupportService().delete_staff_chat(85, agent=SUPERVISOR))
    assert calls == ["get"]


def test_media_cleanup_failure_does_not_undo_the_delete(monkeypatch) -> None:
    calls: list[str] = []
    _patch_repo(monkeypatch, _chat(), calls)

    def broken_media(_chat_id: int) -> None:
        raise OSError("disk")

    monkeypatch.setattr(support_service_module.support_media_store, "delete_ticket_media", broken_media)
    assert asyncio.run(SupportService().delete_staff_chat(85, agent=SUPERVISOR))["deleted"] is True


# --- repository transaction --------------------------------------------------

class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self.rowcount = 0
        self._row = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        self.conn.statements.append(normalized)
        self._row = self.conn.row if normalized.startswith("SELECT") else None
        self.rowcount = 1 if normalized.startswith("DELETE FROM support_tickets") else 0

    async def fetchone(self):
        return self._row


class FakeConnection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.statements: list[str] = []
        self.committed = False
        self.rolled_back = False

    def cursor(self, *_args):
        return FakeCursor(self)

    async def begin(self) -> None:
        pass

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakePool:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Acquire()


def _repo(row: dict[str, Any] | None) -> tuple[SupportRepository, FakeConnection]:
    conn = FakeConnection(row)
    repo = SupportRepository()
    repo._pool = FakePool(conn)  # type: ignore[assignment]
    return repo, conn


def test_repository_locks_revalidates_and_deletes_the_whole_chat_timeline() -> None:
    repo, conn = _repo(_chat())
    deleted = asyncio.run(repo.delete_staff_chat(85))
    assert deleted["id"] == 85
    assert conn.statements[0].endswith("FOR UPDATE")
    deletes = [statement for statement in conn.statements if statement.startswith("DELETE")]
    for table in (
        "support_ticket_events", "support_message_reactions", "support_message_user_deletions",
        "support_message_change_events", "support_message_change_operations",
        "support_ticket_messages", "support_tickets",
    ):
        assert any(f"DELETE FROM {table} " in statement for statement in deletes), table
    assert deletes[-1] == "DELETE FROM support_tickets WHERE id = %s"
    audit = [index for index, statement in enumerate(conn.statements) if statement.startswith("INSERT INTO support_deletion_audit")]
    assert len(audit) == 1 and audit[0] < conn.statements.index(deletes[0]), "who deleted it is recorded first"
    assert conn.committed and not conn.rolled_back


@pytest.mark.parametrize(
    ("row", "error"),
    [
        (None, SupportNotFoundError),
        (_chat(conversation_origin="CUSTOMER"), SupportNotFoundError),
        (_chat(status="IN_PROGRESS", active_key="business:96170123456"), SupportConflictError),
    ],
)
def test_repository_refuses_missing_customer_or_reopened_rows(row, error) -> None:
    repo, conn = _repo(row)
    with pytest.raises(error):
        asyncio.run(repo.delete_staff_chat(85))
    assert conn.rolled_back and not conn.committed
    assert not any(statement.startswith("DELETE") for statement in conn.statements)


def test_history_ticket_delete_also_removes_reactions() -> None:
    repo, conn = _repo(_chat(conversation_origin="CUSTOMER", status="RESOLVED"))
    asyncio.run(repo.delete_history_ticket(85))
    assert "DELETE FROM support_message_reactions WHERE ticket_id = %s" in conn.statements
    assert conn.committed


# --- permission flag, route, UI ----------------------------------------------

def _detail_permissions(monkeypatch, chat: dict[str, Any], agent: SupportAgent) -> dict[str, Any]:
    async def get_ticket(_ticket_id: int):
        return dict(chat)

    async def empty(*_args, **_kwargs):
        return []

    async def resolve(_phone: str):
        return SimpleNamespace(source="guest", name=None, user_id=None, userid=None)

    repo = support_service_module.support_repository
    monkeypatch.setattr(repo, "get_ticket", get_ticket)
    monkeypatch.setattr(repo, "list_messages", empty)
    monkeypatch.setattr(repo, "list_events", empty)
    monkeypatch.setattr(repo, "agent_has_participated", lambda *_a: asyncio.sleep(0, result=True))
    monkeypatch.setattr(support_service_module.customer_directory, "resolve", resolve)
    return asyncio.run(SupportService().ticket_detail(85, agent))["permissions"]


def test_delete_chat_permission_is_only_for_managers_on_closed_chats(monkeypatch) -> None:
    assert _detail_permissions(monkeypatch, _chat(), SUPERVISOR)["can_delete_chat"] is True
    assert _detail_permissions(monkeypatch, _chat(), AGENT)["can_delete_chat"] is False
    open_chat = _chat(status="IN_PROGRESS", active_key="business:96170123456", current_assignee_id="2")
    assert _detail_permissions(monkeypatch, open_chat, SUPERVISOR)["can_delete_chat"] is False
    customer = _chat(conversation_origin="CUSTOMER", status="RESOLVED")
    permissions = _detail_permissions(monkeypatch, customer, SUPERVISOR)
    assert permissions["can_delete_chat"] is False and permissions["can_delete_history"] is True


def test_delete_chat_route_requires_csrf() -> None:
    route = next(
        r for r in support_api.router.routes
        if getattr(r, "path", "").endswith("/chats/{chat_id}") and "DELETE" in getattr(r, "methods", set())
    )
    assert any(dep.call is require_support_csrf for dep in route.dependant.dependencies)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_closed_chat_shows_delete_and_sends_the_delete_request() -> None:
    render = _HTML[_HTML.index("function actionButton("): _HTML.index("function setWorkspaceBusy(")]
    delete_chat = _HTML[_HTML.index("async function deleteStaffChat("): _HTML.index("function stopMediaTracks(")]
    script = r"""
const assert = require('node:assert/strict');
const makeElement = tag => ({
  tagName: tag, children: [], dataset: {}, attributes: {}, className: '', textContent: '',
  append(...nodes) { this.children.push(...nodes) }, replaceChildren() { this.children = [] },
  setAttribute(name, value) { this.attributes[name] = value }, querySelector() { return null },
});
const document = {createElement: makeElement, activeElement: null};
const container = makeElement('div');
const $ = () => container;
const createIcon = () => makeElement('svg');
const state = {busy: false, ticketId: 85, currentTicketData: null};
const isStaffChat = ticket => ticket.conversation_origin === 'STAFF';
const noop = () => {};
const openTransferModal = noop, closeStaffChat = noop, resolveTicket = noop, deleteHistoryTicket = noop;
const setStatus = noop, doAction = noop, formatStatus = s => s;
const labels = () => container.children.map(button => button.attributes['aria-label']);
const calls = [];
let confirmed = true;
const confirmDialog = async options => { calls.push(['confirm', options.tone]); return confirmed; };
const api = async (path, options) => { calls.push(['api', path, options.method]); };
const notify = message => calls.push(['notify', message]);
const showError = noop, setWorkspaceBusy = noop;
const clearTicketWorkspace = () => calls.push(['clear']);
const loadQueue = async () => calls.push(['load']);
""" + render + delete_chat + r"""
(async () => {
  const closed = {ticket: {conversation_origin: 'STAFF', active_key: null, customer_name: 'Rana'},
    permissions: {can_transfer: false, can_operate: true, can_delete_chat: true}};
  renderActions(closed);
  assert.deepEqual(labels(), ['Delete chat']);

  renderActions({...closed, permissions: {...closed.permissions, can_delete_chat: false}});
  assert.deepEqual(labels(), [], 'agents and open chats get no delete action');

  state.currentTicketData = closed;
  confirmed = false;
  await deleteStaffChat();
  assert.deepEqual(calls, [['confirm', 'danger']], 'cancelling the dialog sends nothing');

  calls.length = 0;
  confirmed = true;
  await deleteStaffChat();
  assert.deepEqual(calls, [
    ['confirm', 'danger'], ['api', '/api/support/chats/85', 'DELETE'],
    ['notify', 'Chat deleted.'], ['clear'], ['load'],
  ]);
})().catch(error => {console.error(error); process.exitCode = 1});
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
