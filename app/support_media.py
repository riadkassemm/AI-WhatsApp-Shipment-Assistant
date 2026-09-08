from __future__ import annotations

import asyncio
import json
import logging
import os
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


def normalize_mime_type(value: str | None) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def media_kind_for_mime(value: str | None, *, allow_browser_audio: bool = False) -> str | None:
    mime_type = normalize_mime_type(value)
    if mime_type in IMAGE_MIME_EXTENSIONS:
        return "image"
    audio_map = BROWSER_AUDIO_MIME_EXTENSIONS if allow_browser_audio else WHATSAPP_AUDIO_MIME_EXTENSIONS
    if mime_type in audio_map:
        return "audio"
    return None


def media_extension(value: str | None, *, allow_browser_audio: bool = False) -> str:
    mime_type = normalize_mime_type(value)
    if mime_type in IMAGE_MIME_EXTENSIONS:
        return IMAGE_MIME_EXTENSIONS[mime_type]
    audio_map = BROWSER_AUDIO_MIME_EXTENSIONS if allow_browser_audio else WHATSAPP_AUDIO_MIME_EXTENSIONS
    return audio_map.get(mime_type, ".bin")


def max_media_bytes(kind: str) -> int:
    if kind == "image":
        return max(1, int(settings.support_image_max_bytes))
    if kind == "audio":
        return max(1, int(settings.support_audio_max_bytes))
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

    def get(self, ticket_id: int, message_id: int) -> StoredSupportMedia | None:
        directory = self._message_dir(ticket_id, message_id)
        metadata_path = directory / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            media_name = str(metadata.get("stored_name") or "")
            mime_type = normalize_mime_type(metadata.get("mime_type"))
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
        normalized = normalize_mime_type(mime_type)
        kind = media_kind_for_mime(normalized, allow_browser_audio=allow_browser_audio)
        if kind is None:
            raise SupportMediaError("Unsupported image or audio format.")
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

        metadata: dict[str, Any] = {
            "mime_type": normalized,
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
            if path.is_file() and path.name not in {stored_name, "metadata.json"} and not path.name.startswith("."):
                try:
                    path.unlink()
                except OSError:
                    logger.warning("Could not remove stale support media file: %s", path)

        return StoredSupportMedia(
            ticket_id=int(ticket_id),
            message_id=int(message_id),
            path=media_path,
            mime_type=normalized,
            size_bytes=len(content),
            filename=display_name,
        )

    async def prepare_voice_note(
        self,
        content: bytes,
        mime_type: str,
    ) -> tuple[bytes, str, str]:
        """Return an Ogg/Opus voice note suitable for WhatsApp audio sending."""
        source_mime = normalize_mime_type(mime_type)
        if media_kind_for_mime(source_mime, allow_browser_audio=True) != "audio":
            raise SupportMediaError("Unsupported audio format.")
        if not content:
            raise SupportMediaError("The voice note is empty.")
        limit = max_media_bytes("audio")
        if len(content) > limit:
            raise SupportMediaError(
                f"The voice note is too large. Maximum size is {limit // (1024 * 1024)} MB."
            )

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
                raise SupportMediaError(
                    "Voice recording conversion requires ffmpeg on the server."
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
            return converted, "audio/ogg; codecs=opus", "voice-note.ogg"


support_media_store = SupportMediaStore()
