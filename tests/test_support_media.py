from __future__ import annotations

import asyncio
from pathlib import Path

from app import config
from app.support_media import SupportMediaStore, media_kind_for_mime, normalize_mime_type
from app.support_ui import _HTML
from app.whatsapp_client import WhatsAppClient, extract_incoming_message


def _payload(message: dict) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [message],
                        }
                    }
                ]
            }
        ]
    }


def test_extract_incoming_image_keeps_media_metadata_and_caption() -> None:
    incoming = extract_incoming_message(
        _payload(
            {
                "from": "96170123456",
                "id": "wamid.image",
                "timestamp": "123",
                "type": "image",
                "image": {
                    "id": "media-image-1",
                    "mime_type": "image/jpeg",
                    "sha256": "abc123",
                    "caption": "damaged package",
                },
            }
        )
    )
    assert incoming is not None
    assert incoming.message_type == "image"
    assert incoming.text is None
    assert incoming.media_id == "media-image-1"
    assert incoming.media_mime_type == "image/jpeg"
    assert incoming.media_sha256 == "abc123"
    assert incoming.media_caption == "damaged package"


def test_extract_incoming_voice_note_keeps_voice_flag() -> None:
    incoming = extract_incoming_message(
        _payload(
            {
                "from": "96170123456",
                "id": "wamid.audio",
                "timestamp": "124",
                "type": "audio",
                "audio": {
                    "id": "media-audio-1",
                    "mime_type": "audio/ogg; codecs=opus",
                    "sha256": "def456",
                    "voice": True,
                },
            }
        )
    )
    assert incoming is not None
    assert incoming.message_type == "audio"
    assert incoming.media_id == "media-audio-1"
    assert incoming.is_voice is True
    assert normalize_mime_type(incoming.media_mime_type) == "audio/ogg"
    assert media_kind_for_mime(incoming.media_mime_type) == "audio"


def test_extract_incoming_pdf_keeps_filename_and_reply_context() -> None:
    incoming = extract_incoming_message(
        _payload(
            {
                "from": "96170123456",
                "id": "wamid.pdf",
                "timestamp": "125",
                "type": "document",
                "context": {"id": "wamid.original"},
                "document": {
                    "id": "media-pdf-1",
                    "mime_type": "application/pdf",
                    "sha256": "pdf123",
                    "filename": "invoice.pdf",
                    "caption": "Invoice copy",
                },
            }
        )
    )
    assert incoming is not None
    assert incoming.message_type == "document"
    assert incoming.media_id == "media-pdf-1"
    assert incoming.media_mime_type == "application/pdf"
    assert incoming.media_filename == "invoice.pdf"
    assert incoming.media_caption == "Invoice copy"
    assert incoming.context_message_id == "wamid.original"
    assert media_kind_for_mime(incoming.media_mime_type) == "document"


def test_extract_incoming_location_keeps_map_fields_and_reply_context() -> None:
    incoming = extract_incoming_message(
        _payload(
            {
                "from": "96170123456",
                "id": "wamid.location",
                "timestamp": "126",
                "type": "location",
                "context": {"id": "wamid.question"},
                "location": {
                    "latitude": 33.8938,
                    "longitude": 35.5018,
                    "name": "Beirut",
                    "address": "Beirut, Lebanon",
                },
            }
        )
    )
    assert incoming is not None
    assert incoming.message_type == "location"
    assert incoming.location_latitude == 33.8938
    assert incoming.location_longitude == 35.5018
    assert incoming.location_name == "Beirut"
    assert incoming.location_address == "Beirut, Lebanon"
    assert incoming.context_message_id == "wamid.question"


def test_extract_incoming_sticker_keeps_webp_media_metadata() -> None:
    incoming = extract_incoming_message(
        _payload(
            {
                "from": "96170123456",
                "id": "wamid.sticker",
                "timestamp": "127",
                "type": "sticker",
                "sticker": {
                    "id": "media-sticker-1",
                    "mime_type": "image/webp",
                    "sha256": "sticker123",
                    "animated": True,
                },
            }
        )
    )
    assert incoming is not None
    assert incoming.message_type == "sticker"
    assert incoming.media_id == "media-sticker-1"
    assert incoming.media_mime_type == "image/webp"
    assert media_kind_for_mime(incoming.media_mime_type) == "sticker"


def test_extract_incoming_reaction_targets_original_message_and_keeps_removal() -> None:
    incoming = extract_incoming_message(
        _payload(
            {
                "from": "96170123456",
                "id": "wamid.reaction-event",
                "timestamp": "128",
                "type": "reaction",
                "reaction": {
                    "message_id": "wamid.original",
                    "emoji": "",
                },
            }
        )
    )
    assert incoming is not None
    assert incoming.message_type == "reaction"
    assert incoming.reaction_message_id == "wamid.original"
    assert incoming.reaction_emoji == ""


def test_support_media_store_keeps_files_outside_public_ui(tmp_path: Path) -> None:
    original = config.settings.support_media_directory
    config.settings.support_media_directory = str(tmp_path / "support-media")
    try:
        store = SupportMediaStore()
        asset = store.save(
            ticket_id=12,
            message_id=34,
            content=b"fake-jpeg-payload",
            mime_type="image/jpeg",
            filename="proof.jpg",
        )
        reloaded = store.get(12, 34)
    finally:
        config.settings.support_media_directory = original

    assert asset.path.is_file()
    assert reloaded is not None
    assert reloaded.path.read_bytes() == b"fake-jpeg-payload"
    assert reloaded.mime_type == "image/jpeg"
    assert reloaded.filename == "proof.jpg"
    assert str(reloaded.path).startswith(str(tmp_path.resolve()))


def test_whatsapp_media_send_uses_image_caption_and_audio_without_caption() -> None:
    async def run() -> None:
        client = WhatsAppClient()
        seen: list[dict] = []

        async def fake_send(payload: dict, *, label: str) -> dict:
            seen.append({"payload": payload, "label": label})
            return {"messages": [{"id": "wamid.out"}]}

        client._send_payload = fake_send  # type: ignore[method-assign]
        await client.send_media(
            to="96170123456",
            media_type="image",
            media_id="media-1",
            caption="photo caption",
        )
        await client.send_media(
            to="96170123456",
            media_type="audio",
            media_id="media-2",
            caption="must not be sent",
            voice=True,
        )

        assert seen[0]["payload"]["type"] == "image"
        assert seen[0]["payload"]["image"] == {
            "id": "media-1",
            "caption": "photo caption",
        }
        assert seen[1]["payload"]["type"] == "audio"
        assert seen[1]["payload"]["audio"] == {"id": "media-2", "voice": True}

    asyncio.run(run())


def test_whatsapp_sticker_and_reaction_send_use_native_payload_types() -> None:
    async def run() -> None:
        client = WhatsAppClient()
        seen: list[dict] = []

        async def fake_send(payload: dict, *, label: str, phone_number_id=None) -> dict:
            seen.append({"payload": payload, "label": label, "phone_number_id": phone_number_id})
            return {"messages": [{"id": "wamid.out"}]}

        client._send_payload = fake_send  # type: ignore[method-assign]
        await client.send_media(
            to="96170123456",
            media_type="sticker",
            media_id="media-sticker",
        )
        await client.send_reaction(
            to="96170123456",
            message_id="wamid.original",
            emoji="❤️",
            phone_number_id="line-1",
        )

        assert seen[0]["payload"] == {
            "messaging_product": "whatsapp",
            "to": "96170123456",
            "type": "sticker",
            "sticker": {"id": "media-sticker"},
        }
        assert seen[1]["payload"]["type"] == "reaction"
        assert seen[1]["payload"]["reaction"] == {
            "message_id": "wamid.original",
            "emoji": "❤️",
        }

    asyncio.run(run())


def test_support_dashboard_contains_image_voice_and_inline_media_controls() -> None:
    assert 'id="imageBtn"' in _HTML
    assert 'id="voiceBtn"' in _HTML
    assert 'id="recordingBar"' in _HTML
    assert "new FormData()" in _HTML
    assert "m.media_url" in _HTML
    assert "document.createElement('audio')" in _HTML
    assert "document.createElement('img')" in _HTML
    assert 'image/webp' in _HTML
    assert "messageReactionBar" in _HTML
    assert "sendMessageReaction" in _HTML
    assert "focusMessageComposer" in _HTML


def test_support_media_store_persists_remote_reference_for_recovery(tmp_path: Path) -> None:
    original = config.settings.support_media_directory
    config.settings.support_media_directory = str(tmp_path / "support-media")
    try:
        store = SupportMediaStore()
        saved = store.save_remote_reference(
            ticket_id=9,
            message_id=10,
            media_id="meta-media-123",
            message_type="audio",
            expected_mime_type="audio/ogg; codecs=opus",
            business_phone_number_id="phone-number-id",
        )
        loaded = store.get_remote_reference(9, 10)
    finally:
        config.settings.support_media_directory = original

    assert saved.media_id == "meta-media-123"
    assert loaded is not None
    assert loaded.media_id == "meta-media-123"
    assert loaded.message_type == "audio"
    assert loaded.expected_mime_type == "audio/ogg; codecs=opus"
    assert loaded.business_phone_number_id == "phone-number-id"


def test_native_ogg_opus_voice_note_does_not_require_ffmpeg(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        original = config.settings.support_media_directory
        config.settings.support_media_directory = str(tmp_path / "support-media")
        try:
            store = SupportMediaStore()

            async def must_not_run(*_args, **_kwargs):
                raise AssertionError("ffmpeg should not run for native Ogg/Opus")

            monkeypatch.setattr(asyncio, "create_subprocess_exec", must_not_run)
            content, mime_type, filename, send_as_voice = await store.prepare_voice_note(
                b"OggS" + b"\x00" * 64,
                "audio/ogg; codecs=opus",
            )
        finally:
            config.settings.support_media_directory = original

        assert content.startswith(b"OggS")
        assert mime_type == "audio/ogg; codecs=opus"
        assert filename == "voice-note.ogg"
        assert send_as_voice is True

    asyncio.run(run())


def test_supported_mp4_audio_falls_back_when_ffmpeg_is_missing(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        original = config.settings.support_media_directory
        config.settings.support_media_directory = str(tmp_path / "support-media")
        try:
            store = SupportMediaStore()

            async def missing_ffmpeg(*_args, **_kwargs):
                raise FileNotFoundError("ffmpeg")

            monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_ffmpeg)
            content, mime_type, filename, send_as_voice = await store.prepare_voice_note(
                b"fake-mp4-audio",
                "audio/mp4",
            )
        finally:
            config.settings.support_media_directory = original

        assert content == b"fake-mp4-audio"
        assert mime_type == "audio/mp4"
        assert filename.endswith(".m4a")
        assert send_as_voice is False

    asyncio.run(run())

def test_whatsapp_media_upload_uses_base_mime_and_documented_form(monkeypatch) -> None:
    async def run() -> None:
        seen: dict = {}

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"id": "uploaded-media-id"}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs) -> None:
                seen["client_kwargs"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb) -> None:
                return None

            async def post(self, url, **kwargs):
                seen["url"] = url
                seen["post_kwargs"] = kwargs
                return FakeResponse()

        import app.whatsapp_client as whatsapp_module

        monkeypatch.setattr(whatsapp_module.settings, "whatsapp_phone_number_id", "123456789012345")
        monkeypatch.setattr(whatsapp_module.httpx, "AsyncClient", FakeAsyncClient)
        client = WhatsAppClient()
        media_id = await client.upload_media(
            content=b"OggSfake-opus",
            mime_type="audio/ogg; codecs=opus",
            filename="voice-note.ogg",
        )

        assert media_id == "uploaded-media-id"
        assert seen["post_kwargs"]["data"] == {"messaging_product": "whatsapp"}
        uploaded = seen["post_kwargs"]["files"]["file"]
        assert uploaded[0] == "voice-note.ogg"
        assert uploaded[1] == b"OggSfake-opus"
        assert uploaded[2] == "audio/ogg"

    asyncio.run(run())


def test_support_ui_has_whatsapp_reply_location_pdf_viewer_without_image_editor() -> None:
    html = _HTML
    assert 'id="replyDraft"' in html
    assert 'icon-reply' in html
    assert "reply_to_message_id" in html
    assert 'id="mediaViewerModal"' in html
    assert 'View PDF' in html
    assert 'application/pdf' in html
    assert 'id="annotationModal"' not in html
    assert 'id="annotationCanvas"' not in html
    assert 'Use edited image' not in html
    assert 'id="editMediaBtn"' in html
    assert "kind === 'location'" in html
    assert 'Open map' in html
    assert "window.open(m.media_url" not in html


def test_support_ui_simplifies_customer_context_and_notifies_unassigned() -> None:
    html = _HTML
    assert "['Language', ticket.current_language" not in html
    assert "['Communication style', ticket.communication_style" not in html
    assert "if (shipment)" in html
    assert 'function pollUnassignedActivity()' in html
    assert 'playNewConversationSound()' in html
    assert 'New unassigned customer conversation.' in html
