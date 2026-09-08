from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings


logger = logging.getLogger("shipment-bot")


class WhatsAppSendError(RuntimeError):
    """Outbound send failed. ``delivery_uncertain`` means do not retry blindly."""

    def __init__(self, message: str, *, delivery_uncertain: bool) -> None:
        super().__init__(message)
        self.delivery_uncertain = delivery_uncertain


class WhatsAppMediaError(RuntimeError):
    """Media upload/download failed. ``retryable`` is safe for webhook retries."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class IncomingWhatsAppMessage:
    from_number: str
    message_id: str | None
    message_type: str
    text: str | None
    timestamp: str | None
    business_phone_number_id: str | None = None
    business_display_phone_number: str | None = None
    media_id: str | None = None
    media_mime_type: str | None = None
    media_sha256: str | None = None
    media_caption: str | None = None
    is_voice: bool = False


@dataclass(frozen=True)
class DownloadedWhatsAppMedia:
    content: bytes
    mime_type: str | None
    file_size: int | None
    sha256: str | None


class WhatsAppClient:
    def __init__(self) -> None:
        self._graph_base = f"https://graph.facebook.com/{settings.whatsapp_api_version}"
        self._auth_headers = {
            "Authorization": f"Bearer {settings.whatsapp_token}",
        }
        self._json_headers = {
            **self._auth_headers,
            "Content-Type": "application/json",
        }

    def _phone_number_id(self, phone_number_id: str | None = None) -> str:
        selected = str(phone_number_id or settings.whatsapp_phone_number_id or "").strip()
        if not selected:
            raise WhatsAppSendError(
                "WhatsApp phone-number ID is not configured.",
                delivery_uncertain=False,
            )
        return selected

    def _messages_url(self, phone_number_id: str | None = None) -> str:
        return f"{self._graph_base}/{self._phone_number_id(phone_number_id)}/messages"

    def _media_upload_url(self, phone_number_id: str | None = None) -> str:
        return f"{self._graph_base}/{self._phone_number_id(phone_number_id)}/media"

    @staticmethod
    def _message_id(data: Any) -> str | None:
        messages = data.get("messages") if isinstance(data, dict) else None
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            value = messages[0].get("id")
            return str(value) if value else None
        return None

    async def _send_payload(
        self,
        payload: dict,
        *,
        label: str,
        phone_number_id: str | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(
                    self._messages_url(phone_number_id),
                    headers=self._json_headers,
                    json=payload,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else "unknown"
                raise WhatsAppSendError(
                    f"WhatsApp rejected the message with HTTP {status}.",
                    delivery_uncertain=False,
                ) from exc
            except httpx.RequestError as exc:
                raise WhatsAppSendError(
                    "WhatsApp send result is uncertain because the provider connection failed.",
                    delivery_uncertain=True,
                ) from exc

            try:
                data = resp.json()
            except ValueError as exc:
                raise WhatsAppSendError(
                    "WhatsApp returned a successful but invalid response.",
                    delivery_uncertain=True,
                ) from exc

            logger.info("WhatsApp %s sent: message_id=%s", label, self._message_id(data))
            return data

    async def send_text(
        self,
        to: str,
        body: str,
        *,
        phone_number_id: str | None = None,
    ) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        if phone_number_id:
            return await self._send_payload(
                payload,
                label="text",
                phone_number_id=phone_number_id,
            )
        return await self._send_payload(payload, label="text")

    async def send_media(
        self,
        *,
        to: str,
        media_type: str,
        media_id: str,
        caption: str | None = None,
        voice: bool = False,
        phone_number_id: str | None = None,
    ) -> dict:
        if media_type not in {"image", "audio"}:
            raise WhatsAppSendError("Unsupported WhatsApp media type.", delivery_uncertain=False)
        media_node: dict[str, str] = {"id": media_id}
        if media_type == "image" and caption:
            media_node["caption"] = caption
        if media_type == "audio" and voice:
            media_node["voice"] = True
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": media_type,
            media_type: media_node,
        }
        if phone_number_id:
            return await self._send_payload(
                payload,
                label=media_type,
                phone_number_id=phone_number_id,
            )
        return await self._send_payload(payload, label=media_type)

    async def upload_media(
        self,
        *,
        content: bytes,
        mime_type: str,
        filename: str,
        phone_number_id: str | None = None,
    ) -> str:
        timeout = max(10.0, float(settings.support_media_download_timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.post(
                    self._media_upload_url(phone_number_id),
                    headers=self._auth_headers,
                    data={"messaging_product": "whatsapp", "type": mime_type},
                    files={"file": (filename, content, mime_type)},
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                raise WhatsAppMediaError(
                    f"WhatsApp rejected the media upload with HTTP {status or 'unknown'}.",
                    retryable=bool(status >= 500 or status == 429),
                ) from exc
            except httpx.RequestError as exc:
                raise WhatsAppMediaError(
                    "WhatsApp media upload failed because the provider connection failed.",
                    retryable=True,
                ) from exc
            try:
                data = resp.json()
            except ValueError as exc:
                raise WhatsAppMediaError(
                    "WhatsApp returned an invalid media-upload response.", retryable=True
                ) from exc
            media_id = data.get("id") if isinstance(data, dict) else None
            if not media_id:
                raise WhatsAppMediaError(
                    "WhatsApp did not return a media ID after upload.", retryable=True
                )
            logger.info("WhatsApp media uploaded: media_id=%s", media_id)
            return str(media_id)

    async def download_media(
        self,
        media_id: str,
        *,
        phone_number_id: str | None = None,
    ) -> DownloadedWhatsAppMedia:
        timeout = max(10.0, float(settings.support_media_download_timeout_seconds))
        metadata_url = f"{self._graph_base}/{media_id}"
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            try:
                metadata_response = await client.get(
                    metadata_url,
                    headers=self._auth_headers,
                    params={"phone_number_id": self._phone_number_id(phone_number_id)},
                )
                metadata_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                raise WhatsAppMediaError(
                    f"WhatsApp media metadata request failed with HTTP {status or 'unknown'}.",
                    retryable=bool(status >= 500 or status == 429),
                ) from exc
            except httpx.RequestError as exc:
                raise WhatsAppMediaError(
                    "WhatsApp media metadata request failed because the provider connection failed.",
                    retryable=True,
                ) from exc

            try:
                metadata = metadata_response.json()
            except ValueError as exc:
                raise WhatsAppMediaError(
                    "WhatsApp returned invalid media metadata.", retryable=True
                ) from exc
            media_url = metadata.get("url") if isinstance(metadata, dict) else None
            if not media_url:
                raise WhatsAppMediaError("WhatsApp media URL was missing.", retryable=True)

            try:
                media_response = await client.get(str(media_url), headers=self._auth_headers)
                media_response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                raise WhatsAppMediaError(
                    f"WhatsApp media download failed with HTTP {status or 'unknown'}.",
                    retryable=bool(status >= 500 or status == 429),
                ) from exc
            except httpx.RequestError as exc:
                raise WhatsAppMediaError(
                    "WhatsApp media download failed because the provider connection failed.",
                    retryable=True,
                ) from exc

            mime_type = metadata.get("mime_type") if isinstance(metadata, dict) else None
            if not mime_type:
                mime_type = media_response.headers.get("content-type")
            raw_size = metadata.get("file_size") if isinstance(metadata, dict) else None
            try:
                file_size = int(raw_size) if raw_size is not None else len(media_response.content)
            except (TypeError, ValueError):
                file_size = len(media_response.content)
            sha256 = metadata.get("sha256") if isinstance(metadata, dict) else None
            return DownloadedWhatsAppMedia(
                content=media_response.content,
                mime_type=str(mime_type) if mime_type else None,
                file_size=file_size,
                sha256=str(sha256) if sha256 else None,
            )


whatsapp_client = WhatsAppClient()


def extract_incoming_message(payload: dict) -> IncomingWhatsAppMessage | None:
    """Parse the first inbound WhatsApp message, including image/audio metadata."""
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]
        metadata = change.get("metadata") or {}
        messages = change.get("messages")
        if not messages:
            return None
        message = messages[0]
        message_type = str(message.get("type") or "unknown")
        text = None
        media_id = None
        media_mime_type = None
        media_sha256 = None
        media_caption = None
        is_voice = False

        if message_type == "text":
            text_node = message.get("text") or {}
            text = text_node.get("body")
            if text is not None:
                text = str(text)
        elif message_type in {"image", "audio"}:
            media_node = message.get(message_type) or {}
            if media_node.get("id"):
                media_id = str(media_node.get("id"))
            if media_node.get("mime_type"):
                media_mime_type = str(media_node.get("mime_type"))
            if media_node.get("sha256"):
                media_sha256 = str(media_node.get("sha256"))
            if message_type == "image" and media_node.get("caption") is not None:
                media_caption = str(media_node.get("caption"))
            if message_type == "audio":
                is_voice = bool(media_node.get("voice"))

        return IncomingWhatsAppMessage(
            from_number=str(message["from"]),
            message_id=(str(message.get("id")) if message.get("id") else None),
            message_type=message_type,
            text=text,
            timestamp=(str(message.get("timestamp")) if message.get("timestamp") else None),
            business_phone_number_id=(
                str(metadata.get("phone_number_id")) if metadata.get("phone_number_id") else None
            ),
            business_display_phone_number=(
                str(metadata.get("display_phone_number"))
                if metadata.get("display_phone_number")
                else None
            ),
            media_id=media_id,
            media_mime_type=media_mime_type,
            media_sha256=media_sha256,
            media_caption=media_caption,
            is_voice=is_voice,
        )
    except (KeyError, IndexError, TypeError):
        return None
