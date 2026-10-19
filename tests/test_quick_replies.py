"""Quick replies: private, per-staff saved messages copied from the composer."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import types
from datetime import datetime
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
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass
    fake_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules["openai"] = fake_openai

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.support_api as support_api
import app.support_service as support_service_module
from app.support_auth import SupportAgent, get_current_agent, require_support_csrf
from app.support_repository import (
    MAX_QUICK_REPLIES_PER_STAFF,
    SupportConflictError,
    SupportNotFoundError,
    SupportRepository,
)
from app.support_service import SupportService, SupportValidationError
from app.support_ui import _HTML

AGENT_A = SupportAgent(id="7", name="Agent A", role="agent")
AGENT_B = SupportAgent(id="8", name="Agent B", role="supervisor")


# --- validation ----------------------------------------------------------------

def test_quick_reply_validation_normalizes_and_rejects_bad_input() -> None:
    normalize = support_service_module._normalize_quick_reply
    assert normalize("  Delivery   times ", "Hello\r\nWe deliver daily.\r\n") == (
        "Delivery times",
        "Hello\nWe deliver daily.",
    )
    assert normalize("   ", "Body") == (None, "Body")
    assert normalize(None, "\tTabbed") == (None, "Tabbed")
    for title, body in (("Title", "   "), ("x" * 81, "Body"), ("Title", "x" * 4097), ("Title", "bad\x00body")):
        with pytest.raises(SupportValidationError):
            normalize(title, body)


# --- service passes only the signed-in agent's id --------------------------------

def test_service_scopes_every_call_to_the_authenticated_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    row = {"id": 3, "title": None, "body": "Hi", "created_at": datetime(2026, 10, 19), "updated_at": datetime(2026, 10, 19)}

    async def list_replies(staff_id: int):
        calls.append(("list", staff_id))
        return [row]

    async def create(staff_id: int, *, title, body):
        calls.append(("create", staff_id, title, body))
        return row

    async def update(staff_id: int, reply_id: int, *, title, body):
        calls.append(("update", staff_id, reply_id, title, body))
        return row

    async def delete(staff_id: int, reply_id: int):
        calls.append(("delete", staff_id, reply_id))

    repo = support_service_module.support_repository
    monkeypatch.setattr(repo, "list_quick_replies", list_replies)
    monkeypatch.setattr(repo, "create_quick_reply", create)
    monkeypatch.setattr(repo, "update_quick_reply", update)
    monkeypatch.setattr(repo, "delete_quick_reply", delete)
    service = SupportService()

    async def run() -> None:
        listed = await service.list_quick_replies(AGENT_A)
        assert listed["items"] == [{"id": 3, "title": "", "body": "Hi", "created_at": listed["items"][0]["created_at"], "updated_at": listed["items"][0]["updated_at"]}]
        await service.create_quick_reply(AGENT_A, title=" ", body=" Hi ")
        await service.update_quick_reply(AGENT_A, 3, title="T", body="Hi")
        assert await service.delete_quick_reply(AGENT_A, 3) == {"deleted": True, "id": 3}

    asyncio.run(run())
    assert calls == [
        ("list", 7),
        ("create", 7, None, "Hi"),
        ("update", 7, 3, "T", "Hi"),
        ("delete", 7, 3),
    ]


def test_quick_reply_logs_do_not_contain_the_message(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    async def create(staff_id: int, *, title, body):
        return {"id": 4, "title": title, "body": body, "created_at": None, "updated_at": None}

    monkeypatch.setattr(support_service_module.support_repository, "create_quick_reply", create)
    with caplog.at_level("INFO", logger="shipment-bot"):
        asyncio.run(SupportService().create_quick_reply(AGENT_A, title="Secret title", body="Customer tracking 123"))
    assert "Support quick reply created" in caplog.text
    assert "Secret title" not in caplog.text and "tracking 123" not in caplog.text


# --- repository SQL is always owner-scoped --------------------------------------

class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self.rowcount = 0
        self.lastrowid = 0
        self._result: Any = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, sql: str, params=None) -> None:
        normalized = " ".join(sql.split())
        params = tuple(params or ())
        self.conn.statements.append((normalized, params))
        self._result = None
        self.rowcount = 0
        if normalized.startswith("SELECT id FROM staff"):
            self._result = {"id": params[0]} if self.conn.staff_exists else None
        elif normalized.startswith("SELECT COUNT(*)"):
            self._result = {"total": self.conn.existing_total}
        elif normalized.startswith("INSERT"):
            self.lastrowid = 41
            self.rowcount = 1
        elif normalized.startswith("SELECT"):
            self._result = self.conn.row
        elif normalized.startswith("DELETE"):
            self.rowcount = self.conn.delete_rowcount

    async def fetchone(self):
        return self._result

    async def fetchall(self):
        return [self.conn.row] if self.conn.row else []


class FakeConnection:
    def __init__(self, *, row=None, existing_total=0, staff_exists=True, delete_rowcount=1) -> None:
        self.row = row
        self.existing_total = existing_total
        self.staff_exists = staff_exists
        self.delete_rowcount = delete_rowcount
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
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


def _repo(**kwargs: Any) -> tuple[SupportRepository, FakeConnection]:
    conn = FakeConnection(**kwargs)
    repo = SupportRepository()
    repo._pool = FakePool(conn)  # type: ignore[assignment]
    return repo, conn


def _assert_owner_scoped(conn: FakeConnection, staff_id: int) -> None:
    for sql, params in conn.statements:
        if "support_quick_replies" in sql:
            assert "staff_id" in sql, sql
            assert staff_id in params, (sql, params)


def test_repository_list_update_delete_are_owner_scoped() -> None:
    row = {"id": 5, "title": "T", "body": "B", "created_at": None, "updated_at": None}
    repo, conn = _repo(row=row)
    assert asyncio.run(repo.list_quick_replies(7)) == [row]
    assert asyncio.run(repo.update_quick_reply(7, 5, title="T", body="B")) == row
    asyncio.run(repo.delete_quick_reply(7, 5))
    _assert_owner_scoped(conn, 7)
    update_sql = next(sql for sql, _ in conn.statements if sql.startswith("UPDATE"))
    assert update_sql.endswith("WHERE id = %s AND staff_id = %s")
    delete = next(item for item in conn.statements if item[0].startswith("DELETE"))
    assert delete == ("DELETE FROM support_quick_replies WHERE id = %s AND staff_id = %s", (5, 7))


def test_repository_reports_another_staff_members_reply_as_not_found() -> None:
    repo, _conn = _repo(row=None, delete_rowcount=0)
    with pytest.raises(SupportNotFoundError):
        asyncio.run(repo.update_quick_reply(8, 5, title=None, body="B"))
    with pytest.raises(SupportNotFoundError):
        asyncio.run(repo.delete_quick_reply(8, 5))


def test_repository_create_locks_staff_row_and_enforces_the_limit() -> None:
    saved = {"id": 41, "title": None, "body": "Hello", "created_at": None, "updated_at": None}
    repo, conn = _repo(row=saved, existing_total=MAX_QUICK_REPLIES_PER_STAFF - 1)
    assert asyncio.run(repo.create_quick_reply(7, title=None, body="Hello")) == saved
    assert conn.statements[0][0].endswith("FOR UPDATE")
    insert_sql, insert_params = next(item for item in conn.statements if item[0].startswith("INSERT"))
    assert insert_params == (7, None, "Hello")
    _assert_owner_scoped(conn, 7)
    assert conn.committed and not conn.rolled_back

    repo, conn = _repo(row=saved, existing_total=MAX_QUICK_REPLIES_PER_STAFF)
    with pytest.raises(SupportConflictError):
        asyncio.run(repo.create_quick_reply(7, title=None, body="Hello"))
    assert conn.rolled_back and not any(sql.startswith("INSERT") for sql, _ in conn.statements)

    repo, conn = _repo(row=saved, staff_exists=False)
    with pytest.raises(SupportNotFoundError):
        asyncio.run(repo.create_quick_reply(7, title=None, body="Hello"))
    assert conn.rolled_back


# --- API: isolation between two signed-in staff members ------------------------

class InMemoryQuickReplies:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.next_id = 1

    async def list_quick_replies(self, staff_id: int):
        return [dict(row) for row in self.rows.values() if row["staff_id"] == staff_id]

    async def create_quick_reply(self, staff_id: int, *, title, body):
        row = {"id": self.next_id, "staff_id": staff_id, "title": title, "body": body, "created_at": None, "updated_at": None}
        self.rows[self.next_id] = row
        self.next_id += 1
        return dict(row)

    async def update_quick_reply(self, staff_id: int, reply_id: int, *, title, body):
        row = self.rows.get(reply_id)
        if not row or row["staff_id"] != staff_id:
            raise SupportNotFoundError("Quick reply not found.")
        row.update(title=title, body=body)
        return dict(row)

    async def delete_quick_reply(self, staff_id: int, reply_id: int):
        row = self.rows.get(reply_id)
        if not row or row["staff_id"] != staff_id:
            raise SupportNotFoundError("Quick reply not found.")
        del self.rows[reply_id]


def test_api_keeps_each_staff_members_quick_replies_private(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryQuickReplies()
    repo = support_service_module.support_repository
    for name in ("list_quick_replies", "create_quick_reply", "update_quick_reply", "delete_quick_reply"):
        monkeypatch.setattr(repo, name, getattr(store, name))

    app = FastAPI()
    app.include_router(support_api.router)
    current = {"agent": AGENT_A}
    app.dependency_overrides[get_current_agent] = lambda: current["agent"]
    app.dependency_overrides[require_support_csrf] = lambda: current["agent"]
    client = TestClient(app)

    created = client.post("/api/support/quick-replies", json={"title": "Hours", "body": "We open at 9."})
    assert created.status_code == 201
    reply_id = created.json()["id"]
    assert "staff_id" not in created.json()

    # A request cannot choose whose list it writes to.
    assert client.post("/api/support/quick-replies", json={"body": "x", "staff_id": 8}).status_code == 422

    current["agent"] = AGENT_B  # even a supervisor cannot see or change another account's replies
    assert client.get("/api/support/quick-replies").json() == {"items": []}
    assert client.put(f"/api/support/quick-replies/{reply_id}", json={"body": "Hijacked"}).status_code == 404
    assert client.delete(f"/api/support/quick-replies/{reply_id}").status_code == 404

    current["agent"] = AGENT_A
    items = client.get("/api/support/quick-replies").json()["items"]
    assert [(item["title"], item["body"]) for item in items] == [("Hours", "We open at 9.")]
    updated = client.put(f"/api/support/quick-replies/{reply_id}", json={"title": "", "body": "We open at 8."})
    assert updated.status_code == 200 and updated.json()["title"] == "" and updated.json()["body"] == "We open at 8."
    assert client.put(f"/api/support/quick-replies/{reply_id}", json={"body": "  "}).status_code == 422
    assert client.delete(f"/api/support/quick-replies/{reply_id}").json() == {"deleted": True, "id": reply_id}
    assert client.get("/api/support/quick-replies").json() == {"items": []}


def test_quick_reply_routes_require_session_and_csrf_for_changes() -> None:
    routes = {
        (method, route.path): route
        for route in support_api.router.routes
        if "/quick-replies" in getattr(route, "path", "")
        for method in route.methods
    }
    assert set(routes) == {
        ("GET", "/api/support/quick-replies"),
        ("POST", "/api/support/quick-replies"),
        ("PUT", "/api/support/quick-replies/{reply_id}"),
        ("DELETE", "/api/support/quick-replies/{reply_id}"),
    }
    for (method, _path), route in routes.items():
        dependencies = {dep.call for dep in route.dependant.dependencies}
        expected = get_current_agent if method == "GET" else require_support_csrf
        assert expected in dependencies, method


# --- dashboard ------------------------------------------------------------------

def test_composer_has_a_quick_replies_button_and_private_modal() -> None:
    assert 'id="quickRepliesBtn"' in _HTML
    composer_tools = _HTML[_HTML.index('<div class="composer-tools">'): _HTML.index('<span class="composer-count"')]
    assert 'id="quickRepliesBtn"' in composer_tools
    assert 'id="quickRepliesModal"' in _HTML
    assert "Only you can see your quick replies." in _HTML
    assert "resetQuickReplies();" in _HTML, "sign-out must forget the previous account's replies"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_quick_replies_modal_copies_saves_and_deletes() -> None:
    functions = _HTML[_HTML.index("const QUICK_REPLIES_LIST_SUBTITLE = "): _HTML.index("const AI_REASON_LABELS = {")]
    script = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.listeners = {};
    this.className = ''; this.value = ''; this.style = {}; this._text = '';
    const classes = new Set();
    this.classList = {add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name)};
  }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  click() { return this.listeners.click?.(); }
  focus() { focused = this; }
  findAll(predicate, found = []) {
    for (const child of this.children) { if (child instanceof Element) { if (predicate(child)) found.push(child); child.findAll(predicate, found); } }
    return found;
  }
}
let focused = null;
const elements = {};
const $ = id => (elements[id] ||= new Element('div'));
const document = {
  createElement: tag => new Element(tag),
  createTextNode: value => ({textContent: String(value)}),
  querySelector: selector => {
    const match = /data-quick-reply-id="([^"]+)"/.exec(selector);
    const row = match && $('quickRepliesList').findAll(el => el.dataset.quickReplyId === match[1])[0];
    return row ? row.children[0] : null;
  },
};
const CSS = {escape: value => value};
const text = (element, value) => { element.textContent = value ?? ''; };
const createIcon = () => new Element('svg');
const emptyMarkup = (title, body) => { const outer = new Element('div'); outer.textContent = title; return {outer}; };
const setButtonBusy = () => {};
const calls = [];
const notify = message => calls.push(['notify', message]);
const copyMessageText = async (value, label) => { calls.push(['copy', value, label]); return true; };
const focusMessageComposer = () => calls.push(['focus-composer']);
const openDialog = (id, focusId) => calls.push(['open', id, focusId]);
const closeDialog = (id, restoreFocus) => calls.push(['close', id, restoreFocus]);
let confirmed = true;
const confirmDialog = async options => { calls.push(['confirm', options.tone]); return confirmed; };
let replies = [{id: 2, title: '', body: 'Thanks for waiting!\nWe are checking.'}, {id: 1, title: 'Hours', body: 'We open at 9.'}];
const api = async (path, options = {}) => {
  calls.push(['api', options.method || 'GET', path, options.body ? JSON.parse(options.body) : null]);
  if (!options.method) return {items: replies};
  if (options.method === 'DELETE') return {deleted: true};
  const body = JSON.parse(options.body);
  return {id: options.method === 'POST' ? 9 : Number(path.split('/').pop()), title: body.title, body: body.body};
};
const state = {authenticated: true, activeModal: null, composerEnabled: true,
  quickReplies: [], quickRepliesLoaded: false, quickRepliesBusy: false, quickRepliesRequest: 0, quickReplyEditing: null};
const rows = () => $('quickRepliesList').findAll(el => el.className === 'quick-reply-row');
const rowLabels = () => rows().map(row => row.children[0].attributes['aria-label']);
""" + functions + r"""
(async () => {
  await openQuickRepliesModal();
  assert.deepEqual(calls.slice(0, 2), [['open', 'quickRepliesModal', 'addQuickReplyBtn'], ['api', 'GET', '/api/support/quick-replies', null]]);
  assert.deepEqual(rowLabels(), ['Copy quick reply: Thanks for waiting!', 'Copy quick reply: Hours'], 'untitled replies use their first line');
  const [untitled, titled] = rows().map(row => row.children[0].children[0]);
  assert.ok(untitled.classList.contains('untitled'));
  assert.deepEqual(untitled.children.map(child => child.tagName), ['span'], 'untitled replies show the message once');
  assert.deepEqual(titled.children.map(child => child.tagName), ['strong', 'span']);

  $('quickRepliesSearch').value = 'open at';
  renderQuickReplies();
  assert.deepEqual(rowLabels(), ['Copy quick reply: Hours'], 'search matches the message text');
  $('quickRepliesSearch').value = 'nothing like this';
  renderQuickReplies();
  assert.equal(rows().length, 0);
  assert.equal($('quickRepliesList').textContent, 'No matching quick replies');
  $('quickRepliesSearch').value = '';
  renderQuickReplies();

  calls.length = 0;
  await rows()[1].children[0].click();
  assert.deepEqual(calls, [
    ['copy', 'We open at 9.', 'Quick reply copied. Paste it into the chat.'],
    ['close', 'quickRepliesModal', false], ['focus-composer'],
  ], 'selecting a reply copies it and returns to the composer');

  // Add a reply.
  calls.length = 0;
  showQuickReplyForm(null);
  assert.equal(state.quickReplyEditing, 'new');
  assert.equal($('quickRepliesTitle').textContent, 'New quick reply');
  $('quickReplyBody').value = '   ';
  await saveQuickReply({preventDefault() {}});
  assert.equal($('quickRepliesError').textContent, 'Enter the quick reply message.');
  assert.ok(!calls.some(call => call[0] === 'api'), 'blank replies are not sent');
  $('quickReplyTitle').value = 'Address';
  $('quickReplyBody').value = 'Beirut, Hamra street';
  await saveQuickReply({preventDefault() {}});
  assert.deepEqual(calls.find(call => call[0] === 'api'), ['api', 'POST', '/api/support/quick-replies', {title: 'Address', body: 'Beirut, Hamra street'}]);
  assert.equal(state.quickReplyEditing, null);
  assert.deepEqual(rowLabels(), ['Copy quick reply: Address', 'Copy quick reply: Hours', 'Copy quick reply: Thanks for waiting!']);
  assert.equal(focused, rows()[0].children[0], 'focus returns to the saved reply');

  // Edit a reply.
  calls.length = 0;
  rows()[1].children[1].children[0].click();
  assert.equal(state.quickReplyEditing, 1);
  assert.equal($('quickReplyBody').value, 'We open at 9.');
  $('quickReplyBody').value = 'We open at 8.';
  await saveQuickReply({preventDefault() {}});
  assert.deepEqual(calls.find(call => call[0] === 'api'), ['api', 'PUT', '/api/support/quick-replies/1', {title: 'Hours', body: 'We open at 8.'}]);
  assert.equal(state.quickReplies.find(reply => reply.id === 1).body, 'We open at 8.');
  assert.equal(state.quickReplies.length, 3);

  // Delete: cancelling keeps it, confirming removes it.
  calls.length = 0;
  confirmed = false;
  await rows()[1].children[1].children[1].click();
  assert.deepEqual(calls, [['confirm', 'danger']]);
  confirmed = true;
  await rows()[1].children[1].children[1].click();
  assert.deepEqual(calls.filter(call => call[0] === 'api'), [['api', 'DELETE', '/api/support/quick-replies/1', null]]);
  assert.deepEqual(rowLabels(), ['Copy quick reply: Address', 'Copy quick reply: Thanks for waiting!']);

  resetQuickReplies();
  assert.deepEqual(state.quickReplies, []);
  assert.equal(state.quickRepliesLoaded, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
