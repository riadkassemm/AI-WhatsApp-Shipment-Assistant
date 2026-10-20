from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.config import settings
from app.conversation_store import Conversation, conversation_store
from app.customer_directory import ContactConflictError, ContactNotFoundError, customer_directory, normalize_phone
from app.client_chat_auth import redact_sensitive_text
from app.handoff import notify_human_agents
from app.google_contacts import google_contacts_service
from app.shipment_client import shipment_client
from app.staff_repository import staff_repository
from app.support_auth import SupportAgent
from app.support_models import ALLOWED_TRANSITIONS, ACTIVE_TICKET_STATUSES, HandoffRequest
from app.support_media import (
    SUPPORT_MEDIA_MESSAGE_TYPES,
    SupportMediaError,
    StoredSupportMedia,
    max_media_bytes,
    media_extension,
    media_kind_for_mime,
    normalize_mime_type,
    support_media_store,
)
from app.support_events import publish_support_event
from app.support_repository import (
    DELIVERY_STATUS_RANK,
    SupportConflictError,
    SupportNotFoundError,
    support_repository,
)
from app.phone_numbers import PhoneNumberError, region_for_number, to_whatsapp_digits
from app.whatsapp_client import IncomingWhatsAppMessage, WhatsAppMediaError, get_transport, is_linked_device_message, linked_device_client, whatsapp_client
from app.whatsapp_registry import whatsapp_registry_service


logger = logging.getLogger("shipment-bot")

SUPPORTED_REACTION_EMOJIS: frozenset[str] = frozenset(
    {"👍", "❤️", "😂", "😮", "😢", "🙏"}
)


class SupportValidationError(Exception):
    pass


class SupportForbiddenError(Exception):
    pass


class SupportSendError(Exception):
    pass


_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(password|pass|passwd|pwd)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+\-/]+=*"),
    re.compile(r"(?i)\b(api[_ -]?key|access[_ -]?token|db[_ -]?password)\s*[:=]\s*\S+"),
]


def sanitize_support_text(value: str | None) -> str:
    text = redact_sensitive_text(str(value or ""))
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}: [REDACTED]", text)
    return text


def _normalize_staff_message_preference(value: str | None) -> str | None:
    """Validate a staff-authored automatic customer message.

    Blank means "send no automatic message". Newlines and tabs are
    allowed for normal message formatting, while other control characters are
    rejected before persistence or delivery.
    """
    rendered = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not rendered:
        return None
    if len(rendered) > 2000:
        raise SupportValidationError("Automatic support messages must be 2000 characters or fewer.")
    if any(ord(char) < 32 and char not in {"\n", "\t"} for char in rendered):
        raise SupportValidationError("Automatic support messages contain unsupported control characters.")
    return rendered


QUICK_REPLY_TITLE_MAX_LENGTH = 80
QUICK_REPLY_BODY_MAX_LENGTH = 4096


def _normalize_quick_reply(title: str | None, body: str | None) -> tuple[str | None, str]:
    """Validate one staff quick reply; a blank title is stored as NULL."""
    rendered_title = " ".join(str(title or "").split())
    rendered_body = str(body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not rendered_body:
        raise SupportValidationError("Enter the quick reply message.")
    if len(rendered_title) > QUICK_REPLY_TITLE_MAX_LENGTH:
        raise SupportValidationError(f"Quick reply titles must be {QUICK_REPLY_TITLE_MAX_LENGTH} characters or fewer.")
    if len(rendered_body) > QUICK_REPLY_BODY_MAX_LENGTH:
        raise SupportValidationError(f"Quick replies must be {QUICK_REPLY_BODY_MAX_LENGTH} characters or fewer.")
    if any(ord(char) < 32 for char in rendered_title) or any(
        ord(char) < 32 and char not in {"\n", "\t"} for char in rendered_body
    ):
        raise SupportValidationError("Quick replies contain unsupported control characters.")
    return rendered_title or None, rendered_body


def _quick_reply_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    normalized = _normalize_row(row) or {}
    return {
        "id": int(normalized.get("id") or 0),
        "title": str(normalized.get("title") or ""),
        "body": str(normalized.get("body") or ""),
        "created_at": normalized.get("created_at"),
        "updated_at": normalized.get("updated_at"),
    }


def _safe_context_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_support_text(value)
    if isinstance(value, list):
        return [_safe_context_value(item) for item in value]
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if any(secret in str(key).lower() for secret in ("password", "token", "secret", "credential")):
                safe[str(key)] = "[REDACTED]"
            else:
                safe[str(key)] = _safe_context_value(item)
        return safe
    return value


def _conversation_context(conversation: Conversation) -> dict[str, Any]:
    recent: list[dict[str, Any]] = []
    for message in conversation.messages[1:][-30:]:
        role = str(message.get("role") or "")
        if role == "system":
            continue
        # Tool-call structures can include internal arguments. They are useful
        # for context, but they must pass the recursive secret scrubber.
        recent.append(_safe_context_value(message))

    return {
        "summary": sanitize_support_text(conversation.summary),
        "recent_messages": recent,
        "authenticated": conversation.verified_customer_id is not None,
        "authenticated_userid": conversation.authenticated_userid,
        "authenticated_name": conversation.authenticated_name,
        "current_language": conversation.current_language,
        "communication_style": conversation.communication_style,
    }


def _ticket_reference() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"CS-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def _chat_reference() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"CH-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def _json_safe_db_value(value: Any) -> Any:
    """Convert DB-driver/native values to JSON-safe primitives.

    MariaDB drivers may return Decimal, bytes, date/time, or timedelta values
    depending on column type and server configuration. Ticket detail is a core
    support-console path, so no such native value should be able to turn an
    otherwise valid ticket into an opaque HTTP 500 during FastAPI encoding.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        # Preserve integer decimals losslessly; coordinates/rates with a scale
        # are safe as floats for the support UI's display purposes.
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): _json_safe_db_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe_db_value(item) for item in value]
    # Last-resort protection for driver-specific scalar wrappers. Do not let an
    # unknown representation break ticket viewing; values shown to staff are
    # informational and stringification is safer than an HTTP 500.
    return str(value)


def _normalize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {str(key): _json_safe_db_value(value) for key, value in row.items()}


def _safe_ticket_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    normalized = _normalize_row(row)
    if normalized is None:
        return None
    safe = dict(normalized)
    for key in ("reason", "ai_summary", "requested_action", "customer_name"):
        if isinstance(safe.get(key), str):
            safe[key] = sanitize_support_text(safe[key])
    if row and row.get("context_json"):
        # Never return the raw stored JSON alongside the sanitized parsed context.
        safe["context_json"] = json.dumps(_decode_context(row), ensure_ascii=False)
    return safe


def _history_ticket_summary(row: dict[str, Any]) -> dict[str, Any]:
    safe = _safe_ticket_row(row) or {}
    keys = (
        "id",
        "ticket_reference",
        "status",
        "conversation_origin",
        "business_phone_number_id",
        "business_display_phone_number",
        "display_name",
        "created_at",
        "updated_at",
        "last_activity_at",
        "resolved_at",
        "closed_at",
    )
    return {key: safe.get(key) for key in keys}


def _decode_context(ticket: dict[str, Any]) -> dict[str, Any]:
    raw = ticket.get("context_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return _safe_context_value(parsed) if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _can_operate(ticket: dict[str, Any], agent: SupportAgent) -> bool:
    return agent.is_supervisor or str(ticket.get("current_assignee_id") or "") == agent.id


def _is_assignee(ticket: dict[str, Any], agent: SupportAgent) -> bool:
    return str(ticket.get("current_assignee_id") or "") == agent.id


async def _gather_or_cancel(*awaitables: Any) -> list[Any]:
    """Await all concurrently; if one fails, cancel the rest and re-raise it."""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _can_view_ticket(ticket: dict[str, Any], agent: SupportAgent) -> bool:
    """Authorize confidential ticket contents for one support user.

    All staff may inspect closed customer history. Live conversations remain
    restricted to supervisors, their participants, and claimable NEW tickets.
    """
    if agent.is_supervisor:
        return True
    if ticket.get("active_key") is None and ticket.get("status") in {"RESOLVED", "CLOSED"}:
        return True
    if (
        ticket.get("status") == "NEW"
        and ticket.get("current_assignee_id") is None
        and ticket.get("active_key") is not None
    ):
        return True
    if _is_assignee(ticket, agent):
        return True
    return await support_repository.agent_has_participated(int(ticket["id"]), agent.id)


def _conversation_key_for_ticket(ticket: dict[str, Any]) -> str:
    phone = str(ticket.get("whatsapp_phone") or "")
    business_id = str(ticket.get("business_phone_number_id") or "").strip()
    return f"{business_id}:{phone}" if business_id else phone


def _same_customer_phone(left: dict[str, Any], right: dict[str, Any]) -> bool:
    phone = normalize_phone(str(left.get("whatsapp_phone") or ""))
    return bool(phone and phone == normalize_phone(str(right.get("whatsapp_phone") or "")))


async def _historical_ticket_accessible(
    previous: dict[str, Any], current: dict[str, Any], agent: SupportAgent,
) -> bool:
    """Share closed conversations for the same customer across support lines.

    The caller must first authorize access to ``current``. A closed staff chat
    can be reopened in place, so its ID may be older than a ticket that was
    resolved between chat sessions.
    """
    _ = agent
    if int(previous.get("id") or 0) == int(current.get("id") or 0):
        return False
    if previous.get("active_key") is not None:
        return False
    if str(previous.get("status") or "") not in {"RESOLVED", "CLOSED"}:
        return False
    if not _same_customer_phone(previous, current):
        return False
    return True


def _provider_change_timestamp(value: Any) -> int:
    """Normalize protocol seconds/milliseconds without inventing a remote order."""
    if isinstance(value, bool):
        raise ValueError("Invalid WhatsApp change timestamp")
    timestamp = int(value)
    if timestamp < 1_000_000_000_000:
        timestamp *= 1000
    if timestamp <= 0 or timestamp > int(datetime.now(timezone.utc).timestamp() * 1000) + 300_000:
        raise ValueError("Invalid WhatsApp change timestamp")
    return timestamp


def _message_age_seconds(message: dict[str, Any]) -> float | None:
    original = message.get("whatsapp_timestamp") or message.get("created_at")
    try:
        if isinstance(original, datetime):
            created = original.replace(tzinfo=timezone.utc) if original.tzinfo is None else original
        elif isinstance(original, (int, float)) or str(original or "").isdigit():
            timestamp = float(original)
            created = datetime.fromtimestamp(timestamp / 1000 if timestamp >= 1_000_000_000_000 else timestamp, tz=timezone.utc)
        else:
            created = datetime.fromisoformat(str(original).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - created).total_seconds()
        return age if age >= -60 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _message_change_capabilities(message: dict[str, Any]) -> dict[str, bool]:
    provider_id = str(message.get("whatsapp_message_id") or "")
    linked = is_linked_device_message(provider_id) and get_transport(provider_id.split(":", 2)[1]) == "linked_device"
    age = _message_age_seconds(message)
    eligible = linked and message.get("direction") == "OUTBOUND" and str(message.get("sender_type") or "").upper() == "AGENT" and bool(message.get("sender_id")) and message.get("send_status") == "SENT" and not message.get("deleted_for_everyone_at") and age is not None
    return {
        "can_edit": bool(eligible and str(message.get("message_type") or "").lower() == "text" and age <= 15 * 60),
        "can_delete_for_everyone": bool(eligible and age <= 2 * 24 * 60 * 60),
    }


def _serialize_support_messages(
    ticket_id: int,
    messages: list[dict[str, Any]],
    *,
    media_context_ticket_id: int | None = None,
) -> list[dict[str, Any] | None]:
    """Sanitize support messages and attach protected media/reply metadata."""
    safe_messages: list[dict[str, Any] | None] = []
    for row in messages:
        safe_row = dict(row)
        safe_row.update(_message_change_capabilities(row))
        safe_row.pop("pending_change_operation_id", None)
        if safe_row.get("deleted_for_everyone_at"):
            # Do not expose cached media, captions, locations, or reactions through
            # the ticket detail and reply-preview paths after global deletion.
            for field in ("body", "location_latitude", "location_longitude", "location_name", "location_address", "provider_media_id",
                          "provider_media_mime_type", "provider_media_filename", "error_message"):
                safe_row[field] = None
            for field in ("media_url", "media_filename", "media_mime_type", "media_size_bytes", "media_available", "media_cached"):
                safe_row.pop(field, None)
            safe_row["reactions"] = []
        if isinstance(safe_row.get("body"), str):
            safe_row["body"] = sanitize_support_text(safe_row["body"])
        if isinstance(safe_row.get("error_message"), str):
            safe_row["error_message"] = sanitize_support_text(safe_row["error_message"])
        if not safe_row.get("deleted_for_everyone_at") and str(safe_row.get("message_type") or "") in SUPPORT_MEDIA_MESSAGE_TYPES:
            message_id = int(safe_row["id"])
            asset = support_media_store.get(ticket_id, message_id)
            remote = support_media_store.get_remote_reference(ticket_id, message_id)
            provider_media_id = str(safe_row.get("provider_media_id") or "").strip()
            provider_media_mime = str(
                safe_row.get("provider_media_mime_type") or ""
            ).strip()
            provider_media_filename = str(
                safe_row.get("provider_media_filename") or ""
            ).strip()
            recoverable = remote is not None or bool(provider_media_id)
            safe_row["media_kind"] = str(safe_row.get("message_type"))
            safe_row["media_available"] = asset is not None or recoverable
            safe_row["media_cached"] = asset is not None
            if asset is not None or recoverable:
                media_url = f"/api/support/tickets/{ticket_id}/messages/{message_id}/media"
                if media_context_ticket_id and int(media_context_ticket_id) != int(ticket_id):
                    media_url += f"?context_ticket_id={int(media_context_ticket_id)}"
                safe_row["media_url"] = media_url
            if asset is not None:
                safe_row["media_mime_type"] = asset.mime_type
                safe_row["media_size_bytes"] = asset.size_bytes
                safe_row["media_filename"] = asset.filename
            elif remote is not None and remote.expected_mime_type:
                safe_row["media_mime_type"] = normalize_mime_type(
                    remote.expected_mime_type
                )
            elif provider_media_mime:
                safe_row["media_mime_type"] = normalize_mime_type(
                    provider_media_mime
                )
            if asset is None:
                if provider_media_filename:
                    safe_row["media_filename"] = provider_media_filename
                elif remote is not None and remote.filename:
                    safe_row["media_filename"] = remote.filename
            safe_row.pop("provider_media_id", None)
            safe_row.pop("provider_media_mime_type", None)
            safe_row.pop("provider_media_filename", None)
        else:
            safe_row.pop("provider_media_id", None)
            safe_row.pop("provider_media_mime_type", None)
            safe_row.pop("provider_media_filename", None)
        safe_reactions: list[dict[str, Any]] = []
        for reaction in safe_row.get("reactions") or []:
            if not isinstance(reaction, dict) or not str(reaction.get("emoji") or ""):
                continue
            safe_reaction = dict(reaction)
            if isinstance(safe_reaction.get("reactor_name"), str):
                safe_reaction["reactor_name"] = sanitize_support_text(
                    safe_reaction["reactor_name"]
                )
            safe_reactions.append(_normalize_row(safe_reaction) or {})
        safe_row["reactions"] = safe_reactions
        if safe_row.get("location_latitude") is not None:
            try:
                safe_row["location_latitude"] = float(safe_row["location_latitude"])
                safe_row["location_longitude"] = float(safe_row["location_longitude"])
            except (TypeError, ValueError):
                safe_row["location_latitude"] = None
                safe_row["location_longitude"] = None
        safe_messages.append(_normalize_row(safe_row))

    message_by_whatsapp_id = {
        str(item.get("whatsapp_message_id")): item
        for item in safe_messages
        if item and item.get("whatsapp_message_id")
    }
    for item in safe_messages:
        if not item:
            continue
        reply_id = str(item.get("reply_to_whatsapp_message_id") or "").strip()
        target = message_by_whatsapp_id.get(reply_id)
        if target is None:
            continue
        item["reply_to"] = {
            "id": target.get("id"),
            "sender_type": target.get("sender_type"),
            "sender_name": target.get("sender_name"),
            "message_type": target.get("message_type"),
            "deleted_for_everyone_at": target.get("deleted_for_everyone_at"),
            "body": "Original message deleted" if target.get("deleted_for_everyone_at") else target.get("body"),
            "media_kind": target.get("media_kind"),
            "media_filename": target.get("media_filename"),
            "location_name": target.get("location_name"),
            "location_address": target.get("location_address"),
        }
    return safe_messages


async def _return_conversation_to_ai(
    resolved: dict[str, Any], *, resolved_by: str = "by an authorized agent",
) -> None:
    # DB was updated first. Redis is a cache; if this write fails, the next
    # inbound webhook reconciles stale human mode against the durable DB.
    phone = str(resolved["whatsapp_phone"])
    try:
        conversation = await conversation_store.get_or_create(
            phone,
            conversation_key=_conversation_key_for_ticket(resolved),
            business_phone_number_id=(
                str(resolved.get("business_phone_number_id"))
                if resolved.get("business_phone_number_id")
                else None
            ),
            business_display_phone_number=(
                str(resolved.get("business_display_phone_number"))
                if resolved.get("business_display_phone_number")
                else None
            ),
        )
        conversation.mode = "ai"
        conversation.active_ticket_id = None

        # Keep the historical handoff transcript, but record an explicit
        # authoritative lifecycle boundary. Without this marker, the next
        # AI turn can see an older transfer_to_human tool call / "agent is
        # joining" reply and mistakenly behave as if that handoff were
        # still active even though the durable ticket has been resolved.
        conversation.messages.append(
            {
                "role": "system",
                "content": (
                    f"SUPPORT EVENT: Human support has been resolved {resolved_by} "
                    "and control has explicitly returned "
                    "to AI. Earlier handoff, transfer, waiting-for-agent, "
                    "and agent-joining messages in the conversation are "
                    "historical and are no longer active. Handle the next "
                    "customer message normally. Start a new human handoff "
                    "only if the customer's current/new request independently "
                    "meets the escalation policy."
                ),
            }
        )

        await conversation_store.save(conversation)
    except Exception:
        logger.exception(
            "Ticket resolved but Redis conversation cache could not be updated; it will self-heal on next inbound message"
        )


class SupportService:
    async def can_receive_support_event(
        self,
        event: dict[str, Any],
        agent: SupportAgent,
    ) -> bool:
        """Authorize one realtime event before it reaches a staff browser."""
        if event.get("type") == "message_deleted_for_me" and str(event.get("user_id") or "") != agent.id:
            return False
        raw_ticket_id = event.get("ticket_id")
        if raw_ticket_id is None:
            # Workspace-wide events (for example contact sync completion) do not
            # carry ticket/chat data and retain their existing broadcast behavior.
            return True
        try:
            ticket_id = int(raw_ticket_id)
        except (TypeError, ValueError):
            return False
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            return False
        if not await _can_view_ticket(ticket, agent):
            # A former owner can receive changes to their own historical
            # message without gaining the rest of that ticket's event stream.
            if event.get("type") not in {"message_updated", "message_deleted_for_everyone", "message_deleted_for_me"}:
                return False
            try:
                message_id = int(event["message_id"])
            except (KeyError, TypeError, ValueError):
                return False
            message = await support_repository.get_message(message_id)
            if (
                not message or int(message.get("ticket_id") or 0) != ticket_id
                or str(message.get("sender_type") or "").upper() != "AGENT"
                or str(message.get("sender_id") or "") != agent.id
                or message.get("direction") != "OUTBOUND"
            ):
                return False
        if event.get("type") in {
            "outbound_message", "message_media_ready", "message_updated",
            "message_deleted_for_everyone", "message_reaction", "message_status",
        } and event.get("message_id") is not None:
            try:
                message_id = int(event["message_id"])
            except (TypeError, ValueError):
                return False
            return not await support_repository.message_hidden_for_user(message_id, agent.id)
        return True

    async def get_active_ticket(
        self,
        phone_number: str,
        *,
        business_phone_number_id: str | None = None,
        business_display_phone_number: str | None = None,
    ) -> dict[str, Any] | None:
        ticket = await support_repository.find_active_ticket_by_phone(
            phone_number,
            business_phone_number_id,
        )
        if (
            ticket
            and business_phone_number_id
            and not ticket.get("business_phone_number_id")
        ):
            ticket = await support_repository.bind_ticket_channel(
                int(ticket["id"]),
                phone_number=phone_number,
                business_phone_number_id=business_phone_number_id,
                business_display_phone_number=business_display_phone_number,
            )
            # Two receiving lines can race while upgrading one legacy active ticket.
            # If another line won the bind, never leak that ticket into this channel;
            # re-query this exact business number instead.
            if str(ticket.get("business_phone_number_id") or "") != str(
                business_phone_number_id
            ):
                ticket = await support_repository.find_active_ticket_by_phone(
                    phone_number,
                    business_phone_number_id,
                )
        return ticket

    async def get_ticket_by_source_message(
        self,
        whatsapp_message_id: str,
    ) -> dict[str, Any] | None:
        return await support_repository.get_ticket_by_source_message(whatsapp_message_id)

    async def get_message_by_whatsapp_id(
        self,
        whatsapp_message_id: str,
    ) -> dict[str, Any] | None:
        return await support_repository.get_message_by_whatsapp_id(whatsapp_message_id)

    async def reconcile_conversation_mode(
        self,
        conversation: Conversation,
        active_ticket: dict[str, Any] | None,
    ) -> None:
        changed = False
        if active_ticket:
            ticket_id = str(active_ticket["id"])
            if conversation.mode != "human":
                conversation.mode = "human"
                changed = True
            if getattr(conversation, "active_ticket_id", None) != ticket_id:
                conversation.active_ticket_id = ticket_id
                changed = True
        else:
            if conversation.mode == "human":
                conversation.mode = "ai"
                changed = True
            if getattr(conversation, "active_ticket_id", None) is not None:
                conversation.active_ticket_id = None
                changed = True
        if changed:
            await conversation_store.save(conversation)

    async def finalize_handoff(
        self,
        *,
        conversation: Conversation,
        handoff: HandoffRequest,
        source_whatsapp_message_id: str | None,
        source_message_text: str | None,
        source_whatsapp_timestamp: str | None = None,
        reply_to_whatsapp_message_id: str | None = None,
        business_phone_number_id: str | None = None,
        business_display_phone_number: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        reason = sanitize_support_text(handoff.reason).strip()[:2000]
        if not reason:
            reason = "Human support requested."

        summary = sanitize_support_text(handoff.summary).strip()[:6000]
        if not summary:
            summary = sanitize_support_text(conversation.summary).strip()[:6000] or reason

        tracking = sanitize_support_text(handoff.tracking_number).strip()[:191] or None
        requested_action = sanitize_support_text(handoff.requested_action).strip()[:500] or None
        context = _conversation_context(conversation)

        directory_identity = None
        try:
            directory_identity = await customer_directory.resolve(conversation.phone_number)
        except Exception:
            logger.exception("Could not resolve support customer identity from phone number")

        ticket_customer_id = conversation.verified_customer_id
        ticket_customer_userid = conversation.authenticated_userid
        ticket_customer_name = conversation.authenticated_name
        if directory_identity is not None:
            # A dashboard name given to this registered customer wins over the
            # account name, unless the customer signed in to a different account.
            renamed_same_customer = (
                directory_identity.source == "registered_user"
                and directory_identity.contact_key is not None
                and (not ticket_customer_id or str(ticket_customer_id) == str(directory_identity.user_id))
            )
            if renamed_same_customer or not ticket_customer_name:
                ticket_customer_name = directory_identity.name
            if directory_identity.source == "registered_user":
                ticket_customer_id = ticket_customer_id or directory_identity.user_id
                ticket_customer_userid = ticket_customer_userid or directory_identity.userid

        receiving_line = business_phone_number_id or conversation.business_phone_number_id
        if receiving_line and get_transport(str(receiving_line)) == "removed":
            raise SupportConflictError("This WhatsApp number has been removed. A new ticket cannot be opened on it.")
        ticket, created = await support_repository.create_ticket(
            ticket_reference=_ticket_reference(),
            phone_number=conversation.phone_number,
            business_phone_number_id=(
                business_phone_number_id or conversation.business_phone_number_id
            ),
            business_display_phone_number=(
                business_display_phone_number
                or conversation.business_display_phone_number
            ),
            customer_id=ticket_customer_id,
            customer_userid=ticket_customer_userid,
            customer_name=ticket_customer_name,
            reason=reason,
            ai_summary=summary,
            tracking_reference=tracking,
            requested_action=requested_action,
            current_language=conversation.current_language,
            communication_style=conversation.communication_style,
            context_json=json.dumps(context, ensure_ascii=False),
            source_whatsapp_message_id=source_whatsapp_message_id,
        )

        # Persist the handoff-triggering customer message after the durable
        # ticket exists. A unique WhatsApp ID makes this idempotent on retries.
        if source_message_text:
            try:
                await support_repository.add_ticket_message(
                    ticket_id=int(ticket["id"]),
                    sender_type="CUSTOMER",
                    direction="INBOUND",
                    body=sanitize_support_text(source_message_text),
                    whatsapp_message_id=source_whatsapp_message_id,
                    whatsapp_timestamp=source_whatsapp_timestamp,
                    message_type="text",
                    reply_to_whatsapp_message_id=reply_to_whatsapp_message_id,
                    send_status="RECEIVED",
                    require_active=True,
                    current_language=conversation.current_language,
                    communication_style=conversation.communication_style,
                )
            except Exception:
                # Ticket creation is the durable handoff boundary. The trigger
                # message is also present in the scrubbed context snapshot, so a
                # secondary transcript-copy failure must not strand the ticket in
                # AI mode after the durable ticket already exists.
                logger.exception(
                    "Support ticket exists but handoff-trigger message copy failed: ticket_id=%s",
                    ticket["id"],
                )

        # DB is authoritative. Only after persistence succeeds do we cache human
        # mode in Redis.
        conversation.mode = "human"
        conversation.active_ticket_id = str(ticket["id"])
        await conversation_store.save(conversation)

        if created:
            try:
                await notify_human_agents(
                    customer_id=conversation.phone_number,
                    reason=reason,
                    ticket_reference=str(ticket["ticket_reference"]),
                )
            except Exception:
                # Notification is secondary and must never invalidate the ticket.
                logger.exception("Optional human-handoff notification failed")
            await publish_support_event(
                "ticket_created",
                ticket_id=int(ticket["id"]),
                customer_name=ticket.get("customer_name"),
                customer_phone=ticket.get("whatsapp_phone"),
            )

        return ticket, created

    async def route_human_inbound(
        self,
        *,
        ticket: dict[str, Any],
        body: str,
        whatsapp_message_id: str | None,
        whatsapp_timestamp: str | None,
        message_type: str,
        reply_to_whatsapp_message_id: str | None = None,
        location_latitude: float | None = None,
        location_longitude: float | None = None,
        location_name: str | None = None,
        location_address: str | None = None,
        current_language: str | None = None,
        communication_style: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        message, created = await support_repository.add_ticket_message(
            ticket_id=int(ticket["id"]),
            sender_type="CUSTOMER",
            direction="INBOUND",
            body=sanitize_support_text(body),
            whatsapp_message_id=whatsapp_message_id,
            whatsapp_timestamp=whatsapp_timestamp,
            message_type=message_type,
            reply_to_whatsapp_message_id=reply_to_whatsapp_message_id,
            location_latitude=location_latitude,
            location_longitude=location_longitude,
            location_name=sanitize_support_text(location_name).strip()[:255] or None,
            location_address=sanitize_support_text(location_address).strip()[:1000] or None,
            send_status="RECEIVED",
            require_active=True,
            current_language=current_language,
            communication_style=communication_style,
        )

        if created and ticket.get("status") == "WAITING_CUSTOMER":
            try:
                await support_repository.mark_customer_replied(int(ticket["id"]))
            except Exception:
                logger.exception(
                    "Customer message was stored but WAITING_CUSTOMER transition failed: ticket_id=%s",
                    ticket["id"],
                )
        if created:
            await publish_support_event(
                "customer_message",
                ticket_id=int(ticket["id"]),
                customer_name=ticket.get("customer_name"),
                customer_phone=ticket.get("whatsapp_phone"),
                message_id=int(message["id"]),
                message=_serialize_support_messages(int(ticket["id"]), [message])[0],
            )
        return message, created

    async def sync_linked_device_outbound(self, incoming: IncomingWhatsAppMessage) -> bool:
        """Mirror a message sent in the WhatsApp app without entering the AI flow.

        The phone does not identify which staff account sent it. Keep that
        distinction in sender_id while showing the message on the outgoing side.
        A new phone-origin conversation opens an unassigned ticket so no message
        disappears merely because the dashboard did not initiate the chat.
        """
        line_id = str(incoming.business_phone_number_id or "")
        peer = normalize_phone(incoming.from_number)
        provider_id = str(incoming.message_id or "")
        if (
            not peer or not line_id or get_transport(line_id) != "linked_device"
            or not is_linked_device_message(provider_id, line_id)
        ):
            raise SupportValidationError("Invalid linked-device outgoing message.")

        if incoming.message_type == "reaction":
            target_id = str(incoming.reaction_message_id or "")
            if not is_linked_device_message(target_id, line_id):
                raise SupportValidationError("Invalid linked-device reaction target.")
            target = await support_repository.get_message_by_whatsapp_id(target_id)
            if not target:
                return False
            ticket = await support_repository.get_ticket(int(target["ticket_id"]))
            if not ticket or str(ticket.get("business_phone_number_id") or "") != line_id or normalize_phone(str(ticket.get("whatsapp_phone") or "")) != peer:
                raise SupportValidationError("The reaction target belongs to another conversation.")
            emoji = str(incoming.reaction_emoji or "").strip()
            if len(emoji) > 64 or any(ord(char) < 32 for char in emoji):
                raise SupportValidationError("Invalid linked-device reaction.")
            _reaction, changed = await support_repository.apply_linked_device_reaction(
                ticket_id=int(ticket["id"]), message_id=int(target["id"]),
                target_whatsapp_message_id=target_id, line_id=line_id,
                emoji=emoji, provider_reaction_id=provider_id,
                provider_timestamp=int(incoming.timestamp) if str(incoming.timestamp or "").isdigit() else None,
            )
            if changed:
                await publish_support_event(
                    "message_reaction", ticket_id=int(ticket["id"]),
                    message_id=int(target["id"]), customer_name=ticket.get("customer_name"),
                    customer_phone=ticket.get("whatsapp_phone"),
                )
            return True

        if incoming.message_type not in SUPPORT_MEDIA_MESSAGE_TYPES | {"text", "location", "unsupported"}:
            raise SupportValidationError("Unsupported linked-device outgoing message type.")
        if incoming.message_type in SUPPORT_MEDIA_MESSAGE_TYPES:
            media_id = str(incoming.media_id or "")
            if not re.fullmatch(rf"ldmedia:{re.escape(line_id)}:[0-9a-fA-F]{{8}}-(?:[0-9a-fA-F]{{4}}-){{3}}[0-9a-fA-F]{{12}}", media_id):
                raise SupportValidationError("Invalid linked-device outgoing media reference.")
        if incoming.context_message_id and not is_linked_device_message(incoming.context_message_id, line_id):
            raise SupportValidationError("Invalid linked-device reply reference.")
        try:
            sent_ms = _provider_change_timestamp(int(str(incoming.timestamp or "")) * 1000)
            sent_at = datetime.fromtimestamp(sent_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise SupportValidationError("Invalid linked-device outgoing timestamp.") from exc

        if incoming.message_type == "text":
            body = sanitize_support_text(incoming.text)
            if not body:
                raise SupportValidationError("Invalid linked-device outgoing text.")
            if len(body) > 4096:
                marker = "\n[Message exceeded dashboard length; truncated]"
                body = body[:4096 - len(marker)] + marker
        elif incoming.message_type == "image":
            body = sanitize_support_text(incoming.media_caption).strip() or "Image"
        elif incoming.message_type == "video":
            body = sanitize_support_text(incoming.media_caption).strip() or "Video"
        elif incoming.message_type == "audio":
            body = "Voice note" if incoming.is_voice else "Audio message"
        elif incoming.message_type == "document":
            body = sanitize_support_text(incoming.media_caption).strip() or sanitize_support_text(incoming.media_filename).strip() or "Document"
        elif incoming.message_type == "sticker":
            body = "Sticker"
        elif incoming.message_type == "unsupported":
            body = "[Unsupported WhatsApp message sent]"
        else:
            name = sanitize_support_text(incoming.location_name).strip()
            address = sanitize_support_text(incoming.location_address).strip()
            body = f"Location: {name or address}" if name or address else "Location"

        existing = await support_repository.get_message_by_whatsapp_id(provider_id)
        if existing:
            ticket = await support_repository.get_ticket(int(existing["ticket_id"]))
            if not ticket or str(ticket.get("business_phone_number_id") or "") != line_id or normalize_phone(str(ticket.get("whatsapp_phone") or "")) != peer or existing.get("direction") != "OUTBOUND":
                raise SupportValidationError("The outgoing message belongs to another conversation.")
            message = existing
            created = False
        else:
            ticket = await support_repository.find_ticket_at_whatsapp_timestamp(peer, line_id, sent_at)
            if not ticket:
                ticket = await self.get_active_ticket(
                    peer, business_phone_number_id=line_id,
                    business_display_phone_number=incoming.business_display_phone_number,
                )
            if not ticket:
                ticket, ticket_created = await support_repository.create_ticket(
                    ticket_reference=_ticket_reference(), phone_number=peer,
                    business_phone_number_id=line_id,
                    business_display_phone_number=incoming.business_display_phone_number,
                    customer_id=None, customer_userid=None, customer_name=None,
                    reason="Conversation started in WhatsApp app.",
                    ai_summary="Conversation started in WhatsApp app.",
                    tracking_reference=None, requested_action=None,
                    current_language=None, communication_style=None,
                    context_json="{}", source_whatsapp_message_id=None,
                )
                if ticket_created:
                    await publish_support_event(
                        "ticket_created", ticket_id=int(ticket["id"]),
                        customer_name=ticket.get("customer_name"), customer_phone=peer,
                        phone_initiated=True,
                    )

            message, created = await support_repository.add_ticket_message(
                ticket_id=int(ticket["id"]), sender_type="AGENT", sender_id=None,
                sender_name="WhatsApp app", direction="OUTBOUND", body=body,
                whatsapp_message_id=provider_id, whatsapp_timestamp=incoming.timestamp,
                message_type=incoming.message_type,
                reply_to_whatsapp_message_id=incoming.context_message_id,
                location_latitude=incoming.location_latitude,
                location_longitude=incoming.location_longitude,
                location_name=sanitize_support_text(incoming.location_name).strip()[:255] or None,
                location_address=sanitize_support_text(incoming.location_address).strip()[:1000] or None,
                send_status="SENT", require_active=(ticket.get("active_key") is not None and ticket.get("status") in ACTIVE_TICKET_STATUSES),
                expected_phone=peer, expected_line_id=line_id,
            )
            if int(message.get("ticket_id") or 0) != int(ticket["id"]) or message.get("direction") != "OUTBOUND":
                raise SupportValidationError("The outgoing message belongs to another conversation.")
            if created:
                await publish_support_event(
                    "outbound_message", ticket_id=int(ticket["id"]),
                    message_id=int(message["id"]),
                    message=_serialize_support_messages(int(ticket["id"]), [message])[0],
                )

        if incoming.message_type in SUPPORT_MEDIA_MESSAGE_TYPES:
            try:
                await self.capture_inbound_media(
                    ticket=ticket, message=message, media_id=str(incoming.media_id),
                    message_type=incoming.message_type,
                    expected_mime_type=incoming.media_mime_type,
                    filename=incoming.media_filename,
                )
            except SupportMediaError as exc:
                if exc.retryable:
                    raise
                logger.warning("WhatsApp app media could not be cached: message_id=%s", provider_id)
        return True

    async def sync_linked_device_tombstone(self, event: dict[str, Any]) -> bool:
        """Store a revoked customer message without exposing content to the AI flow."""
        line_id = str(event.get("line_id") or "")
        peer = str(event.get("sender_phone") or "")
        provider_id = str(event.get("message_id") or "")
        event_id = str(event.get("event_id") or "")
        if (
            event.get("type") != "message_tombstone" or event.get("from_me") is not False
            or get_transport(line_id) != "linked_device"
            or not is_linked_device_message(provider_id, line_id)
            or not re.fullmatch(r"[0-9]{5,20}", peer)
            or not event_id or len(event_id) > 128
            or event.get("payload") is not None or event.get("body") is not None
        ):
            raise SupportValidationError("Invalid linked-device inbound tombstone.")
        try:
            sent_ms = _provider_change_timestamp(event.get("timestamp"))
            deleted_ms = _provider_change_timestamp(
                event.get("deleted_timestamp")
                or int(datetime.now(timezone.utc).timestamp() * 1000)
            )
            sent_at = datetime.fromtimestamp(sent_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise SupportValidationError("Invalid linked-device inbound tombstone timestamp.") from exc

        # The original inbound webhook may already be acknowledged but still
        # queued behind another turn. Mark its provider ID before looking up the
        # ticket so that queued work cannot later expose revoked content to AI.
        await conversation_store.mark_inbound_revoked(provider_id)

        existing = await support_repository.get_message_by_whatsapp_id(provider_id)
        if existing:
            ticket = await support_repository.get_ticket(int(existing["ticket_id"]))
            if (
                not ticket or existing.get("direction") != "INBOUND"
                or str(existing.get("sender_type") or "").upper() != "CUSTOMER"
                or str(ticket.get("business_phone_number_id") or "") != line_id
                or normalize_phone(str(ticket.get("whatsapp_phone") or "")) != peer
            ):
                raise SupportValidationError("The deleted message belongs to another conversation.")
            message, created = existing, False
        else:
            ticket = await support_repository.find_ticket_at_whatsapp_timestamp(peer, line_id, sent_at)
            if not ticket:
                # A routine chatbot message can be revoked too. Without a
                # support interval, acknowledging the tombstone must not open a
                # new ticket solely because the customer deleted that message.
                return True
            message, created = await support_repository.add_ticket_message(
                ticket_id=int(ticket["id"]), sender_type="CUSTOMER",
                direction="INBOUND", body="[Deleted WhatsApp message]",
                whatsapp_message_id=provider_id,
                whatsapp_timestamp=str(sent_ms // 1000), message_type="unsupported",
                send_status="RECEIVED",
                require_active=(ticket.get("active_key") is not None and ticket.get("status") in ACTIVE_TICKET_STATUSES),
                expected_phone=peer, expected_line_id=line_id,
            )
            if int(message.get("ticket_id") or 0) != int(ticket["id"]) or message.get("direction") != "INBOUND":
                raise SupportValidationError("The deleted message belongs to another conversation.")

        message, changed = await support_repository.apply_message_change_event(
            int(message["id"]), event_id=event_id,
            action="everyone", content=None, timestamp=deleted_ms,
        )
        if message.get("deleted_for_everyone_at"):
            self._purge_deleted_message_media(int(ticket["id"]), int(message["id"]))
        if created:
            await publish_support_event(
                "customer_message", ticket_id=int(ticket["id"]),
                customer_name=ticket.get("customer_name"), customer_phone=peer,
                message_id=int(message["id"]),
                message=_serialize_support_messages(int(ticket["id"]), [message])[0],
            )
        elif changed:
            await publish_support_event(
                "message_deleted_for_everyone", ticket_id=int(ticket["id"]),
                message_id=int(message["id"]), version=message.get("version"),
                message=_serialize_support_messages(int(ticket["id"]), [message])[0],
            )
        return True

    async def confirm_linked_device_outgoing(self, event: dict[str, Any]) -> bool:
        """Reconcile an app send after its WhatsApp ACK or a later own echo."""
        line_id = str(event.get("line_id") or "")
        provider_id = str(event.get("message_id") or "")
        client_id = str(event.get("client_message_id") or "")
        peer = normalize_phone(str(event.get("sender_phone") or ""))
        if (
            event.get("type") != "outgoing_confirmation" or event.get("from_me") is not True
            or get_transport(line_id) != "linked_device"
            or not is_linked_device_message(provider_id, line_id)
            or not client_id or len(client_id) > 191
            or any(ord(char) < 32 or ord(char) == 127 for char in client_id)
            or not peer
        ):
            raise SupportValidationError("Invalid linked-device outgoing confirmation.")
        try:
            timestamp = _provider_change_timestamp(event.get("timestamp"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise SupportValidationError("Invalid linked-device outgoing confirmation timestamp.") from exc
        result = await support_repository.confirm_linked_device_outgoing(
            client_message_id=client_id, whatsapp_message_id=provider_id,
            phone_number=peer, business_phone_number_id=line_id,
            whatsapp_timestamp=str(timestamp // 1000),
        )
        if not result:
            return False
        message, changed = result
        if changed:
            ticket_id = int(message["ticket_id"])
            await publish_support_event(
                "outbound_message", ticket_id=ticket_id,
                message_id=int(message["id"]),
                message=_serialize_support_messages(ticket_id, [message])[0],
            )
        return True

    async def apply_delivery_status(
        self,
        *,
        whatsapp_message_id: str,
        status: str,
        timestamp_ms: int | None,
        business_phone_number_id: str | None,
        phone_number: str | None,
    ) -> bool:
        """Record a WhatsApp delivered/read receipt. False when no support row matches."""
        status = str(status or "").lower()
        if status not in DELIVERY_STATUS_RANK:
            raise SupportValidationError("Unknown WhatsApp delivery status.")
        try:
            at = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc).replace(tzinfo=None) if timestamp_ms else datetime.now(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise SupportValidationError("Invalid WhatsApp delivery status timestamp.") from exc
        result = await support_repository.apply_delivery_status(
            str(whatsapp_message_id), status=status, at=at,
            business_phone_number_id=business_phone_number_id or None,
            phone_number=normalize_phone(phone_number) if phone_number else None,
        )
        if not result:
            return False
        message, changed = result
        if changed:
            ticket_id = int(message["ticket_id"])
            await publish_support_event(
                "message_status", ticket_id=ticket_id,
                message_id=int(message["id"]),
                message=_serialize_support_messages(ticket_id, [message])[0],
            )
        return True

    async def resend_failed_message(
        self,
        ticket_id: int,
        message_id: int,
        *,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        """Send a FAILED outgoing message again, on the same timeline row.

        The row keeps its client message ID. If an earlier attempt reached
        WhatsApp after all, its late confirmation is ignored once this attempt
        is confirmed, so the dashboard never shows the message twice.
        """
        ticket = await self._prepare_agent_outbound_ticket(ticket_id, agent)
        message = await support_repository.get_message(message_id)
        if (
            not message or int(message.get("ticket_id") or 0) != int(ticket_id)
            or message.get("direction") != "OUTBOUND"
            or str(message.get("sender_type") or "").upper() != "AGENT"
            or str(message.get("sender_id") or "") != agent.id
            or not message.get("client_message_id")
            or message.get("deleted_for_everyone_at")
        ):
            raise SupportNotFoundError("This failed message is not available to resend.")
        if message.get("send_status") != "FAILED":
            raise SupportConflictError("Only messages that failed to send can be resent.")
        message_type = str(message.get("message_type") or "text")
        if message_type not in {"text", "location"} | SUPPORT_MEDIA_MESSAGE_TYPES:
            raise SupportValidationError("This message type cannot be resent.")
        asset = None
        if message_type in SUPPORT_MEDIA_MESSAGE_TYPES:
            asset = support_media_store.get(int(ticket_id), int(message_id))
            if asset is None:
                raise SupportConflictError("The file for this message is no longer stored. Send it again from your device.")
        if not await support_repository.restart_failed_send(int(message_id)):
            raise SupportConflictError("This message is already being resent.")

        pending = await support_repository.get_message(int(message_id))
        if pending:
            await publish_support_event(
                "outbound_message", ticket_id=ticket_id, message_id=int(message_id),
                message=_serialize_support_messages(ticket_id, [pending])[0],
            )
        line_id = str(ticket.get("business_phone_number_id")) if ticket.get("business_phone_number_id") else None
        client_message_id = str(message["client_message_id"])
        reply_whatsapp_id = message.get("reply_to_whatsapp_message_id") or None
        body = str(message.get("body") or "")
        try:
            if message_type == "text":
                response = await whatsapp_client.send_text(
                    to=str(ticket["whatsapp_phone"]), body=body, client_message_id=client_message_id,
                    phone_number_id=line_id, context_message_id=reply_whatsapp_id,
                )
            elif message_type == "location":
                response = await whatsapp_client.send_location(
                    to=str(ticket["whatsapp_phone"]),
                    latitude=float(message["location_latitude"]), longitude=float(message["location_longitude"]),
                    name=message.get("location_name"), address=message.get("location_address"),
                    client_message_id=client_message_id, phone_number_id=line_id,
                    context_message_id=reply_whatsapp_id,
                )
            else:
                assert asset is not None
                content = asset.path.read_bytes()
                default_bodies = {"image": {"Image"}, "video": {"Video"}, "document": {asset.filename, "Document"}}
                caption = body if message_type in default_bodies and body not in default_bodies[message_type] else None
                media_id = await whatsapp_client.upload_media(
                    content=content, mime_type=asset.mime_type, filename=asset.filename, phone_number_id=line_id,
                )
                response = await whatsapp_client.send_media(
                    to=str(ticket["whatsapp_phone"]), media_type=message_type, media_id=media_id,
                    client_message_id=client_message_id, caption=caption,
                    voice=(message_type == "audio" and body == "Voice note"),
                    filename=(asset.filename if message_type == "document" else None),
                    context_message_id=reply_whatsapp_id, phone_number_id=line_id,
                )
        except Exception as exc:
            await support_repository.update_message_send_state(
                int(message_id), send_status="FAILED",
                error_message=sanitize_support_text(str(exc))[:500] or "WhatsApp send failed.",
            )
            failed = await support_repository.get_message(int(message_id))
            if failed and failed.get("send_status") == "SENT":
                return _serialize_support_messages(ticket_id, [failed])[0] or {}
            if failed:
                await publish_support_event(
                    "outbound_message", ticket_id=ticket_id, message_id=int(message_id),
                    message=_serialize_support_messages(ticket_id, [failed])[0],
                )
            logger.warning("Agent WhatsApp resend failed for ticket_id=%s type=%s", ticket_id, message_type)
            raise SupportSendError("WhatsApp could not send the message again.") from exc

        whatsapp_message_id = None
        messages = response.get("messages") if isinstance(response, dict) else None
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            whatsapp_message_id = messages[0].get("id")
        try:
            await support_repository.update_message_send_state(
                int(message_id), send_status="SENT",
                whatsapp_message_id=str(whatsapp_message_id) if whatsapp_message_id else None,
                error_message=None,
            )
        except SupportConflictError:
            # The earlier attempt was confirmed meanwhile; the message is sent.
            logger.info("Resent message was already confirmed by an earlier attempt: ticket_id=%s", ticket_id)
        if ticket.get("status") == "ASSIGNED":
            try:
                await support_repository.mark_agent_reply_started(ticket_id, agent_id=agent.id, agent_name=agent.name)
            except Exception:
                logger.exception("Message resent but automatic IN_PROGRESS transition failed: ticket_id=%s", ticket_id)
        updated = await support_repository.get_message(int(message_id))
        serialized = _serialize_support_messages(ticket_id, [updated])[0] if updated else {}
        if updated:
            await publish_support_event(
                "outbound_message", ticket_id=ticket_id, message_id=int(message_id), message=serialized,
            )
        return serialized or {}

    async def route_whatsapp_reaction(
        self,
        *,
        from_number: str,
        target_whatsapp_message_id: str | None,
        emoji: str | None,
        provider_reaction_id: str | None,
        provider_timestamp: str | None,
        business_phone_number_id: str | None,
    ) -> dict[str, Any] | None:
        """Apply a customer reaction to its original support timeline message."""
        target_id = str(target_whatsapp_message_id or "").strip()
        if not target_id:
            return None
        target = await support_repository.get_message_by_whatsapp_id(target_id)
        if not target:
            return None
        ticket = await support_repository.get_ticket(int(target["ticket_id"]))
        if not ticket:
            return None
        if normalize_phone(str(ticket.get("whatsapp_phone") or "")) != normalize_phone(
            from_number
        ):
            logger.warning(
                "Ignored WhatsApp reaction whose sender did not match the target ticket: ticket_id=%s",
                ticket.get("id"),
            )
            return None
        ticket_line = str(ticket.get("business_phone_number_id") or "").strip()
        inbound_line = str(business_phone_number_id or "").strip()
        if ticket_line and inbound_line and ticket_line != inbound_line:
            logger.warning(
                "Ignored WhatsApp reaction whose business line did not match the target ticket: ticket_id=%s",
                ticket.get("id"),
            )
            return None

        rendered_emoji = str(emoji or "").strip()
        if len(rendered_emoji) > 64 or any(ord(char) < 32 for char in rendered_emoji):
            return None
        try:
            timestamp = int(str(provider_timestamp or "").strip())
        except (TypeError, ValueError):
            timestamp = None
        reaction = await support_repository.upsert_message_reaction(
            ticket_id=int(ticket["id"]),
            message_id=int(target["id"]),
            target_whatsapp_message_id=target_id,
            reactor_key=f"customer:{normalize_phone(from_number)}",
            reactor_type="CUSTOMER",
            reactor_id=normalize_phone(from_number),
            reactor_name=(str(ticket.get("customer_name")) if ticket.get("customer_name") else None),
            emoji=rendered_emoji,
            provider_reaction_id=provider_reaction_id,
            provider_timestamp=timestamp,
        )
        # A stale out-of-order event leaves the newer provider ID in place. Do not
        # emit a notification for an event that did not become current state.
        if not provider_reaction_id or str(reaction.get("provider_reaction_id") or "") == str(
            provider_reaction_id
        ):
            await publish_support_event(
                "message_reaction",
                ticket_id=int(ticket["id"]),
                message_id=int(target["id"]),
                customer_name=ticket.get("customer_name"),
                customer_phone=ticket.get("whatsapp_phone"),
            )
        return _normalize_row(reaction)

    async def capture_inbound_media(
        self,
        *,
        ticket: dict[str, Any],
        message: dict[str, Any],
        media_id: str,
        message_type: str,
        expected_mime_type: str | None = None,
        filename: str | None = None,
    ) -> StoredSupportMedia:
        ticket_id = int(ticket["id"])
        message_id = int(message["id"])
        existing = support_media_store.get(ticket_id, message_id)
        if existing is not None:
            return existing

        if message_type not in SUPPORT_MEDIA_MESSAGE_TYPES:
            raise SupportMediaError("Unsupported inbound media type.")

        # Persist the provider reference before the network download. If Meta is
        # temporarily unavailable (or a local media file is later lost), the
        # authenticated media endpoint can retry the same attachment instead of
        # leaving a permanent placeholder in the staff UI. The database reference
        # is durable across backend instances; the protected local reference keeps
        # deployments backward-compatible before the recovery migration is applied.
        try:
            await support_repository.set_message_media_reference(
                message_id,
                provider_media_id=str(media_id),
                provider_media_mime_type=(
                    str(expected_mime_type).strip() if expected_mime_type else None
                ),
                provider_media_filename=(
                    sanitize_support_text(filename).strip()[:255] if filename else None
                ),
            )
        except Exception:
            logger.exception(
                "Could not persist durable WhatsApp media reference: ticket_id=%s message_id=%s",
                ticket_id,
                message_id,
            )

        support_media_store.save_remote_reference(
            ticket_id=ticket_id,
            message_id=message_id,
            media_id=media_id,
            message_type=message_type,
            expected_mime_type=expected_mime_type,
            business_phone_number_id=(
                str(ticket.get("business_phone_number_id"))
                if ticket.get("business_phone_number_id")
                else None
            ),
            filename=filename,
        )
        try:
            downloaded = await whatsapp_client.download_media(
                media_id,
                phone_number_id=(
                    str(ticket.get("business_phone_number_id"))
                    if ticket.get("business_phone_number_id")
                    else None
                ),
            )
        except WhatsAppMediaError as exc:
            raise SupportMediaError(str(exc), retryable=exc.retryable) from exc

        downloaded_mime = str(downloaded.mime_type or "").strip()
        expected_mime = str(expected_mime_type or "").strip()
        downloaded_kind = media_kind_for_mime(downloaded_mime)
        expected_kind = media_kind_for_mime(expected_mime)
        if downloaded_kind == message_type:
            raw_mime_type = downloaded_mime
        elif expected_kind == message_type:
            # Meta occasionally omits or generalizes the content type on the
            # download response even though the signed webhook metadata carries
            # the concrete supported type. Use that trusted webhook metadata as
            # the validation fallback rather than discarding otherwise valid media.
            raw_mime_type = expected_mime
        else:
            raise SupportMediaError("WhatsApp returned an unsupported media format.")
        mime_type = normalize_mime_type(raw_mime_type)
        limit = max_media_bytes(message_type)
        if len(downloaded.content) > limit:
            raise SupportMediaError(
                f"Inbound {message_type} exceeded the configured {limit // (1024 * 1024)} MB limit."
            )
        stored = support_media_store.save(
            ticket_id=ticket_id,
            message_id=message_id,
            content=downloaded.content,
            mime_type=raw_mime_type or mime_type,
            filename=(
                filename
                or (
                    "customer-image"
                    if message_type == "image"
                    else "customer-video"
                    if message_type == "video"
                    else "customer-sticker.webp"
                    if message_type == "sticker"
                    else "customer-voice-note"
                    if message_type == "audio"
                    else "customer-document.pdf"
                )
            ),
        )
        await publish_support_event(
            "message_media_ready", ticket_id=ticket_id, message_id=message_id,
            message=_serialize_support_messages(ticket_id, [message])[0],
        )
        return stored

    async def get_ticket_media(
        self,
        ticket_id: int,
        message_id: int,
        agent: SupportAgent,
        *,
        context_ticket_id: int | None = None,
    ) -> StoredSupportMedia:
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        allowed = await _can_view_ticket(ticket, agent)
        if not allowed and context_ticket_id and int(context_ticket_id) != int(ticket_id):
            context_ticket = await support_repository.get_ticket(int(context_ticket_id))
            allowed = bool(
                context_ticket
                and await _can_view_ticket(context_ticket, agent)
                and await _historical_ticket_accessible(ticket, context_ticket, agent)
            )
        if not allowed:
            raise SupportForbiddenError(
                "You can only view media from conversations available to you."
            )
        message = await support_repository.get_message(message_id)
        if not message or int(message.get("ticket_id") or 0) != int(ticket_id):
            raise SupportNotFoundError("Media message not found.")
        if message.get("deleted_for_everyone_at") or await support_repository.message_hidden_for_user(message_id, agent.id):
            raise SupportNotFoundError("Media message not found.")
        asset = support_media_store.get(ticket_id, message_id)
        if asset is not None:
            return asset

        remote = support_media_store.get_remote_reference(ticket_id, message_id)
        provider_media_id = str(message.get("provider_media_id") or "").strip()
        media_type = str(message.get("message_type") or "").strip()
        expected_mime_type = (
            str(message.get("provider_media_mime_type") or "").strip() or None
        )
        if remote is not None:
            provider_media_id = remote.media_id
            media_type = remote.message_type
            expected_mime_type = remote.expected_mime_type or expected_mime_type
        if not provider_media_id or media_type not in SUPPORT_MEDIA_MESSAGE_TYPES:
            raise SupportNotFoundError("Media could not be recovered for this message.")

        # Lazy recovery is intentionally authenticated through this method. It is
        # useful when the first webhook-time download failed transiently and also
        # prevents a one-off storage miss from permanently hiding media from staff.
        try:
            return await self.capture_inbound_media(
                ticket=ticket,
                message=message,
                media_id=provider_media_id,
                message_type=media_type,
                expected_mime_type=expected_mime_type,
                filename=(
                    str(message.get("provider_media_filename") or "").strip()
                    or (remote.filename if remote is not None else None)
                ),
            )
        except SupportMediaError:
            raise
        except Exception as exc:
            raise SupportMediaError(
                "Media could not be recovered from WhatsApp.", retryable=True
            ) from exc

    async def save_contact(
        self,
        ticket_id: int,
        *,
        agent: SupportAgent,
        display_name: str,
    ) -> dict[str, Any]:
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        if not _can_operate(ticket, agent):
            raise SupportForbiddenError(
                "Only the assigned agent or a supervisor can save this contact."
            )
        name = sanitize_support_text(display_name).strip()
        if not name:
            raise SupportValidationError("Contact name is required.")
        if len(name) > 191:
            raise SupportValidationError("Contact name is too long.")
        try:
            identity = await customer_directory.save_contact(
                phone=str(ticket.get("whatsapp_phone") or ""),
                display_name=name,
                staff_id=agent.id,
            )
        except ValueError as exc:
            raise SupportValidationError(str(exc)) from exc
        await support_repository.update_customer_name_for_phone(
            str(ticket.get("whatsapp_phone") or ""), identity.name or name
        )
        await publish_support_event(
            "contact_saved", staff_id=agent.id, customer_phone=identity.phone, customer_name=identity.name or name
        )
        return {
            "source": identity.source,
            "name": identity.name,
            "phone": identity.phone,
        }

    async def edit_contact(
        self,
        *,
        agent: SupportAgent,
        contact_key: str,
        display_name: str,
        phone: str,
    ) -> dict[str, Any]:
        """Edit any contact from the Contacts list (saved, Google, or VCF)."""
        name = sanitize_support_text(display_name).strip()
        if not name:
            raise SupportValidationError("Contact name is required.")
        if len(name) > 191:
            raise SupportValidationError("Contact name is too long.")
        try:
            normalized = to_whatsapp_digits(phone)
        except PhoneNumberError as exc:
            raise SupportValidationError(str(exc)) from exc
        try:
            contact = await customer_directory.edit_contact(
                contact_key=contact_key, display_name=name, phone=normalized, staff_id=agent.id,
            )
        except ContactNotFoundError as exc:
            raise SupportNotFoundError(str(exc)) from exc
        except ContactConflictError as exc:
            raise SupportConflictError(str(exc)) from exc
        except ValueError as exc:
            raise SupportValidationError(str(exc)) from exc
        await support_repository.update_customer_name_for_phone(contact["normalized_phone"], contact["display_name"])
        logger.info(
            "Support contact edited: staff_id=%s source=%s phone_ref=%s",
            agent.id, str(contact_key).partition(":")[0], hashlib.sha256(normalized.encode()).hexdigest()[:12],
        )
        await publish_support_event(
            "contact_saved", staff_id=agent.id, customer_phone=contact["normalized_phone"], customer_name=contact["display_name"]
        )
        return contact

    async def list_chat_lines(self, agent: SupportAgent) -> list[dict[str, Any]]:
        return await whatsapp_registry_service.list_enabled_numbers(agent)

    async def _whatsapp_number_for_line(
        self, raw_phone: str, *, line_id: str, line_display_phone: str | None
    ) -> str:
        """International digits for a staff-entered number on one WhatsApp line.

        Local numbers are read in the line's country. On linked-device lines,
        WhatsApp is asked whether the number has an account, so a chat is never
        started with a number that cannot receive it.
        """
        try:
            phone = to_whatsapp_digits(raw_phone, default_region=region_for_number(line_display_phone))
        except PhoneNumberError as exc:
            raise SupportValidationError(str(exc)) from exc
        if get_transport(line_id) != "linked_device":
            return phone
        try:
            result = await linked_device_client.check_number(phone, phone_number_id=line_id)
        except Exception as exc:
            # The lookup is a safety check; an unavailable lookup must not block sending.
            logger.warning(
                "WhatsApp number lookup unavailable: line_id=%s exception_type=%s", line_id, type(exc).__name__
            )
            return phone
        if not result["exists"]:
            raise SupportValidationError(
                "This number is not on WhatsApp. Check the number and its country code."
            )
        return result["whatsapp_phone"]

    async def start_chat(
        self,
        *,
        agent: SupportAgent,
        phone_number: str,
        business_phone_number_id: str,
        body: str,
        customer_name: str | None = None,
    ) -> dict[str, Any]:
        """Start or reopen a staff-owned chat thread.

        Staff-origin chats are deliberately not given ticket lifecycle semantics.
        Re-contacting the same customer on the same WhatsApp line reuses the
        latest chat thread. Shared customer history remains visible to staff
        even when a different agent handled an earlier conversation.
        """
        line_id = str(business_phone_number_id or "").strip()
        if not line_id or not await whatsapp_registry_service.is_number_enabled(line_id):
            raise SupportValidationError("Choose an enabled WhatsApp line.")
        message_body = sanitize_support_text(body).strip()
        if not message_body:
            raise SupportValidationError("Enter the first message for this chat.")
        if len(message_body) > 4096:
            raise SupportValidationError("WhatsApp messages must be 4096 characters or fewer.")
        line_rows = await whatsapp_registry_service.list_enabled_numbers(agent)
        line = next((item for item in line_rows if str(item.get("phone_number_id")) == line_id), None)
        display = (line or {}).get("display_phone_number")
        phone = await self._whatsapp_number_for_line(phone_number, line_id=line_id, line_display_phone=display)

        identity = await customer_directory.resolve(phone)
        name = str(customer_name or "").strip() or identity.name

        reopened = False
        existing_active = await support_repository.find_active_ticket_by_phone(phone, line_id)
        if existing_active:
            if str(existing_active.get("conversation_origin") or "CUSTOMER") != "STAFF":
                raise SupportConflictError(
                    "This customer already has an active support ticket on that WhatsApp line."
                )
            if not _is_assignee(existing_active, agent):
                raise SupportConflictError(
                    "This chat is currently active with another staff member."
                )
            chat = existing_active
        else:
            previous = await support_repository.find_latest_staff_chat_by_phone(phone, line_id)
            if previous:
                chat, reopened = await support_repository.reopen_staff_chat(
                    int(previous["id"]),
                    agent_id=agent.id,
                    agent_name=agent.name,
                )
                if name and name != chat.get("customer_name"):
                    chat = await support_repository.update_customer_identity(
                        int(chat["id"]),
                        customer_name=name,
                        customer_id=(identity.user_id if identity.source == "registered_user" else None),
                        customer_userid=(identity.userid if identity.source == "registered_user" else None),
                    )
            else:
                chat = await support_repository.create_staff_chat(
                    ticket_reference=_chat_reference(),
                    phone_number=phone,
                    business_phone_number_id=line_id,
                    business_display_phone_number=str(display) if display else None,
                    customer_id=(identity.user_id if identity.source == "registered_user" else None),
                    customer_userid=(identity.userid if identity.source == "registered_user" else None),
                    customer_name=name,
                    agent_id=agent.id,
                    agent_name=agent.name,
                )

        try:
            message = await self.send_agent_message(
                int(chat["id"]),
                agent=agent,
                body=message_body,
                client_message_id=f"chat-start:{uuid.uuid4()}",
                reply_to_message_id=None,
            )
        except Exception:
            logger.exception(
                "Staff chat %s but WhatsApp send failed: chat_id=%s",
                "reopened" if reopened else "started",
                chat.get("id"),
            )
            raise
        await publish_support_event(
            "staff_chat_reopened" if reopened else "staff_chat_started",
            ticket_id=int(chat["id"]),
            customer_name=chat.get("customer_name"),
            customer_phone=chat.get("whatsapp_phone"),
            message_id=int(message["id"]),
        )
        safe_chat = _safe_ticket_row(chat)
        return {
            "chat": safe_chat,
            # Compatibility for older frontends during rolling deployment.
            "ticket": safe_chat,
            "message": message,
            "reopened": reopened,
        }

    async def close_staff_chat(
        self,
        chat_id: int,
        *,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        chat = await support_repository.get_ticket(chat_id)
        if not chat or str(chat.get("conversation_origin") or "") != "STAFF":
            raise SupportNotFoundError("Chat not found.")
        if not _can_operate(chat, agent):
            raise SupportForbiddenError("Only the assigned staff member or a supervisor can close this chat.")

        closed, changed = await support_repository.close_staff_chat(
            chat_id,
            agent_id=agent.id,
            agent_name=agent.name,
            expected_assignee_id=(
                str(chat.get("current_assignee_id"))
                if chat.get("current_assignee_id") is not None
                else None
            ),
        )

        # Chat closure only releases human ownership. It does not send the support
        # ticket resolution notice and does not create ticket/return-to-AI audit
        # events. The next inbound customer message is evaluated normally.
        try:
            conversation = await conversation_store.get_or_create(
                str(closed["whatsapp_phone"]),
                conversation_key=_conversation_key_for_ticket(closed),
                business_phone_number_id=(
                    str(closed.get("business_phone_number_id"))
                    if closed.get("business_phone_number_id")
                    else None
                ),
                business_display_phone_number=(
                    str(closed.get("business_display_phone_number"))
                    if closed.get("business_display_phone_number")
                    else None
                ),
            )
            conversation.mode = "ai"
            conversation.active_ticket_id = None
            conversation.messages.append({
                "role": "system",
                "content": (
                    "SUPPORT EVENT: A staff-initiated chat was closed. The prior "
                    "staff conversation is historical; evaluate any future customer "
                    "message normally under the current AI/handoff rules."
                ),
            })
            await conversation_store.save(conversation)
        except Exception:
            logger.exception(
                "Staff chat closed but Redis conversation cache could not be updated; it will self-heal on next inbound message"
            )

        logger.info("Staff chat closed: chat_id=%s staff_id=%s changed=%s", chat_id, agent.id, changed)
        return {"chat": _normalize_row(closed), "changed": changed}

    async def forward_message(
        self,
        *,
        source_ticket_id: int,
        source_message_id: int,
        destination_ticket_id: int,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        source_ticket = await support_repository.get_ticket(source_ticket_id)
        if not source_ticket:
            raise SupportNotFoundError("Source conversation not found.")
        if not await _can_view_ticket(source_ticket, agent):
            raise SupportForbiddenError("You cannot forward messages from this conversation.")
        source = await support_repository.get_message(source_message_id)
        if not source or int(source.get("ticket_id") or 0) != int(source_ticket_id) or source.get("deleted_for_everyone_at") or await support_repository.message_hidden_for_user(source_message_id, agent.id):
            raise SupportNotFoundError("Message not found.")

        destination = await support_repository.get_ticket(destination_ticket_id)
        if not destination:
            raise SupportNotFoundError("Destination conversation not found.")
        if not (
            _is_assignee(destination, agent)
            and destination.get("active_key") is not None
            and destination.get("status") in ACTIVE_TICKET_STATUSES
        ):
            raise SupportForbiddenError("You can only forward to an active conversation assigned to you.")
        if int(source_ticket_id) == int(destination_ticket_id):
            raise SupportValidationError("Choose a different destination conversation.")

        kind = str(source.get("message_type") or "text").lower()
        client_id = f"forward:{source_message_id}:{uuid.uuid4()}"
        if kind == "location":
            if source.get("location_latitude") is None or source.get("location_longitude") is None:
                raise SupportValidationError("This location message cannot be forwarded.")
            forwarded = await self.send_agent_location(
                destination_ticket_id,
                agent=agent,
                latitude=float(source["location_latitude"]),
                longitude=float(source["location_longitude"]),
                name=source.get("location_name"),
                address=source.get("location_address"),
                client_message_id=client_id,
                reply_to_message_id=None,
                is_forwarded=True,
                forwarded_from_ticket_id=source_ticket_id,
                forwarded_from_message_id=source_message_id,
            )
        elif kind in SUPPORT_MEDIA_MESSAGE_TYPES:
            asset = await self.get_ticket_media(source_ticket_id, source_message_id, agent)
            content = await asyncio.to_thread(asset.path.read_bytes)
            body = str(source.get("body") or "").strip()
            caption = "" if body in {"Image", "Video", "Sticker", "Voice note", "Audio message", "PDF document"} else body
            forwarded = await self.send_agent_media(
                destination_ticket_id,
                agent=agent,
                media_type=kind,
                content=content,
                content_type=asset.mime_type,
                filename=asset.filename,
                caption=caption,
                client_message_id=client_id,
                reply_to_message_id=None,
                is_forwarded=True,
                forwarded_from_ticket_id=source_ticket_id,
                forwarded_from_message_id=source_message_id,
            )
        else:
            body = str(source.get("body") or "").strip()
            if not body:
                raise SupportValidationError("This message cannot be forwarded.")
            forwarded = await self.send_agent_message(
                destination_ticket_id,
                agent=agent,
                body=body,
                client_message_id=client_id,
                reply_to_message_id=None,
                is_forwarded=True,
                forwarded_from_ticket_id=source_ticket_id,
                forwarded_from_message_id=source_message_id,
            )
        try:
            await support_repository.add_event(
                destination_ticket_id,
                event_type="MESSAGE_FORWARDED",
                actor_type="AGENT",
                actor_id=agent.id,
                actor_name=agent.name,
                details={"source_ticket_id": source_ticket_id, "source_message_id": source_message_id},
            )
        except Exception:
            logger.exception("Forwarded message sent but audit event failed: destination_ticket_id=%s", destination_ticket_id)
        return _normalize_row(forwarded) or {}

    async def queue_counts(self, agent: SupportAgent) -> dict[str, int]:
        views = ("unassigned", "mine", "chats", "watching", "active", "waiting", "history")
        if not agent.is_supervisor:
            views = tuple(view for view in views if view != "active")
        results = await asyncio.gather(*(
            support_repository.list_tickets(
                view=view,
                agent_id=agent.id,
                can_view_all=agent.is_supervisor,
                page=1,
                page_size=1,
            )
            for view in views
        ), return_exceptions=True)
        counts: dict[str, int] = {}
        for view, result in zip(views, results):
            if isinstance(result, Exception):
                logger.warning("Could not refresh queue count: view=%s exception_type=%s", view, type(result).__name__)
                continue
            counts[view] = int(result.get("total") or 0)
        try:
            counts["contacts"] = await google_contacts_service.count_contacts()
        except Exception as exc:
            logger.warning("Could not refresh contacts count: exception_type=%s", type(exc).__name__)
        return counts

    async def forward_message_to_contact(
        self,
        *,
        source_ticket_id: int,
        source_message_id: int,
        phone_number: str,
        customer_name: str | None,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        source_ticket = await support_repository.get_ticket(source_ticket_id)
        if not source_ticket:
            raise SupportNotFoundError("Source conversation not found.")
        if not await _can_view_ticket(source_ticket, agent):
            raise SupportForbiddenError("You cannot forward messages from this conversation.")
        source = await support_repository.get_message(source_message_id)
        if not source or int(source.get("ticket_id") or 0) != int(source_ticket_id) or source.get("deleted_for_everyone_at") or await support_repository.message_hidden_for_user(source_message_id, agent.id):
            raise SupportNotFoundError("Message not found.")

        line_id = str(source_ticket.get("business_phone_number_id") or "").strip()
        if not line_id:
            raise SupportValidationError("The source conversation does not have a WhatsApp business line.")
        if not await whatsapp_registry_service.is_number_enabled(line_id):
            raise SupportConflictError("The source WhatsApp business line is disabled.")
        phone = await self._whatsapp_number_for_line(
            phone_number, line_id=line_id,
            line_display_phone=str(source_ticket.get("business_display_phone_number") or "") or None,
        )
        if phone == normalize_phone(str(source_ticket.get("whatsapp_phone") or "")):
            raise SupportValidationError("Choose a different contact to forward this message to.")

        destination = await support_repository.find_active_ticket_by_phone(phone, line_id)
        reopened = False
        if destination:
            if not (
                _is_assignee(destination, agent)
                and destination.get("active_key") is not None
                and destination.get("status") in ACTIVE_TICKET_STATUSES
            ):
                raise SupportForbiddenError(
                    "That contact already has an active conversation owned by another staff member."
                )
        else:
            identity = await customer_directory.resolve(phone)
            name = str(customer_name or "").strip() or identity.name
            previous = await support_repository.find_latest_staff_chat_by_phone(phone, line_id)
            if previous:
                destination, reopened = await support_repository.reopen_staff_chat(
                    int(previous["id"]),
                    agent_id=agent.id,
                    agent_name=agent.name,
                )
                if name and name != destination.get("customer_name"):
                    destination = await support_repository.update_customer_identity(
                        int(destination["id"]),
                        customer_name=name,
                        customer_id=(identity.user_id if identity.source == "registered_user" else None),
                        customer_userid=(identity.userid if identity.source == "registered_user" else None),
                    )
            else:
                line_rows = await whatsapp_registry_service.list_enabled_numbers(agent)
                line = next((item for item in line_rows if str(item.get("phone_number_id")) == line_id), None)
                destination = await support_repository.create_staff_chat(
                    ticket_reference=_chat_reference(),
                    phone_number=phone,
                    business_phone_number_id=line_id,
                    business_display_phone_number=(line or {}).get("display_phone_number"),
                    customer_id=(identity.user_id if identity.source == "registered_user" else None),
                    customer_userid=(identity.userid if identity.source == "registered_user" else None),
                    customer_name=name,
                    agent_id=agent.id,
                    agent_name=agent.name,
                )

        message = await self.forward_message(
            source_ticket_id=source_ticket_id,
            source_message_id=source_message_id,
            destination_ticket_id=int(destination["id"]),
            agent=agent,
        )
        await publish_support_event(
            "staff_chat_reopened" if reopened else "staff_chat_updated",
            ticket_id=int(destination["id"]),
            customer_name=destination.get("customer_name"),
            customer_phone=destination.get("whatsapp_phone"),
            message_id=int(message["id"]),
        )
        return {
            "message": message,
            "destination": _safe_ticket_row(destination),
            "reopened": reopened,
        }

    async def list_tickets(
        self,
        *,
        view: str,
        agent: SupportAgent,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        if view == "active" and not agent.is_supervisor:
            # Active lists every live ticket for monitoring; agents work from their own queues.
            raise SupportForbiddenError("Only Supervisors and Admins can view all active tickets.")
        result = await support_repository.list_tickets(
            view=view,
            agent_id=agent.id,
            can_view_all=agent.is_supervisor,
            page=page,
            page_size=page_size,
        )
        result["items"] = [_safe_ticket_row(row) for row in result["items"]]
        return result

    async def search_messages(
        self,
        *,
        query: str,
        agent: SupportAgent,
        ticket_id: int | None = None,
        limit: int = 80,
    ) -> dict[str, Any]:
        normalized = sanitize_support_text(query).strip()
        if len(normalized) < 2:
            raise SupportValidationError("Enter at least 2 characters to search messages.")
        if len(normalized) > 200:
            raise SupportValidationError("Search text is too long.")

        if ticket_id is not None:
            ticket = await support_repository.get_ticket(ticket_id)
            if not ticket:
                raise SupportNotFoundError("Ticket not found.")
            if not await _can_view_ticket(ticket, agent):
                raise SupportForbiddenError(
                    "You cannot search this conversation."
                )

        rows = await support_repository.search_messages(
            query=normalized,
            agent_id=agent.id,
            can_view_all=agent.is_supervisor,
            ticket_id=ticket_id,
            limit=limit,
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _normalize_row(row) or {}
            body = sanitize_support_text(str(item.get("body") or "")).strip()
            location_name = sanitize_support_text(str(item.get("location_name") or "")).strip()
            location_address = sanitize_support_text(str(item.get("location_address") or "")).strip()
            filename = sanitize_support_text(str(item.get("provider_media_filename") or "")).strip()
            message_type = str(item.get("message_type") or "text").lower()
            preview = body
            if not preview and message_type == "location":
                preview = location_name or location_address or "Shared location"
            elif not preview and message_type == "document":
                preview = filename or "PDF document"
            elif not preview and message_type == "image":
                preview = "Image"
            elif not preview and message_type == "video":
                preview = "Video"
            elif not preview and message_type == "sticker":
                preview = "Sticker"
            elif not preview and message_type == "audio":
                preview = "Voice note"
            item["preview"] = preview[:700]
            item.pop("body", None)
            item.pop("location_address", None)
            items.append(item)
        return {"query": normalized, "items": items, "count": len(items)}

    async def _history_page_for_ticket(
        self,
        current: dict[str, Any],
        agent: SupportAgent,
        *,
        before_ticket_id: int | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Load shared customer history across all configured business lines."""
        limit = max(1, min(int(limit), 20))
        selected: list[dict[str, Any]] = []
        cursor = before_ticket_id
        while len(selected) <= limit:
            candidates = await support_repository.list_previous_customer_tickets(
                current_ticket_id=int(current["id"]),
                phone_number=str(current.get("whatsapp_phone") or ""),
                before_ticket_id=cursor,
                limit=100,
            )
            if not candidates:
                break
            for previous in candidates:
                cursor = int(previous["id"])
                if await _historical_ticket_accessible(previous, current, agent):
                    selected.append(previous)
                    if len(selected) > limit:
                        break
            if len(selected) > limit or len(candidates) < 100:
                break

        shown = list(reversed(selected[:limit]))
        # Load the earlier tickets' messages concurrently; the order of the
        # returned pages matches ``shown``.
        pages = await _gather_or_cancel(*(
            support_repository.list_messages(int(previous["id"]), limit=101, user_id=agent.id)
            for previous in shown
        ))
        history: list[dict[str, Any]] = []
        for previous, page in zip(shown, pages):
            previous_id = int(previous["id"])
            history.append({
                "ticket": _history_ticket_summary(previous),
                "context": _decode_context(previous),
                "messages": _serialize_support_messages(
                    previous_id, page[-100:], media_context_ticket_id=int(current["id"]),
                ),
                "messages_has_more": len(page) > 100,
            })
        return {
            "previous_ticket_history": history,
            "previous_ticket_history_has_more": len(selected) > limit,
        }

    async def history_page(
        self,
        ticket_id: int,
        agent: SupportAgent,
        *,
        before_ticket_id: int | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        current = await support_repository.get_ticket(ticket_id)
        if not current:
            raise SupportNotFoundError("Ticket not found.")
        if not await _can_view_ticket(current, agent):
            raise SupportForbiddenError("You can only view conversations available to you.")
        return await self._history_page_for_ticket(
            current, agent, before_ticket_id=before_ticket_id, limit=limit,
        )

    async def history_messages_page(
        self,
        ticket_id: int,
        history_ticket_id: int,
        agent: SupportAgent,
        *,
        before_message_id: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        current = await support_repository.get_ticket(ticket_id)
        if not current:
            raise SupportNotFoundError("Ticket not found.")
        if not await _can_view_ticket(current, agent):
            raise SupportForbiddenError("You can only view conversations available to you.")
        previous = await support_repository.get_ticket(history_ticket_id)
        if not previous or not await _historical_ticket_accessible(previous, current, agent):
            raise SupportNotFoundError("Previous conversation not found.")
        limit = max(1, min(int(limit), 100))
        page = await support_repository.list_messages(
            history_ticket_id,
            before_id=before_message_id,
            limit=limit + 1,
            user_id=agent.id,
        )
        return {
            "messages": _serialize_support_messages(
                history_ticket_id, page[-limit:], media_context_ticket_id=ticket_id,
            ),
            "messages_has_more": len(page) > limit,
        }

    async def messages_page(
        self,
        ticket_id: int,
        agent: SupportAgent,
        *,
        before_message_id: int | None = None,
        limit: int = 150,
    ) -> dict[str, Any]:
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        if not await _can_view_ticket(ticket, agent):
            raise SupportForbiddenError("You can only view conversations available to you.")
        limit = max(1, min(int(limit), 150))
        page = await support_repository.list_messages(
            ticket_id, before_id=before_message_id, limit=limit + 1, user_id=agent.id,
        )
        return {
            "messages": _serialize_support_messages(ticket_id, page[-limit:]),
            "messages_has_more": len(page) > limit,
        }

    async def ticket_detail(
        self,
        ticket_id: int,
        agent: SupportAgent,
        *,
        around_message_id: int | None = None,
        include_previous_history: bool = False,
    ) -> dict[str, Any]:
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        if not await _can_view_ticket(ticket, agent):
            raise SupportForbiddenError(
                "You cannot view this conversation."
            )

        # Every lookup below is a database (or shipment API) round trip. They are
        # independent of each other, so run them concurrently: opening a
        # conversation then waits for the slowest branch, not for their sum.
        # Shipment tracking still runs after identity enrichment because the
        # enrichment can supply the customer_id it needs.
        async def load_identity_and_shipment() -> tuple[Any, dict[str, Any], Any]:
            enriched = ticket
            identity = None
            try:
                identity = await customer_directory.resolve(str(enriched.get("whatsapp_phone") or ""))
                if identity.name and not enriched.get("customer_name"):
                    enriched = await support_repository.update_customer_identity(
                        ticket_id,
                        customer_name=identity.name,
                        customer_id=(identity.user_id if identity.source == "registered_user" else None),
                        customer_userid=(identity.userid if identity.source == "registered_user" else None),
                    )
            except Exception:
                logger.exception("Could not enrich ticket customer identity: ticket_id=%s", ticket_id)

            shipment = None
            if enriched.get("customer_id") and enriched.get("tracking_reference"):
                try:
                    candidate = await shipment_client.track_shipment(
                        customer_id=str(enriched["customer_id"]),
                        tracking_number=str(enriched["tracking_reference"]),
                    )
                    if candidate.get("found") is True and candidate.get("authorized") is True:
                        shipment = candidate
                except Exception:
                    logger.exception(
                        "Could not enrich support ticket with shipment context: ticket_id=%s",
                        ticket_id,
                    )
            return identity, enriched, shipment

        async def load_messages() -> tuple[list[dict[str, Any]], bool]:
            if around_message_id is not None:
                around = await support_repository.list_messages_around(
                    ticket_id, around_message_id, before=70, after=70, user_id=agent.id
                )
                if not around:
                    raise SupportNotFoundError("Search result message not found.")
                return around, False
            page = await support_repository.list_messages(ticket_id, limit=151, user_id=agent.id)
            return page[-150:], len(page) > 150

        async def load_history() -> dict[str, Any]:
            if include_previous_history and str(ticket.get("whatsapp_phone") or "").strip():
                return await self._history_page_for_ticket(ticket, agent)
            return {"previous_ticket_history": [], "previous_ticket_history_has_more": False}

        (identity, ticket, shipment_context), (messages, messages_has_more), events, history = (
            await _gather_or_cancel(
                load_identity_and_shipment(),
                load_messages(),
                support_repository.list_events(ticket_id, limit=150),
                load_history(),
            )
        )

        safe_messages = _serialize_support_messages(ticket_id, messages)

        return {
            "ticket": _safe_ticket_row(ticket),
            "context": _decode_context(ticket),
            "shipment": shipment_context,
            "messages": safe_messages,
            "messages_has_more": messages_has_more,
            **history,
            "events": [_safe_context_value(_normalize_row(row)) for row in events],
            "customer_identity": {
                "source": identity.source if identity is not None else "guest",
                "name": identity.name if identity is not None else ticket.get("customer_name"),
                "registered_user": bool(identity is not None and identity.source == "registered_user"),
                "saved_contact": bool(identity is not None and identity.source == "saved_contact"),
                # Set for saved, Google and VCF contacts; any of them can be edited.
                "contact_key": getattr(identity, "contact_key", None),
                "account_name": getattr(identity, "account_name", None),
            },
            "permissions": {
                "can_operate": _can_operate(ticket, agent),
                # Viewing a ticket is the source authorization for forwarding.
                # Unlike replying, forwarding remains available for resolved/closed
                # history so staff can reuse prior messages as evidence.
                "can_forward": True,
                "can_reply": (
                    (
                        _is_assignee(ticket, agent)
                        and ticket.get("active_key") is not None
                        and ticket.get("status") in ACTIVE_TICKET_STATUSES
                    )
                    or (
                        str(ticket.get("conversation_origin") or "") == "STAFF"
                        and ticket.get("active_key") is None
                        and str(ticket.get("status") or "") in {"RESOLVED", "CLOSED"}
                    )
                ) and get_transport(str(ticket.get("business_phone_number_id") or "") or None) != "removed",
                "can_transfer": (
                    _can_operate(ticket, agent)
                    and ticket.get("current_assignee_id") is not None
                    and ticket.get("active_key") is not None
                    and ticket.get("status") in ACTIVE_TICKET_STATUSES
                ),
                "is_read_only_observer": (
                    ticket.get("active_key") is not None
                    and ticket.get("current_assignee_id") is not None
                    and not _is_assignee(ticket, agent)
                ),
                "can_claim": (
                    ticket.get("status") == "NEW"
                    and ticket.get("current_assignee_id") is None
                    and ticket.get("active_key") is not None
                ),
                "can_delete_history": (
                    agent.is_supervisor
                    and str(ticket.get("conversation_origin") or "CUSTOMER") != "STAFF"
                    and ticket.get("active_key") is None
                    and ticket.get("status") in {"RESOLVED", "CLOSED"}
                ),
                "can_delete_chat": (
                    agent.is_supervisor
                    and str(ticket.get("conversation_origin") or "") == "STAFF"
                    and ticket.get("active_key") is None
                    and ticket.get("status") in {"RESOLVED", "CLOSED"}
                ),
                "is_supervisor": agent.is_supervisor,
            },
        }

    async def delete_history_ticket(
        self,
        ticket_id: int,
        *,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        """Permanently remove a history ticket.

        Agents intentionally cannot perform destructive history cleanup. The
        repository independently re-checks resolved/closed state under a row
        lock before deleting messages/events/ticket data.
        """
        if not agent.is_supervisor:
            raise SupportForbiddenError(
                "Only Supervisors and Admins can delete history tickets."
            )

        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        if str(ticket.get("conversation_origin") or "CUSTOMER") == "STAFF":
            raise SupportConflictError("Staff chats are not history tickets and cannot be deleted from ticket history.")
        if (
            ticket.get("active_key") is not None
            or ticket.get("status") not in {"RESOLVED", "CLOSED"}
        ):
            raise SupportConflictError(
                "Only resolved or closed history tickets can be deleted."
            )

        deleted = await support_repository.delete_history_ticket(ticket_id)
        try:
            await asyncio.to_thread(support_media_store.delete_ticket_media, ticket_id)
        except Exception:
            # The database is authoritative. A local cache cleanup failure must
            # not resurrect a deleted ticket, but it is logged for maintenance.
            logger.exception(
                "Deleted history ticket but local media cleanup failed: ticket_id=%s",
                ticket_id,
            )

        logger.info(
            "Support history ticket deleted: ticket_id=%s ticket_reference=%s actor_staff_id=%s actor_role=%s",
            ticket_id,
            deleted.get("ticket_reference"),
            agent.id,
            agent.role,
        )
        return {
            "deleted": True,
            "ticket_id": int(ticket_id),
            "ticket_reference": deleted.get("ticket_reference"),
        }

    async def delete_staff_chat(
        self,
        chat_id: int,
        *,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        """Permanently remove a closed staff-initiated chat.

        Mirrors history ticket deletion: Supervisors and Admins only, and the
        chat must be closed. The repository re-checks both under a row lock.
        Closing already released the Redis conversation, so no cache cleanup
        is needed here. Messages already delivered to the customer's WhatsApp
        are not affected.
        """
        if not agent.is_supervisor:
            raise SupportForbiddenError("Only Supervisors and Admins can delete chats.")

        chat = await support_repository.get_ticket(chat_id)
        if not chat or str(chat.get("conversation_origin") or "") != "STAFF":
            raise SupportNotFoundError("Chat not found.")
        if chat.get("active_key") is not None or chat.get("status") not in {"RESOLVED", "CLOSED"}:
            raise SupportConflictError("Close this chat before deleting it.")

        deleted = await support_repository.delete_staff_chat(chat_id)
        try:
            await asyncio.to_thread(support_media_store.delete_ticket_media, chat_id)
        except Exception:
            logger.exception("Deleted staff chat but local media cleanup failed: chat_id=%s", chat_id)

        logger.info(
            "Staff chat deleted: chat_id=%s actor_staff_id=%s actor_role=%s",
            chat_id,
            agent.id,
            agent.role,
        )
        return {"deleted": True, "chat_id": int(chat_id)}

    async def transfer_targets(self, agent: SupportAgent) -> list[dict[str, Any]]:
        """Return the minimal active-staff directory needed for ticket transfers."""
        _ = agent
        rows = await staff_repository.list_public_staff(limit=500)
        targets: list[dict[str, Any]] = []
        for row in rows:
            if not bool(row.get("is_active")):
                continue
            role = str(row.get("role") or "")
            if role not in {"agent", "admin", "supervisor"}:
                continue
            targets.append(
                {
                    "id": str(row.get("id")),
                    "name": str(row.get("name") or "Support Agent"),
                    "role": role,
                }
            )
        return targets

    async def transfer(
        self,
        ticket_id: int,
        *,
        target_staff_id: int,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        is_chat = str(ticket.get("conversation_origin") or "CUSTOMER") == "STAFF"
        noun = "chat" if is_chat else "ticket"
        if (
            ticket.get("active_key") is None
            or ticket.get("status") not in ACTIVE_TICKET_STATUSES
        ):
            raise SupportConflictError(f"Cannot transfer a closed {noun}.")
        if ticket.get("current_assignee_id") is None:
            raise SupportConflictError(f"The {noun} has no current assignee.")
        if not _can_operate(ticket, agent):
            raise SupportForbiddenError(
                f"Only the assigned staff member or a supervisor can transfer this {noun}."
            )

        transferred = await support_repository.transfer_ticket(
            ticket_id,
            expected_assignee_id=str(ticket.get("current_assignee_id")),
            target_staff_id=int(target_staff_id),
            actor_id=agent.id,
            actor_name=agent.name,
        )
        logger.info(
            "Support conversation transferred: conversation_id=%s from_staff_id=%s to_staff_id=%s actor_staff_id=%s",
            ticket_id,
            ticket.get("current_assignee_id"),
            target_staff_id,
            agent.id,
        )
        return _normalize_row(transferred) or {}

    async def _send_system_customer_message(
        self,
        *,
        ticket: dict[str, Any],
        body: str,
        client_message_id: str,
        event_type: str,
        require_active: bool,
    ) -> tuple[dict[str, Any], bool]:
        """Persist a customer-visible lifecycle notice before sending it.

        The unique client_message_id is the database idempotency key. A retry can
        create a notice that was never persisted, but it never starts a second
        WhatsApp send for a notice row that already exists. This favors duplicate
        prevention when provider delivery acknowledgement is uncertain.
        """
        existing = await support_repository.get_message_by_client_id(client_message_id)
        if existing:
            return _normalize_row(existing) or {}, False

        message, created = await support_repository.add_ticket_message(
            ticket_id=int(ticket["id"]),
            sender_type="SYSTEM",
            sender_name="Customer Support",
            direction="OUTBOUND",
            body=body,
            client_message_id=client_message_id,
            message_type="text",
            send_status="PENDING",
            require_active=require_active,
        )
        if not created:
            return _normalize_row(message) or {}, False

        try:
            response = await whatsapp_client.send_text(
                to=str(ticket["whatsapp_phone"]),
                body=body,
                client_message_id=client_message_id,
                phone_number_id=(
                    str(ticket.get("business_phone_number_id"))
                    if ticket.get("business_phone_number_id")
                    else None
                ),
            )
        except Exception as exc:
            safe_error = sanitize_support_text(str(exc))[:500]
            await support_repository.update_message_send_state(
                int(message["id"]),
                send_status="FAILED",
                error_message=safe_error or "WhatsApp send failed.",
            )
            updated = await support_repository.get_message(int(message["id"]))
            if updated and updated.get("send_status") == "SENT":
                await support_repository.add_event(
                    int(ticket["id"]), event_type=event_type, actor_type="SYSTEM",
                    details={"message_id": message["id"]},
                )
                return _normalize_row(updated) or {}, True
            await support_repository.add_event(
                int(ticket["id"]),
                event_type=f"{event_type}_SEND_FAILED",
                actor_type="SYSTEM",
                details={"message_id": message["id"]},
            )
            logger.warning(
                "System customer notification send failed: ticket_id=%s event=%s",
                ticket["id"],
                event_type,
            )
            updated = await support_repository.get_message(int(message["id"]))
            return _normalize_row(updated) or {}, True

        whatsapp_message_id = None
        messages = response.get("messages") if isinstance(response, dict) else None
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            whatsapp_message_id = messages[0].get("id")
        await support_repository.update_message_send_state(
            int(message["id"]),
            send_status="SENT",
            whatsapp_message_id=str(whatsapp_message_id) if whatsapp_message_id else None,
            error_message=None,
        )
        await support_repository.add_event(
            int(ticket["id"]),
            event_type=event_type,
            actor_type="SYSTEM",
            details={"message_id": message["id"]},
        )
        updated = await support_repository.get_message(int(message["id"]))
        return _normalize_row(updated) or {}, True

    async def get_message_preferences(self, agent: SupportAgent) -> dict[str, Any]:
        row = await staff_repository.get_message_preferences(int(agent.id))
        normalized = _normalize_row(row) or {}
        return {
            "greeting_message": str(normalized.get("custom_greeting_message") or ""),
            "resolution_message": str(normalized.get("custom_resolution_message") or ""),
            "updated_at": normalized.get("updated_at"),
        }

    async def update_message_preferences(
        self,
        agent: SupportAgent,
        *,
        greeting_message: str | None,
        resolution_message: str | None,
    ) -> dict[str, Any]:
        greeting = _normalize_staff_message_preference(greeting_message)
        resolution = _normalize_staff_message_preference(resolution_message)
        row = await staff_repository.update_message_preferences(
            int(agent.id),
            greeting_message=greeting,
            resolution_message=resolution,
        )
        logger.info(
            "Support message preferences updated: staff_id=%s custom_greeting=%s custom_resolution=%s",
            agent.id,
            bool(greeting),
            bool(resolution),
        )
        normalized = _normalize_row(row) or {}
        return {
            "greeting_message": str(normalized.get("custom_greeting_message") or ""),
            "resolution_message": str(normalized.get("custom_resolution_message") or ""),
            "updated_at": normalized.get("updated_at"),
        }

    async def list_quick_replies(self, agent: SupportAgent) -> dict[str, Any]:
        rows = await support_repository.list_quick_replies(int(agent.id))
        return {"items": [_quick_reply_payload(row) for row in rows]}

    async def create_quick_reply(
        self, agent: SupportAgent, *, title: str | None, body: str | None
    ) -> dict[str, Any]:
        clean_title, clean_body = _normalize_quick_reply(title, body)
        row = await support_repository.create_quick_reply(int(agent.id), title=clean_title, body=clean_body)
        reply = _quick_reply_payload(row)
        logger.info("Support quick reply created: staff_id=%s quick_reply_id=%s", agent.id, reply["id"])
        return reply

    async def update_quick_reply(
        self, agent: SupportAgent, reply_id: int, *, title: str | None, body: str | None
    ) -> dict[str, Any]:
        clean_title, clean_body = _normalize_quick_reply(title, body)
        row = await support_repository.update_quick_reply(
            int(agent.id), int(reply_id), title=clean_title, body=clean_body
        )
        logger.info("Support quick reply updated: staff_id=%s quick_reply_id=%s", agent.id, reply_id)
        return _quick_reply_payload(row)

    async def delete_quick_reply(self, agent: SupportAgent, reply_id: int) -> dict[str, Any]:
        await support_repository.delete_quick_reply(int(agent.id), int(reply_id))
        logger.info("Support quick reply deleted: staff_id=%s quick_reply_id=%s", agent.id, reply_id)
        return {"deleted": True, "id": int(reply_id)}

    async def _automatic_message_for_staff(
        self,
        *,
        staff_id: str,
        preference_key: str,
    ) -> str | None:
        """Return a configured message, or None to leave the customer undisturbed."""
        try:
            row = await staff_repository.get_message_preferences(int(staff_id))
            return _normalize_staff_message_preference(row.get(preference_key))
        except Exception:
            logger.exception(
                "Could not load staff automatic message preference; skipping message: staff_id=%s preference=%s",
                staff_id,
                preference_key,
            )
            return None

    async def claim(self, ticket_id: int, agent: SupportAgent) -> dict[str, Any]:
        existing = await support_repository.get_ticket(ticket_id)
        if existing and str(existing.get("conversation_origin") or "CUSTOMER") == "STAFF":
            raise SupportValidationError("Staff chats are not claimable support tickets.")
        ticket, changed = await support_repository.claim_ticket(
            ticket_id,
            agent_id=agent.id,
            agent_name=agent.name,
        )
        greeting = await self._automatic_message_for_staff(
            staff_id=agent.id,
            preference_key="custom_greeting_message",
        )
        greeting_message, greeting_created = None, False
        if greeting:
            greeting_message, greeting_created = await self._send_system_customer_message(
                ticket=ticket,
                body=greeting,
                client_message_id=f"system:claim:{ticket_id}",
                event_type="CLAIM_GREETING_SENT",
                require_active=True,
            )
        logger.info(
            "Support ticket claimed: ticket_id=%s staff_id=%s changed=%s greeting_created=%s",
            ticket_id,
            agent.id,
            changed,
            greeting_created,
        )
        return {
            "ticket": _normalize_row(ticket),
            "changed": changed,
            "greeting_message": greeting_message,
        }

    async def update_status(
        self,
        ticket_id: int,
        *,
        new_status: str,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        if str(ticket.get("conversation_origin") or "CUSTOMER") == "STAFF":
            raise SupportValidationError("Staff chats do not use support-ticket status transitions.")
        if not _can_operate(ticket, agent):
            raise SupportForbiddenError("Only the assigned agent or a supervisor can update this ticket.")

        current = str(ticket["status"])
        if new_status == "RESOLVED":
            raise SupportValidationError("Use the resolve endpoint to return the conversation to AI.")
        if new_status not in ALLOWED_TRANSITIONS.get(current, set()):
            raise SupportValidationError(f"Invalid ticket transition: {current} -> {new_status}")

        return _normalize_row(
            await support_repository.update_status(
                ticket_id,
                status=new_status,
                agent_id=agent.id,
                agent_name=agent.name,
                expected_assignee_id=(
                    str(ticket.get("current_assignee_id"))
                    if ticket.get("current_assignee_id") is not None
                    else None
                ),
            )
        ) or {}

    async def _resolve_reply_target(
        self,
        ticket_id: int,
        reply_to_message_id: int | None,
    ) -> tuple[int | None, str | None]:
        if reply_to_message_id is None:
            return None, None
        target = await support_repository.get_message(int(reply_to_message_id))
        if not target or int(target.get("ticket_id") or 0) != int(ticket_id):
            raise SupportValidationError("The replied-to message is not part of this conversation.")
        if target.get("deleted_for_everyone_at"):
            raise SupportValidationError("This message was deleted.")
        whatsapp_message_id = str(target.get("whatsapp_message_id") or "").strip()
        if not whatsapp_message_id:
            raise SupportValidationError("That message cannot be replied to on WhatsApp yet.")
        return int(target["id"]), whatsapp_message_id

    async def _prepare_agent_outbound_ticket(
        self,
        ticket_id: int,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        """Return an owned active ticket, reopening a closed staff chat in place.

        Customer-origin support tickets keep their existing resolve/close behavior.
        Only a durable staff-origin chat can be reactivated by sending, and the
        repository's active-key transaction remains the concurrency/deduplication
        guard for the customer and WhatsApp line.
        """
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")

        line_id = str(ticket.get("business_phone_number_id") or "")
        if line_id and get_transport(line_id) == "removed":
            raise SupportConflictError("This WhatsApp number has been removed. Its chat history is read-only.")
        active = (
            ticket.get("active_key") is not None
            and ticket.get("status") in ACTIVE_TICKET_STATUSES
        )
        if not active:
            is_closed_staff_chat = (
                str(ticket.get("conversation_origin") or "") == "STAFF"
                and ticket.get("active_key") is None
                and str(ticket.get("status") or "") in {"RESOLVED", "CLOSED"}
            )
            if not is_closed_staff_chat:
                raise SupportConflictError("Cannot message a resolved or closed ticket.")
            if not await _can_view_ticket(ticket, agent):
                raise SupportForbiddenError("You cannot reopen this chat.")
            ticket, reopened = await support_repository.reopen_staff_chat(
                ticket_id,
                agent_id=agent.id,
                agent_name=agent.name,
            )
            if reopened:
                await publish_support_event(
                    "staff_chat_reopened",
                    ticket_id=ticket_id,
                )

        if not _is_assignee(ticket, agent):
            raise SupportForbiddenError("Only the currently assigned agent can reply to this ticket.")
        if ticket.get("current_assignee_id") is None:
            raise SupportConflictError("Claim the ticket before replying.")
        if (
            ticket.get("active_key") is None
            or ticket.get("status") not in ACTIVE_TICKET_STATUSES
        ):
            raise SupportConflictError("Ticket is no longer active.")
        return ticket

    async def send_agent_message(
        self,
        ticket_id: int,
        *,
        agent: SupportAgent,
        body: str,
        client_message_id: str,
        reply_to_message_id: int | None = None,
        is_forwarded: bool = False,
        forwarded_from_ticket_id: int | None = None,
        forwarded_from_message_id: int | None = None,
    ) -> dict[str, Any]:
        body = body.strip()
        if not body:
            raise SupportValidationError("Message cannot be empty.")
        if len(body) > 4096:
            raise SupportValidationError("Message is too long for a WhatsApp text message.")
        if not client_message_id or len(client_message_id) > 191:
            raise SupportValidationError("A valid client_message_id is required.")

        ticket = await self._prepare_agent_outbound_ticket(ticket_id, agent)

        existing = await support_repository.get_message_by_client_id(client_message_id)
        if existing:
            if existing.get("send_status") == "FAILED":
                raise SupportSendError(
                    "This idempotent send attempt already failed. Refresh the ticket before trying a new message."
                )
            return _serialize_support_messages(ticket_id, [existing])[0] or {}

        _reply_local_id, reply_whatsapp_id = await self._resolve_reply_target(
            ticket_id, reply_to_message_id
        )

        message, _ = await support_repository.add_ticket_message(
            ticket_id=ticket_id,
            sender_type="AGENT",
            sender_id=agent.id,
            sender_name=agent.name,
            direction="OUTBOUND",
            body=body,
            client_message_id=client_message_id,
            message_type="text",
            reply_to_whatsapp_message_id=reply_whatsapp_id,
            send_status="PENDING",
            is_forwarded=is_forwarded,
            forwarded_from_ticket_id=forwarded_from_ticket_id,
            forwarded_from_message_id=forwarded_from_message_id,
            require_active=True,
            require_assignee_id=agent.id,
        )

        await publish_support_event(
            "outbound_message", ticket_id=ticket_id,
            message_id=int(message["id"]),
            message=_serialize_support_messages(ticket_id, [message])[0],
        )

        try:
            response = await whatsapp_client.send_text(
                to=str(ticket["whatsapp_phone"]),
                body=body,
                client_message_id=client_message_id,
                phone_number_id=(
                    str(ticket.get("business_phone_number_id"))
                    if ticket.get("business_phone_number_id")
                    else None
                ),
                context_message_id=reply_whatsapp_id,
            )
        except Exception as exc:
            safe_error = sanitize_support_text(str(exc))[:500]
            await support_repository.update_message_send_state(
                int(message["id"]),
                send_status="FAILED",
                error_message=safe_error or "WhatsApp send failed.",
            )
            failed_message = await support_repository.get_message(int(message["id"]))
            if failed_message and failed_message.get("send_status") == "SENT":
                return _serialize_support_messages(ticket_id, [failed_message])[0] or {}
            if failed_message:
                await publish_support_event(
                    "outbound_message", ticket_id=ticket_id,
                    message_id=int(message["id"]),
                    message=_serialize_support_messages(ticket_id, [failed_message])[0],
                )
            try:
                await support_repository.add_event(
                    ticket_id,
                    event_type="MESSAGE_SEND_FAILED",
                    actor_type="AGENT",
                    actor_id=agent.id,
                    actor_name=agent.name,
                    details={"message_id": message["id"]},
                )
            except Exception:
                logger.exception(
                    "Message failure was stored but failure audit event could not be written: ticket_id=%s",
                    ticket_id,
                )
            logger.warning("Agent WhatsApp send failed for ticket_id=%s", ticket_id)
            raise SupportSendError("WhatsApp could not send the message.") from exc

        whatsapp_message_id = None
        messages = response.get("messages") if isinstance(response, dict) else None
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            whatsapp_message_id = messages[0].get("id")
        try:
            await support_repository.update_message_send_state(
                int(message["id"]),
                send_status="SENT",
                whatsapp_message_id=str(whatsapp_message_id) if whatsapp_message_id else None,
                error_message=None,
            )
        except Exception as exc:
            # The message may already have been accepted by Meta. Keep the row
            # PENDING and do not blindly resend the same idempotency key.
            logger.exception(
                "WhatsApp accepted outbound message but support send-state persistence failed for ticket_id=%s",
                ticket_id,
            )
            raise SupportSendError(
                "WhatsApp accepted the message, but its send status could not be saved. Refresh before resending."
            ) from exc

        if ticket.get("status") == "ASSIGNED":
            try:
                await support_repository.mark_agent_reply_started(
                    ticket_id,
                    agent_id=agent.id,
                    agent_name=agent.name,
                )
            except Exception:
                # The WhatsApp send result is already durable. A concurrent
                # resolve/status action must not turn a successful send into a
                # resend-worthy API failure.
                logger.exception(
                    "Agent message sent but automatic IN_PROGRESS transition failed: ticket_id=%s",
                    ticket_id,
                )

        updated = await support_repository.get_message(int(message["id"]))
        if updated:
            await publish_support_event(
                "outbound_message", ticket_id=ticket_id,
                message_id=int(message["id"]),
                message=_serialize_support_messages(ticket_id, [updated])[0],
            )
        return _serialize_support_messages(ticket_id, [updated])[0] if updated else {}

    async def change_message(
        self, ticket_id: int, message_id: int, *, agent: SupportAgent,
        action: str, content: str | None = None, version: int | None = None,
    ) -> dict[str, Any]:
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportForbiddenError("You cannot change a message in this conversation.")
        existing = await support_repository.get_message(message_id)
        if not existing or int(existing.get("ticket_id") or 0) != int(ticket_id):
            if not await _can_view_ticket(ticket, agent):
                raise SupportForbiddenError("You cannot change a message in this conversation.")
            raise SupportNotFoundError("Message not found.")
        original_staff_sender = (
            str(existing.get("sender_type") or "").upper() == "AGENT"
            and str(existing.get("sender_id") or "") == agent.id
            and existing.get("direction") == "OUTBOUND"
        )
        # A former agent can still correct their own historical message after
        # a ticket was reassigned, even when legacy ownership audit events are
        # missing. This does not grant access to the rest of the conversation.
        if not await _can_view_ticket(ticket, agent) and not (action in {"edit", "everyone", "me"} and original_staff_sender):
            raise SupportForbiddenError("You cannot change a message in this conversation.")
        if action not in {"edit", "everyone", "me"}:
            raise SupportValidationError("Invalid message action.")
        if action != "me" and not original_staff_sender:
            raise SupportForbiddenError("Only the original sender can change this message.")
        if action != "me" and await support_repository.message_hidden_for_user(message_id, agent.id):
            raise SupportNotFoundError("Message not found.")
        if action == "me":
            message = await support_repository.change_message(message_id, ticket_id=ticket_id, user_id=agent.id, action=action)
        else:
            line_id = str(ticket.get("business_phone_number_id") or "")
            provider_id = str(existing.get("whatsapp_message_id") or "")
            if get_transport(line_id) != "linked_device" or not is_linked_device_message(provider_id) or provider_id.split(":", 2)[1] != line_id:
                raise SupportConflictError("The WhatsApp Cloud API cannot edit or recall this message in the customer's chat. Only messages sent through the connected linked-device line support this action.")
            if existing.get("deleted_for_everyone_at"):
                if action == "everyone":
                    return _serialize_support_messages(ticket_id, [existing])[0] or {}
                raise SupportConflictError("This message was already deleted for everyone.")
            age = _message_age_seconds(existing)
            if age is None:
                raise SupportConflictError("The original send time is unavailable; this message cannot be changed safely.")
            window_open = age <= (15 * 60 if action == "edit" else 2 * 24 * 60 * 60)
            if action == "edit":
                content = str(content or "").strip()
                if not content or len(content) > 4096:
                    raise SupportValidationError("Enter a message between 1 and 4096 characters.")
                if version is None:
                    raise SupportValidationError("The message version is required before editing.")
            else:
                content = None
            base_version = int(version if version is not None else existing.get("version") or 1)
            # No new public idempotency key is needed: the exact original
            # version/action/body always maps to the same durable provider ID.
            operation_id = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([message_id, base_version if action == "edit" else "revoke", agent.id, action, content], ensure_ascii=False, separators=(",", ":"))).hex
            claim = await support_repository.claim_message_change(message_id, ticket_id=ticket_id, user_id=agent.id, action=action, content=content, version=base_version, operation_id=operation_id, window_open=window_open)
            message = claim["message"]
            if claim["needs_remote"]:
                try:
                    kwargs = {"to": str(ticket["whatsapp_phone"]), "message_id": provider_id, "phone_number_id": line_id, "operation_id": operation_id}
                    response = await whatsapp_client.edit_message(**kwargs, body=content) if action == "edit" else await whatsapp_client.delete_message(**kwargs)
                    if not isinstance(response, dict) or response.get("confirmed") is not True or response.get("id") != provider_id or response.get("operation_id", operation_id) != operation_id:
                        raise ValueError("WhatsApp did not confirm the message change.")
                    timestamp = _provider_change_timestamp(response.get("timestamp"))
                except Exception as exc:
                    try:
                        reconciled = await support_repository.record_message_change_failure(message_id, operation_id=operation_id, definitive=bool(getattr(exc, "definitive_failure", False)))
                    except Exception:
                        logger.exception("Could not persist WhatsApp message change outcome: message_id=%s", message_id)
                        reconciled = None
                    if not reconciled:
                        if getattr(exc, "definitive_failure", False):
                            raise SupportSendError("WhatsApp rejected the requested message change.") from exc
                        raise SupportSendError("WhatsApp has not confirmed the message change. Refresh, then retry the same change to check its result.") from exc
                    message = reconciled
                else:
                    try:
                        message = await support_repository.confirm_message_change(message_id, operation_id=operation_id, timestamp=timestamp)
                    except Exception as exc:
                        logger.exception("WhatsApp confirmed message change but support persistence failed: message_id=%s", message_id)
                        raise SupportSendError("WhatsApp confirmed the change, but the conversation could not be updated. Refresh before retrying.") from exc
        safe = None if action == "me" else _serialize_support_messages(ticket_id, [message])[0]
        event_type = "message_deleted_for_me" if action == "me" else "message_deleted_for_everyone" if action == "everyone" else "message_updated"
        await publish_support_event(
            event_type, ticket_id=ticket_id, message_id=message_id,
            user_id=agent.id if action == "me" else None,
            version=message.get("version"),
            message=safe,
        )
        if action == "me":
            return {"id": message_id, "deleted_for_me": True}
        if message.get("deleted_for_everyone_at"):
            self._purge_deleted_message_media(ticket_id, message_id)
        return safe or {}

    @staticmethod
    def _purge_deleted_message_media(ticket_id: int, message_id: int) -> None:
        try:
            support_media_store.delete_message_media(ticket_id, message_id)
        except Exception:
            # The tombstone already denies authenticated downloads, including
            # lazy recovery. Cache cleanup must not turn a revoke into failure.
            logger.exception("Deleted message media cache cleanup failed: message_id=%s", message_id)

    async def apply_linked_device_change(self, event: dict[str, Any]) -> bool:
        """Apply an authenticated protocol edit/revoke, including customer changes.

        False means the original has not been persisted yet; the durable bridge
        callback must retry. Protocol changes do not create a new chat message.
        """
        event_type = str(event.get("type") or "")
        line_id = str(event.get("line_id") or "")
        provider_id = str(event.get("message_id") or "")
        event_id = str(event.get("event_id") or "")
        if event_type not in {"message_updated", "message_deleted_for_everyone"} or get_transport(line_id) != "linked_device" or not is_linked_device_message(provider_id) or provider_id.split(":", 2)[1] != line_id or not event_id or len(event_id) > 191 or not isinstance(event.get("from_me"), bool):
            raise SupportValidationError("Invalid linked-device message change event.")
        try:
            timestamp = _provider_change_timestamp(event.get("timestamp"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise SupportValidationError("Invalid linked-device message change timestamp.") from exc
        original = await support_repository.get_message_by_whatsapp_id(provider_id)
        if not original:
            return False
        ticket_id = int(original["ticket_id"])
        ticket = await support_repository.get_ticket(ticket_id)
        expected_direction = "OUTBOUND" if event["from_me"] else "INBOUND"
        if not ticket or str(ticket.get("business_phone_number_id") or "") != line_id or original.get("direction") != expected_direction or normalize_phone(str(ticket.get("whatsapp_phone") or "")) != normalize_phone(str(event.get("sender_phone") or "")) or (not event["from_me"] and str(original.get("sender_type") or "").upper() != "CUSTOMER"):
            raise SupportValidationError("Linked-device message change does not match its conversation.")
        action = "everyone" if event_type == "message_deleted_for_everyone" else "edit"
        content = None
        if action == "edit":
            if not isinstance(event.get("body"), str) or not event["body"] or len(event["body"]) > 4096:
                raise SupportValidationError("Invalid linked-device edited message body.")
            content = event["body"]
        message, changed = await support_repository.apply_message_change_event(int(original["id"]), event_id=event_id, action=action, content=content, timestamp=timestamp, operation_id=str(event.get("operation_id") or "") or None)
        if changed:
            if message.get("deleted_for_everyone_at"):
                self._purge_deleted_message_media(ticket_id, int(message["id"]))
            await publish_support_event(event_type, ticket_id=ticket_id, message_id=int(message["id"]), version=message.get("version"), message=_serialize_support_messages(ticket_id, [message])[0])
        return True

    async def send_agent_location(
        self,
        ticket_id: int,
        *,
        agent: SupportAgent,
        latitude: float,
        longitude: float,
        name: str | None,
        address: str | None,
        client_message_id: str,
        reply_to_message_id: int | None = None,
        is_forwarded: bool = False,
        forwarded_from_ticket_id: int | None = None,
        forwarded_from_message_id: int | None = None,
    ) -> dict[str, Any]:
        if not (-90 <= float(latitude) <= 90) or not (-180 <= float(longitude) <= 180):
            raise SupportValidationError("Invalid location coordinates.")
        if not client_message_id or len(client_message_id) > 191:
            raise SupportValidationError("A valid client_message_id is required.")
        safe_name = sanitize_support_text(name).strip()[:255] or None
        safe_address = sanitize_support_text(address).strip()[:1000] or None

        ticket = await self._prepare_agent_outbound_ticket(ticket_id, agent)

        existing = await support_repository.get_message_by_client_id(client_message_id)
        if existing:
            if existing.get("send_status") == "FAILED":
                raise SupportSendError("This idempotent location send already failed. Refresh before trying again.")
            return _serialize_support_messages(ticket_id, [existing])[0] or {}

        _reply_local_id, reply_whatsapp_id = await self._resolve_reply_target(
            ticket_id, reply_to_message_id
        )
        body = safe_name or safe_address or "Location"
        message, _ = await support_repository.add_ticket_message(
            ticket_id=ticket_id,
            sender_type="AGENT",
            sender_id=agent.id,
            sender_name=agent.name,
            direction="OUTBOUND",
            body=body,
            client_message_id=client_message_id,
            message_type="location",
            reply_to_whatsapp_message_id=reply_whatsapp_id,
            location_latitude=float(latitude),
            location_longitude=float(longitude),
            location_name=safe_name,
            location_address=safe_address,
            send_status="PENDING",
            is_forwarded=is_forwarded,
            forwarded_from_ticket_id=forwarded_from_ticket_id,
            forwarded_from_message_id=forwarded_from_message_id,
            require_active=True,
            require_assignee_id=agent.id,
        )
        try:
            response = await whatsapp_client.send_location(
                to=str(ticket["whatsapp_phone"]),
                latitude=float(latitude),
                longitude=float(longitude),
                name=safe_name,
                address=safe_address,
                client_message_id=client_message_id,
                phone_number_id=(
                    str(ticket.get("business_phone_number_id"))
                    if ticket.get("business_phone_number_id") else None
                ),
                context_message_id=reply_whatsapp_id,
            )
        except Exception as exc:
            await support_repository.update_message_send_state(
                int(message["id"]),
                send_status="FAILED",
                error_message=sanitize_support_text(str(exc))[:500] or "WhatsApp location send failed.",
            )
            reconciled = await support_repository.get_message(int(message["id"]))
            if reconciled and reconciled.get("send_status") == "SENT":
                return _serialize_support_messages(ticket_id, [reconciled])[0] or {}
            raise SupportSendError("WhatsApp could not send the location.") from exc

        whatsapp_message_id = None
        messages = response.get("messages") if isinstance(response, dict) else None
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            whatsapp_message_id = messages[0].get("id")
        await support_repository.update_message_send_state(
            int(message["id"]),
            send_status="SENT",
            whatsapp_message_id=str(whatsapp_message_id) if whatsapp_message_id else None,
            error_message=None,
        )
        if ticket.get("status") == "ASSIGNED":
            try:
                await support_repository.mark_agent_reply_started(
                    ticket_id, agent_id=agent.id, agent_name=agent.name
                )
            except Exception:
                logger.exception("Location sent but automatic IN_PROGRESS transition failed: ticket_id=%s", ticket_id)
        updated = await support_repository.get_message(int(message["id"]))
        return _serialize_support_messages(ticket_id, [updated])[0] if updated else {}

    async def send_agent_media(
        self,
        ticket_id: int,
        *,
        agent: SupportAgent,
        media_type: str,
        content: bytes,
        content_type: str | None,
        filename: str | None,
        caption: str | None,
        client_message_id: str,
        reply_to_message_id: int | None = None,
        is_forwarded: bool = False,
        forwarded_from_ticket_id: int | None = None,
        forwarded_from_message_id: int | None = None,
    ) -> dict[str, Any]:
        media_type = str(media_type or "").lower().strip()
        if media_type not in SUPPORT_MEDIA_MESSAGE_TYPES:
            raise SupportValidationError("Only images, videos, stickers, voice notes, and PDF documents are supported.")
        if not client_message_id or len(client_message_id) > 191:
            raise SupportValidationError("A valid client_message_id is required.")

        ticket = await self._prepare_agent_outbound_ticket(ticket_id, agent)

        existing = await support_repository.get_message_by_client_id(client_message_id)
        if existing:
            if existing.get("send_status") == "FAILED":
                raise SupportSendError(
                    "This idempotent media send already failed. Refresh the ticket before trying a new message."
                )
            return _serialize_support_messages(ticket_id, [existing])[0] or {}

        normalized_mime = normalize_mime_type(content_type)
        send_as_voice = False
        if media_type == "image":
            if media_kind_for_mime(normalized_mime) != "image":
                raise SupportValidationError("Use a JPEG or PNG image.")
            if not content:
                raise SupportValidationError("The image is empty.")
            if len(content) > max_media_bytes("image"):
                raise SupportValidationError("The image is larger than the 5 MB limit.")
            prepared = content
            prepared_mime = normalized_mime
            prepared_filename = filename or "image"
            safe_caption = sanitize_support_text(caption).strip()[:1024] if caption else ""
            body = safe_caption or "Image"
        elif media_type == "video":
            if media_kind_for_mime(normalized_mime) != "video":
                raise SupportValidationError("Use an MP4 or 3GP video.")
            if not content:
                raise SupportValidationError("The video is empty.")
            if len(content) > max_media_bytes("video"):
                raise SupportValidationError(
                    f"The video is larger than the {max_media_bytes('video') // (1024 * 1024)} MB WhatsApp limit."
                )
            prepared = content
            prepared_mime = normalized_mime
            prepared_filename = sanitize_support_text(filename).strip()[:180] or "video" + media_extension(normalized_mime)
            safe_caption = sanitize_support_text(caption).strip()[:1024] if caption else ""
            body = safe_caption or "Video"
        elif media_type == "sticker":
            if media_kind_for_mime(normalized_mime) != "sticker":
                raise SupportValidationError("Use a WebP file for a WhatsApp sticker.")
            if not content:
                raise SupportValidationError("The sticker is empty.")
            if len(content) > max_media_bytes("sticker"):
                raise SupportValidationError("The sticker is larger than the 500 KB limit.")
            prepared = content
            prepared_mime = "image/webp"
            prepared_filename = "sticker.webp"
            safe_caption = ""
            body = "Sticker"
        elif media_type == "audio":
            if media_kind_for_mime(normalized_mime, allow_browser_audio=True) != "audio":
                raise SupportValidationError("Unsupported voice-note audio format.")
            try:
                prepared, prepared_mime, prepared_filename, send_as_voice = (
                    await support_media_store.prepare_voice_note(content, content_type or normalized_mime)
                )
            except SupportMediaError as exc:
                raise SupportValidationError(str(exc)) from exc
            safe_caption = ""
            body = "Voice note" if send_as_voice else "Audio message"
        else:
            if media_kind_for_mime(normalized_mime) != "document":
                raise SupportValidationError("Only PDF documents are supported.")
            if not content:
                raise SupportValidationError("The PDF document is empty.")
            if len(content) > max_media_bytes("document"):
                raise SupportValidationError("The PDF document is too large.")
            prepared = content
            prepared_mime = "application/pdf"
            prepared_filename = sanitize_support_text(filename).strip()[:180] or "document.pdf"
            if not prepared_filename.lower().endswith(".pdf"):
                prepared_filename += ".pdf"
            safe_caption = sanitize_support_text(caption).strip()[:1024] if caption else ""
            body = safe_caption or prepared_filename

        _reply_local_id, reply_whatsapp_id = await self._resolve_reply_target(
            ticket_id, reply_to_message_id
        )

        message, _ = await support_repository.add_ticket_message(
            ticket_id=ticket_id,
            sender_type="AGENT",
            sender_id=agent.id,
            sender_name=agent.name,
            direction="OUTBOUND",
            body=body,
            client_message_id=client_message_id,
            message_type=media_type,
            reply_to_whatsapp_message_id=reply_whatsapp_id,
            send_status="PENDING",
            is_forwarded=is_forwarded,
            forwarded_from_ticket_id=forwarded_from_ticket_id,
            forwarded_from_message_id=forwarded_from_message_id,
            require_active=True,
            require_assignee_id=agent.id,
        )

        try:
            support_media_store.save(
                ticket_id=ticket_id,
                message_id=int(message["id"]),
                content=prepared,
                mime_type=prepared_mime,
                filename=prepared_filename,
            )
            meta_media_id = await whatsapp_client.upload_media(
                content=prepared,
                mime_type=prepared_mime,
                filename=prepared_filename,
                phone_number_id=(
                    str(ticket.get("business_phone_number_id"))
                    if ticket.get("business_phone_number_id")
                    else None
                ),
            )
            response = await whatsapp_client.send_media(
                to=str(ticket["whatsapp_phone"]),
                media_type=media_type,
                media_id=meta_media_id,
                client_message_id=client_message_id,
                caption=safe_caption or None,
                voice=(media_type == "audio" and send_as_voice),
                filename=(prepared_filename if media_type == "document" else None),
                context_message_id=reply_whatsapp_id,
                phone_number_id=(
                    str(ticket.get("business_phone_number_id"))
                    if ticket.get("business_phone_number_id")
                    else None
                ),
            )
        except Exception as exc:
            safe_error = sanitize_support_text(str(exc))[:500]
            await support_repository.update_message_send_state(
                int(message["id"]),
                send_status="FAILED",
                error_message=safe_error or "WhatsApp media send failed.",
            )
            reconciled = await support_repository.get_message(int(message["id"]))
            if reconciled and reconciled.get("send_status") == "SENT":
                return _serialize_support_messages(ticket_id, [reconciled])[0] or {}
            try:
                await support_repository.add_event(
                    ticket_id,
                    event_type="MESSAGE_SEND_FAILED",
                    actor_type="AGENT",
                    actor_id=agent.id,
                    actor_name=agent.name,
                    details={"message_id": message["id"], "message_type": media_type},
                )
            except Exception:
                logger.exception(
                    "Media failure was stored but audit event could not be written: ticket_id=%s",
                    ticket_id,
                )
            logger.warning("Agent WhatsApp media send failed for ticket_id=%s type=%s", ticket_id, media_type)
            raise SupportSendError("WhatsApp could not send the media message.") from exc

        whatsapp_message_id = None
        messages = response.get("messages") if isinstance(response, dict) else None
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            whatsapp_message_id = messages[0].get("id")
        try:
            await support_repository.update_message_send_state(
                int(message["id"]),
                send_status="SENT",
                whatsapp_message_id=str(whatsapp_message_id) if whatsapp_message_id else None,
                error_message=None,
            )
        except Exception as exc:
            logger.exception(
                "WhatsApp accepted outbound media but send-state persistence failed for ticket_id=%s",
                ticket_id,
            )
            raise SupportSendError(
                "WhatsApp accepted the media, but its send status could not be saved. Refresh before resending."
            ) from exc

        if ticket.get("status") == "ASSIGNED":
            try:
                await support_repository.mark_agent_reply_started(
                    ticket_id, agent_id=agent.id, agent_name=agent.name
                )
            except Exception:
                logger.exception(
                    "Agent media sent but automatic IN_PROGRESS transition failed: ticket_id=%s",
                    ticket_id,
                )

        updated = await support_repository.get_message(int(message["id"]))
        return _serialize_support_messages(ticket_id, [updated])[0] if updated else {}

    async def send_agent_reaction(
        self,
        ticket_id: int,
        message_id: int,
        *,
        agent: SupportAgent,
        emoji: str,
        client_reaction_id: str,
    ) -> dict[str, Any]:
        rendered_emoji = str(emoji or "").strip()
        if rendered_emoji and rendered_emoji not in SUPPORTED_REACTION_EMOJIS:
            raise SupportValidationError("Choose a supported WhatsApp reaction.")
        if not client_reaction_id or len(client_reaction_id) > 191:
            raise SupportValidationError("A valid client_reaction_id is required.")

        ticket = await self._prepare_agent_outbound_ticket(ticket_id, agent)
        target = await support_repository.get_message(message_id)
        if not target or int(target.get("ticket_id") or 0) != int(ticket_id) or target.get("deleted_for_everyone_at") or await support_repository.message_hidden_for_user(message_id, agent.id):
            raise SupportNotFoundError("Message not found.")
        target_whatsapp_id = str(target.get("whatsapp_message_id") or "").strip()
        if not target_whatsapp_id:
            raise SupportValidationError("That message cannot be reacted to on WhatsApp yet.")

        existing = await support_repository.get_reaction_by_client_id(client_reaction_id)
        if existing:
            if (
                int(existing.get("ticket_id") or 0) != int(ticket_id)
                or int(existing.get("message_id") or 0) != int(message_id)
            ):
                raise SupportConflictError("That reaction request ID was already used.")
            return _normalize_row(existing) or {}

        try:
            response = await whatsapp_client.send_reaction(
                to=str(ticket["whatsapp_phone"]),
                message_id=target_whatsapp_id,
                emoji=rendered_emoji,
                phone_number_id=(
                    str(ticket.get("business_phone_number_id"))
                    if ticket.get("business_phone_number_id")
                    else None
                ),
            )
        except Exception as exc:
            logger.warning(
                "Agent WhatsApp reaction send failed for ticket_id=%s message_id=%s",
                ticket_id,
                message_id,
            )
            raise SupportSendError("WhatsApp could not update the reaction.") from exc

        provider_reaction_id = None
        messages = response.get("messages") if isinstance(response, dict) else None
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            provider_reaction_id = messages[0].get("id")
        line_id = str(ticket.get("business_phone_number_id") or "default")
        reaction = await support_repository.upsert_message_reaction(
            ticket_id=ticket_id,
            message_id=message_id,
            target_whatsapp_message_id=target_whatsapp_id,
            reactor_key=f"business:{line_id}",
            reactor_type="AGENT",
            reactor_id=agent.id,
            reactor_name=agent.name,
            emoji=rendered_emoji,
            provider_reaction_id=(
                str(provider_reaction_id) if provider_reaction_id else None
            ),
            client_reaction_id=client_reaction_id,
            provider_timestamp=int(datetime.now(timezone.utc).timestamp()),
        )
        return _normalize_row(reaction) or {}

    async def resolve_and_return_to_ai(
        self,
        ticket_id: int,
        *,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        if str(ticket.get("conversation_origin") or "CUSTOMER") == "STAFF":
            raise SupportValidationError("Staff-initiated chats must be closed from Chats; they are not support tickets.")
        if not _can_operate(ticket, agent):
            raise SupportForbiddenError("Only the assigned agent or a supervisor can resolve this ticket.")
        if ticket.get("current_assignee_id") is None and not agent.is_supervisor:
            raise SupportConflictError("Claim the ticket before resolving it.")

        resolved, changed = await support_repository.resolve_ticket(
            ticket_id,
            agent_id=agent.id,
            agent_name=agent.name,
            expected_assignee_id=(
                str(ticket.get("current_assignee_id"))
                if ticket.get("current_assignee_id") is not None
                else None
            ),
        )

        notice = await self._automatic_message_for_staff(
            staff_id=agent.id,
            preference_key="custom_resolution_message",
        )
        resolution_notice, notice_created = None, False
        if notice:
            resolution_notice, notice_created = await self._send_system_customer_message(
                ticket=resolved,
                body=notice,
                client_message_id=f"system:resolved:{ticket_id}",
                event_type="RESOLUTION_NOTICE_SENT",
                require_active=False,
            )

        await _return_conversation_to_ai(resolved)

        logger.info(
            "Support ticket resolved: ticket_id=%s staff_id=%s changed=%s notice_created=%s",
            ticket_id,
            agent.id,
            changed,
            notice_created,
        )
        return {
            "ticket": _normalize_row(resolved),
            "changed": changed,
            "resolution_message": resolution_notice,
            "conversation_mode": "ai",
        }

    async def auto_resolve_idle_phone_app_tickets(self) -> int:
        """Resolve unclaimed tickets answered in the WhatsApp app that went quiet.

        No resolution message is sent: the customer was already being helped
        from the phone. Returns how many tickets were resolved.
        """
        idle_minutes = settings.support_phone_app_idle_resolve_minutes
        if idle_minutes <= 0:
            return 0
        resolved_count = 0
        for ticket_id in await support_repository.list_idle_phone_app_tickets(idle_minutes=idle_minutes):
            try:
                resolved = await support_repository.auto_resolve_idle_phone_app_ticket(
                    ticket_id, idle_minutes=idle_minutes,
                )
            except Exception:
                logger.exception("Idle phone-app ticket could not be resolved: ticket_id=%s", ticket_id)
                continue
            if not resolved:
                continue
            resolved_count += 1
            await _return_conversation_to_ai(
                resolved, resolved_by="automatically after the conversation went quiet",
            )
            await publish_support_event("ticket_updated", ticket_id=ticket_id, status="RESOLVED")
            logger.info(
                "Idle phone-app ticket resolved: ticket_id=%s idle_minutes=%s",
                ticket_id, idle_minutes,
            )
        return resolved_count


support_service = SupportService()
