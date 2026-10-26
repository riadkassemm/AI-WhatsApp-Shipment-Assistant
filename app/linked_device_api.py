from __future__ import annotations

import hmac
import asyncio
import json
import logging
import re
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import removed_linked_device_line_ids, settings
from app.customer_directory import normalize_phone
from app.support_auth import SupportAgent, get_current_agent, require_support_csrf
from app.support_media import SupportMediaError
from app.support_repository import SupportConflictError
from app.support_service import SupportValidationError, support_service
from app.whatsapp_registry import WhatsAppRegistryError, whatsapp_registry_service
from app.whatsapp_client import (
    WhatsAppSendError,
    configured_linked_device_lines,
    extract_incoming_message,
    get_transport,
    is_linked_device_message,
    linked_device_client,
)


router = APIRouter(tags=["whatsapp-linked-device"])
logger = logging.getLogger("shipment-bot")
_MAX_EVENT_BYTES = 128 * 1024
_PHONE = re.compile(r"^[0-9]{5,20}$")
# How long a receipt for a not-yet-saved message is retried before it is dropped.
_STATUS_RETRY_WINDOW_MS = 120_000
_REACTION_TARGET_RETRY_WINDOW_MS = 24 * 60 * 60 * 1000
_line_addition_lock = asyncio.Lock()


class AddLinkedDeviceLineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_phone: str = Field(default="", max_length=32)

    @field_validator("expected_phone")
    @classmethod
    def validate_expected_phone(cls, value: str) -> str:
        if not value.strip():
            return ""
        if not re.fullmatch(r"[+0-9\s().-]+", value):
            raise ValueError("Enter a phone number in international format.")
        normalized = normalize_phone(value)
        if not re.fullmatch(r"[0-9]{5,15}", normalized):
            raise ValueError("Enter a phone number in international format.")
        return normalized


class RemoveLinkedDeviceLineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    close_active_tickets: bool = Field(default=False, strict=True)


class LinkedDeviceEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    type: Literal["message", "message_snapshot", "message_tombstone", "outgoing_message", "outgoing_confirmation", "message_status", "message_updated", "message_deleted_for_everyone"]
    line_id: str = Field(pattern=r"^[0-9]{5,32}$")
    display_phone_number: str | None = Field(default=None, max_length=64)
    payload: dict | None = None
    message_id: str | None = Field(default=None, max_length=191)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=191)
    operation_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    body: str | None = Field(default=None, max_length=4096)
    timestamp: int | None = Field(default=None, gt=0)
    deleted_timestamp: int | None = Field(default=None, gt=0)
    original_timestamp_known: bool = Field(default=True, strict=True)
    from_me: bool | None = None
    sender_phone: str | None = Field(default=None, pattern=r"^[0-9]{5,20}$")
    status: Literal["delivered", "read", "played"] | None = None


def _require_manager(agent: SupportAgent) -> None:
    if not agent.is_supervisor:
        raise HTTPException(403, "Only Super Admins and Admins can manage WhatsApp connections.")


def _require_line(line_id: str) -> None:
    if line_id not in configured_linked_device_lines():
        raise HTTPException(404, "This WhatsApp linked device line is not configured.")


@router.get("/api/support/whatsapp-linked-device/status")
async def linked_device_status(agent: SupportAgent = Depends(get_current_agent)) -> dict:
    _require_manager(agent)
    configured = bool(
        len(settings.whatsapp_linked_device_secret) >= 32
    )
    if not configured:
        return {"configured": False, "lines": [], "default_transport": settings.whatsapp_transport}
    try:
        payload = await linked_device_client.get_status()
    except WhatsAppSendError as exc:
        raise HTTPException(503, str(exc)) from exc
    # The bridge may have sessions that were removed from this server's config.
    # Never expose their QR codes or allow actions on those accounts.
    allowed = configured_linked_device_lines()
    lines = []
    for item in payload.get("lines", []):
        if not isinstance(item, dict) or str(item.get("line_id")) not in allowed:
            continue
        line_id = str(item["line_id"])
        lines.append({
            "line_id": line_id,
            "display_phone_number": item.get("display_phone_number") or allowed[line_id],
            "status": item.get("status") or "disconnected",
            "is_paired": item.get("is_paired") is True,
            "qr_data_url": item.get("qr_data_url"),
            "error": item.get("error"),
            "active_transport": get_transport(line_id),
            "pending_callbacks": item.get("pending_callbacks", 0),
            "waiting_history_reactions": item.get("waiting_history_reactions", 0),
            "failed_callbacks": item.get("failed_callbacks", 0),
            "uncertain_sends": item.get("uncertain_sends", 0),
        })
    return {"configured": True, "lines": lines, "default_transport": settings.whatsapp_transport}


@router.post("/api/support/whatsapp-linked-device/lines")
async def add_linked_device_line(
    body: AddLinkedDeviceLineRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    """Allocate durably at the bridge, then prepare its QR without enabling sends."""
    _require_manager(agent)
    if len(settings.whatsapp_linked_device_secret) < 32:
        raise HTTPException(503, "WhatsApp linked device connections are not configured.")
    from app.whatsapp_registry import WhatsAppRegistryError, whatsapp_registry_service

    line_id = None
    try:
        # Node's durable UUID ledger is the cross-process authority. This lock
        # also keeps registry snapshots and registration ordered in this worker.
        async with _line_addition_lock:
            registry = await whatsapp_registry_service.list_payload(agent)
            reserved_ids = sorted({str(row["phone_number_id"]) for row in registry.get("numbers", []) if row.get("phone_number_id")})
            registration = await linked_device_client.add_line(str(body.request_id), body.expected_phone, reserved_line_ids=reserved_ids)
            line_id = str(registration.get("line_id") or "")
            if not re.fullmatch(r"[0-9]{5,32}", line_id) or int(line_id) < 90001:
                raise WhatsAppSendError("The linked-device bridge returned an invalid new line.", delivery_uncertain=True)
            _require_line(line_id)
            # The shared manifest now contains this account. Registry seeding
            # creates its disabled row; existing enabled accounts retain state.
            registry = await whatsapp_registry_service.list_payload(agent)
            if registration.get("created") is True:
                try:
                    await whatsapp_registry_service._audit(
                        action="LINKED_DEVICE_LINE_CREATED", actor_staff_id=int(agent.id),
                        phone_number_id=line_id,
                        details={"request_id": str(body.request_id)},
                    )
                except Exception:
                    logger.exception("Linked-device number was registered but its audit could not be saved: line_id=%s", line_id)
        await linked_device_client.connect(line_id)
        result = await linked_device_status(agent)
        return {**result, "added_line_id": line_id, "registry": registry}
    except WhatsAppRegistryError as exc:
        raise HTTPException(503, str(exc)) from exc
    except WhatsAppSendError as exc:
        if line_id:
            raise HTTPException(503, "The number was added, but pairing could not finish. Refresh connections and use Pair to retry.") from exc
        raise HTTPException(503, str(exc)) from exc


@router.post("/api/support/whatsapp-linked-device/lines/{line_id}/connect")
async def connect_linked_device(
    line_id: str, agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    _require_manager(agent)
    _require_line(line_id)
    try:
        await linked_device_client.connect(line_id)
        return await linked_device_status(agent)
    except WhatsAppSendError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/api/support/whatsapp-linked-device/lines/{line_id}/disconnect")
async def disconnect_linked_device(
    line_id: str, agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    _require_manager(agent)
    _require_line(line_id)
    # A logout breaks sending on this line. Require its removal from routing first.
    from app.whatsapp_registry import whatsapp_registry_service

    if await whatsapp_registry_service.is_number_enabled(line_id):
        raise HTTPException(409, "Remove this line from the app before unlinking it.")
    try:
        await linked_device_client.disconnect(line_id)
        return await linked_device_status(agent)
    except WhatsAppSendError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.delete("/api/support/whatsapp-linked-device/lines/{line_id}")
async def remove_linked_device_line(
    line_id: str, body: RemoveLinkedDeviceLineRequest,
    agent: SupportAgent = Depends(require_support_csrf),
) -> dict:
    _require_manager(agent)
    if line_id not in configured_linked_device_lines() and line_id not in removed_linked_device_line_ids():
        raise HTTPException(404, "This WhatsApp linked-device number is not registered.")
    from app.whatsapp_registry import (
        WhatsAppRegistryConflictError, WhatsAppRegistryError,
        WhatsAppRegistryValidationError, whatsapp_registry_service,
    )

    try:
        # This lock serializes in-worker management snapshots; DB transactions
        # and the bridge's durable removal ledger also protect other workers.
        async with _line_addition_lock:
            await whatsapp_registry_service.prepare_linked_device_removal(
                agent, line_id, close_active_tickets=body.close_active_tickets,
            )
            try:
                result = await linked_device_client.remove_line(line_id)
            except WhatsAppSendError as exc:
                raise HTTPException(503, "The number is inactive in the app, but WhatsApp unlinking did not finish. Its conversations are retained. Retry Remove and unlink, or add it back to the app if it is still connected.") from exc
            if not isinstance(result, dict) or str(result.get("line_id")) != line_id or result.get("removed") is not True or result.get("unlinked") is not True:
                raise HTTPException(503, "WhatsApp did not confirm unlinking. The number remains inactive and its conversations are retained. Retry Remove and unlink.")
            try:
                closed = await whatsapp_registry_service.finish_linked_device_removal(
                    agent, line_id, close_active_tickets=body.close_active_tickets,
                )
            except (WhatsAppRegistryConflictError, WhatsAppRegistryValidationError):
                raise
            except Exception as exc:
                logger.exception("WhatsApp unlinked but conversation archival failed: line_id=%s", line_id)
                raise HTTPException(503, "WhatsApp was unlinked, but closing its conversations could not finish. Their history is retained. Retry Remove and unlink to complete removal.") from exc
            registry = await whatsapp_registry_service.list_payload(agent)
        # Successful native removal must not be reported as failure merely
        # because a subsequent status fetch races a bridge restart.
        try:
            status = await linked_device_status(agent)
        except HTTPException:
            status = {"configured": True, "lines": [], "default_transport": settings.whatsapp_transport, "status_unavailable": True}
        return {**status, "removed_line_id": line_id, "unlinked": True, "closed_conversations": closed, "registry": registry}
    except WhatsAppRegistryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except WhatsAppRegistryValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except WhatsAppRegistryError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/api/whatsapp/linked-device/events")
async def receive_linked_device_event(request: Request):
    """Authenticated bridge callbacks; QR sessions never receive staff privileges."""
    secret = settings.whatsapp_linked_device_secret
    if len(secret) < 32:
        raise HTTPException(503, "WhatsApp linked device callbacks are not configured.")
    authorization = request.headers.get("authorization", "")
    if not hmac.compare_digest(authorization.encode("utf-8"), ("Bearer " + secret).encode("utf-8")):
        raise HTTPException(403, "Invalid linked device callback credentials.")
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > _MAX_EVENT_BYTES:
            raise HTTPException(413, "Linked device event is too large.")
    try:
        event = LinkedDeviceEvent.model_validate(json.loads(chunks))
    except (ValueError, ValidationError) as exc:
        raise HTTPException(400, "Invalid linked device event.") from exc
    if event.line_id in removed_linked_device_line_ids():
        return {"status": "ignored", "reason": "removed_line"}
    _require_line(event.line_id)
    if get_transport(event.line_id) != "linked_device":
        return {"status": "ignored", "reason": "inactive_transport"}

    if event.type == "message":
        incoming = extract_incoming_message(event.payload or {})
        if (
            not incoming
            or incoming.business_phone_number_id != event.line_id
            or not is_linked_device_message(incoming.message_id, event.line_id)
            or not _PHONE.fullmatch(incoming.from_number)
            or event.from_me is True
        ):
            raise HTTPException(400, "Invalid linked device inbound message.")
        # Import lazily to keep this router independent of FastAPI startup order.
        from app.main import process_whatsapp_payload

        return await process_whatsapp_payload(event.payload or {}, source="linked_device")

    if event.type == "message_tombstone":
        if (
            event.from_me is None or not event.timestamp
            or not event.sender_phone or not _PHONE.fullmatch(event.sender_phone)
            or not is_linked_device_message(event.message_id, event.line_id)
            or event.payload is not None or event.body is not None
        ):
            raise HTTPException(400, "Invalid linked device tombstone.")
        try:
            line_enabled = await whatsapp_registry_service.observe_inbound(
                waba_id=None, phone_number_id=event.line_id,
                display_phone_number=event.display_phone_number,
            )
        except WhatsAppRegistryError as exc:
            raise HTTPException(503, "WhatsApp number registry is unavailable.") from exc
        if not line_enabled:
            return {"status": "ignored", "reason": "whatsapp_number_disabled"}
        try:
            await support_service.sync_linked_device_tombstone(event.model_dump())
        except SupportValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except SupportConflictError as exc:
            raise HTTPException(503, "The deleted message could not be synchronized yet. Retry this event.") from exc
        return {"status": "applied"}

    if event.type in {"outgoing_message", "message_snapshot"}:
        from_me = event.type == "outgoing_message"
        incoming = extract_incoming_message(event.payload or {})
        try:
            payload_timestamp = int(incoming.timestamp) if incoming else 0
        except (TypeError, ValueError):
            payload_timestamp = 0
        if (
            not incoming or event.from_me is not from_me or not event.timestamp
            or not event.sender_phone or not _PHONE.fullmatch(event.sender_phone)
            or incoming.business_phone_number_id != event.line_id
            or incoming.message_id != event.message_id
            or not is_linked_device_message(incoming.message_id, event.line_id)
            or incoming.from_number != event.sender_phone
            or payload_timestamp <= 0
            or abs(event.timestamp - payload_timestamp * 1000) >= 1000
        ):
            raise HTTPException(400, "Invalid linked device transcript message.")
        try:
            line_enabled = await whatsapp_registry_service.observe_inbound(
                waba_id=None, phone_number_id=event.line_id,
                display_phone_number=incoming.business_display_phone_number,
            )
        except WhatsAppRegistryError as exc:
            raise HTTPException(503, "WhatsApp number registry is unavailable.") from exc
        if not line_enabled:
            return {"status": "ignored", "reason": "whatsapp_number_disabled"}
        try:
            applied = await (support_service.sync_linked_device_outbound(incoming) if from_me
                             else support_service.sync_linked_device_snapshot(incoming))
        except SupportValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (SupportConflictError, SupportMediaError) as exc:
            raise HTTPException(503, "The message could not be synchronized yet. Retry this event.") from exc
        if not applied:
            # A phone can react to a pre-pairing or chatbot-only message that
            # never belonged to support. Keep a day for delayed target recovery;
            # do not report this non-message update as pending forever.
            if incoming.message_type == "reaction" and time.time() * 1000 - event.timestamp >= _REACTION_TARGET_RETRY_WINDOW_MS:
                return {"status": "ignored", "reason": "reaction_target_not_in_support"}
            if incoming.message_type == "reaction":
                # Retain the event in the bridge's durable queue without
                # reporting this expected history wait as a blocked message.
                # Older bridges still safely retry the 503 response.
                raise HTTPException(
                    503, "Waiting for the original reaction target to sync.",
                    headers={"X-WhatsApp-Callback-State": "waiting_for_reaction_target"},
                )
            raise HTTPException(503, "The original message is not available yet. Retry this event.")
        return {"status": "applied"}

    if event.type == "outgoing_confirmation":
        if (
            event.from_me is not True or not event.client_message_id
            or not event.sender_phone or not _PHONE.fullmatch(event.sender_phone)
            or not event.timestamp
            or not is_linked_device_message(event.message_id, event.line_id)
        ):
            raise HTTPException(400, "Invalid linked device outgoing confirmation.")
        try:
            applied = await support_service.confirm_linked_device_outgoing(event.model_dump())
        except (SupportValidationError, SupportConflictError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if not applied:
            raise HTTPException(503, "The dashboard message is not available yet. Retry this event.")
        return {"status": "applied"}

    if event.type == "message_status":
        if (
            event.from_me is not True or event.status is None or not event.timestamp
            or not event.sender_phone or not _PHONE.fullmatch(event.sender_phone)
            or not is_linked_device_message(event.message_id, event.line_id)
        ):
            raise HTTPException(400, "Invalid linked device message status.")
        try:
            applied = await support_service.apply_delivery_status(
                whatsapp_message_id=str(event.message_id), status=event.status,
                timestamp_ms=event.timestamp, business_phone_number_id=event.line_id,
                phone_number=event.sender_phone,
            )
        except SupportValidationError as exc:
            raise HTTPException(400, str(exc)) from exc
        if applied:
            return {"status": "applied"}
        # The message may still be finishing its send, or its mirror callback
        # may still be queued. Retry briefly, then drop the receipt.
        if abs(time.time() * 1000 - event.timestamp) < _STATUS_RETRY_WINDOW_MS:
            raise HTTPException(503, "The message is not available yet. Retry this event.")
        return {"status": "ignored", "reason": "message_not_in_support"}

    if (
        not is_linked_device_message(event.message_id, event.line_id)
        or event.from_me is None
        or not event.sender_phone
        or not event.timestamp
        or (event.type == "message_updated" and event.body is None)
    ):
        raise HTTPException(400, "Invalid linked device message change.")
    try:
        applied = await support_service.apply_linked_device_change(event.model_dump())
    except SupportValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not applied:
        if event.type == "message_deleted_for_everyone" and event.from_me is False:
            # The bridge delivered a preceding content-free tombstone. If no
            # support conversation matched that original send time, there is
            # intentionally no row for this follow-up revoke to mutate.
            return {"status": "ignored", "reason": "message_not_in_support"}
        # A mutation may arrive before the original inbound queue or outbound
        # database commit completes. The bridge keeps this event and retries.
        raise HTTPException(503, "The original message is not available yet. Retry this event.")
    return {"status": "applied"}
