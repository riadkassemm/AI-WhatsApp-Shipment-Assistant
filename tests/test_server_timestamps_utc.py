"""Server timestamps are UTC and must be shown as UTC in the viewer's time zone.

The database runs in local time (UTC+3 on the dev VPS) while the app writes
UTC_TIMESTAMP(). A naive datetime sent without an offset was read by the
browser as local time, so a Google sync done "just now" showed "3h ago".
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import datetime

import pytest

from app import google_contacts
from app.google_contacts import GoogleContactsService
from app.support_ui import _HTML
from app.whatsapp_registry import WhatsAppRegistryService


def test_google_status_sends_last_sync_with_utc_offset(monkeypatch) -> None:
    service = GoogleContactsService()

    async def account():
        return {"google_email": "ops@example.com", "google_display_name": "Ops",
                "last_synced_at": datetime(2026, 10, 17, 22, 27, 0, 125000)}

    monkeypatch.setattr(service, "_account", account)
    status = asyncio.run(service.status())
    assert status["account"]["last_synced_at"] == "2026-10-17T22:27:00.125000+00:00"


def test_google_status_without_a_sync_stays_empty(monkeypatch) -> None:
    service = GoogleContactsService()

    async def account():
        return {"google_email": "ops@example.com", "google_display_name": "Ops", "last_synced_at": None}

    monkeypatch.setattr(service, "_account", account)
    assert asyncio.run(service.status())["account"]["last_synced_at"] is None


def test_whatsapp_registry_sync_times_carry_utc_offset_but_db_default_columns_do_not() -> None:
    synced = datetime(2026, 10, 17, 22, 27, 0)
    created = datetime(2026, 10, 18, 1, 0, 0)  # DB default CURRENT_TIMESTAMP: server local time
    account = WhatsAppRegistryService._normalize_account({"waba_id": "1", "last_synced_at": synced, "created_at": created})
    number = WhatsAppRegistryService._normalize_number({"phone_number_id": "9", "last_synced_at": synced, "created_at": created})
    for payload in (account, number):
        assert payload["last_synced_at"] == "2026-10-17T22:27:00+00:00"
        assert payload["created_at"] is created, "local-time default columns must not be relabelled as UTC"


def test_utc_isoformat_keeps_aware_values_and_non_datetimes() -> None:
    from datetime import timedelta, timezone

    beirut = timezone(timedelta(hours=3))
    aware = datetime(2026, 10, 18, 1, 27, tzinfo=beirut)
    assert google_contacts._utc_isoformat(aware) == "2026-10-17T22:27:00+00:00"
    assert google_contacts._utc_isoformat("2026-10-17T22:27:00Z") == "2026-10-17T22:27:00Z"
    assert google_contacts._utc_isoformat(None) is None


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_dashboard_reads_offsetless_server_timestamps_as_utc_in_a_utc_plus_3_browser() -> None:
    helpers = _HTML[_HTML.index("const NAIVE_SERVER_TIMESTAMP = "): _HTML.index("function messageOccurredAt(")]
    relative = _HTML[_HTML.index("function relativeTime("): _HTML.index("function formatStatus(")]
    script = r"""
const assert = require('node:assert/strict');
""" + helpers + relative + r"""
assert.equal(new Date().getTimezoneOffset(), -180, 'test runs in UTC+3');
const utc = '2026-10-17T22:27:00.125000';
assert.equal(parseServerDate(utc).toISOString(), '2026-10-17T22:27:00.125Z');
assert.equal(parseServerDate('2026-10-17 22:27:00').toISOString(), '2026-10-17T22:27:00.000Z');
assert.equal(parseServerDate('2026-10-17T22:27:00+00:00').toISOString(), '2026-10-17T22:27:00.000Z');
assert.equal(parseServerDate('2026-10-18T01:27:00+03:00').toISOString(), '2026-10-17T22:27:00.000Z');
assert.equal(parseServerDate('2026-10-17T22:27:00Z').toISOString(), '2026-10-17T22:27:00.000Z');
const date = new Date(0);
assert.equal(parseServerDate(date), date);
assert.ok(Number.isNaN(parseServerDate('').getTime()));
assert.ok(Number.isNaN(parseServerDate('not a date').getTime()));

// A sync that just happened, sent without an offset, must read as "now", not "3 hr. ago".
const justNow = new Date(Date.now() - 5000).toISOString().replace('Z', '');
const label = relativeTime(justNow);
assert.doesNotMatch(label, /hr|hour/, label);
"""
    env = {**os.environ, "TZ": "Etc/GMT-3"}
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10, env=env)
    assert result.returncode == 0, result.stderr
