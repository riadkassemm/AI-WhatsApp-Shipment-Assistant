from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.conversation_store import Conversation, conversation_store
from app.client_chat_auth import redact_sensitive_text
from app.handoff import notify_human_agents
from app.language import claim_greeting, resolution_message
from app.shipment_client import shipment_client
from app.support_auth import SupportAgent
from app.support_models import ALLOWED_TRANSITIONS, ACTIVE_TICKET_STATUSES, HandoffRequest
from app.support_repository import (
    SupportConflictError,
    SupportNotFoundError,
    support_repository,
)
from app.whatsapp_client import whatsapp_client


logger = logging.getLogger("shipment-bot")


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


def _normalize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            normalized[key] = value.isoformat()
        else:
            normalized[key] = value
    return normalized


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


class SupportService:
    async def get_active_ticket(self, phone_number: str) -> dict[str, Any] | None:
        return await support_repository.find_active_ticket_by_phone(phone_number)

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

        ticket, created = await support_repository.create_ticket(
            ticket_reference=_ticket_reference(),
            phone_number=conversation.phone_number,
            customer_id=conversation.verified_customer_id,
            customer_userid=conversation.authenticated_userid,
            customer_name=conversation.authenticated_name,
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

        return ticket, created

    async def route_human_inbound(
        self,
        *,
        ticket: dict[str, Any],
        body: str,
        whatsapp_message_id: str | None,
        whatsapp_timestamp: str | None,
        message_type: str,
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
        return message, created

    async def list_tickets(
        self,
        *,
        view: str,
        agent: SupportAgent,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        result = await support_repository.list_tickets(
            view=view,
            agent_id=agent.id,
            page=page,
            page_size=page_size,
        )
        result["items"] = [_safe_ticket_row(row) for row in result["items"]]
        return result

    async def ticket_detail(self, ticket_id: int, agent: SupportAgent) -> dict[str, Any]:
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        messages = await support_repository.list_messages(ticket_id, limit=150)
        events = await support_repository.list_events(ticket_id, limit=150)

        shipment_context = None
        if ticket.get("customer_id") and ticket.get("tracking_reference"):
            try:
                candidate = await shipment_client.track_shipment(
                    customer_id=str(ticket["customer_id"]),
                    tracking_number=str(ticket["tracking_reference"]),
                )
                if candidate.get("found") is True and candidate.get("authorized") is True:
                    shipment_context = candidate
            except Exception:
                logger.exception(
                    "Could not enrich support ticket with shipment context: ticket_id=%s",
                    ticket_id,
                )

        safe_messages: list[dict[str, Any] | None] = []
        for row in messages:
            safe_row = dict(row)
            if isinstance(safe_row.get("body"), str):
                safe_row["body"] = sanitize_support_text(safe_row["body"])
            if isinstance(safe_row.get("error_message"), str):
                safe_row["error_message"] = sanitize_support_text(safe_row["error_message"])
            safe_messages.append(_normalize_row(safe_row))

        return {
            "ticket": _safe_ticket_row(ticket),
            "context": _decode_context(ticket),
            "shipment": shipment_context,
            "messages": safe_messages,
            "events": [_safe_context_value(_normalize_row(row)) for row in events],
            "permissions": {
                "can_operate": _can_operate(ticket, agent),
                "can_claim": (
                    ticket.get("status") == "NEW"
                    and ticket.get("current_assignee_id") is None
                    and ticket.get("active_key") is not None
                ),
                "is_supervisor": agent.is_supervisor,
            },
        }

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
            )
        except Exception as exc:
            safe_error = sanitize_support_text(str(exc))[:500]
            await support_repository.update_message_send_state(
                int(message["id"]),
                send_status="FAILED",
                error_message=safe_error or "WhatsApp send failed.",
            )
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

    async def claim(self, ticket_id: int, agent: SupportAgent) -> dict[str, Any]:
        ticket, changed = await support_repository.claim_ticket(
            ticket_id,
            agent_id=agent.id,
            agent_name=agent.name,
        )
        greeting = claim_greeting(ticket.get("communication_style"), agent.name)
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
            )
        ) or {}

    async def send_agent_message(
        self,
        ticket_id: int,
        *,
        agent: SupportAgent,
        body: str,
        client_message_id: str,
    ) -> dict[str, Any]:
        body = body.strip()
        if not body:
            raise SupportValidationError("Message cannot be empty.")
        if len(body) > 4096:
            raise SupportValidationError("Message is too long for a WhatsApp text message.")
        if not client_message_id or len(client_message_id) > 191:
            raise SupportValidationError("A valid client_message_id is required.")

        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        if ticket.get("status") not in ACTIVE_TICKET_STATUSES or ticket.get("active_key") is None:
            raise SupportConflictError("Cannot message a resolved or closed ticket.")
        if not _can_operate(ticket, agent):
            raise SupportForbiddenError("Only the assigned agent or a supervisor can reply to this ticket.")
        if ticket.get("current_assignee_id") is None:
            raise SupportConflictError("Claim the ticket before replying.")

        existing = await support_repository.get_message_by_client_id(client_message_id)
        if existing:
            if existing.get("send_status") == "FAILED":
                raise SupportSendError(
                    "This idempotent send attempt already failed. Refresh the ticket before trying a new message."
                )
            return _normalize_row(existing) or {}

        message, _ = await support_repository.add_ticket_message(
            ticket_id=ticket_id,
            sender_type="AGENT",
            sender_id=agent.id,
            sender_name=agent.name,
            direction="OUTBOUND",
            body=body,
            client_message_id=client_message_id,
            message_type="text",
            send_status="PENDING",
            require_active=True,
        )

        try:
            response = await whatsapp_client.send_text(
                to=str(ticket["whatsapp_phone"]),
                body=body,
            )
        except Exception as exc:
            safe_error = sanitize_support_text(str(exc))[:500]
            await support_repository.update_message_send_state(
                int(message["id"]),
                send_status="FAILED",
                error_message=safe_error or "WhatsApp send failed.",
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
        return _normalize_row(updated) or {}

    async def resolve_and_return_to_ai(
        self,
        ticket_id: int,
        *,
        agent: SupportAgent,
    ) -> dict[str, Any]:
        ticket = await support_repository.get_ticket(ticket_id)
        if not ticket:
            raise SupportNotFoundError("Ticket not found.")
        if not _can_operate(ticket, agent):
            raise SupportForbiddenError("Only the assigned agent or a supervisor can resolve this ticket.")
        if ticket.get("current_assignee_id") is None and not agent.is_supervisor:
            raise SupportConflictError("Claim the ticket before resolving it.")

        resolved, changed = await support_repository.resolve_ticket(
            ticket_id,
            agent_id=agent.id,
            agent_name=agent.name,
        )

        notice = resolution_message(resolved.get("communication_style"))
        resolution_notice, notice_created = await self._send_system_customer_message(
            ticket=resolved,
            body=notice,
            client_message_id=f"system:resolved:{ticket_id}",
            event_type="RESOLUTION_NOTICE_SENT",
            require_active=False,
        )

        # DB was updated first. Redis is a cache; if this write fails, the next
        # inbound webhook reconciles stale human mode against the durable DB.
        phone = str(resolved["whatsapp_phone"])
        try:
            conversation = await conversation_store.get_or_create(phone)
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
                        "SUPPORT EVENT: Human support has been resolved by an "
                        "authorized agent and control has explicitly returned "
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


support_service = SupportService()
