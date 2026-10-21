from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import configured_linked_device_lines, linked_device_routing_configuration, settings


logger = logging.getLogger("shipment-bot")


class WhatsAppSendError(RuntimeError):
    """Outbound send failed. ``delivery_uncertain`` means do not retry blindly."""

    def __init__(self, message: str, *, delivery_uncertain: bool) -> None:
        super().__init__(message)
        self.delivery_uncertain = delivery_uncertain
        self.definitive_failure = not delivery_uncertain


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
    business_account_id: str | None = None
    business_phone_number_id: str | None = None
    business_display_phone_number: str | None = None
    media_id: str | None = None
    media_mime_type: str | None = None
    media_sha256: str | None = None
    media_caption: str | None = None
    media_filename: str | None = None
    is_voice: bool = False
    context_message_id: str | None = None
    location_latitude: float | None = None
    location_longitude: float | None = None
    location_name: str | None = None
    location_address: str | None = None
    reaction_message_id: str | None = None
    reaction_emoji: str | None = None


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

    async def edit_message(
        self,
        to: str,
        message_id: str,
        body: str,
        *,
        phone_number_id: str | None = None,
        operation_id: str | None = None,
    ) -> dict:
        raise WhatsAppSendError(
            "Editing delivered messages requires the linked-device WhatsApp connection.",
            delivery_uncertain=False,
        )

    async def delete_message(
        self,
        to: str,
        message_id: str,
        *,
        phone_number_id: str | None = None,
        operation_id: str | None = None,
    ) -> dict:
        raise WhatsAppSendError(
            "Deleting delivered messages for everyone requires the linked-device WhatsApp connection.",
            delivery_uncertain=False,
        )

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
        client_message_id: str | None = None,
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
                    delivery_uncertain=not isinstance(status, int) or status >= 500,
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

            if not self._message_id(data):
                raise WhatsAppSendError("WhatsApp returned no message ID.", delivery_uncertain=True)
            logger.info("WhatsApp %s sent: message_id=%s", label, self._message_id(data))
            return data

    async def send_text(
        self,
        to: str,
        body: str,
        *,
        phone_number_id: str | None = None,
        context_message_id: str | None = None,
        client_message_id: str | None = None,
    ) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        if context_message_id:
            payload["context"] = {"message_id": str(context_message_id)}
        send_options = {"client_message_id": client_message_id} if client_message_id is not None else {}
        if phone_number_id:
            return await self._send_payload(
                payload,
                label="text",
                phone_number_id=phone_number_id,
                **send_options,
            )
        return await self._send_payload(payload, label="text", **send_options)

    async def send_location(
        self,
        to: str,
        *,
        latitude: float,
        longitude: float,
        name: str | None = None,
        address: str | None = None,
        phone_number_id: str | None = None,
        context_message_id: str | None = None,
        client_message_id: str | None = None,
    ) -> dict:
        location: dict[str, object] = {
            "latitude": float(latitude),
            "longitude": float(longitude),
        }
        if name:
            location["name"] = str(name)
        if address:
            location["address"] = str(address)
        payload: dict[str, object] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "location",
            "location": location,
        }
        if context_message_id:
            payload["context"] = {"message_id": str(context_message_id)}
        send_options = {"client_message_id": client_message_id} if client_message_id is not None else {}
        return await self._send_payload(
            payload,
            label="location",
            phone_number_id=phone_number_id,
            **send_options,
        )

    async def send_media(
        self,
        *,
        to: str,
        media_type: str,
        media_id: str,
        caption: str | None = None,
        voice: bool = False,
        filename: str | None = None,
        phone_number_id: str | None = None,
        context_message_id: str | None = None,
        client_message_id: str | None = None,
    ) -> dict:
        if media_type not in {"image", "audio", "document", "sticker", "video"}:
            raise WhatsAppSendError("Unsupported WhatsApp media type.", delivery_uncertain=False)
        media_node: dict[str, str | bool] = {"id": media_id}
        if media_type in {"image", "document", "video"} and caption:
            media_node["caption"] = caption
        if media_type == "document" and filename:
            media_node["filename"] = filename
        if media_type == "audio" and voice:
            media_node["voice"] = True
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": media_type,
            media_type: media_node,
        }
        if context_message_id:
            payload["context"] = {"message_id": str(context_message_id)}
        send_options = {"client_message_id": client_message_id} if client_message_id is not None else {}
        if phone_number_id:
            return await self._send_payload(
                payload,
                label=media_type,
                phone_number_id=phone_number_id,
                **send_options,
            )
        return await self._send_payload(payload, label=media_type, **send_options)

    async def send_reaction(
        self,
        *,
        to: str,
        message_id: str,
        emoji: str,
        phone_number_id: str | None = None,
    ) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "reaction",
            "reaction": {
                "message_id": str(message_id),
                # Meta uses an empty string to remove the business reaction.
                "emoji": str(emoji),
            },
        }
        return await self._send_payload(
            payload,
            label="reaction",
            phone_number_id=phone_number_id,
        )

    async def upload_media(
        self,
        *,
        content: bytes,
        mime_type: str,
        filename: str,
        phone_number_id: str | None = None,
    ) -> str:
        timeout = max(10.0, float(settings.support_media_download_timeout_seconds))
        upload_mime_type = str(mime_type or "").split(";", 1)[0].strip().lower()
        if not upload_mime_type:
            raise WhatsAppMediaError("Media upload MIME type is missing.", retryable=False)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.post(
                    self._media_upload_url(phone_number_id),
                    headers=self._auth_headers,
                    data={"messaging_product": "whatsapp"},
                    files={"file": (filename, content, upload_mime_type)},
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


def get_transport(phone_number_id: str | None = None) -> str:
    lines, removed = linked_device_routing_configuration()
    if phone_number_id:
        if str(phone_number_id) in removed:
            return "removed"
        enabled = settings.whatsapp_linked_device_enabled or settings.whatsapp_transport == "linked_device"
        return "linked_device" if enabled and str(phone_number_id) in lines else "cloud"
    default_line = settings.whatsapp_phone_number_id if settings.whatsapp_transport == "cloud" else settings.whatsapp_linked_device_default_line_id or settings.whatsapp_phone_number_id
    if default_line in removed:
        return "removed"
    return settings.whatsapp_transport


def is_linked_device_message(message_id: str | None, phone_number_id: str | None = None) -> bool:
    """A provider reference must belong to its exact route, never a Cloud ID."""
    if not isinstance(message_id, str) or len(message_id) > 191:
        return False
    match = re.fullmatch(r"ld:([0-9]{5,32}):([A-Za-z0-9_.-]+)", message_id)
    return bool(match and (phone_number_id is None or match.group(1) == str(phone_number_id)))


def create_whatsapp_client():
    if get_transport() == "removed":
        raise WhatsAppSendError("The default WhatsApp line has been removed.", delivery_uncertain=False)
    if get_transport() == "linked_device":
        from app.linked_device_client import LinkedDeviceWhatsAppClient
        return LinkedDeviceWhatsAppClient()
    return WhatsAppClient()


# Neither client opens a session or performs network requests at import time.
from app.linked_device_client import LinkedDeviceWhatsAppClient

linked_device_client = LinkedDeviceWhatsAppClient()


class RoutedWhatsAppClient:
    """Choose the connection per business line, keeping existing callers stable."""

    def __init__(self) -> None:
        self._cloud = WhatsAppClient()

    def _route(self, phone_number_id: str | None, *references: str | None):
        selected = phone_number_id
        for reference in references:
            if not isinstance(reference, str) or not reference.startswith(("ld:", "ldmedia:")):
                continue
            parts = reference.split(":", 2)
            if len(parts) != 3 or not re.fullmatch(r"[0-9]{5,32}", parts[1]):
                raise WhatsAppSendError("Invalid linked-device provider reference.", delivery_uncertain=False)
            if selected and str(selected) != parts[1]:
                raise WhatsAppSendError("This provider reference belongs to a different WhatsApp line.", delivery_uncertain=False)
            selected = parts[1]
            if get_transport(selected) != "linked_device":
                raise WhatsAppSendError("The linked-device connection for this message is not enabled.", delivery_uncertain=False)
        transport = get_transport(selected)
        if transport == "removed":
            raise WhatsAppSendError("This WhatsApp line has been permanently removed.", delivery_uncertain=False)
        client = linked_device_client if transport == "linked_device" else self._cloud
        return client, selected

    async def send_text(self, to: str, body: str, *, phone_number_id: str | None = None, context_message_id: str | None = None, client_message_id: str | None = None) -> dict:
        client, line = self._route(phone_number_id, context_message_id)
        return await client.send_text(to, body, phone_number_id=line, context_message_id=context_message_id, client_message_id=client_message_id)

    async def send_location(
        self, to: str, *, latitude: float, longitude: float, name: str | None = None,
        address: str | None = None, phone_number_id: str | None = None, context_message_id: str | None = None,
        client_message_id: str | None = None,
    ) -> dict:
        client, line = self._route(phone_number_id, context_message_id)
        return await client.send_location(to, latitude=latitude, longitude=longitude, name=name, address=address, phone_number_id=line, context_message_id=context_message_id, client_message_id=client_message_id)

    async def send_media(
        self, *, to: str, media_type: str, media_id: str, caption: str | None = None,
        voice: bool = False, filename: str | None = None, phone_number_id: str | None = None,
        context_message_id: str | None = None, client_message_id: str | None = None,
    ) -> dict:
        client, line = self._route(phone_number_id, media_id, context_message_id)
        return await client.send_media(to=to, media_type=media_type, media_id=media_id, caption=caption, voice=voice, filename=filename, phone_number_id=line, context_message_id=context_message_id, client_message_id=client_message_id)

    async def send_reaction(self, *, to: str, message_id: str, emoji: str, phone_number_id: str | None = None) -> dict:
        client, line = self._route(phone_number_id, message_id)
        return await client.send_reaction(to=to, message_id=message_id, emoji=emoji, phone_number_id=line)

    async def edit_message(self, to: str, message_id: str, body: str, *, phone_number_id: str | None = None, operation_id: str | None = None) -> dict:
        client, line = self._route(phone_number_id, message_id)
        return await client.edit_message(to, message_id, body, phone_number_id=line, operation_id=operation_id)

    async def delete_message(self, to: str, message_id: str, *, phone_number_id: str | None = None, operation_id: str | None = None) -> dict:
        client, line = self._route(phone_number_id, message_id)
        return await client.delete_message(to, message_id, phone_number_id=line, operation_id=operation_id)

    async def upload_media(self, *, content: bytes, mime_type: str, filename: str, phone_number_id: str | None = None) -> str:
        try:
            client, line = self._route(phone_number_id)
        except WhatsAppSendError as exc:
            raise WhatsAppMediaError(str(exc), retryable=False) from exc
        return await client.upload_media(content=content, mime_type=mime_type, filename=filename, phone_number_id=line)

    async def download_media(self, media_id: str, *, phone_number_id: str | None = None) -> DownloadedWhatsAppMedia:
        try:
            client, line = self._route(phone_number_id, media_id)
        except WhatsAppSendError as exc:
            raise WhatsAppMediaError(str(exc), retryable=False) from exc
        return await client.download_media(media_id, phone_number_id=line)


whatsapp_client = RoutedWhatsAppClient()


def extract_incoming_message(payload: dict) -> IncomingWhatsAppMessage | None:
    """Parse the first inbound WhatsApp message, including media and reactions."""
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
        media_filename = None
        is_voice = False
        context_node = message.get("context") or {}
        context_message_id = (
            str(context_node.get("id")) if context_node.get("id") else None
        )
        location_latitude = None
        location_longitude = None
        location_name = None
        location_address = None
        reaction_message_id = None
        reaction_emoji = None

        if message_type == "text":
            text_node = message.get("text") or {}
            text = text_node.get("body")
            if text is not None:
                text = str(text)
        elif message_type in {"image", "audio", "document", "sticker", "video"}:
            media_node = message.get(message_type) or {}
            if media_node.get("id"):
                media_id = str(media_node.get("id"))
            if media_node.get("mime_type"):
                media_mime_type = str(media_node.get("mime_type"))
            if media_node.get("sha256"):
                media_sha256 = str(media_node.get("sha256"))
            if message_type in {"image", "document", "video"} and media_node.get("caption") is not None:
                media_caption = str(media_node.get("caption"))
            if message_type == "document" and media_node.get("filename") is not None:
                media_filename = str(media_node.get("filename"))
            if message_type == "audio":
                is_voice = bool(media_node.get("voice"))
        elif message_type == "reaction":
            reaction_node = message.get("reaction") or {}
            if reaction_node.get("message_id"):
                reaction_message_id = str(reaction_node.get("message_id"))
            if reaction_node.get("emoji") is not None:
                reaction_emoji = str(reaction_node.get("emoji"))
        elif message_type == "location":
            location_node = message.get("location") or {}
            try:
                location_latitude = float(location_node.get("latitude"))
                location_longitude = float(location_node.get("longitude"))
            except (TypeError, ValueError):
                location_latitude = None
                location_longitude = None
            if location_node.get("name") is not None:
                location_name = str(location_node.get("name"))
            if location_node.get("address") is not None:
                location_address = str(location_node.get("address"))

        return IncomingWhatsAppMessage(
            from_number=str(message["from"]),
            message_id=(str(message.get("id")) if message.get("id") else None),
            message_type=message_type,
            text=text,
            timestamp=(str(message.get("timestamp")) if message.get("timestamp") else None),
            business_account_id=(str(entry.get("id")) if entry.get("id") else None),
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
            media_filename=media_filename,
            is_voice=is_voice,
            context_message_id=context_message_id,
            location_latitude=location_latitude,
            location_longitude=location_longitude,
            location_name=location_name,
            location_address=location_address,
            reaction_message_id=reaction_message_id,
            reaction_emoji=reaction_emoji,
        )
    except (KeyError, IndexError, TypeError):
        return None
