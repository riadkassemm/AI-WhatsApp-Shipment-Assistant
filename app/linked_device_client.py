"""Authenticated, bounded HTTP adapter for the local WhatsApp session bridge.

The bridge owns WhatsApp credentials and protocol state. This adapter retains the
Cloud client's payload/response shapes so ticket routing and the bot can use it.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import httpx

from app.config import Settings, configured_linked_device_lines, linked_device_routing_configuration, settings

if TYPE_CHECKING:
    from app.whatsapp_client import DownloadedWhatsAppMedia, WhatsAppSendError


def _send_error(message: str, *, uncertain: bool = False, retryable: bool = False, http_status: int | None = None) -> WhatsAppSendError:
    # Lazy imports let either transport module be imported first.
    from app.whatsapp_client import WhatsAppSendError
    error = WhatsAppSendError(message, delivery_uncertain=uncertain)
    error.retryable = retryable
    error.http_status = http_status
    return error


class LinkedDeviceWhatsAppClient:
    _JSON_MAX_BYTES = 1024 * 1024

    def _configuration(self) -> tuple[str, dict[str, str]]:
        secret = settings.whatsapp_linked_device_secret
        if len(secret) < 32 or any(not 33 <= ord(char) <= 126 for char in secret):
            raise _send_error("The linked-device bridge requires a shared secret of at least 32 characters.")
        raw_url = settings.whatsapp_linked_device_url.rstrip("/")
        if len(raw_url) > 2048 or any(ord(char) < 33 for char in raw_url):
            raise _send_error("The linked-device bridge URL is invalid.")
        try:
            parsed = urlsplit(raw_url)
            hostname = parsed.hostname
            # Force validation of malformed/out-of-range ports.
            parsed.port
        except ValueError as exc:
            raise _send_error("The linked-device bridge URL is invalid.") from exc
        try:
            is_loopback = bool(hostname and ipaddress.ip_address(hostname).is_loopback)
        except ValueError:
            is_loopback = hostname == "localhost"
        if (
            not hostname
            or parsed.scheme not in {"http", "https"}
            or (parsed.scheme == "http" and not is_loopback)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise _send_error("The linked-device bridge URL must be an HTTPS or loopback HTTP origin without credentials, path, query, or fragment.")
        try:
            lines = configured_linked_device_lines()
        except ValueError as exc:
            raise _send_error("The linked-device line configuration is invalid.") from exc
        return raw_url, lines

    def _phone_number_id(self, phone_number_id: str | None = None) -> str:
        _, lines = self._configuration()
        selected = str(phone_number_id or settings.whatsapp_linked_device_default_line_id or "").strip()
        if not selected and settings.whatsapp_phone_number_id in lines:
            selected = settings.whatsapp_phone_number_id
        if not selected and len(lines) == 1:
            selected = next(iter(lines))
        if selected not in lines:
            raise _send_error("This WhatsApp line is not configured for the linked-device bridge.")
        return selected

    @staticmethod
    def _message_reference(message_id: str, line: str) -> str:
        from app.whatsapp_client import is_linked_device_message
        if not is_linked_device_message(message_id, line):
            raise _send_error("This message was not sent through the selected linked-device line.")
        return quote(message_id, safe="")

    @staticmethod
    def _media_reference(media_id: str, line: str) -> str:
        if not isinstance(media_id, str) or not re.fullmatch(
            rf"ldmedia:{re.escape(line)}:[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}", media_id
        ):
            raise _send_error("This media does not belong to the selected linked-device line.")
        return quote(media_id, safe="")

    @staticmethod
    def _operation_id(operation_id: str | None) -> str:
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{8,128}", operation_id):
            raise _send_error("A stable operation ID is required for this message change.")
        return operation_id

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        max_bytes: int = _JSON_MAX_BYTES,
        mutating: bool = False,
        timeout_seconds: float | None = None,
    ) -> tuple[bytes, httpx.Headers]:
        base_url, _ = self._configuration()
        timeout = max(1.0, min(120.0, float(timeout_seconds if timeout_seconds is not None else settings.whatsapp_linked_device_timeout_seconds)))
        headers = {"Authorization": f"Bearer {settings.whatsapp_linked_device_secret}"}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            try:
                async with client.stream(method, f"{base_url}{path}", headers=headers, json=payload) as response:
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > max_bytes:
                            raise _send_error("The linked-device bridge response exceeded the size limit.", uncertain=mutating)
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    if not 200 <= response.status_code < 300:
                        uncertain = mutating and response.status_code >= 500
                        try:
                            failure = json.loads(content)
                        except (ValueError, UnicodeError):
                            failure = None
                        if isinstance(failure, dict) and isinstance(failure.get("delivery_uncertain"), bool):
                            uncertain = mutating and failure["delivery_uncertain"]
                        raise _send_error(
                            f"The linked-device bridge rejected the request with HTTP {response.status_code}.",
                            uncertain=uncertain,
                            retryable=response.status_code >= 500 or response.status_code == 429,
                            http_status=response.status_code,
                        )
                    return content, response.headers
            except httpx.RequestError as exc:
                raise _send_error(
                    "The linked-device bridge connection failed; the operation outcome is uncertain."
                    if mutating else "The linked-device bridge connection failed.",
                    uncertain=mutating,
                    retryable=True,
                ) from exc

    async def _json_request(self, method: str, path: str, *, payload: dict | None = None, mutating: bool = False, timeout_seconds: float | None = None) -> dict:
        if method == "POST" and payload is None:
            # Pairing endpoints accept an empty JSON object, not an empty body.
            payload = {}
        content, _ = await self._request(method, path, payload=payload, mutating=mutating, timeout_seconds=timeout_seconds)
        try:
            data = json.loads(content)
        except (ValueError, UnicodeError) as exc:
            raise _send_error("The linked-device bridge returned an invalid JSON response.", uncertain=mutating, retryable=True) from exc
        if not isinstance(data, dict):
            raise _send_error("The linked-device bridge returned an invalid response.", uncertain=mutating, retryable=True)
        return data

    async def _send_payload(self, payload: dict, *, label: str, phone_number_id: str | None = None, client_message_id: str | None = None) -> dict:
        line = self._phone_number_id(phone_number_id)
        if payload.get("context"):
            self._message_reference(payload["context"].get("message_id"), line)
        kind = payload.get("type")
        if kind == "reaction":
            self._message_reference(payload["reaction"].get("message_id"), line)
        elif kind in {"image", "audio", "document", "sticker", "video"}:
            self._media_reference(payload[kind].get("id"), line)
        if client_message_id is not None:
            payload = {**payload, "client_message_id": client_message_id}
        data = await self._json_request("POST", f"/v1/lines/{line}/messages", payload=payload, mutating=True)
        from app.whatsapp_client import WhatsAppClient, is_linked_device_message
        if not is_linked_device_message(WhatsAppClient._message_id(data), line):
            raise _send_error("The linked-device bridge did not confirm a valid message ID.", uncertain=True)
        return data

    async def send_text(self, to: str, body: str, *, phone_number_id: str | None = None, context_message_id: str | None = None, client_message_id: str | None = None) -> dict:
        from app.whatsapp_client import WhatsAppClient
        return await WhatsAppClient.send_text(self, to, body, phone_number_id=phone_number_id, context_message_id=context_message_id, client_message_id=client_message_id)

    async def send_location(
        self, to: str, *, latitude: float, longitude: float, name: str | None = None,
        address: str | None = None, phone_number_id: str | None = None, context_message_id: str | None = None,
        client_message_id: str | None = None,
    ) -> dict:
        from app.whatsapp_client import WhatsAppClient
        return await WhatsAppClient.send_location(self, to, latitude=latitude, longitude=longitude, name=name, address=address, phone_number_id=phone_number_id, context_message_id=context_message_id, client_message_id=client_message_id)

    async def send_media(
        self, *, to: str, media_type: str, media_id: str, caption: str | None = None,
        voice: bool = False, filename: str | None = None, phone_number_id: str | None = None,
        context_message_id: str | None = None, client_message_id: str | None = None,
    ) -> dict:
        from app.whatsapp_client import WhatsAppClient
        return await WhatsAppClient.send_media(self, to=to, media_type=media_type, media_id=media_id, caption=caption, voice=voice, filename=filename, phone_number_id=phone_number_id, context_message_id=context_message_id, client_message_id=client_message_id)

    async def send_reaction(self, *, to: str, message_id: str, emoji: str, phone_number_id: str | None = None) -> dict:
        from app.whatsapp_client import WhatsAppClient
        return await WhatsAppClient.send_reaction(self, to=to, message_id=message_id, emoji=emoji, phone_number_id=phone_number_id)

    async def _change_message(
        self, action: str, to: str, message_id: str, *, body: str | None = None,
        phone_number_id: str | None = None, operation_id: str | None = None,
    ) -> dict:
        line = self._phone_number_id(phone_number_id)
        reference = self._message_reference(message_id, line)
        payload: dict[str, Any] = {"to": to, "operation_id": self._operation_id(operation_id)}
        if body is not None:
            payload["body"] = body
        data = await self._json_request("POST", f"/v1/lines/{line}/messages/{reference}/{action}", payload=payload, mutating=True)
        operation_message_id = data.get("message_id")
        if (
            data.get("confirmed") is not True
            or data.get("id") != message_id
            or not isinstance(operation_message_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", operation_message_id)
        ):
            raise _send_error("WhatsApp has not confirmed this message change.", uncertain=True)
        return data

    async def edit_message(self, to: str, message_id: str, body: str, *, phone_number_id: str | None = None, operation_id: str | None = None) -> dict:
        return await self._change_message("edit", to, message_id, body=body, phone_number_id=phone_number_id, operation_id=operation_id)

    async def delete_message(self, to: str, message_id: str, *, phone_number_id: str | None = None, operation_id: str | None = None) -> dict:
        return await self._change_message("delete", to, message_id, phone_number_id=phone_number_id, operation_id=operation_id)

    @staticmethod
    def _media_limit() -> int:
        return max(int(settings.support_image_max_bytes), int(settings.support_audio_max_bytes), int(settings.support_document_max_bytes), int(settings.support_sticker_max_bytes), int(settings.support_video_max_bytes))

    async def upload_media(self, *, content: bytes, mime_type: str, filename: str, phone_number_id: str | None = None) -> str:
        from app.whatsapp_client import WhatsAppMediaError, WhatsAppSendError
        try:
            line = self._phone_number_id(phone_number_id)
            if not content or len(content) > self._media_limit() or not mime_type:
                raise WhatsAppMediaError("Linked-device media content or MIME type is invalid.", retryable=False)
            data = await self._json_request("POST", f"/v1/lines/{line}/media", payload={
                "content_base64": base64.b64encode(content).decode("ascii"),
                "mime_type": str(mime_type).split(";", 1)[0].strip().lower(),
                "filename": filename,
            })
            media_id = data.get("id")
            self._media_reference(media_id, line)
            return media_id
        except WhatsAppSendError as exc:
            # Uploads only store an asset; retrying never sends a customer message.
            raise WhatsAppMediaError(str(exc), retryable=bool(getattr(exc, "retryable", False))) from exc

    async def download_media(self, media_id: str, *, phone_number_id: str | None = None) -> DownloadedWhatsAppMedia:
        from app.whatsapp_client import DownloadedWhatsAppMedia, WhatsAppMediaError, WhatsAppSendError
        try:
            line = self._phone_number_id(phone_number_id)
            reference = self._media_reference(media_id, line)
            content, headers = await self._request("GET", f"/v1/lines/{line}/media/{reference}", max_bytes=self._media_limit())
            return DownloadedWhatsAppMedia(
                content=content,
                mime_type=headers.get("content-type", "application/octet-stream").split(";", 1)[0],
                file_size=len(content),
                sha256=base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
            )
        except WhatsAppSendError as exc:
            raise WhatsAppMediaError(str(exc), retryable=bool(getattr(exc, "retryable", False))) from exc

    async def get_status(self) -> dict:
        data = await self._json_request("GET", "/v1/status")
        _, configured = self._configuration()
        lines = data.get("lines")
        if not isinstance(lines, list) or any(not isinstance(line, dict) for line in lines):
            raise _send_error("The linked-device bridge returned an invalid line status.")
        allowed_lines = [line for line in lines if line.get("line_id") in configured]
        if any(line.get("status") not in {"disconnected", "connecting", "qr", "connected", "logged_out", "error"} for line in allowed_lines):
            raise _send_error("The linked-device bridge returned an invalid line status.")
        return {**data, "lines": allowed_lines}

    async def add_line(
        self, request_id: str, expected_phone: str = "", *, reserved_line_ids: list[str] | None = None,
    ) -> dict:
        """Register a dashboard line using Node's durable allocation ledger."""
        try:
            canonical_request = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise _send_error("A valid UUID request ID is required to add a WhatsApp line.") from exc
        if canonical_request != request_id:
            raise _send_error("A canonical UUID request ID is required to add a WhatsApp line.")
        if not isinstance(expected_phone, str):
            raise _send_error("The expected WhatsApp phone number is invalid.")
        try:
            Settings.validate_linked_device_lines({"90001": expected_phone})
        except (ValueError, TypeError) as exc:
            raise _send_error("The expected WhatsApp phone number is invalid.") from exc
        reserved = reserved_line_ids or []
        if not isinstance(reserved, list) or any(not isinstance(line, str) or not re.fullmatch(r"[0-9]{5,64}", line) for line in reserved):
            raise _send_error("Reserved WhatsApp line IDs must be numeric.")
        data = await self._json_request("POST", "/v1/lines", payload={
            "request_id": canonical_request,
            "expected_phone": re.sub(r"[^0-9]", "", expected_phone),
            "reserved_line_ids": reserved,
        }, mutating=True)
        if (
            not isinstance(data.get("line_id"), str)
            or not re.fullmatch(r"[0-9]{5,32}", data["line_id"])
            or data.get("status") not in {"disconnected", "connecting", "qr", "connected", "logged_out", "error"}
        ):
            raise _send_error("The linked-device bridge did not confirm a valid registered line.", uncertain=True)
        return data

    async def connect(self, phone_number_id: str) -> dict:
        line = self._phone_number_id(phone_number_id)
        return await self._json_request("POST", f"/v1/lines/{line}/connect")

    async def disconnect(self, phone_number_id: str) -> dict:
        line = self._phone_number_id(phone_number_id)
        return await self._json_request("POST", f"/v1/lines/{line}/disconnect")

    async def remove_line(self, phone_number_id: str) -> dict:
        """Unlink and erase a bridge registration, with safe tombstone retries."""
        if not isinstance(phone_number_id, str) or not re.fullmatch(r"[0-9]{5,32}", phone_number_id):
            raise _send_error("A numeric WhatsApp line ID is required for removal.")
        self._configuration()
        try:
            lines, removed = linked_device_routing_configuration()
        except ValueError as exc:
            raise _send_error("The linked-device line configuration is invalid.") from exc
        if phone_number_id not in lines and phone_number_id not in removed:
            raise _send_error("This WhatsApp line is not configured for the linked-device bridge.")
        data = await self._json_request("DELETE", f"/v1/lines/{phone_number_id}", mutating=True, timeout_seconds=75.0)
        if data.get("line_id") != phone_number_id or data.get("removed") is not True or data.get("unlinked") is not True:
            raise _send_error("The linked-device bridge has not confirmed complete line removal.", uncertain=True)
        return data
