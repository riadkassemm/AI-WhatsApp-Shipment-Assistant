from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings


logger = logging.getLogger("shipment-bot")


IMAGE_MIME_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
}
STICKER_MIME_EXTENSIONS: dict[str, str] = {
    "image/webp": ".webp",
}
DOCUMENT_MIME_EXTENSIONS: dict[str, str] = {
    "application/pdf": ".pdf",
}
# WhatsApp delivers and accepts MP4 (H.264/AAC) and 3GPP videos.
VIDEO_MIME_EXTENSIONS: dict[str, str] = {
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
}

# Message types whose attachment is stored and served by the support dashboard.
SUPPORT_MEDIA_MESSAGE_TYPES: frozenset[str] = frozenset({"image", "audio", "document", "sticker", "video"})

# WhatsApp Cloud API accepts these common audio formats. Browser microphone
# recording may also arrive as WebM/Opus; that format is transcoded to Ogg/Opus
# before it is uploaded to WhatsApp.
WHATSAPP_AUDIO_MIME_EXTENSIONS: dict[str, str] = {
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
}
BROWSER_AUDIO_MIME_EXTENSIONS: dict[str, str] = {
    **WHATSAPP_AUDIO_MIME_EXTENSIONS,
    "audio/webm": ".webm",
    "video/webm": ".webm",
}


class SupportMediaError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class StoredSupportMedia:
    ticket_id: int
    message_id: int
    path: Path
    mime_type: str
    size_bytes: int
    filename: str


@dataclass(frozen=True)
class RemoteSupportMediaReference:
    ticket_id: int
    message_id: int
    media_id: str
    message_type: str
    expected_mime_type: str | None
    business_phone_number_id: str | None
    filename: str | None = None


def normalize_mime_type(value: str | None) -> str:
    normalized = str(value or "").split(";", 1)[0].strip().lower()
    aliases = {
        "image/jpg": "image/jpeg",
        "audio/mp3": "audio/mpeg",
        "audio/m4a": "audio/mp4",
        "audio/x-m4a": "audio/mp4",
        "audio/opus": "audio/ogg",
        "audio/x-opus+ogg": "audio/ogg",
    }
    return aliases.get(normalized, normalized)


def media_kind_for_mime(value: str | None, *, allow_browser_audio: bool = False) -> str | None:
    mime_type = normalize_mime_type(value)
    if mime_type in STICKER_MIME_EXTENSIONS:
        return "sticker"
    if mime_type in IMAGE_MIME_EXTENSIONS:
        return "image"
    if mime_type in DOCUMENT_MIME_EXTENSIONS:
        return "document"
    if mime_type in VIDEO_MIME_EXTENSIONS:
        return "video"
    audio_map = BROWSER_AUDIO_MIME_EXTENSIONS if allow_browser_audio else WHATSAPP_AUDIO_MIME_EXTENSIONS
    if mime_type in audio_map:
        return "audio"
    return None


def media_extension(value: str | None, *, allow_browser_audio: bool = False) -> str:
    mime_type = normalize_mime_type(value)
    if mime_type in STICKER_MIME_EXTENSIONS:
        return STICKER_MIME_EXTENSIONS[mime_type]
    if mime_type in IMAGE_MIME_EXTENSIONS:
        return IMAGE_MIME_EXTENSIONS[mime_type]
    if mime_type in DOCUMENT_MIME_EXTENSIONS:
        return DOCUMENT_MIME_EXTENSIONS[mime_type]
    if mime_type in VIDEO_MIME_EXTENSIONS:
        return VIDEO_MIME_EXTENSIONS[mime_type]
    audio_map = BROWSER_AUDIO_MIME_EXTENSIONS if allow_browser_audio else WHATSAPP_AUDIO_MIME_EXTENSIONS
    return audio_map.get(mime_type, ".bin")


def max_media_bytes(kind: str) -> int:
    if kind == "image":
        return max(1, int(settings.support_image_max_bytes))
    if kind == "sticker":
        return max(1, int(settings.support_sticker_max_bytes))
    if kind == "audio":
        return max(1, int(settings.support_audio_max_bytes))
    if kind == "document":
        return max(1, int(settings.support_document_max_bytes))
    if kind == "video":
        return max(1, int(settings.support_video_max_bytes))
    raise SupportMediaError("Unsupported media type.")


def _safe_filename(value: str | None, fallback: str) -> str:
    candidate = Path(str(value or "")).name.strip()
    if not candidate:
        return fallback
    # Keep metadata/UI display names small and free of control characters.
    candidate = "".join(ch for ch in candidate if ch >= " " and ch not in "\r\n\t")
    return candidate[:180] or fallback


class SupportMediaStore:
    def __init__(self) -> None:
        self._root = Path(settings.support_media_directory).expanduser().resolve()

    def _message_dir(self, ticket_id: int, message_id: int) -> Path:
        return self._root / str(int(ticket_id)) / str(int(message_id))

    def _metadata_path(self, ticket_id: int, message_id: int) -> Path:
        return self._message_dir(ticket_id, message_id) / "metadata.json"

    def _remote_reference_path(self, ticket_id: int, message_id: int) -> Path:
        return self._message_dir(ticket_id, message_id) / "remote.json"

    def save_remote_reference(
        self,
        *,
        ticket_id: int,
        message_id: int,
        media_id: str,
        message_type: str,
        expected_mime_type: str | None = None,
        business_phone_number_id: str | None = None,
        filename: str | None = None,
    ) -> RemoteSupportMediaReference:
        if message_type not in SUPPORT_MEDIA_MESSAGE_TYPES:
            raise SupportMediaError("Unsupported inbound media type.")
        normalized_media_id = str(media_id or "").strip()
        if not normalized_media_id or len(normalized_media_id) > 512:
            raise SupportMediaError("WhatsApp media ID is missing or invalid.")
        directory = self._message_dir(ticket_id, message_id)
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "media_id": normalized_media_id,
            "message_type": message_type,
            "expected_mime_type": (str(expected_mime_type).strip()[:191] if expected_mime_type else None),
            "business_phone_number_id": (str(business_phone_number_id).strip()[:191] if business_phone_number_id else None),
            "filename": _safe_filename(filename, "document.pdf") if filename else None,
        }
        target = self._remote_reference_path(ticket_id, message_id)
        tmp = directory / f".remote.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
        return RemoteSupportMediaReference(
            ticket_id=int(ticket_id),
            message_id=int(message_id),
            media_id=normalized_media_id,
            message_type=message_type,
            expected_mime_type=payload["expected_mime_type"],
            business_phone_number_id=payload["business_phone_number_id"],
            filename=payload["filename"],
        )

    def get_remote_reference(
        self, ticket_id: int, message_id: int
    ) -> RemoteSupportMediaReference | None:
        try:
            payload = json.loads(
                self._remote_reference_path(ticket_id, message_id).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        media_id = str(payload.get("media_id") or "").strip()
        message_type = str(payload.get("message_type") or "").strip()
        if not media_id or message_type not in SUPPORT_MEDIA_MESSAGE_TYPES:
            return None
        expected_mime_type = payload.get("expected_mime_type")
        business_phone_number_id = payload.get("business_phone_number_id")
        filename = payload.get("filename")
        return RemoteSupportMediaReference(
            ticket_id=int(ticket_id),
            message_id=int(message_id),
            media_id=media_id,
            message_type=message_type,
            expected_mime_type=(str(expected_mime_type) if expected_mime_type else None),
            business_phone_number_id=(str(business_phone_number_id) if business_phone_number_id else None),
            filename=_safe_filename(filename, "document.pdf") if filename else None,
        )

    def get(self, ticket_id: int, message_id: int) -> StoredSupportMedia | None:
        directory = self._message_dir(ticket_id, message_id)
        metadata_path = directory / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            media_name = str(metadata.get("stored_name") or "")
            raw_mime_type = str(metadata.get("mime_type") or "").strip()
            normalized_mime_type = normalize_mime_type(raw_mime_type)
            mime_type = raw_mime_type if normalized_mime_type else ""
            filename = _safe_filename(metadata.get("filename"), media_name or "media")
            size_bytes = int(metadata.get("size_bytes") or 0)
            media_path = (directory / media_name).resolve()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        try:
            media_path.relative_to(directory.resolve())
        except ValueError:
            return None
        if not media_name or not media_path.is_file() or not mime_type:
            return None
        actual_size = media_path.stat().st_size
        if size_bytes <= 0 or size_bytes != actual_size:
            size_bytes = actual_size
        return StoredSupportMedia(
            ticket_id=int(ticket_id),
            message_id=int(message_id),
            path=media_path,
            mime_type=mime_type,
            size_bytes=size_bytes,
            filename=filename,
        )

    def delete_ticket_media(self, ticket_id: int) -> None:
        """Remove locally cached support media for a deleted history ticket.

        Ticket media is stored below one directory named with the numeric ticket
        ID. Resolve the target and verify it remains under the configured media
        root before recursively deleting it so an unexpected path value can
        never escape the support-media directory.
        """
        ticket_directory = (self._root / str(int(ticket_id))).resolve()
        try:
            ticket_directory.relative_to(self._root)
        except ValueError as exc:
            raise SupportMediaError("Invalid support media ticket path.") from exc
        if ticket_directory.is_dir():
            shutil.rmtree(ticket_directory)

    def delete_message_media(self, ticket_id: int, message_id: int) -> None:
        """Purge the cached file and recovery reference after a remote revoke."""
        if int(ticket_id) <= 0 or int(message_id) <= 0:
            raise SupportMediaError("Invalid support media message path.")
        directory = self._message_dir(ticket_id, message_id).resolve()
        try:
            directory.relative_to(self._root)
        except ValueError as exc:
            raise SupportMediaError("Invalid support media message path.") from exc
        if directory.is_dir():
            shutil.rmtree(directory)

    def save(
        self,
        *,
        ticket_id: int,
        message_id: int,
        content: bytes,
        mime_type: str,
        filename: str | None = None,
        allow_browser_audio: bool = False,
    ) -> StoredSupportMedia:
        raw_mime_type = str(mime_type or "").strip()
        normalized = normalize_mime_type(raw_mime_type)
        kind = media_kind_for_mime(normalized, allow_browser_audio=allow_browser_audio)
        if kind is None:
            raise SupportMediaError("Unsupported support media format.")
        if not content:
            raise SupportMediaError("The media file is empty.")
        limit = max_media_bytes(kind)
        if len(content) > limit:
            raise SupportMediaError(
                f"The {kind} is too large. Maximum size is {limit // (1024 * 1024)} MB."
            )

        directory = self._message_dir(ticket_id, message_id)
        directory.mkdir(parents=True, exist_ok=True)
        extension = media_extension(normalized, allow_browser_audio=allow_browser_audio)
        stored_name = f"media{extension}"
        media_path = directory / stored_name
        fallback_name = f"{kind}{extension}"
        display_name = _safe_filename(filename, fallback_name)

        # Replace atomically so a dashboard request cannot observe a partially
        # written attachment while the webhook is still downloading it.
        tmp_path = directory / f".{stored_name}.{os.getpid()}.tmp"
        tmp_path.write_bytes(content)
        os.replace(tmp_path, media_path)

        served_mime_type = (
            "audio/ogg; codecs=opus"
            if normalized == "audio/ogg" and "opus" in raw_mime_type.lower()
            else normalized
        )
        metadata: dict[str, Any] = {
            "mime_type": served_mime_type,
            "size_bytes": len(content),
            "filename": display_name,
            "stored_name": stored_name,
        }
        metadata_tmp = directory / f".metadata.{os.getpid()}.tmp"
        metadata_tmp.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        os.replace(metadata_tmp, directory / "metadata.json")

        # Remove a previous file with a different extension after the new media
        # and metadata have both been committed.
        for path in directory.iterdir():
            if path.is_file() and path.name not in {stored_name, "metadata.json", "remote.json"} and not path.name.startswith("."):
                try:
                    path.unlink()
                except OSError:
                    logger.warning("Could not remove stale support media file: %s", path)

        return StoredSupportMedia(
            ticket_id=int(ticket_id),
            message_id=int(message_id),
            path=media_path,
            mime_type=served_mime_type,
            size_bytes=len(content),
            filename=display_name,
        )

    async def prepare_voice_note(
        self,
        content: bytes,
        mime_type: str,
    ) -> tuple[bytes, str, str, bool]:
        """Prepare browser audio for WhatsApp.

        Returns ``(content, mime_type, filename, send_as_voice)``. Native
        Ogg/Opus remains a WhatsApp voice note. If ffmpeg is unavailable but
        the browser already produced another WhatsApp-supported audio format,
        the recording can still be delivered as a regular audio attachment.
        """
        raw_mime = str(mime_type or "").strip().lower()
        source_mime = normalize_mime_type(raw_mime)
        if media_kind_for_mime(source_mime, allow_browser_audio=True) != "audio":
            raise SupportMediaError("Unsupported audio format.")
        if not content:
            raise SupportMediaError("The voice note is empty.")
        limit = max_media_bytes("audio")
        if len(content) > limit:
            raise SupportMediaError(
                f"The voice note is too large. Maximum size is {limit // (1024 * 1024)} MB."
            )

        # Firefox and some browsers can record Ogg/Opus directly. Avoid an
        # unnecessary ffmpeg dependency when the recording is already a native
        # WhatsApp voice-note format. The explicit codec marker is important:
        # arbitrary Ogg containers must still be normalized.
        if source_mime == "audio/ogg" and "opus" in raw_mime and content.startswith(b"OggS"):
            return content, "audio/ogg; codecs=opus", "voice-note.ogg", True

        ffmpeg_path = str(settings.support_media_ffmpeg_path or "ffmpeg").strip() or "ffmpeg"
        timeout_seconds = max(5.0, float(settings.support_media_transcode_timeout_seconds))

        with tempfile.TemporaryDirectory(prefix="support-voice-") as temp_dir:
            source = Path(temp_dir) / f"input{media_extension(source_mime, allow_browser_audio=True)}"
            output = Path(temp_dir) / "voice-note.ogg"
            source.write_bytes(content)
            try:
                process = await asyncio.create_subprocess_exec(
                    ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-map_metadata",
                    "-1",
                    "-c:a",
                    "libopus",
                    "-application",
                    "voip",
                    "-ac",
                    "1",
                    "-b:a",
                    "32k",
                    "-vbr",
                    "on",
                    "-f",
                    "ogg",
                    str(output),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                # MP4/AAC/AMR/MP3 are valid WhatsApp audio attachments even
                # though they are not native push-to-talk voice notes. Prefer a
                # successful human reply over blocking the agent when ffmpeg is
                # missing. WebM still requires conversion because WhatsApp does
                # not accept it.
                if source_mime in WHATSAPP_AUDIO_MIME_EXTENSIONS and source_mime != "audio/ogg":
                    return (
                        content,
                        source_mime,
                        f"audio{media_extension(source_mime)}",
                        False,
                    )
                raise SupportMediaError(
                    "This browser recorded WebM audio, which requires ffmpeg on the server before WhatsApp can send it."
                ) from exc

            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_seconds
                )
            except TimeoutError as exc:
                process.kill()
                await process.communicate()
                raise SupportMediaError("Voice note conversion timed out.") from exc

            if process.returncode != 0 or not output.is_file():
                detail = (stderr or b"").decode("utf-8", "replace").strip()[:300]
                logger.warning("Voice note conversion failed: %s", detail or process.returncode)
                raise SupportMediaError("Voice note could not be converted for WhatsApp.")

            converted = output.read_bytes()
            if not converted:
                raise SupportMediaError("Voice note conversion produced an empty file.")
            if len(converted) > limit:
                raise SupportMediaError(
                    f"The converted voice note is too large. Maximum size is {limit // (1024 * 1024)} MB."
                )
            return converted, "audio/ogg; codecs=opus", "voice-note.ogg", True


support_media_store = SupportMediaStore()
