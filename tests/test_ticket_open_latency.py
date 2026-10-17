"""Opening a conversation must not wait for database round trips one after another.

The dev database sits behind an SSH tunnel (~60 ms per query), so each lookup
that ticket_detail awaits in sequence adds visible delay when an agent taps a
ticket. These tests use barriers instead of timings: a branch only finishes once
every other branch has started, so sequential code would time out.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.support_auth as support_auth
import app.support_service as support_service_module
from app.customer_directory import CustomerDirectory
from app.support_auth import SupportAgent
from app.support_service import SupportNotFoundError


def _barrier(parties: int):
    started: list[str] = []
    ready = asyncio.Event()

    async def arrive(name: str) -> None:
        started.append(name)
        if len(started) >= parties:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=1)

    return arrive, started


def _ticket(**overrides):
    ticket = {
        "id": 52,
        "ticket_reference": "CS-52",
        "conversation_origin": "CUSTOMER",
        "active_key": "business:96170123456",
        "whatsapp_phone": "96170123456",
        "business_phone_number_id": "business",
        "status": "NEW",
        "current_assignee_id": None,
        "customer_name": None,
        "customer_id": None,
        "tracking_reference": "TRK-1",
        "context_json": "{}",
    }
    ticket.update(overrides)
    return ticket


def _previous(ticket_id: int):
    return _ticket(id=ticket_id, ticket_reference=f"CS-{ticket_id}", active_key=None, status="RESOLVED")


def _patch_media(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(support_service_module.support_media_store, "get", lambda *_args: None)
    monkeypatch.setattr(support_service_module.support_media_store, "get_remote_reference", lambda *_args: None)


def test_ticket_detail_loads_identity_messages_events_and_history_concurrently(monkeypatch) -> None:
    async def run() -> dict:
        arrive, started = _barrier(4)
        current = _ticket()

        async def get_ticket(_ticket_id: int):
            return dict(current)

        async def resolve(_phone: str):
            await arrive("identity")
            return SimpleNamespace(source="registered_user", name="Rana", user_id="900", userid="rana")

        async def update_customer_identity(ticket_id: int, **fields):
            return {**current, "customer_name": fields["customer_name"], "customer_id": fields["customer_id"]}

        async def track_shipment(*, customer_id: str, tracking_number: str):
            # Shipment lookup must see the customer_id supplied by identity enrichment.
            assert (customer_id, tracking_number) == ("900", "TRK-1")
            return {"found": True, "authorized": True, "status": "In transit"}

        async def list_messages(ticket_id: int, *, limit: int = 100, before_id=None, user_id=None):
            if ticket_id == 52:
                await arrive("messages")
            return [{"id": ticket_id * 10, "ticket_id": ticket_id, "sender_type": "CUSTOMER",
                     "direction": "INBOUND", "message_type": "text", "body": "hi"}]

        async def list_events(_ticket_id: int, limit: int = 100):
            await arrive("events")
            return []

        async def list_previous_customer_tickets(**_kwargs):
            await arrive("history")
            return [_previous(40)]

        repo = support_service_module.support_repository
        monkeypatch.setattr(repo, "get_ticket", get_ticket)
        monkeypatch.setattr(repo, "update_customer_identity", update_customer_identity)
        monkeypatch.setattr(repo, "list_messages", list_messages)
        monkeypatch.setattr(repo, "list_events", list_events)
        monkeypatch.setattr(repo, "list_previous_customer_tickets", list_previous_customer_tickets)
        monkeypatch.setattr(support_service_module.customer_directory, "resolve", resolve)
        monkeypatch.setattr(support_service_module.shipment_client, "track_shipment", track_shipment)
        _patch_media(monkeypatch)

        agent = SupportAgent(id="1", name="Supervisor", role="supervisor")
        detail = await support_service_module.support_service.ticket_detail(52, agent, include_previous_history=True)
        assert sorted(started) == ["events", "history", "identity", "messages"]
        return detail

    detail = asyncio.run(run())
    assert detail["ticket"]["customer_name"] == "Rana"
    assert detail["shipment"]["status"] == "In transit"
    assert detail["customer_identity"]["registered_user"] is True
    assert [message["id"] for message in detail["messages"]] == [520]
    assert [thread["ticket"]["id"] for thread in detail["previous_ticket_history"]] == [40]


def test_previous_ticket_pages_load_concurrently_and_keep_their_order(monkeypatch) -> None:
    async def run() -> dict:
        arrive, _started = _barrier(3)

        async def list_previous_customer_tickets(**_kwargs):
            return [_previous(48), _previous(45), _previous(41)]

        async def list_messages(ticket_id: int, *, limit: int = 100, before_id=None, user_id=None):
            await arrive(str(ticket_id))
            return [{"id": ticket_id * 10, "ticket_id": ticket_id, "sender_type": "CUSTOMER",
                     "direction": "INBOUND", "message_type": "text", "body": str(ticket_id)}]

        repo = support_service_module.support_repository
        monkeypatch.setattr(repo, "list_previous_customer_tickets", list_previous_customer_tickets)
        monkeypatch.setattr(repo, "list_messages", list_messages)
        _patch_media(monkeypatch)
        agent = SupportAgent(id="1", name="Supervisor", role="supervisor")
        return await support_service_module.support_service._history_page_for_ticket(_ticket(), agent)

    history = asyncio.run(run())
    threads = history["previous_ticket_history"]
    assert [thread["ticket"]["id"] for thread in threads] == [41, 45, 48]
    assert [thread["messages"][0]["body"] for thread in threads] == ["41", "45", "48"]


def test_a_failing_branch_cancels_the_other_lookups(monkeypatch) -> None:
    cancelled: list[str] = []

    async def run() -> None:
        async def get_ticket(_ticket_id: int):
            return _ticket()

        async def resolve(_phone: str):
            return SimpleNamespace(source="guest", name=None, user_id=None, userid=None)

        async def list_messages_around(*_args, **_kwargs):
            return []

        async def list_events(_ticket_id: int, limit: int = 100):
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append("events")
                raise
            return []

        repo = support_service_module.support_repository
        monkeypatch.setattr(repo, "get_ticket", get_ticket)
        monkeypatch.setattr(repo, "list_messages_around", list_messages_around)
        monkeypatch.setattr(repo, "list_events", list_events)
        monkeypatch.setattr(support_service_module.customer_directory, "resolve", resolve)
        agent = SupportAgent(id="1", name="Supervisor", role="supervisor")
        with pytest.raises(SupportNotFoundError):
            await support_service_module.support_service.ticket_detail(52, agent, around_message_id=99)

    asyncio.run(asyncio.wait_for(run(), timeout=2))
    assert cancelled == ["events"]


def test_customer_directory_reuses_one_pool_and_looks_up_user_and_contact_together(monkeypatch) -> None:
    pools: list[object] = []

    class FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, *_args):
            pass

        async def fetchone(self):
            return None

        async def fetchall(self):
            return []

    class FakeConnection:
        def cursor(self, *_args):
            return FakeCursor()

    class FakePool:
        def acquire(self):
            class _Acquire:
                async def __aenter__(self):
                    return FakeConnection()

                async def __aexit__(self, *_exc):
                    return False

            return _Acquire()

    async def create_pool(**_kwargs):
        await asyncio.sleep(0)
        pool = FakePool()
        pools.append(pool)
        return pool

    async def run() -> None:
        import app.customer_directory as directory_module

        def connect(**_kwargs):
            raise AssertionError("must not open a connection per query")

        # Other test modules may install a stub aiomysql; replace the reference
        # the directory uses so this test works whichever module is loaded.
        monkeypatch.setattr(
            directory_module, "aiomysql",
            SimpleNamespace(create_pool=create_pool, connect=connect, DictCursor=object),
        )
        directory = CustomerDirectory()
        await directory.resolve("+961 70 123 456")
        await directory.resolve("96170123456")
        assert len(pools) == 1

        arrive, started = _barrier(2)
        guarded = CustomerDirectory()

        async def registered_user(_phone: str):
            await arrive("user")
            return None

        async def saved_contact(_phone: str):
            await arrive("contact")
            return {"id": 3, "display_name": "Saved Name"}

        guarded._registered_user = registered_user  # type: ignore[method-assign]
        guarded._saved_contact = saved_contact  # type: ignore[method-assign]
        identity = await guarded.resolve("96170123456")
        assert sorted(started) == ["contact", "user"]
        assert (identity.source, identity.name) == ("saved_contact", "Saved Name")

    asyncio.run(run())


def test_session_last_seen_is_written_in_the_background_and_throttled(monkeypatch) -> None:
    touched: list[int] = []

    async def run() -> None:
        release = asyncio.Event()

        async def touch_session(session_id: int) -> None:
            touched.append(session_id)
            await release.wait()

        monkeypatch.setattr(support_auth.staff_repository, "touch_session", touch_session)
        monkeypatch.setattr(support_auth, "_session_touched_at", {})
        support_auth._schedule_session_touch(5)  # returns without waiting for the write
        support_auth._schedule_session_touch(5)  # within the interval: skipped
        support_auth._schedule_session_touch(6)
        await asyncio.sleep(0)
        assert touched == [5, 6]
        release.set()
        await asyncio.gather(*support_auth._session_touch_tasks)

    asyncio.run(run())


def test_valid_session_authenticates_and_schedules_last_seen(monkeypatch) -> None:
    from starlette.requests import Request

    touched: list[int] = []

    async def get_active_session(_token_hash: str):
        return {"session_id": 31, "staff_id": 4, "name": "Agent Four", "role": "agent", "csrf_hash": "abc"}

    async def touch_session(session_id: int) -> None:
        touched.append(session_id)

    monkeypatch.setattr(support_auth.staff_repository, "get_active_session", get_active_session)
    monkeypatch.setattr(support_auth.staff_repository, "touch_session", touch_session)
    monkeypatch.setattr(support_auth, "_session_touched_at", {})

    async def run() -> SupportAgent:
        cookie = f"{support_auth.settings.support_cookie_name}=token-value".encode()
        request = Request({"type": "http", "method": "GET", "path": "/api/support/me", "headers": [(b"cookie", cookie)]})
        agent = await support_auth.get_current_agent(request)
        await asyncio.gather(*support_auth._session_touch_tasks)
        return agent

    agent = asyncio.run(run())
    assert (agent.id, agent.role, agent.session_id, agent.csrf_hash) == ("4", "agent", 31, "abc")
    assert touched == [31]
