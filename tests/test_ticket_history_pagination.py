import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.support_auth import SupportAgent
from app.support_repository import SupportNotFoundError, SupportRepository
from app.support_service import SupportService
import app.support_service as support_service_module


def test_history_matches_customer_across_business_lines() -> None:
    current = _ticket(30, line_id="90006", display="+961 76 063 972", status="NEW")
    other_line = _ticket(20, line_id="1375394715655384", display="96170111111")
    different_customer = _ticket(10, line_id="90006", display="96176063972", phone="96170999999")

    assert asyncio.run(support_service_module._historical_ticket_accessible(other_line, current, _agent()))
    assert not asyncio.run(support_service_module._historical_ticket_accessible(different_customer, current, _agent()))


def _ticket(
    ticket_id: int,
    *,
    line_id: str,
    display: str,
    phone: str = "96170123456",
    assignee: str = "7",
    status: str = "RESOLVED",
    origin: str = "CUSTOMER",
) -> dict[str, Any]:
    return {
        "id": ticket_id,
        "ticket_reference": f"CS-{ticket_id}",
        "status": status,
        "conversation_origin": origin,
        "active_key": f"{line_id}:{phone}" if status == "NEW" else None,
        "whatsapp_phone": phone,
        "business_phone_number_id": line_id,
        "business_display_phone_number": display,
        "current_assignee_id": assignee,
        "customer_id": None,
        "tracking_reference": None,
        "context_json": "{}",
    }


def _agent(role: str = "agent") -> SupportAgent:
    return SupportAgent(id="7", name="Agent Seven", role=role)


def test_history_pages_include_all_staff_tickets_and_chats_across_business_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _ticket(100, line_id="90006", display="+961 76 063 972", status="NEW")
    old_meta = _ticket(90, line_id="1375394715655384", display="96176063972")
    other_line = _ticket(80, line_id="90003", display="96170111111")
    other_staff = _ticket(70, line_id="1375394715655384", display="96176063972", assignee="9")
    old_chat = _ticket(60, line_id="1375394715655384", display="96176063972", assignee="9", origin="STAFF")
    other_customer = _ticket(50, line_id="90006", display="96176063972", phone="96170999999")
    candidates = [old_meta, other_line, other_staff, old_chat, other_customer]
    calls: list[int | None] = []

    async def get_ticket(ticket_id: int) -> dict[str, Any]:
        return dict({row["id"]: row for row in [current, *candidates]}[ticket_id])

    async def list_previous_customer_tickets(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs.get("before_ticket_id"))
        before = kwargs.get("before_ticket_id") or 100
        return [dict(row) for row in candidates if row["id"] < before]

    async def participated(ticket_id: int, agent_id: str) -> bool:
        assert agent_id == "7"
        return ticket_id == 60

    async def list_messages(ticket_id: int, **kwargs: Any) -> list[dict[str, Any]]:
        return [{
            "id": ticket_id * 10,
            "ticket_id": ticket_id,
            "message_type": "text",
            "sender_type": "CUSTOMER",
            "direction": "INBOUND",
            "body": f"conversation {ticket_id}",
        }]

    async def list_events(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def resolve_identity(_phone: str) -> SimpleNamespace:
        return SimpleNamespace(source="guest", name=None, user_id=None, userid=None)

    monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(support_service_module.support_repository, "list_previous_customer_tickets", list_previous_customer_tickets)
    monkeypatch.setattr(support_service_module.support_repository, "agent_has_participated", participated)
    monkeypatch.setattr(support_service_module.support_repository, "list_messages", list_messages)
    monkeypatch.setattr(support_service_module.support_repository, "list_events", list_events)
    monkeypatch.setattr(support_service_module.customer_directory, "resolve", resolve_identity)

    async def run() -> None:
        service = SupportService()
        detail = await service.ticket_detail(100, _agent(), include_previous_history=True)
        assert [item["ticket"]["id"] for item in detail["previous_ticket_history"]] == [60, 70, 80, 90]
        assert detail["previous_ticket_history"][0]["ticket"]["conversation_origin"] == "STAFF"
        assert detail["previous_ticket_history_has_more"] is False
        assert detail["messages_has_more"] is False

        older = await service.history_page(100, _agent(), before_ticket_id=90)
        assert [item["ticket"]["id"] for item in older["previous_ticket_history"]] == [60, 70, 80]
        assert calls == [None, 90]

        shared = await service.history_messages_page(100, 80, _agent())
        assert shared["messages"][0]["body"] == "conversation 80"
        with pytest.raises(SupportNotFoundError):
            await service.history_messages_page(100, 50, _agent())

        supervisor = await service.history_page(100, _agent("supervisor"))
        assert [item["ticket"]["id"] for item in supervisor["previous_ticket_history"]] == [60, 70, 80, 90]

    asyncio.run(run())


def test_chat_detail_route_passes_previous_history_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.support_api import chat_detail
    import app.support_api as support_api_module

    calls: list[dict[str, Any]] = []

    async def ticket_detail(ticket_id: int, agent: SupportAgent, **kwargs: Any) -> dict[str, Any]:
        calls.append({"ticket_id": ticket_id, "agent_id": agent.id, **kwargs})
        return {"ticket": {"conversation_origin": "STAFF"}, "previous_ticket_history": []}

    monkeypatch.setattr(support_api_module.support_service, "ticket_detail", ticket_detail)
    result = asyncio.run(chat_detail(
        42, around_message_id=None, include_previous_history=True, agent=_agent(),
    ))
    assert result["ticket"]["conversation_origin"] == "STAFF"
    assert calls == [{
        "ticket_id": 42,
        "agent_id": "7",
        "around_message_id": None,
        "include_previous_history": True,
    }]


def test_history_and_current_message_pages_expose_older_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _ticket(100, line_id="90006", display="96176063972", status="NEW")
    previous = _ticket(90, line_id="1375394715655384", display="+961 76 063 972")
    calls: list[tuple[int, int | None, int, str | None]] = []

    async def get_ticket(ticket_id: int) -> dict[str, Any]:
        return dict(current if ticket_id == 100 else previous)

    async def list_messages(ticket_id: int, *, before_id: int | None = None, limit: int, user_id: str | None) -> list[dict[str, Any]]:
        calls.append((ticket_id, before_id, limit, user_id))
        oldest = 1 if ticket_id == 90 else 1000
        return [{
            "id": message_id,
            "ticket_id": ticket_id,
            "message_type": "text",
            "sender_type": "CUSTOMER",
            "direction": "INBOUND",
            "body": str(message_id),
        } for message_id in range(oldest, oldest + limit)]

    monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(support_service_module.support_repository, "list_messages", list_messages)

    async def run() -> None:
        service = SupportService()
        historical = await service.history_messages_page(
            100, 90, _agent(), before_message_id=150, limit=100,
        )
        assert historical["messages_has_more"] is True
        assert len(historical["messages"]) == 100
        assert historical["messages"][0]["id"] == 2
        assert calls[-1] == (90, 150, 101, "7")

        current_page = await service.messages_page(100, _agent(), before_message_id=1200)
        assert current_page["messages_has_more"] is True
        assert len(current_page["messages"]) == 150
        assert current_page["messages"][0]["id"] == 1001
        assert calls[-1] == (100, 1200, 151, "7")

    asyncio.run(run())


def test_history_cursor_scans_past_other_customer_phones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _ticket(300, line_id="90006", display="96176063972", status="NEW")
    unrelated = [
        _ticket(ticket_id, line_id="90003", display="96170111111", phone="96170999999")
        for ticket_id in range(299, 199, -1)
    ]
    eligible = [
        _ticket(199, line_id="1375394715655384", display="+961 76 063 972"),
        _ticket(198, line_id="1375394715655384", display="96176063972"),
    ]
    candidates = unrelated + eligible
    cursors: list[int | None] = []

    async def get_ticket(_ticket_id: int) -> dict[str, Any]:
        return dict(current)

    async def list_previous_customer_tickets(**kwargs: Any) -> list[dict[str, Any]]:
        before = kwargs.get("before_ticket_id") or 300
        cursors.append(kwargs.get("before_ticket_id"))
        return [dict(row) for row in candidates if row["id"] < before][:kwargs["limit"]]

    async def list_messages(_ticket_id: int, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(support_service_module.support_repository, "list_previous_customer_tickets", list_previous_customer_tickets)
    monkeypatch.setattr(support_service_module.support_repository, "list_messages", list_messages)

    async def run() -> None:
        service = SupportService()
        first = await service.history_page(300, _agent(), limit=1)
        assert [item["ticket"]["id"] for item in first["previous_ticket_history"]] == [199]
        assert first["previous_ticket_history_has_more"] is True
        assert cursors[:2] == [None, 200]

        second = await service.history_page(300, _agent(), before_ticket_id=199, limit=1)
        assert [item["ticket"]["id"] for item in second["previous_ticket_history"]] == [198]
        assert second["previous_ticket_history_has_more"] is False

    asyncio.run(run())


def test_reopened_older_chat_includes_ticket_created_between_chat_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = _ticket(40, line_id="90006", display="96176063972", status="NEW", origin="STAFF")
    intervening_ticket = _ticket(60, line_id="90006", display="96176063972")

    async def get_ticket(_ticket_id: int) -> dict[str, Any]:
        return dict(chat)

    async def list_previous_customer_tickets(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["current_ticket_id"] == 40
        return [dict(intervening_ticket)]

    async def list_messages(_ticket_id: int, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
    monkeypatch.setattr(support_service_module.support_repository, "list_previous_customer_tickets", list_previous_customer_tickets)
    monkeypatch.setattr(support_service_module.support_repository, "list_messages", list_messages)

    shared = asyncio.run(SupportService().history_page(40, _agent()))
    assert [item["ticket"]["id"] for item in shared["previous_ticket_history"]] == [60]


class _CaptureCursor:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls

    async def __aenter__(self) -> "_CaptureCursor":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.calls.append((sql, params))

    async def fetchone(self) -> dict[str, Any]:
        return {"total": 0}

    async def fetchall(self) -> tuple[Any, ...]:
        return ()


class _CaptureConnection:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls

    def cursor(self, *_args: Any) -> _CaptureCursor:
        return _CaptureCursor(self.calls)


class _CaptureAcquire:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls

    async def __aenter__(self) -> _CaptureConnection:
        return _CaptureConnection(self.calls)

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _CapturePool:
    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]) -> None:
        self.calls = calls

    def acquire(self) -> _CaptureAcquire:
        return _CaptureAcquire(self.calls)


def test_repository_history_and_search_count_staff_chat_starter_as_participant() -> None:
    async def run() -> None:
        calls: list[tuple[str, tuple[Any, ...]]] = []
        repository = SupportRepository()
        repository._pool = _CapturePool(calls)  # type: ignore[assignment]

        await repository.agent_has_participated(42, "7")
        assert "ticket.initiated_by_staff_id = %s" in calls[-1][0]
        assert calls[-1][1] == (42, "7", "7", "7", "7", "7")

        calls.clear()
        await repository.list_tickets(view="chats", agent_id="7")
        assert len(calls) == 2
        assert "support_tickets.initiated_by_staff_id = %s" in calls[0][0]
        assert calls[0][1] == ("7", "7", "7", "7", "7")
        assert "LEFT JOIN whatsapp_phone_numbers n" in calls[1][0]
        assert "n.display_name" in calls[1][0]

        calls.clear()
        await repository.search_messages(query="hello", agent_id="7")
        assert len(calls) == 1
        assert "t.initiated_by_staff_id = %s" in calls[0][0]
        assert calls[0][1][:5] == ("7", "7", "7", "7", "7")
        assert "LEFT JOIN whatsapp_phone_numbers n" in calls[0][0]
        assert "n.display_name" in calls[0][0]

        calls.clear()
        await repository.list_previous_customer_tickets(
            current_ticket_id=300,
            phone_number="+961 70 123 456",
            before_ticket_id=199,
            limit=5,
        )
        assert "ORDER BY t.id DESC" in calls[0][0]
        assert "REGEXP_REPLACE(t.whatsapp_phone" in calls[0][0]
        assert "LEFT JOIN whatsapp_phone_numbers n" in calls[0][0]
        assert calls[0][1] == (300, 199, 199, "+961 70 123 456", "96170123456", "0096170123456", 5)

        calls.clear()
        await repository.get_ticket(300)
        assert "LEFT JOIN whatsapp_phone_numbers n" in calls[0][0]
        assert "n.display_name" in calls[0][0]

    asyncio.run(run())
