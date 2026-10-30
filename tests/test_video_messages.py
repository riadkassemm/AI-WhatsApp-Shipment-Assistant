"""WhatsApp video messages and image copying in the support dashboard."""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

os.environ["REDIS_URL"] = ""

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

import app.support_service as support_service_module
from app import config, main, support_api
from app.client_chat_auth import extract_credentials
from app.support_auth import SupportAgent
from app.support_media import (
    SUPPORT_MEDIA_MESSAGE_TYPES,
    SupportMediaStore,
    max_media_bytes,
    media_extension,
    media_kind_for_mime,
)
from app.support_service import SupportValidationError
from app.support_ui import _HTML, support_dashboard
from app.whatsapp_client import IncomingWhatsAppMessage, WhatsAppClient, extract_incoming_message

AGENT = SupportAgent(id="7", name="Rana", role="agent")


def _payload(message: dict) -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


def test_cloud_api_video_keeps_media_reference_and_caption() -> None:
    incoming = extract_incoming_message(_payload({
        "from": "96170123456", "id": "wamid.video", "timestamp": "1", "type": "video",
        "video": {"id": "media-video-1", "mime_type": "video/mp4", "sha256": "f00d", "caption": "Box arrived crushed"},
    }))
    assert incoming is not None
    assert incoming.message_type == "video"
    assert incoming.text is None
    assert (incoming.media_id, incoming.media_mime_type, incoming.media_sha256) == ("media-video-1", "video/mp4", "f00d")
    assert incoming.media_caption == "Box arrived crushed"


def test_video_mime_types_size_limit_and_storage(tmp_path: Path, monkeypatch) -> None:
    assert "video" in SUPPORT_MEDIA_MESSAGE_TYPES
    assert media_kind_for_mime("video/mp4") == "video"
    assert media_kind_for_mime("video/3gpp") == "video"
    assert media_kind_for_mime("video/quicktime") is None
    assert media_extension("video/mp4") == ".mp4"
    assert media_extension("video/3gpp") == ".3gp"
    # Browser voice recordings may arrive as video/webm; they must stay audio.
    assert media_kind_for_mime("video/webm", allow_browser_audio=True) == "audio"
    assert max_media_bytes("video") == 16 * 1024 * 1024

    monkeypatch.setattr(config.settings, "support_media_directory", str(tmp_path / "media"))
    store = SupportMediaStore()
    asset = store.save(ticket_id=3, message_id=9, content=b"\x00\x00\x00\x18ftypmp42", mime_type="video/mp4")
    assert asset.path.name == "media.mp4"
    assert store.get(3, 9).mime_type == "video/mp4"
    reference = store.save_remote_reference(ticket_id=3, message_id=10, media_id="media-video-1", message_type="video")
    assert store.get_remote_reference(3, 10) == reference

    monkeypatch.setattr(config.settings, "support_video_max_bytes", 4)
    with pytest.raises(Exception, match="too large"):
        store.save(ticket_id=3, message_id=11, content=b"12345", mime_type="video/mp4")


def test_cloud_api_video_send_includes_caption() -> None:
    async def run() -> dict:
        client = WhatsAppClient()
        seen: list[dict] = []

        async def fake_send(payload: dict, *, label: str) -> dict:
            seen.append(payload)
            return {"messages": [{"id": "wamid.out"}]}

        client._send_payload = fake_send  # type: ignore[method-assign]
        await client.send_media(to="96170123456", media_type="video", media_id="media-9", caption="Return label")
        return seen[0]

    payload = asyncio.run(run())
    assert payload["type"] == "video"
    assert payload["video"] == {"id": "media-9", "caption": "Return label"}


def test_inbound_video_is_labelled_for_the_timeline_instead_of_unsupported() -> None:
    def body(caption: str | None) -> str:
        incoming = IncomingWhatsAppMessage(
            "96170123456", "wamid.v", "video", None, "1", media_id="media-v", media_caption=caption,
        )
        return main._human_inbound_body(incoming, extract_credentials(""))

    assert body("Look at the box") == "Look at the box"
    assert body(None) == "Video"
    assert "Unsupported" not in body(None)


def _stub_agent_send(monkeypatch) -> dict:
    ticket = {"id": 77, "status": "ASSIGNED", "active_key": "business:96170123456", "current_assignee_id": "7",
              "whatsapp_phone": "96170123456", "business_phone_number_id": "business"}
    sent: dict = {}

    async def get_ticket(_ticket_id):
        return dict(ticket)

    async def no_existing(_client_id, **_scope):
        return None

    async def add_message(**kwargs):
        sent["stored"] = kwargs
        return {"id": 901, "ticket_id": 77, "transport_client_id": "unit-wire-id", **kwargs}, True

    real_save = support_service_module.support_media_store.save

    def save(**kwargs):
        sent["saved"] = kwargs
        return real_save(**kwargs)

    async def upload_media(**kwargs):
        sent["upload"] = kwargs
        return "meta-media-id"

    async def send_media(**kwargs):
        sent["send"] = kwargs
        return {"messages": [{"id": "wamid.out.video"}]}

    async def update_send_state(message_id, **kwargs):
        sent["send_state"] = kwargs

    async def nothing(*_args, **_kwargs):
        return None

    async def get_message(message_id):
        return {"id": message_id, "ticket_id": 77, "message_type": "video", "send_status": "SENT"}

    repo = support_service_module.support_repository
    monkeypatch.setattr(repo, "get_ticket", get_ticket)
    monkeypatch.setattr(repo, "get_message_by_client_id", no_existing)
    monkeypatch.setattr(repo, "add_ticket_message", add_message)
    monkeypatch.setattr(repo, "update_message_send_state", update_send_state)
    monkeypatch.setattr(repo, "mark_agent_reply_started", nothing)
    monkeypatch.setattr(repo, "get_message", get_message)
    monkeypatch.setattr(support_service_module.support_media_store, "save", save)
    monkeypatch.setattr(support_service_module.whatsapp_client, "upload_media", upload_media)
    monkeypatch.setattr(support_service_module.whatsapp_client, "send_media", send_media)
    return sent


def test_staff_can_send_a_video_with_caption(monkeypatch, outbound_unit) -> None:
    sent = _stub_agent_send(monkeypatch)
    result = asyncio.run(support_service_module.support_service.send_agent_media(
        77, agent=AGENT, media_type="video", content=b"mp4-bytes", content_type="video/mp4",
        filename="unboxing.mp4", caption="  Video of the return  ", client_message_id="client-video-1",
    ))
    assert result["send_status"] == "SENT"
    assert sent["stored"]["message_type"] == "video"
    assert sent["stored"]["body"] == "Video of the return"
    assert sent["upload"]["mime_type"] == "video/mp4"
    assert sent["upload"]["filename"] == "unboxing.mp4"
    assert sent["send"]["media_type"] == "video"
    assert sent["send"]["caption"] == "Video of the return"

    sent = _stub_agent_send(monkeypatch)
    asyncio.run(support_service_module.support_service.send_agent_media(
        77, agent=AGENT, media_type="video", content=b"mp4", content_type="video/mp4",
        filename=None, caption="", client_message_id="client-video-2",
    ))
    assert sent["stored"]["body"] == "Video"
    assert sent["upload"]["filename"] == "video.mp4"


@pytest.mark.parametrize(("content", "mime", "message"), [
    (b"mov", "video/quicktime", "MP4 or 3GP"),
    (b"", "video/mp4", "empty"),
])
def test_unsupported_or_empty_videos_are_rejected_before_sending(monkeypatch, content, mime, message) -> None:
    sent = _stub_agent_send(monkeypatch)
    with pytest.raises(SupportValidationError, match=message):
        asyncio.run(support_service_module.support_service.send_agent_media(
            77, agent=AGENT, media_type="video", content=content, content_type=mime,
            filename="clip", caption=None, client_message_id="client-video-3",
        ))
    assert "send" not in sent


def test_oversized_video_is_rejected(monkeypatch, outbound_unit) -> None:
    monkeypatch.setattr(config.settings, "support_video_max_bytes", 3)
    sent = _stub_agent_send(monkeypatch)
    with pytest.raises(SupportValidationError, match="3 MB|0 MB|WhatsApp limit"):
        asyncio.run(support_service_module.support_service.send_agent_media(
            77, agent=AGENT, media_type="video", content=b"1234", content_type="video/mp4",
            filename="clip.mp4", caption=None, client_message_id="client-video-4",
        ))
    assert "send" not in sent


def test_video_messages_are_served_with_a_playable_media_url(monkeypatch) -> None:
    asset = types.SimpleNamespace(mime_type="video/mp4", size_bytes=9, filename="customer-video.mp4")
    monkeypatch.setattr(support_service_module.support_media_store, "get", lambda *_args: asset)
    monkeypatch.setattr(support_service_module.support_media_store, "get_remote_reference", lambda *_args: None)
    row = {"id": 5, "ticket_id": 2, "message_type": "video", "body": "Video", "provider_media_id": "media-v",
           "sender_type": "CUSTOMER", "direction": "INBOUND"}
    message = support_service_module._serialize_support_messages(2, [row])[0]
    assert message["media_kind"] == "video"
    assert message["media_url"] == "/api/support/tickets/2/messages/5/media"
    assert message["media_mime_type"] == "video/mp4"
    assert "provider_media_id" not in message


def test_upload_route_accepts_video_with_the_video_size_limit() -> None:
    annotation = inspect.signature(support_api.send_ticket_media).parameters["media_type"].annotation
    assert "video" in str(annotation)
    assert max_media_bytes("video") == config.settings.support_video_max_bytes


def test_dashboard_page_gets_the_configured_video_limit(monkeypatch) -> None:
    monkeypatch.setattr(config.settings, "support_ui_enabled", True)
    monkeypatch.setattr(config.settings, "support_video_max_bytes", 12345)
    response = asyncio.run(support_dashboard())
    assert b"__VIDEO_MAX_BYTES__" not in response.body
    from app.support_ui import support_asset
    assert b"Number('12345')" in asyncio.run(support_asset("media.js")).body
    assert 'accept="image/jpeg,image/png,image/webp,application/pdf,video/mp4,video/3gpp,audio/mpeg,audio/mp4,audio/aac,audio/amr,audio/ogg,audio/opus,.opus"' in _HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_attach_picker_accepts_videos_within_the_limit() -> None:
    picker = _HTML[_HTML.index("const VIDEO_MAX_BYTES = "): _HTML.index("async function sendMediaMessage(")]
    script = r"""
const assert = require('node:assert/strict');
const state = {ticketId: 42, composerEnabled: true};
const input = {files: [], value: 'C:\\fakepath'};
const $ = id => ({imageInput: input, message: {focus() {}}})[id];
const calls = [];
const showError = message => calls.push(['error', message]);
const setPendingMedia = (kind, file) => calls.push(['pending', kind, file.name]);
const setAppStatus = message => calls.push(['status', message]);
const formatBytes = bytes => Math.round(bytes / 1024 / 1024) + ' MB';
""" + picker.replace("'__VIDEO_MAX_BYTES__'", "'16777216'") + r"""
input.files = [{name: 'clip.mp4', type: 'video/mp4', size: 5 * 1024 * 1024}];
imageSelected();
assert.deepEqual(calls[0], ['pending', 'video', 'clip.mp4']);
assert.match(calls[1][1], /Review the preview/);
calls.length = 0;
input.files = [{name: 'big.mp4', type: 'video/mp4', size: 40 * 1024 * 1024}];
imageSelected();
assert.deepEqual(calls[0], ['error', 'This file exceeds the 16 MB limit.']);
assert.deepEqual(calls[1], ['status', '0 of 1 attachments added. Review the file errors.']);
assert.equal(input.value, '');
calls.length = 0;
input.files = [{name: 'clip.mov', type: 'video/quicktime', size: 10}];
imageSelected();
assert.match(calls[0][1], /MP4\/3GP/);
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_copy_image_writes_png_to_the_clipboard() -> None:
    helpers = _HTML[_HTML.index("async function imageBlobAsPng("): _HTML.index("function renderReplyDraft(")]
    script = r"""
const assert = require('node:assert/strict');
const notifications = [];
const notify = (message, type = 'success') => notifications.push([message, type]);
const written = [];
globalThis.ClipboardItem = class { constructor(items) { this.items = items; } };
const navigator = {clipboard: {write: async items => { written.push(...items); for (const item of items) await item.items['image/png']; }}};
const fetched = [];
globalThis.fetch = async (url, options) => { fetched.push([url, options.credentials]); return {ok: true, blob: async () => new Blob(['png'], {type: 'image/png'})}; };
""" + helpers + r"""
(async () => {
  assert.equal(await copyImageToClipboard('/api/support/tickets/1/messages/2/media'), true);
  assert.deepEqual(fetched, [['/api/support/tickets/1/messages/2/media', 'same-origin']]);
  assert.equal((await written[0].items['image/png']).type, 'image/png');
  assert.deepEqual(notifications.pop(), ['Image copied.', 'success']);

  // An edited image is passed as a function that renders a PNG blob.
  assert.equal(await copyImageToClipboard(async () => new Blob(['edited'], {type: 'image/png'})), true);
  assert.equal(await (await written[1].items['image/png']).text(), 'edited');

  globalThis.fetch = async () => ({ok: false});
  assert.equal(await copyImageToClipboard('/missing'), false);
  assert.deepEqual(notifications.pop(), ['Could not copy the image. Try Download instead.', 'error']);

  delete globalThis.ClipboardItem;
  assert.equal(await copyImageToClipboard('/api/support/tickets/1/messages/2/media'), false);
  assert.match(notifications.pop()[0], /cannot copy images/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_dashboard_renders_video_and_copy_image_actions() -> None:
    add_message = _HTML[_HTML.index("function addMessage("): _HTML.index("function addEvent(")]
    assert "['image', 'audio', 'document', 'sticker', 'video'].includes(kind)" in add_message
    assert "const video = document.createElement('video');" in add_message
    assert "labelMessageAction(copyImage, 'copy-image', 'Copy image');" in add_message
    assert "if (kind === 'video') return 'Video';" in _HTML
    assert 'id="copyMediaBtn"' in _HTML and 'id="editorCopyBtn"' in _HTML
    assert 'id="editorCropBtn"' in _HTML and 'id="editorRotateBtn"' in _HTML
