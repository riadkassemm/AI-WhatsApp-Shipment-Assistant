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


def test_support_dashboard_contains_image_voice_and_inline_media_controls() -> None:
    assert 'id="imageBtn"' in _HTML
    assert 'id="voiceBtn"' in _HTML
    assert 'id="recordingBar"' in _HTML
    assert "new FormData()" in _HTML
    assert "m.media_url" in _HTML
    assert "document.createElement('audio')" in _HTML
    assert "document.createElement('img')" in _HTML
