"""Customer media follow the same size cap as staff uploads (32 MB by default).

A file above the cap is not downloaded or stored. The message records why, the
dashboard shows it, and nothing tries to download the file again.
"""
from __future__ import annotations

import asyncio
import sys
import types

fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {}))
sys.modules["aiomysql"] = fake_aiomysql
fake_bcrypt = types.ModuleType("bcrypt")
fake_bcrypt.checkpw = lambda *_args, **_kwargs: False
fake_bcrypt.hashpw = lambda *_args, **_kwargs: b"hash"
fake_bcrypt.gensalt = lambda *_args, **_kwargs: b"salt"
sys.modules.setdefault("bcrypt", fake_bcrypt)
fake_openai = types.ModuleType("openai")
fake_openai.AsyncOpenAI = type("AsyncOpenAI", (), {"__init__": lambda self, *a, **k: None})
sys.modules.setdefault("openai", fake_openai)

import pytest

from app import support_api, support_service
from app.config import settings
from app.support_media import SupportMediaStore, SupportMediaTooLargeError, max_upload_bytes
from app.support_ui import _HTML
from app.whatsapp_client import DownloadedWhatsAppMedia, WhatsAppMediaError

TICKET = {"id": 7, "business_phone_number_id": "90052"}
MESSAGE = {"id": 41, "ticket_id": 7, "message_type": "video", "provider_media_id": "ldmedia:90052:x"}


@pytest.fixture
def capture(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "support_media_directory", str(tmp_path / "media"))
    store = SupportMediaStore()
    monkeypatch.setattr(support_service, "support_media_store", store)
    state = {"downloads": 0, "result": None, "events": []}

    async def reference(*_args, **_kwargs):
        return None

    async def download(_media_id, **_kwargs):
        state["downloads"] += 1
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    async def publish(event_type, **payload):
        state["events"].append((event_type, payload))

    monkeypatch.setattr(support_service.support_repository, "set_message_media_reference", reference)
    monkeypatch.setattr(support_service.whatsapp_client, "download_media", download)
    monkeypatch.setattr(support_service, "publish_support_event", publish)

    def run():
        return asyncio.run(support_service.SupportService().capture_inbound_media(
            ticket=TICKET, message=dict(MESSAGE), media_id=MESSAGE["provider_media_id"],
            message_type="video", expected_mime_type="video/mp4",
        ))
    return state, store, run


def test_customer_video_above_the_upload_cap_is_rejected_once_and_explained(capture, monkeypatch) -> None:
    state, store, run = capture
    monkeypatch.setattr(settings, "support_upload_max_bytes", 8)
    assert max_upload_bytes("video") == 8
    state["result"] = DownloadedWhatsAppMedia(content=b"123456789", mime_type="video/mp4", file_size=9, sha256=None)
    with pytest.raises(SupportMediaTooLargeError) as rejected:
        run()
    assert rejected.value.retryable is False
    assert store.get(7, 41) is None, "the file is not stored"
    assert store.too_large_limit(7, 41) == 8
    event_type, payload = state["events"][-1]
    assert event_type == "message_media_ready"
    shown = payload["message"]
    assert shown["media_too_large"] is True and shown["media_available"] is False
    assert "media_url" not in shown, "no download link, so the dashboard never retries"
    # A later attempt (webhook retry, a staff member opening the media) does not download again.
    with pytest.raises(SupportMediaTooLargeError):
        run()
    assert state["downloads"] == 1


def test_a_transport_too_large_refusal_is_recorded_the_same_way(capture) -> None:
    state, store, run = capture
    state["result"] = WhatsAppMediaError("Incoming media exceeds the 32 MiB limit.", retryable=False, too_large=True)
    with pytest.raises(SupportMediaTooLargeError) as rejected:
        run()
    assert "larger than 32 MB" in str(rejected.value)
    assert store.too_large_limit(7, 41) == 32 * 1024 * 1024
    assert state["events"][-1][1]["message"]["media_limit_label"] == "32 MB"


def test_a_file_within_the_cap_is_stored_normally(capture) -> None:
    state, store, run = capture
    state["result"] = DownloadedWhatsAppMedia(content=b"\x00\x00\x00\x18ftypmp42", mime_type="video/mp4", file_size=12, sha256=None)
    stored = run()
    assert store.get(7, 41) == stored and store.too_large_limit(7, 41) is None
    shown = state["events"][-1][1]["message"]
    assert shown["media_available"] is True and "media_too_large" not in shown


def test_the_media_endpoint_answers_413_with_the_staff_explanation() -> None:
    error = support_api._translate_error(SupportMediaTooLargeError(32 * 1024 * 1024))
    assert error.status_code == 413
    assert "Ask the customer to send a smaller file" in error.detail


def test_the_dashboard_explains_a_file_that_was_too_large() -> None:
    assert "m.media_too_large" in _HTML
    assert "so it was not downloaded. Ask the customer to send a smaller file." in _HTML
