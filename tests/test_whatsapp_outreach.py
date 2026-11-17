"""Chat limits per WhatsApp number (the outreach guard).

On 2026-11-17 WhatsApp banned Tripoli after one announcement went to 26
customers in under an hour from a four-day-old number. These tests pin the
guard that refuses such sends before WhatsApp is ever asked about a number.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
sys.modules["aiomysql"] = fake_aiomysql

from fastapi import HTTPException
from pydantic import ValidationError

from app import support_api
from app.config import settings
from app.support_auth import SupportAgent
from app.support_service import SupportService
from app.support_ui import _HTML
from app.whatsapp_outreach import (
    OutreachLimitError,
    OutreachLimits,
    OutreachUnavailableError,
    WhatsAppOutreachGuard,
    resolve_limits,
    text_fingerprint,
)

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
AGENT = SupportAgent(id="7", name="Agent", role="agent")
ADMIN = SupportAgent(id="6", name="Admin", role="supervisor")


class FakeRedis:
    """Mirrors WhatsAppOutreachGuard._RESERVE_SCRIPT on Python sets."""

    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.ttl: dict[str, int] = {}
        self.fail = False

    async def eval(self, _script, numkeys, *args):
        if self.fail:
            raise ConnectionError("redis down")
        day_key, text_key = args[:numkeys]
        recipient, daily, same, ttl = args[numkeys:]
        day = self.sets.setdefault(day_key, set())
        text = self.sets.setdefault(text_key, set())
        if recipient not in day and len(day) >= int(daily):
            return [0, 1, len(day)]
        if recipient not in text and len(text) >= int(same):
            return [0, 2, len(text)]
        day.add(recipient)
        text.add(recipient)
        self.ttl[day_key] = self.ttl[text_key] = int(ttl)
        return [1, 0, len(day)]

    async def scard(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return len(self.sets.get(key, set()))


def guard_with(fake: FakeRedis, now: datetime = NOW) -> WhatsAppOutreachGuard:
    guard = WhatsAppOutreachGuard(redis_url="redis://unused", clock=lambda: now)
    guard._redis_client = fake
    return guard


def limits(daily=10, new_daily=5, same=5, new=False) -> OutreachLimits:
    return OutreachLimits(daily, new_daily, same, NOW + timedelta(days=3) if new else None, new)


def test_defaults_apply_until_an_admin_sets_a_number_and_new_numbers_are_lower() -> None:
    old = resolve_limits({"created_at": NOW - timedelta(days=30)}, now=NOW)
    assert (old.daily_limit, old.new_number_daily_limit, old.same_text_limit) == (10, 5, 5)
    assert not old.new_number and old.limit_today == 10
    new = resolve_limits({"created_at": NOW - timedelta(days=4)}, now=NOW)
    assert new.new_number and new.limit_today == 5
    assert new.new_number_until == NOW + timedelta(days=10)
    custom = resolve_limits({"created_at": NOW - timedelta(days=4), "outreach_daily_limit": 25,
                             "outreach_new_number_daily_limit": 8, "outreach_same_text_limit": 0}, now=NOW)
    assert custom.limit_today == 8 and custom.same_text_limit == 0
    assert custom.custom == {"daily_limit": 25, "new_number_daily_limit": 8, "same_text_limit": 0}
    unknown = resolve_limits(None, now=NOW)
    assert unknown.limit_today == 10 and not unknown.new_number


def test_same_text_ignores_case_and_spacing_only() -> None:
    assert text_fingerprint("Dear customers,\n please  collect") == text_fingerprint("dear CUSTOMERS, please collect ")
    assert text_fingerprint("Dear customers") != text_fingerprint("Dear customer")


def test_daily_limit_counts_customers_not_messages() -> None:
    fake = FakeRedis()
    guard = guard_with(fake)

    async def scenario():
        for index in range(10):
            await guard.reserve(line_id="90065", recipient_phone=f"9617000{index:04}", fingerprint=f"text:{index}", limits=limits())
        # Writing again to someone already contacted today uses no slot.
        assert await guard.reserve(line_id="90065", recipient_phone="96170000003", fingerprint="text:x", limits=limits()) == 10
        with pytest.raises(OutreachLimitError, match="started its 10 new chats for today"):
            await guard.reserve(line_id="90065", recipient_phone="96170009999", fingerprint="text:y", limits=limits())
        # Other numbers have their own budget.
        assert await guard.reserve(line_id="90062", recipient_phone="96170009999", fingerprint="text:y", limits=limits()) == 1

    asyncio.run(scenario())
    assert all(ttl == 2 * 24 * 60 * 60 for ttl in fake.ttl.values())
    assert not any("9617000" in key for key in fake.sets), "phone numbers are hashed in Redis keys"


def test_the_tripoli_broadcast_stops_at_the_same_text_limit() -> None:
    guard = guard_with(FakeRedis())
    announcement = text_fingerprint("Bonjour, dear customers, please collect your orders today without delay.")

    async def scenario():
        for index in range(5):
            await guard.reserve(line_id="90065", recipient_phone=f"96170{index}", fingerprint=announcement, limits=limits(daily=50))
        with pytest.raises(OutreachLimitError, match="already sent to 5 customers today"):
            await guard.reserve(line_id="90065", recipient_phone="961709", fingerprint=announcement, limits=limits(daily=50))
        # A different text to a new customer is still fine.
        await guard.reserve(line_id="90065", recipient_phone="961709", fingerprint=text_fingerprint("Your parcel arrived."), limits=limits(daily=50))

    asyncio.run(scenario())


def test_new_number_and_zero_limits_explain_themselves() -> None:
    guard = guard_with(FakeRedis())

    async def scenario():
        for index in range(5):
            await guard.reserve(line_id="90066", recipient_phone=f"9613{index}", fingerprint=f"text:{index}", limits=limits(new=True))
        with pytest.raises(OutreachLimitError, match=r"is new and can start 5 new chats a day until 2026-11-20"):
            await guard.reserve(line_id="90066", recipient_phone="96139", fingerprint="text:9", limits=limits(new=True))
        with pytest.raises(OutreachLimitError, match="turned off for this WhatsApp number"):
            await guard.reserve(line_id="90067", recipient_phone="96139", fingerprint="text:9", limits=limits(daily=0))

    asyncio.run(scenario())


def test_counter_store_outage_fails_closed_and_usage_shows_unknown() -> None:
    fake = FakeRedis()
    fake.fail = True
    guard = guard_with(fake)
    with pytest.raises(OutreachUnavailableError, match="paused"):
        asyncio.run(guard.reserve(line_id="90065", recipient_phone="961", fingerprint="text:a", limits=limits()))
    assert asyncio.run(guard.used_today(["90065"])) == {"90065": None}


def test_the_local_day_is_beirut_not_utc() -> None:
    late = datetime(2026, 11, 17, 22, 30, tzinfo=timezone.utc)  # 00:30 on 18 Nov in Beirut
    assert WhatsAppOutreachGuard(redis_url="redis://unused", clock=lambda: late).local_day() == "2026-11-18"


def _start_chat_mocks(monkeypatch, reserve):
    import app.support_service as module

    lookups, created = [], []
    monkeypatch.setattr(module.whatsapp_registry_service, "is_number_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(module.whatsapp_registry_service, "list_enabled_numbers",
                        AsyncMock(return_value=[{"phone_number_id": "90065", "display_phone_number": "+961 70 000 000"}]))
    monkeypatch.setattr(module.whatsapp_registry_service, "outreach_limits", AsyncMock(return_value=limits()))
    monkeypatch.setattr(module.whatsapp_outreach_guard, "reserve", reserve)
    monkeypatch.setattr(module, "get_transport", lambda _line: "linked_device")

    async def lookup(phone, *, phone_number_id):
        lookups.append(phone)
        return {"exists": True, "whatsapp_phone": phone}

    async def create(**kwargs):
        created.append(kwargs)

    monkeypatch.setattr(module.linked_device_client, "check_number", lookup)
    monkeypatch.setattr(module.support_repository, "create_staff_chat", create)
    return lookups, created


def test_a_refused_chat_never_asks_whatsapp_about_the_number(monkeypatch) -> None:
    lookups, created = _start_chat_mocks(monkeypatch, AsyncMock(side_effect=OutreachLimitError("limit reached")))
    with pytest.raises(OutreachLimitError):
        asyncio.run(SupportService().start_chat(agent=AGENT, phone_number="70 123 456",
                                                business_phone_number_id="90065", body="Dear customers"))
    assert lookups == [] and created == []


def test_start_chat_reserves_with_the_text_and_the_parsed_number(monkeypatch) -> None:
    calls = []

    async def reserve(**kwargs):
        calls.append(kwargs)
        raise OutreachLimitError("stop here")

    _start_chat_mocks(monkeypatch, reserve)
    with pytest.raises(OutreachLimitError):
        asyncio.run(SupportService().start_chat(agent=AGENT, phone_number="70 123 456",
                                                business_phone_number_id="90065", body="  Dear   Customers "))
    assert calls[0]["line_id"] == "90065"
    assert calls[0]["recipient_phone"] == "96170123456"
    assert calls[0]["fingerprint"] == text_fingerprint("dear customers")


@pytest.mark.parametrize(("body", "expected"), [("Dear customers", text_fingerprint("Dear customers")), ("", "message:55")])
def test_forward_to_a_new_contact_counts_as_a_new_chat(monkeypatch, body, expected) -> None:
    import app.support_service as module

    calls = []

    async def reserve(**kwargs):
        calls.append(kwargs)
        raise OutreachLimitError("stop here")

    _start_chat_mocks(monkeypatch, reserve)
    ticket = {"id": 9, "business_phone_number_id": "90065", "business_display_phone_number": "+961 70 000 000",
              "whatsapp_phone": "96171000000", "status": "ASSIGNED", "current_assignee_id": "7"}
    monkeypatch.setattr(module.support_repository, "get_ticket", AsyncMock(return_value=ticket))
    monkeypatch.setattr(module.support_repository, "get_message",
                        AsyncMock(return_value={"id": 55, "ticket_id": 9, "body": body, "message_type": "text" if body else "image"}))
    monkeypatch.setattr(module.support_repository, "message_hidden_for_user", AsyncMock(return_value=False))
    monkeypatch.setattr(module, "_can_view_ticket", AsyncMock(return_value=True))
    with pytest.raises(OutreachLimitError):
        asyncio.run(SupportService().forward_message_to_contact(
            source_ticket_id=9, source_message_id=55, phone_number="70 123 456", customer_name=None, agent=AGENT))
    assert calls[0]["fingerprint"] == expected


def test_api_answers_429_for_a_limit_and_503_when_counters_are_down() -> None:
    assert support_api._translate_error(OutreachLimitError("limit")).status_code == 429
    assert support_api._translate_error(OutreachUnavailableError("down")).status_code == 503


def test_limits_request_accepts_whole_numbers_or_default_only() -> None:
    model = support_api.WhatsAppNumberOutreachLimitsRequest
    assert model(daily_limit=20, new_number_daily_limit=None, same_text_limit=0).model_dump() == {
        "daily_limit": 20, "new_number_daily_limit": None, "same_text_limit": 0}
    for bad in ({"daily_limit": 501}, {"daily_limit": -1}, {"daily_limit": "12"}, {"daily_limit": 2.5}, {"daily_limit": True}):
        with pytest.raises(ValidationError):
            model(**{"daily_limit": 10, "new_number_daily_limit": 5, "same_text_limit": 5, **bad})
    with pytest.raises(ValidationError):
        model(daily_limit=10, new_number_daily_limit=5, same_text_limit=5, override=True)


def test_only_admins_change_limits_and_every_change_is_audited(monkeypatch) -> None:
    from app.whatsapp_registry import (
        WhatsAppRegistryForbiddenError,
        WhatsAppRegistryService,
        WhatsAppRegistryValidationError,
    )

    row = {"phone_number_id": "90065", "waba_id": None, "source": "LINKED_DEVICE", "provider_present": 1,
           "removed_at": None, "outreach_daily_limit": None, "outreach_new_number_daily_limit": None,
           "outreach_same_text_limit": None}
    executed = []

    async def execute(query, params=()):
        executed.append((query, params))
        return 1

    repository = SimpleNamespace(fetch_one=AsyncMock(return_value=row), execute=execute)
    service = WhatsAppRegistryService(repository)
    monkeypatch.setattr(service, "_ensure_schema", AsyncMock())
    monkeypatch.setattr(service, "list_payload", AsyncMock(return_value={"numbers": []}))
    values = {"daily_limit": 20, "new_number_daily_limit": None, "same_text_limit": 3}
    with pytest.raises(WhatsAppRegistryForbiddenError):
        asyncio.run(service.set_outreach_limits(AGENT, "90065", values))
    with pytest.raises(WhatsAppRegistryValidationError):
        asyncio.run(service.set_outreach_limits(ADMIN, "90065", {**values, "daily_limit": 900}))
    assert executed == []
    asyncio.run(service.set_outreach_limits(ADMIN, "90065", values))
    update, audit = executed
    assert "SET outreach_daily_limit = %s, outreach_new_number_daily_limit = %s, outreach_same_text_limit = %s" in update[0]
    assert update[1] == (20, None, 3, 6, "90065")
    assert audit[1][0] == "WHATSAPP_OUTREACH_LIMITS_CHANGED"
    assert '"to": {"daily_limit": 20, "new_number_daily_limit": null, "same_text_limit": 3}' in audit[1][4]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_numbers_list_shows_todays_chats_against_the_limit() -> None:
    code = _HTML[_HTML.index("function outreachChips("): _HTML.index("function openOutreachLimitsEditor(")]
    script = r"""
const assert = require('node:assert/strict');
const createMetaChip = (label, icon) => ({label, icon, title: ''});
""" + code + r"""
let chips = outreachChips({used_today: 3, limit_today: 10, same_text_limit: 5, new_number: false});
assert.deepEqual(chips.map(chip => chip.label), ['New chats today: 3 of 10']);
assert.equal(chips[0].icon, 'activity');
assert.match(chips[0].title, /same text can go to at most 5 customers/);
chips = outreachChips({used_today: 5, limit_today: 5, same_text_limit: 5, new_number: true, new_number_until: '2026-11-27T11:52:33Z'});
assert.equal(chips[0].label, 'New chats today: 5 of 5');
assert.equal(chips[0].icon, 'alert', 'a full day is flagged');
assert.match(chips[1].label, /^New number until /);
assert.equal(outreachChips({used_today: null, limit_today: 10, same_text_limit: 5})[0].label, 'New chats today: unknown of 10');
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_limits_editor_is_for_managers_and_uses_the_limits_route() -> None:
    renderer = _HTML[_HTML.index("function renderWhatsAppRegistry("): _HTML.index("function openWhatsAppNumberNameEditor(")]
    assert "if (canManageWhatsApp() && item.outreach)" in renderer
    assert "'/outreach-limits'" in renderer
    assert "if (!canManageWhatsApp()" in renderer[renderer.index("function openOutreachLimitsEditor("):]


def test_settings_default_to_the_approved_limits() -> None:
    assert settings.whatsapp_outreach_daily_limit == 10
    assert settings.whatsapp_outreach_new_number_daily_limit == 5
    assert settings.whatsapp_outreach_same_text_limit == 5
    assert settings.whatsapp_outreach_new_number_days == 14
    assert settings.whatsapp_outreach_timezone == "Asia/Beirut"
