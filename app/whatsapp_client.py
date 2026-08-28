from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings


logger = logging.getLogger("shipment-bot")


class WhatsAppSendError(RuntimeError):
    """Outbound send failed. ``delivery_uncertain`` means do not retry blindly."""

    def __init__(self, message: str, *, delivery_uncertain: bool) -> None:
        super().__init__(message)
        self.delivery_uncertain = delivery_uncertain


@dataclass(frozen=True)
class IncomingWhatsAppMessage:
    from_number: str
    message_id: str | None
    message_type: str
    text: str | None
    timestamp: str | None


class WhatsAppClient:
    def __init__(self) -> None:
        self._base_url = (
            f"https://graph.facebook.com/{settings.whatsapp_api_version}"
            f"/{settings.whatsapp_phone_number_id}/messages"
        )
        self._headers = {
            "Authorization": f"Bearer {settings.whatsapp_token}",
            "Content-Type": "application/json",
        }

    async def send_text(self, to: str, body: str) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(self._base_url, headers=self._headers, json=payload)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # Meta returned an explicit rejection, so retrying a newly generated
                # response for this same inbound message is safe from duplication.
                status = exc.response.status_code if exc.response is not None else "unknown"
                raise WhatsAppSendError(
                    f"WhatsApp rejected the message with HTTP {status}.",
                    delivery_uncertain=False,
                ) from exc
            except httpx.RequestError as exc:
                # A timeout/reset can happen after Meta accepted the request. Treat the
                # outcome as uncertain and retain the outbound idempotency guard.
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

            # Avoid logging the whole Meta response in production; it may grow to
            # contain customer-related metadata. The message ID is enough.
            message_id = None
            messages = data.get("messages") if isinstance(data, dict) else None
            if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                message_id = messages[0].get("id")
            logger.info("WhatsApp text sent: message_id=%s", message_id)
            return data


whatsapp_client = WhatsAppClient()


def extract_incoming_message(payload: dict) -> IncomingWhatsAppMessage | None:
    """Parse the first inbound WhatsApp message.

    Unlike the original tuple parser, this keeps the stable Meta message ID and
    timestamp required for support idempotency. Non-text messages are returned
    with text=None so an active human ticket can record that unsupported media
    arrived; callers may continue ignoring them in AI mode.
    """
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]["value"]
        messages = change.get("messages")
        if not messages:
            return None
        message = messages[0]
        message_type = str(message.get("type") or "unknown")
        text = None
        if message_type == "text":
            text_node = message.get("text") or {}
            text = text_node.get("body")
            if text is not None:
                text = str(text)
        return IncomingWhatsAppMessage(
            from_number=str(message["from"]),
            message_id=(str(message.get("id")) if message.get("id") else None),
            message_type=message_type,
            text=text,
            timestamp=(str(message.get("timestamp")) if message.get("timestamp") else None),
        )
    except (KeyError, IndexError, TypeError):
        return None
