"""Bounded media preparation; cached annotations are untrusted customer context."""
from __future__ import annotations

import asyncio
import base64
import math
from dataclasses import dataclass

from app.agent.business_data import BusinessDataError
from app.agent.context import _BARE_SECRET
from app.ai_provider import ai_provider_service
from app.ai_usage import ai_usage_service
from app.client_chat_auth import redact_sensitive_text
from app.config import settings
from app.support_media import normalize_mime_type, support_media_store


@dataclass(frozen=True)
class PreparedMedia:
    message_id: int
    image_url: str | None = None


async def audio_duration(path) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-protocol_whitelist", "file",
        "-format_whitelist", "ogg,mp3,mov,wav,matroska,flac",
        "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
        str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    seconds = float(output.strip())
    if process.returncode or not math.isfinite(seconds) or not 0 < seconds <= settings.ai_agent_audio_max_seconds:
        raise BusinessDataError("AI_MEDIA_UNAVAILABLE")
    return math.ceil(seconds)


async def prepare_media(ticket: dict, source_message_id: str | None, repository) -> PreparedMedia:
    row = await repository.get_message_by_whatsapp_id(source_message_id)
    if (not row or int(row["ticket_id"]) != int(ticket["id"]) or row.get("deleted_for_everyone_at")
            or row.get("sender_type") != "CUSTOMER" or row.get("direction") != "INBOUND"
            or row.get("message_type") not in {"image", "audio"}):
        raise BusinessDataError("AI_MEDIA_UNAVAILABLE")
    result = PreparedMedia(int(row["id"]))
    if row.get("ai_media_processed_at") and row.get("ai_media_text"):
        return result
    asset = support_media_store.get(int(ticket["id"]), result.message_id)
    if asset is None or not 0 < asset.size_bytes <= settings.ai_agent_media_max_bytes:
        raise BusinessDataError("AI_MEDIA_UNAVAILABLE")
    content = await asyncio.to_thread(asset.path.read_bytes)
    if len(content) != asset.size_bytes or len(content) > settings.ai_agent_media_max_bytes:
        raise BusinessDataError("AI_MEDIA_UNAVAILABLE")
    mime = normalize_mime_type(asset.mime_type)
    if row["message_type"] == "image":
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise BusinessDataError("AI_MEDIA_UNAVAILABLE")
        return PreparedMedia(result.message_id, f"data:{mime};base64," + base64.b64encode(content).decode("ascii"))
    suffixes = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/wav": "wav", "audio/webm": "webm", "audio/flac": "flac"}
    if mime not in suffixes:
        raise BusinessDataError("AI_MEDIA_UNAVAILABLE")
    duration = await audio_duration(asset.path)
    client = (await ai_provider_service.get_client("transcription")).with_options(max_retries=0)

    async def call():
        try:
            return await client.audio.transcriptions.create(
                model=settings.ai_agent_transcription_model,
                file=("audio." + suffixes[mime], content, mime), response_format="json",
            )
        except Exception as exc:
            await ai_provider_service.report_failure(exc)
            raise

    response = await ai_usage_service.metered(
        purpose="transcription", tier="primary", model=settings.ai_agent_transcription_model,
        modalities="audio", audio_seconds=duration, call=call,
    )
    raw = str(getattr(response, "text", "") or "").strip()
    if not raw or len(raw) > 8000:
        raise BusinessDataError("AI_MEDIA_UNAVAILABLE")
    transcript = "[credential withheld]" if _BARE_SECRET.fullmatch(raw) else redact_sensitive_text(raw)
    if not await repository.save_ai_media_annotation(int(ticket["id"]), result.message_id,
                                                       kind="transcript", text=transcript):
        raise BusinessDataError("AI_MEDIA_UNAVAILABLE")
    return result


async def save_image_notes(ticket: dict, media: PreparedMedia, text: str, repository) -> None:
    notes = redact_sensitive_text(text).strip()
    if not notes or not await repository.save_ai_media_annotation(
        int(ticket["id"]), media.message_id, kind="image_notes", text=notes,
    ):
        raise BusinessDataError("AI_MEDIA_UNAVAILABLE")
