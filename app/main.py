from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app.ai_availability import EffectiveAIAvailability, ai_availability_service
from app.ai_tools import NEEDS_VERIFIED_IDENTITY, dispatch_tool_call
from app.config import settings
from app.conversation_store import Conversation, conversation_store
from app.customer_reply_guard import (
    render_authoritative_tool_result,
    reply_guard_fallback,
    validate_customer_reply,
)
from app.customer_auth import CustomerAuthenticationUnavailable, authenticate_customer
from app.client_chat_auth import (
    CredentialExtraction,
    extract_credentials,
    is_auth_cancel_intent,
    is_auth_topic_switch_intent,
    is_logout_intent,
    redact_sensitive_text,
)
from app.openai_service import ai_unavailable_message, get_ai_reply
from app.ai_rate_limit import AIQuotaDecision, ai_rate_limiter
from app.language import (
    already_authenticated,
    auth_cancelled,
    auth_clarification,
    auth_failure,
    auth_prompt,
    auth_rate_limited,
    ai_rate_limited,
    auth_unavailable,
    detect_communication_style,
    login_confirmation,
    logout_confirmation,
    pending_request_unavailable,
    password_prompt,
    userid_prompt,
)
from app.shipping_quote_service import maybe_handle_shipping_quote
from app.security import (
    client_auth_rate_limiter,
    safe_log_identifier,
    verify_meta_signature,
)
from app.support_api import router as support_api_router
from app.support_repository import SupportConflictError
from app.support_media import SUPPORT_MEDIA_MESSAGE_TYPES, SupportMediaError
from app.support_models import HandoffRequest
from app.support_service import sanitize_support_text, support_service
from app.support_contacts import format_shared_contacts
from app.support_ui import SUPPORT_UI_VERSION, router as support_ui_router
from app.linked_device_api import router as linked_device_api_router
from app.whatsapp_registry import WhatsAppRegistryError, whatsapp_registry_service
from app.whatsapp_client import (
    IncomingWhatsAppMessage,
    WhatsAppSendError,
    extract_incoming_message,
    get_transport,
    is_linked_device_message,
    whatsapp_client,
)


logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("shipment-bot")

_idle_ticket_sweeper: asyncio.Task | None = None


async def _sweep_idle_tickets_forever() -> None:
    """Resolve quiet phone-app tickets in the background (see support_service)."""
    interval = max(10, settings.support_idle_ticket_sweep_seconds)
    while True:
        await asyncio.sleep(interval)
        try:
            await support_service.auto_resolve_idle_phone_app_tickets()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Idle phone-app ticket sweep failed")


async def _start_idle_ticket_sweeper() -> None:
    global _idle_ticket_sweeper
    if settings.support_phone_app_idle_resolve_minutes > 0 and _idle_ticket_sweeper is None:
        _idle_ticket_sweeper = asyncio.create_task(_sweep_idle_tickets_forever())


async def _stop_idle_ticket_sweeper() -> None:
    global _idle_ticket_sweeper
    task, _idle_ticket_sweeper = _idle_ticket_sweeper, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    from app.support_delivery_reconciliation import reconcile_deliveries_forever
    reconciliation = asyncio.create_task(reconcile_deliveries_forever(support_service)) if settings.support_ui_enabled else None
    await _start_idle_ticket_sweeper()
    try:
        yield
    finally:
        if reconciliation is not None:
            reconciliation.cancel()
            try:
                await reconciliation
            except asyncio.CancelledError:
                pass
        await _stop_idle_ticket_sweeper()


app = FastAPI(title="AI Shipment Assistant - WhatsApp Backend", lifespan=_lifespan)
app.include_router(support_api_router)
app.include_router(support_ui_router)
app.include_router(linked_device_api_router)


@app.middleware("http")
async def _disable_support_response_caching(request: Request, call_next):
    """Keep confidential support data out of shared browser/proxy caches."""
    response = await call_next(request)
    path = request.url.path
    if path == "/support" or path.startswith("/api/support/") or path == "/api/support":
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Support-UI-Version"] = SUPPORT_UI_VERSION
    return response


def _json_response(status_code: int, payload: dict) -> Response:
    return Response(
        status_code=status_code,
        content=json.dumps(payload),
        media_type="application/json",
    )


def _inbound_timestamp(value: str | None) -> int | None:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _conversation_route_key(incoming: IncomingWhatsAppMessage) -> str:
    """Separate one customer conversation per receiving WhatsApp number."""
    business_id = str(incoming.business_phone_number_id or "").strip()
    if not business_id:
        return incoming.from_number
    return f"{business_id}:{incoming.from_number}"


async def _get_active_ticket_for_incoming(
    incoming: IncomingWhatsAppMessage,
) -> dict | None:
    if incoming.business_phone_number_id:
        return await support_service.get_active_ticket(
            incoming.from_number,
            business_phone_number_id=incoming.business_phone_number_id,
            business_display_phone_number=incoming.business_display_phone_number,
        )
    return await support_service.get_active_ticket(incoming.from_number)


def _stale_inbound_reason(
    conversation: Conversation,
    incoming: IncomingWhatsAppMessage,
) -> str | None:
    if incoming.message_id and incoming.message_id in conversation.recent_inbound_message_ids:
        return "recent_duplicate"

    timestamp = _inbound_timestamp(incoming.timestamp)
    if timestamp is None:
        return None

    max_age = max(0, int(settings.whatsapp_max_inbound_age_seconds))
    if max_age and int(time.time()) - timestamp > max_age:
        return "expired"

    # Do not discard a unique message merely because its provider timestamp is
    # older than the latest completed turn. Meta deliveries can cross in flight,
    # especially when a customer sends several messages quickly. Message-ID
    # idempotency already protects against true duplicates, while the inbound queue
    # below restores order for adjacent messages before they reach the conversation.
    return None


async def _mark_inbound_completed(incoming: IncomingWhatsAppMessage) -> None:
    """Persist a small sender watermark after successful processing."""
    conversation = await conversation_store.get_or_create(
        incoming.from_number,
        conversation_key=_conversation_route_key(incoming),
        business_phone_number_id=incoming.business_phone_number_id,
        business_display_phone_number=incoming.business_display_phone_number,
    )
    if incoming.message_id:
        recent = [
            item
            for item in conversation.recent_inbound_message_ids
            if item and item != incoming.message_id
        ]
        recent.append(incoming.message_id)
        conversation.recent_inbound_message_ids = recent[-50:]

    timestamp = _inbound_timestamp(incoming.timestamp)
    if timestamp is not None and (
        conversation.last_inbound_timestamp is None
        or timestamp > int(conversation.last_inbound_timestamp)
    ):
        conversation.last_inbound_timestamp = timestamp
    await conversation_store.save(conversation)


def _queued_inbound_item(
    incoming: IncomingWhatsAppMessage,
    claim_token: str | None,
) -> dict:
    return {
        "from_number": incoming.from_number,
        "message_id": incoming.message_id,
        "message_type": incoming.message_type,
        "text": incoming.text,
        "timestamp": incoming.timestamp,
        "business_account_id": incoming.business_account_id,
        "business_phone_number_id": incoming.business_phone_number_id,
        "business_display_phone_number": incoming.business_display_phone_number,
        "media_id": incoming.media_id,
        "media_mime_type": incoming.media_mime_type,
        "media_sha256": incoming.media_sha256,
        "media_caption": incoming.media_caption,
        "media_filename": incoming.media_filename,
        "is_voice": incoming.is_voice,
        "context_message_id": incoming.context_message_id,
        "location_latitude": incoming.location_latitude,
        "location_longitude": incoming.location_longitude,
        "location_name": incoming.location_name,
        "location_address": incoming.location_address,
        "reaction_message_id": incoming.reaction_message_id,
        "reaction_emoji": incoming.reaction_emoji,
        "claim_token": claim_token,
    }


def _incoming_from_queued_item(item: dict) -> IncomingWhatsAppMessage | None:
    from_number = item.get("from_number")
    if not from_number:
        return None
    message_id = item.get("message_id")
    message_type = item.get("message_type") or "unknown"
    text = item.get("text")
    timestamp = item.get("timestamp")
    media_id = item.get("media_id")
    media_mime_type = item.get("media_mime_type")
    media_sha256 = item.get("media_sha256")
    media_caption = item.get("media_caption")
    media_filename = item.get("media_filename")
    context_message_id = item.get("context_message_id")
    location_latitude = item.get("location_latitude")
    location_longitude = item.get("location_longitude")
    location_name = item.get("location_name")
    location_address = item.get("location_address")
    reaction_message_id = item.get("reaction_message_id")
    reaction_emoji = item.get("reaction_emoji")
    business_account_id = item.get("business_account_id")
    business_phone_number_id = item.get("business_phone_number_id")
    business_display_phone_number = item.get("business_display_phone_number")
    return IncomingWhatsAppMessage(
        from_number=str(from_number),
        message_id=str(message_id) if message_id else None,
        message_type=str(message_type),
        text=str(text) if text is not None else None,
        timestamp=str(timestamp) if timestamp else None,
        business_account_id=(str(business_account_id) if business_account_id else None),
        business_phone_number_id=(
            str(business_phone_number_id) if business_phone_number_id else None
        ),
        business_display_phone_number=(
            str(business_display_phone_number) if business_display_phone_number else None
        ),
        media_id=str(media_id) if media_id else None,
        media_mime_type=str(media_mime_type) if media_mime_type else None,
        media_sha256=str(media_sha256) if media_sha256 else None,
        media_caption=str(media_caption) if media_caption is not None else None,
        media_filename=str(media_filename) if media_filename is not None else None,
        is_voice=bool(item.get("is_voice")),
        context_message_id=(str(context_message_id) if context_message_id else None),
        location_latitude=(float(location_latitude) if location_latitude is not None else None),
        location_longitude=(float(location_longitude) if location_longitude is not None else None),
        location_name=str(location_name) if location_name is not None else None,
        location_address=str(location_address) if location_address is not None else None,
        reaction_message_id=(
            str(reaction_message_id) if reaction_message_id is not None else None
        ),
        reaction_emoji=(str(reaction_emoji) if reaction_emoji is not None else None),
    )


def _sort_inbound_burst(items: list[dict]) -> list[dict]:
    """Sort by Meta timestamp when every item has one; otherwise preserve FIFO."""
    parsed = [_inbound_timestamp(item.get("timestamp")) for item in items]
    if not items or any(value is None for value in parsed):
        return items
    return [
        item
        for _index, item in sorted(
            enumerate(items),
            key=lambda pair: (parsed[pair[0]], pair[0]),
        )
    ]


def _combine_inbound_burst(items: list[dict]) -> IncomingWhatsAppMessage | None:
    messages = [_incoming_from_queued_item(item) for item in items]
    if not messages or any(message is None for message in messages):
        return None
    concrete = [message for message in messages if message is not None]
    if len(concrete) < 2 or any(message.text is None for message in concrete):
        return None
    if len({message.from_number for message in concrete}) != 1:
        return None
    if len({message.business_phone_number_id for message in concrete}) != 1:
        return None

    # Newlines preserve the customer's natural message boundaries while giving the
    # deterministic quote pipeline and the model one complete user turn to reason over.
    combined_text = "\n".join((message.text or "").strip() for message in concrete).strip()
    if not combined_text:
        return None
    latest = concrete[-1]
    return IncomingWhatsAppMessage(
        from_number=latest.from_number,
        message_id=latest.message_id,
        message_type="text",
        text=combined_text,
        timestamp=latest.timestamp,
        business_account_id=latest.business_account_id,
        business_phone_number_id=latest.business_phone_number_id,
        business_display_phone_number=latest.business_display_phone_number,
    )


def _response_is_retryable(response: Response) -> bool:
    return response.status_code == 429 or response.status_code >= 500


async def _complete_queued_inbound_items(items: list[dict]) -> None:
    for item in items:
        incoming = _incoming_from_queued_item(item)
        if incoming is None:
            continue
        try:
            await _mark_inbound_completed(incoming)
        except Exception:
            # Customer-facing processing already succeeded. The per-message Redis
            # claim still prevents immediate duplicate execution.
            logger.exception(
                "Could not persist queued inbound completion watermark: message_id=%s",
                incoming.message_id,
            )
        claim_token = item.get("claim_token")
        if incoming.message_id and claim_token:
            try:
                await conversation_store.complete_incoming_message(
                    incoming.message_id,
                    str(claim_token),
                )
            except Exception:
                logger.exception(
                    "Could not finalize queued inbound idempotency: message_id=%s",
                    incoming.message_id,
                )


async def _requeue_inbound_items_front(phone_number: str, items: list[dict]) -> None:
    # Each front insertion prepends one item, so insert in reverse to preserve the
    # original FIFO order ahead of messages that arrived while processing failed.
    for item in reversed(items):
        await conversation_store.enqueue_incoming_work(
            phone_number,
            item,
            front=True,
        )


async def _collect_inbound_burst(phone_number: str) -> list[dict]:
    debounce = max(0.0, float(settings.whatsapp_message_burst_debounce_seconds))
    if debounce:
        # Human-owned conversations do not benefit from the AI turn debounce.
        # Route their queued messages immediately so the support timeline updates
        # without an intentional quiet-period wait.
        business_id, separator, customer_phone = phone_number.partition(":")
        try:
            active_ticket = await support_service.get_active_ticket(
                customer_phone if separator else phone_number,
                business_phone_number_id=business_id if separator else None,
            )
            if active_ticket:
                debounce = 0.0
        except Exception:
            logger.exception("Could not check active ticket before inbound queue debounce")
    max_wait = max(debounce, float(settings.whatsapp_message_burst_max_wait_seconds))
    if debounce:
        started = time.monotonic()
        quiet_since = started
        previous_count = await conversation_store.pending_incoming_work_count(
            phone_number
        )
        # Debounce on a *quiet period* instead of a fixed sleep. If the customer sends
        # another detail while the timer is running, give that new message the same
        # short opportunity to join the burst, up to the bounded max-wait ceiling.
        poll = min(0.10, max(0.02, debounce / 5.0))
        while True:
            await asyncio.sleep(poll)
            now = time.monotonic()
            current_count = await conversation_store.pending_incoming_work_count(
                phone_number
            )
            if current_count != previous_count:
                previous_count = current_count
                quiet_since = now
            if now - quiet_since >= debounce or now - started >= max_wait:
                break

    limit = max(1, int(settings.whatsapp_message_burst_max_messages))
    items: list[dict] = []
    while len(items) < limit:
        item = await conversation_store.pop_incoming_work(phone_number)
        if item is None:
            break
        items.append(item)
    return _sort_inbound_burst(items)


async def _process_inbound_burst(phone_number: str, items: list[dict]) -> Response:
    """Process one queued burst and finalize every original message on success."""
    if not items:
        return _json_response(200, {"status": "queue_empty"})

    # A WhatsApp revoke can arrive after its original webhook was acknowledged
    # but before this durable queue is drained. Drop only that provider ID.
    try:
        revoked = [
            item for item in items
            if await _is_inbound_revoked(str(item.get("message_id") or ""))
        ]
    except Exception:
        await _requeue_inbound_items_front(phone_number, items)
        return _json_response(503, {"status": "revocation_state_unavailable"})
    if revoked:
        await _complete_queued_inbound_items(revoked)
        revoked_ids = {id(item) for item in revoked}
        items = [item for item in items if id(item) not in revoked_ids]
        if not items:
            return _json_response(200, {"status": "revoked_message_ignored"})

    # Never merge messages while a human owns the ticket; the support timeline should
    # retain each exact inbound WhatsApp message as its own durable row.
    first_incoming = _incoming_from_queued_item(items[0]) if items else None
    try:
        active_ticket = (
            await _get_active_ticket_for_incoming(first_incoming)
            if first_incoming is not None
            else await support_service.get_active_ticket(phone_number)
        )
    except Exception:
        active_ticket = object()

    combined = None
    combined_ai_availability: EffectiveAIAvailability | None = None
    if active_ticket is None:
        candidate = _combine_inbound_burst(items)
        if candidate is not None:
            # Do not collapse several original WhatsApp messages into one synthetic
            # turn while AI is globally unavailable. Human support must retain each
            # original inbound message ID in its durable timeline.
            try:
                availability = await ai_availability_service.get_effective_ai_availability()
            except Exception:
                availability = None
                logger.exception(
                    "AI availability could not be evaluated before inbound burst routing: "
                    "customer_ref=%s",
                    safe_log_identifier(phone_number, "wa"),
                )
            if availability is not None and availability.effective_ai_enabled:
                combined = candidate
                combined_ai_availability = availability

    if combined is not None:
        logger.info(
            "Processing WhatsApp message burst: customer_ref=%s count=%d last_message_id=%s",
            safe_log_identifier(phone_number, "wa"),
            len(items),
            combined.message_id,
        )
        response = await _process_incoming_message(
            combined,
            ai_availability_snapshot=combined_ai_availability,
        )
        if _response_is_retryable(response):
            await _requeue_inbound_items_front(phone_number, items)
            return response
        await _complete_queued_inbound_items(items)
        return response

    for index, item in enumerate(items):
        incoming = _incoming_from_queued_item(item)
        if incoming is None:
            await _complete_queued_inbound_items([item])
            continue
        response = await _process_incoming_message(incoming)
        if _response_is_retryable(response):
            await _requeue_inbound_items_front(phone_number, items[index:])
            return response
        await _complete_queued_inbound_items([item])

    return _json_response(200, {"status": "queue_drained"})


async def _drain_inbound_queue(
    phone_number: str,
    claim_token: str,
) -> Response:
    """Drain all currently pending bursts while this worker owns the sender lock."""
    last_response = _json_response(200, {"status": "queue_empty"})
    while True:
        refreshed = await conversation_store.refresh_conversation_processing(
            phone_number,
            claim_token,
        )
        if not refreshed:
            return _json_response(503, {"status": "conversation_lock_lost"})

        if await conversation_store.pending_incoming_work_count(phone_number) <= 0:
            return last_response

        items = await _collect_inbound_burst(phone_number)
        if not items:
            return last_response
        last_response = await _process_inbound_burst(phone_number, items)
        if _response_is_retryable(last_response):
            return last_response


async def _drive_inbound_queue(
    phone_number: str,
    initial_claim_token: str | None = None,
) -> Response | None:
    """Own the sender lock when available and close the enqueue/release race safely."""
    claim_token = initial_claim_token
    while True:
        if claim_token is None:
            claim_token = await conversation_store.claim_conversation_processing(phone_number)
            if claim_token is None:
                return None

        try:
            response = await _drain_inbound_queue(phone_number, claim_token)
        finally:
            await conversation_store.release_conversation_processing(
                phone_number,
                claim_token,
            )

        if _response_is_retryable(response):
            return response

        # A webhook can enqueue after our final empty check but just before the lock
        # release. Check once after release; if work appeared, compete normally to own
        # the next drain. If another request wins, that worker will handle it.
        if await conversation_store.pending_incoming_work_count(phone_number) <= 0:
            return response
        claim_token = await conversation_store.claim_conversation_processing(phone_number)
        if claim_token is None:
            return response


def _reply_idempotency_key(source_message_id: str) -> str:
    return f"primary-customer-reply:{source_message_id}"


async def _is_inbound_revoked(message_id: str | None) -> bool:
    if not message_id or not is_linked_device_message(message_id):
        return False
    checker = getattr(conversation_store, "is_inbound_revoked", None)
    return bool(await checker(message_id)) if callable(checker) else False


async def _send_text_once(
    *,
    to: str,
    body: str,
    source_message_id: str | None,
    phone_number_id: str | None = None,
) -> dict:
    """Attempt at most one primary customer reply per inbound WhatsApp ID."""
    async def _provider_send() -> dict:
        if phone_number_id:
            return await whatsapp_client.send_text(
                to=to,
                body=body,
                phone_number_id=phone_number_id,
            )
        return await whatsapp_client.send_text(to=to, body=body)

    if not source_message_id:
        return await _provider_send()

    if await _is_inbound_revoked(source_message_id):
        return {"revoked": True}

    key = _reply_idempotency_key(source_message_id)
    token = await conversation_store.claim_outbound_reply(key)
    if token is None:
        logger.info(
            "Outbound reply suppressed by idempotency guard: message_id=%s",
            source_message_id,
        )
        return {"deduplicated": True}

    if await _is_inbound_revoked(source_message_id):
        await conversation_store.complete_outbound_reply(key, token)
        return {"revoked": True}

    try:
        response = await _provider_send()
    except WhatsAppSendError as exc:
        if exc.delivery_uncertain:
            # A timeout/reset may have occurred after Meta accepted the send. Keep
            # the guard completed so a later webhook retry cannot send a duplicate.
            await conversation_store.complete_outbound_reply(key, token)
        else:
            # Meta explicitly rejected the request; a retry cannot duplicate it.
            await conversation_store.release_outbound_reply(key, token)
        raise
    except Exception:
        # Unknown provider outcomes are treated as uncertain for duplicate safety.
        try:
            await conversation_store.complete_outbound_reply(key, token)
        except Exception:
            logger.exception(
                "Could not retain outbound reply guard after uncertain failure: message_id=%s",
                source_message_id,
            )
        raise

    await conversation_store.complete_outbound_reply(key, token)
    return response


async def _get_ai_reply_with_deadline(
    conversation: Conversation,
    user_message: str,
    *,
    transient_system_context: str | None = None,
    transient_user_message: str | None = None,
    tools_enabled: bool = True,
):
    timeout = max(1.0, float(settings.openai_turn_timeout_seconds))
    return await asyncio.wait_for(
        get_ai_reply(
            conversation,
            user_message,
            transient_system_context=transient_system_context,
            transient_user_message=transient_user_message,
            tools_enabled=tools_enabled,
        ),
        timeout=timeout,
    )


# =============================================================
# CONVERSATIONAL CLIENT AUTHENTICATION
# =============================================================

def _refresh_language_style(conversation: Conversation, safe_text: str | None) -> None:
    """Update turn-level style metadata from content that has already been redacted."""
    if not safe_text or not any(ch.isalpha() for ch in safe_text):
        # A password-only turn is stored as ********. Do not let that erase the
        # language/style established by the preceding customer request.
        return
    profile = detect_communication_style(
        safe_text,
        previous_style=conversation.communication_style,
    )
    conversation.current_language = profile.language
    conversation.communication_style = profile.style


_AUTH_USER_ID_MARKERS = (
    "user id",
    "userid",
    "user-id",
    "username",
    "identifiant utilisateur",
    "identifiant client",
    "معرّف المستخدم",
    "معرف المستخدم",
    "اسم المستخدم",
    "رقم المستخدم",
    "اليوزر",
    "يوزر",
)

_AUTH_PASSWORD_MARKERS = (
    "password",
    "mot de passe",
    "كلمة السر",
    "كلمة المرور",
    "الباسوورد",
    "باسوورد",
    "الباسورد",
    "باسورد",
)

_AUTH_REQUEST_ACTION_MARKERS = (
    "provide",
    "send",
    "give me",
    "enter",
    "i need",
    "required",
    "3tine",
    "a3tine",
    "b3atle",
    "ba3atle",
    "veuillez",
    "envoyez",
    "donnez",
    "j'ai besoin",
    "أرسل",
    "ارسل",
    "ابعت",
    "ابعث",
    "بعثلي",
    "اعطيني",
    "أعطيني",
    "تزويدي",
    "لازم",
)


def _assistant_auth_step(reply_text: str | None) -> str | None:
    """Detect a model-generated credential prompt without broad substring guesses."""
    folded = str(reply_text or "").casefold()
    asks_for_value = (
        any(marker in folded for marker in _AUTH_REQUEST_ACTION_MARKERS)
        or "?" in folded
        or "؟" in folded
    )
    if not asks_for_value:
        return None
    if any(marker in folded for marker in _AUTH_PASSWORD_MARKERS):
        return "password"
    if any(marker in folded for marker in _AUTH_USER_ID_MARKERS):
        return "userid"
    return None


def _replace_last_assistant_message(conversation: Conversation, body: str) -> None:
    """Replace the model's auth wording with a deterministic style-safe template."""
    safe_body = redact_sensitive_text(body)
    for index in range(len(conversation.messages) - 1, 0, -1):
        if conversation.messages[index].get("role") == "assistant":
            conversation.messages[index] = {
                "role": "assistant",
                "content": safe_body,
            }
            return
    conversation.messages.append({"role": "assistant", "content": safe_body})


def _guard_ai_reply_for_send(conversation: Conversation, body: str | None) -> str:
    """Final synchronous safety net immediately before persistence/WhatsApp send.

    The OpenAI service normally validates and repairs replies first. This second check
    makes the send boundary fail closed even if a future caller bypasses that service or
    a test/mocked result returns raw drafting text.
    """
    candidate = redact_sensitive_text(body).strip()
    if not candidate:
        return ""
    validation = validate_customer_reply(
        candidate,
        conversation.communication_style,
    )
    if validation.safe:
        return candidate

    logger.error(
        "Unsafe AI reply blocked at WhatsApp boundary: customer_ref=%s reasons=%s",
        safe_log_identifier(conversation.phone_number, "wa"),
        ",".join(validation.reasons),
    )
    fallback = reply_guard_fallback(conversation.communication_style)
    _replace_last_assistant_message(conversation, fallback)
    return fallback


def _append_customer_history(conversation: Conversation, text: str | None) -> None:
    safe = redact_sensitive_text(text)
    if safe:
        conversation.messages.append({"role": "user", "content": safe})


async def _send_and_record(
    conversation: Conversation,
    body: str,
    *,
    source_message_id: str | None,
) -> None:
    safe_body = redact_sensitive_text(body)
    conversation.messages.append({"role": "assistant", "content": safe_body})
    await conversation_store.save(conversation)
    await _send_text_once(
        to=conversation.phone_number,
        body=safe_body,
        source_message_id=source_message_id,
        phone_number_id=conversation.business_phone_number_id,
    )


async def _send_auth_failure(
    conversation: Conversation,
    *,
    source_message_id: str | None,
) -> None:
    await _send_and_record(
        conversation,
        auth_failure(conversation.communication_style),
        source_message_id=source_message_id,
    )


def _pending_auth_flow_active(conversation: Conversation) -> bool:
    """Whether this unauthenticated conversation still owns an unfinished login flow."""
    if conversation.verified_customer_id is not None:
        return False
    return bool(
        conversation.auth_state
        in {
            "awaiting_credentials",
            "awaiting_username",
            "awaiting_password",
            "authentication_failed",
        }
        or conversation.pending_auth_userid
        or conversation.pending_request
        or conversation.pending_tool_name
        or conversation.pending_tool_arguments
    )


def _pending_auth_interruption_kind(
    conversation: Conversation,
    text: str | None,
) -> str | None:
    """Classify an explicit auth cancellation or a clear change of subject."""
    if not _pending_auth_flow_active(conversation):
        return None
    if is_auth_cancel_intent(text):
        return "cancel"
    if is_auth_topic_switch_intent(text):
        return "topic_switch"
    return None


async def _abandon_pending_auth(
    conversation: Conversation,
    *,
    reason: str,
) -> None:
    """Clear every partial-auth artifact before processing the customer's next intent.

    Password-first flows keep the secret in a separate one-time store, so clearing only
    Conversation fields would still leave a credential behind. This helper clears both
    stores and persists the reset immediately so a retry cannot fall back into the old
    authentication state.
    """
    conversation.pending_auth_userid = None
    conversation.pending_request = None
    conversation.pending_tool_name = None
    conversation.pending_tool_arguments = None
    conversation.auth_state = "guest"
    await conversation_store.clear_pending_auth_password(conversation.storage_key)
    conversation.messages.append(
        {
            "role": "system",
            "content": (
                "AUTH EVENT: The customer abandoned the pending authentication flow. "
                "The previous protected request and all partial credential state were "
                "cleared. Do not ask for those credentials again unless a later fresh "
                "protected request requires authentication."
            ),
        }
    )
    await conversation_store.save(conversation)
    logger.info(
        "Pending authentication abandoned: customer_ref=%s reason=%s",
        safe_log_identifier(conversation.phone_number, "wa"),
        reason,
    )


async def _complete_authentication(
    conversation: Conversation,
    *,
    userid: str,
    password: str,
) -> bool:
    """Verify credentials using the existing backend auth service only."""
    conversation.auth_state = "authenticating"
    authenticated = await authenticate_customer(userid=userid, password=password)

    # The password must not outlive this verification attempt. This clears a
    # password-first one-time secret even when the supplied pair was invalid.
    await conversation_store.clear_pending_auth_password(conversation.storage_key)

    if authenticated is None:
        conversation.pending_auth_userid = None
        conversation.auth_state = "authentication_failed"
        await conversation_store.save(conversation)
        logger.info(
            "Conversational customer authentication failed: customer_ref=%s",
            safe_log_identifier(conversation.phone_number, "wa"),
        )
        return False

    conversation.verified_customer_id = authenticated.customer_id
    conversation.authenticated_userid = authenticated.userid
    conversation.authenticated_name = authenticated.name
    conversation.pending_auth_userid = None
    conversation.auth_state = "authenticated"
    await conversation_store.save(conversation)
    logger.info(
        "Conversational customer authentication succeeded: customer_ref=%s",
        safe_log_identifier(conversation.phone_number, "wa"),
    )
    return True


async def _attempt_chat_login(
    conversation: Conversation,
    *,
    userid: str,
    password: str,
    source_message_id: str | None,
) -> tuple[bool, str]:
    # This limiter is intentionally independent from normal chat throughput.
    if not client_auth_rate_limiter.allow(conversation.phone_number):
        conversation.pending_auth_userid = None
        conversation.auth_state = "authentication_failed"
        await conversation_store.clear_pending_auth_password(conversation.storage_key)
        await _send_and_record(
            conversation,
            auth_rate_limited(conversation.communication_style),
            source_message_id=source_message_id,
        )
        logger.warning(
            "Conversational customer authentication rate-limited: customer_ref=%s",
            safe_log_identifier(conversation.phone_number, "wa"),
        )
        return True, "authentication_rate_limited"

    try:
        authenticated = await _complete_authentication(
            conversation,
            userid=userid,
            password=password,
        )
    except CustomerAuthenticationUnavailable:
        conversation.auth_state = "awaiting_credentials"
        conversation.pending_auth_userid = None
        await conversation_store.clear_pending_auth_password(conversation.storage_key)
        await _send_and_record(
            conversation,
            auth_unavailable(conversation.communication_style),
            source_message_id=source_message_id,
        )
        logger.warning(
            "Conversational customer authentication backend unavailable: customer_ref=%s",
            safe_log_identifier(conversation.phone_number, "wa"),
        )
        return True, "authentication_unavailable"

    if not authenticated:
        await _send_auth_failure(
            conversation,
            source_message_id=source_message_id,
        )
        return True, "authentication_failed"

    ok, outcome = await _resume_after_auth(
        conversation,
        source_message_id=source_message_id,
    )
    return ok, outcome


async def _handle_conversational_auth(
    conversation: Conversation,
    parsed: CredentialExtraction,
    *,
    raw_text: str,
    source_message_id: str | None,
) -> tuple[bool, str] | None:
    """Handle one redacted conversational-auth turn, or return None for normal AI."""
    if conversation.verified_customer_id is not None:
        if is_logout_intent(raw_text):
            _append_customer_history(conversation, parsed.redacted_text or raw_text)
            conversation.verified_customer_id = None
            conversation.authenticated_userid = None
            conversation.authenticated_name = None
            conversation.pending_auth_userid = None
            conversation.pending_request = None
            conversation.pending_tool_name = None
            conversation.pending_tool_arguments = None
            conversation.auth_state = "guest"
            await conversation_store.clear_pending_auth_password(conversation.storage_key)
            await _send_and_record(
                conversation,
                logout_confirmation(conversation.communication_style),
                source_message_id=source_message_id,
            )
            return True, "logged_out"

        # A chat message alone must never silently replace backend-established identity.
        if parsed.is_authentication_attempt and (
            parsed.username_present or parsed.password_present
        ):
            _append_customer_history(conversation, parsed.redacted_text)
            await _send_and_record(
                conversation,
                already_authenticated(conversation.communication_style),
                source_message_id=source_message_id,
            )
            return True, "already_authenticated"
        return None

    if not parsed.is_authentication_attempt:
        return None

    _append_customer_history(conversation, parsed.redacted_text)

    if parsed.ambiguous:
        conversation.pending_auth_userid = None
        conversation.auth_state = "awaiting_credentials"
        await conversation_store.clear_pending_auth_password(conversation.storage_key)
        await _send_and_record(
            conversation,
            auth_clarification(conversation.communication_style),
            source_message_id=source_message_id,
        )
        return True, "authentication_clarification_required"

    # NOTE: extract_credentials() guarantees a single inbound message never yields both
    # a username and a password (strict two-step auth). Login therefore only ever
    # proceeds below, once the User ID and password have each arrived on their own,
    # separate turn.

    if parsed.username is not None:
        # Password-first flow: consume the one-time secret atomically. If it expired,
        # keep the username (non-secret) and ask only for the password.
        if conversation.auth_state == "awaiting_username":
            pending_password = await conversation_store.pop_pending_auth_password(
                conversation.storage_key
            )
            if pending_password is not None:
                return await _attempt_chat_login(
                    conversation,
                    userid=parsed.username,
                    password=pending_password,
                    source_message_id=source_message_id,
                )

        conversation.pending_auth_userid = parsed.username
        logger.info(
            "AUTH STATE SET: userid=%s state=%s",
            conversation.pending_auth_userid,
            conversation.auth_state,
        )
        conversation.auth_state = "awaiting_password"
        await _send_and_record(
            conversation,
            password_prompt(conversation.communication_style),
            source_message_id=source_message_id,
        )
        return True, "waiting_for_password"

    if parsed.password is not None:
        logger.info(
            "AUTH PASSWORD STEP: pending_userid=%s password_length=%d",
            conversation.pending_auth_userid,
            len(parsed.password),
        )

        if conversation.pending_auth_userid:
            pending_userid = conversation.pending_auth_userid
            return await _attempt_chat_login(
                conversation,
                userid=pending_userid,
                password=parsed.password,
                source_message_id=source_message_id,
            )

        await conversation_store.stash_pending_auth_password(
            conversation.storage_key,
            parsed.password,
        )
        conversation.auth_state = "awaiting_username"
        await _send_and_record(
            conversation,
            userid_prompt(conversation.communication_style),
            source_message_id=source_message_id,
        )
        return True, "waiting_for_userid"

    conversation.auth_state = "awaiting_credentials"
    await _send_and_record(
        conversation,
        auth_prompt(conversation.communication_style),
        source_message_id=source_message_id,
    )
    return True, "waiting_for_credentials"


async def _handle_ai_result(
    *,
    conversation: Conversation,
    incoming: IncomingWhatsAppMessage | None,
    user_message_for_ticket: str | None,
) -> tuple[bool, str]:
    """Run one bounded AI turn and send at most one reply for its inbound ID."""
    history_len_before_ai = len(conversation.messages)
    ai_user_message = incoming.text if incoming is not None and incoming.text is not None else ""
    source_message_id = incoming.message_id if incoming is not None else None

    try:
        result = await _get_ai_reply_with_deadline(conversation, ai_user_message)
    except TimeoutError:
        logger.warning(
            "AI turn timed out: customer_ref=%s message_id=%s",
            safe_log_identifier(conversation.phone_number, "wa"),
            source_message_id,
        )
        conversation.messages = conversation.messages[:history_len_before_ai]
        if ai_user_message:
            conversation.messages.append(
                {"role": "user", "content": redact_sensitive_text(ai_user_message)}
            )
        fallback = ai_unavailable_message(conversation.communication_style)
        conversation.messages.append({"role": "assistant", "content": fallback})
        await conversation_store.save(conversation)
        await _send_text_once(
            to=conversation.phone_number,
            body=fallback,
            source_message_id=source_message_id,
            phone_number_id=conversation.business_phone_number_id,
        )
        return True, "ai_timeout_notified"

    # Authentication is a backend-controlled flow. When a protected tool reports that
    # authentication is required, or the model independently phrases a credential
    # request, always retain the interrupted customer request and replace the model's
    # wording with the deterministic first-step prompt. This closes the path where the
    # model asked for a User ID without setting auth_required, causing login to succeed
    # later with no pending request to resume. It also prevents script/language mixing
    # in the credential prompt.
    assistant_auth_step = _assistant_auth_step(result.reply_text)
    guarded_reply = _guard_ai_reply_for_send(conversation, result.reply_text)
    if (
        conversation.verified_customer_id is None
        and (result.auth_required or assistant_auth_step is not None)
    ):
        conversation.auth_state = "awaiting_credentials"
        conversation.pending_auth_userid = None
        if ai_user_message:
            conversation.pending_request = redact_sensitive_text(ai_user_message)
        if result.auth_tool_name in NEEDS_VERIFIED_IDENTITY:
            conversation.pending_tool_name = result.auth_tool_name
            conversation.pending_tool_arguments = dict(
                result.auth_tool_arguments or {}
            )
        else:
            # Do not retain a stale action from an older interrupted login flow.
            conversation.pending_tool_name = None
            conversation.pending_tool_arguments = None

        prompt = auth_prompt(conversation.communication_style)
        _replace_last_assistant_message(conversation, prompt)
        await conversation_store.save(conversation)
        await _send_text_once(
            to=conversation.phone_number,
            body=prompt,
            source_message_id=source_message_id,
            phone_number_id=conversation.business_phone_number_id,
        )
        logger.info(
            "Authentication gate activated: customer_ref=%s model_flag=%s "
            "model_prompt_step=%s pending_request=%s style=%s",
            safe_log_identifier(conversation.phone_number, "wa"),
            result.auth_required,
            assistant_auth_step,
            bool(conversation.pending_request),
            conversation.communication_style,
        )
        return True, "authentication_required"

    if result.handoff is not None:
        try:
            ticket, _created = await support_service.finalize_handoff(
                conversation=conversation,
                handoff=result.handoff,
                source_whatsapp_message_id=source_message_id,
                source_message_text=user_message_for_ticket,
                source_whatsapp_timestamp=(incoming.timestamp if incoming else None),
                reply_to_whatsapp_message_id=(
                    incoming.context_message_id if incoming else None
                ),
                business_phone_number_id=(
                    incoming.business_phone_number_id if incoming else None
                ),
                business_display_phone_number=(
                    incoming.business_display_phone_number if incoming else None
                ),
            )
        except Exception:
            logger.exception("Durable ticket creation failed during human handoff")
            # Roll back model-only handoff output. A fallback is actually sent below,
            # so this inbound message is considered handled and must not be retried by
            # Meta; retrying after a sent fallback was one duplicate-message path.
            conversation.messages = conversation.messages[:history_len_before_ai]
            if ai_user_message:
                conversation.messages.append(
                    {"role": "user", "content": redact_sensitive_text(ai_user_message)}
                )
            fallback = (
                "I'm having trouble connecting you to human support right now. "
                "Please try again shortly."
            )
            conversation.messages.append({"role": "assistant", "content": fallback})
            conversation.mode = "ai"
            conversation.active_ticket_id = None
            await conversation_store.save(conversation)
            await _send_text_once(
                to=conversation.phone_number,
                body=fallback,
                source_message_id=source_message_id,
                phone_number_id=conversation.business_phone_number_id,
            )
            return True, "handoff_failed_notified"

        if guarded_reply:
            await _send_text_once(
                to=conversation.phone_number,
                body=guarded_reply,
                source_message_id=source_message_id,
                phone_number_id=conversation.business_phone_number_id,
            )
        logger.info(
            "Conversation handed to support: customer_ref=%s ticket_id=%s",
            safe_log_identifier(conversation.phone_number, "wa"),
            ticket["id"],
        )
        return True, "handed_off"

    await conversation_store.save(conversation)
    if guarded_reply:
        await _send_text_once(
            to=conversation.phone_number,
            body=guarded_reply,
            source_message_id=source_message_id,
            phone_number_id=conversation.business_phone_number_id,
        )
        return True, "handled"

    fallback = ai_unavailable_message(conversation.communication_style)
    conversation.messages.append({"role": "assistant", "content": fallback})
    await conversation_store.save(conversation)
    await _send_text_once(
        to=conversation.phone_number,
        body=fallback,
        source_message_id=source_message_id,
        phone_number_id=conversation.business_phone_number_id,
    )
    return True, "empty_ai_reply_notified"


async def _resume_after_auth(
    conversation: Conversation,
    *,
    source_message_id: str | None,
) -> tuple[bool, str]:
    """Resume the exact operation interrupted by the two-step login flow.

    Credential turns are never sent to the model. When the original AI turn reached a
    protected tool, its safe tool name/arguments are replayed directly by the backend
    with the newly verified customer identity, then the model only formats that
    authoritative result. If no blocked tool was captured, the pending request is
    re-presented ephemerally as the active user turn and the response is accepted only
    after a protected tool actually runs.
    """
    pending_request = redact_sensitive_text(conversation.pending_request or "").strip()
    pending_tool_name = str(conversation.pending_tool_name or "").strip() or None
    pending_tool_arguments = dict(conversation.pending_tool_arguments or {})

    if not pending_request:
        # Explicit login with no interrupted protected request: do not spend an AI turn
        # generating a potentially mixed-language confirmation.
        conversation.pending_tool_name = None
        conversation.pending_tool_arguments = None
        confirmation = login_confirmation(conversation.communication_style)
        conversation.messages.append({"role": "assistant", "content": confirmation})
        await conversation_store.save(conversation)
        await _send_text_once(
            to=conversation.phone_number,
            body=confirmation,
            source_message_id=source_message_id,
            phone_number_id=conversation.business_phone_number_id,
        )
        return True, "authenticated"

    history_len_before_ai = len(conversation.messages)
    rollback_history_len = history_len_before_ai

    try:
        if pending_tool_name in NEEDS_VERIFIED_IDENTITY:
            assert conversation.verified_customer_id is not None
            authoritative_result = await dispatch_tool_call(
                name=pending_tool_name,
                arguments=pending_tool_arguments,
                verified_customer_id=conversation.verified_customer_id,
            )

            # The protected operation has now run exactly once. Clear the replay action
            # before rendering its result, especially important for a state-changing
            # operation such as update_shipment_mode. Keep the original request until a
            # customer-facing answer is successfully prepared.
            conversation.pending_tool_name = None
            conversation.pending_tool_arguments = None

            deterministic_reply = render_authoritative_tool_result(
                pending_tool_name,
                authoritative_result,
                conversation.communication_style,
            )
            if deterministic_reply:
                # Order lists are rendered directly from authoritative identifiers. This
                # avoids a second generative pass entirely on the exact incident path.
                conversation.pending_request = None
                conversation.messages.append(
                    {"role": "assistant", "content": deterministic_reply}
                )
                await conversation_store.save(conversation)
                await _send_text_once(
                    to=conversation.phone_number,
                    body=deterministic_reply,
                    source_message_id=source_message_id,
                    phone_number_id=conversation.business_phone_number_id,
                )
                return True, "authenticated_and_handled"

            tool_event = (
                "AUTH EVENT: Backend credential verification succeeded. The backend "
                "has already executed the protected customer operation that was blocked "
                "before login. Treat the following JSON as fresh authoritative "
                "application output. Answer the pending request now in the customer's "
                "current communication style. Do not merely confirm login, do not ask "
                "the customer to repeat the request, do not mention internal tool names, "
                "and do not invent values. AUTHORITATIVE_RESULT_JSON="
                + json.dumps(authoritative_result, ensure_ascii=False, separators=(",", ":"))
            )
            # Persist only the cleared replay marker. The authoritative result is
            # supplied ephemerally to the renderer and is not left in long-lived chat
            # history where it could compete with fresher shipment data later.
            await conversation_store.save(conversation)

            result = await _get_ai_reply_with_deadline(
                conversation,
                "",
                transient_system_context=tool_event,
                transient_user_message=pending_request,
                tools_enabled=False,
            )
        else:
            transient_context = (
                "AUTH EVENT: Backend credential verification succeeded and the server "
                "has established the authenticated customer identity. The pending "
                "request below is the active customer turn. Execute it now in this same "
                "response. Do not send a login-only confirmation and do not ask the "
                "customer to repeat anything. Call the appropriate protected customer "
                "tool before answering; for a generic request to see/check orders or "
                "shipments without one exact reference, call get_customer_shipments. "
                "Never repeat or describe credential values."
            )
            result = await _get_ai_reply_with_deadline(
                conversation,
                "",
                transient_system_context=transient_context,
                transient_user_message=pending_request,
            )

            protected_tool_ran = any(
                name in NEEDS_VERIFIED_IDENTITY for name in result.tool_names
            )
            if result.handoff is None and not protected_tool_ran:
                # Roll back a login-only/model-only answer and make one stricter retry.
                # This prevents the exact observed behavior: "login successful" followed
                # by silence until the customer asks for the order again.
                logger.warning(
                    "Pending request resume produced no protected tool call; retrying: "
                    "customer_ref=%s tools=%s",
                    safe_log_identifier(conversation.phone_number, "wa"),
                    result.tool_names,
                )
                conversation.messages = conversation.messages[:history_len_before_ai]
                retry_context = (
                    transient_context
                    + " REQUIRED: do not answer until one of the protected customer "
                    "tools has been called successfully for this pending request."
                )
                result = await _get_ai_reply_with_deadline(
                    conversation,
                    "",
                    transient_system_context=retry_context,
                    transient_user_message=pending_request,
                )
                protected_tool_ran = any(
                    name in NEEDS_VERIFIED_IDENTITY for name in result.tool_names
                )
                if result.handoff is None and not protected_tool_ran:
                    conversation.messages = conversation.messages[:history_len_before_ai]
                    fallback = pending_request_unavailable(
                        conversation.communication_style
                    )
                    conversation.messages.append(
                        {"role": "assistant", "content": fallback}
                    )
                    # Keep pending_request so an operator can diagnose/retry without the
                    # original customer intent being lost. No protected side effect ran.
                    await conversation_store.save(conversation)
                    await _send_text_once(
                        to=conversation.phone_number,
                        body=fallback,
                        source_message_id=source_message_id,
                        phone_number_id=conversation.business_phone_number_id,
                    )
                    return True, "authentication_succeeded_resume_tool_missing"

    except TimeoutError:
        logger.warning(
            "AI resume after authentication timed out: customer_ref=%s",
            safe_log_identifier(conversation.phone_number, "wa"),
        )
        conversation.messages = conversation.messages[:rollback_history_len]
        fallback = pending_request_unavailable(conversation.communication_style)
        conversation.messages.append({"role": "assistant", "content": fallback})
        # Keep pending_request and the cleared replay fields. A state-changing operation
        # must never execute twice merely because response rendering timed out.
        await conversation_store.save(conversation)
        await _send_text_once(
            to=conversation.phone_number,
            body=fallback,
            source_message_id=source_message_id,
            phone_number_id=conversation.business_phone_number_id,
        )
        return True, "authentication_succeeded_ai_timeout_notified"

    guarded_reply = _guard_ai_reply_for_send(conversation, result.reply_text)

    if result.handoff is not None:
        try:
            ticket, _created = await support_service.finalize_handoff(
                conversation=conversation,
                handoff=result.handoff,
                source_whatsapp_message_id=None,
                source_message_text=None,
            )
        except Exception:
            logger.exception("Ticket creation failed while resuming after authentication")
            conversation.messages = conversation.messages[:rollback_history_len]
            fallback = pending_request_unavailable(conversation.communication_style)
            conversation.messages.append({"role": "assistant", "content": fallback})
            conversation.mode = "ai"
            conversation.active_ticket_id = None
            await conversation_store.save(conversation)
            await _send_text_once(
                to=conversation.phone_number,
                body=fallback,
                source_message_id=source_message_id,
                phone_number_id=conversation.business_phone_number_id,
            )
            return True, "authenticated_handoff_failed_notified"

        conversation.pending_request = None
        conversation.pending_tool_name = None
        conversation.pending_tool_arguments = None
        await conversation_store.save(conversation)
        if guarded_reply:
            await _send_text_once(
                to=conversation.phone_number,
                body=guarded_reply,
                source_message_id=source_message_id,
                phone_number_id=conversation.business_phone_number_id,
            )
        logger.info(
            "Authenticated conversation handed to support: customer_ref=%s ticket_id=%s",
            safe_log_identifier(conversation.phone_number, "wa"),
            ticket["id"],
        )
        return True, "authenticated_and_handed_off"

    if guarded_reply:
        conversation.pending_request = None
        conversation.pending_tool_name = None
        conversation.pending_tool_arguments = None
        await conversation_store.save(conversation)
        await _send_text_once(
            to=conversation.phone_number,
            body=guarded_reply,
            source_message_id=source_message_id,
            phone_number_id=conversation.business_phone_number_id,
        )
        return True, "authenticated_and_handled"

    conversation.messages = conversation.messages[:rollback_history_len]
    fallback = pending_request_unavailable(conversation.communication_style)
    conversation.messages.append({"role": "assistant", "content": fallback})
    await conversation_store.save(conversation)
    await _send_text_once(
        to=conversation.phone_number,
        body=fallback,
        source_message_id=source_message_id,
        phone_number_id=conversation.business_phone_number_id,
    )
    return True, "authentication_succeeded_empty_ai_reply_notified"


# =============================================================
# HEALTH
# =============================================================

@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# =============================================================
# WHATSAPP WEBHOOK VERIFICATION
# =============================================================

@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403)

def _human_inbound_body(
    incoming: IncomingWhatsAppMessage,
    parsed_auth: CredentialExtraction,
) -> str:
    if incoming.message_type == "contacts":
        return format_shared_contacts(incoming.contacts)
    if incoming.message_type == "image" and incoming.media_id:
        return sanitize_support_text(incoming.media_caption).strip() or "Image"
    if incoming.message_type == "sticker" and incoming.media_id:
        return "Sticker"
    if incoming.message_type == "video" and incoming.media_id:
        return sanitize_support_text(incoming.media_caption).strip() or "Video"
    if incoming.message_type == "audio" and incoming.media_id:
        return "Voice note" if incoming.is_voice else "Audio message"
    if incoming.message_type == "document" and incoming.media_id:
        caption = sanitize_support_text(incoming.media_caption).strip()
        filename = sanitize_support_text(incoming.media_filename).strip()
        return caption or filename or "PDF document"
    if incoming.message_type == "location":
        name = sanitize_support_text(incoming.location_name).strip()
        address = sanitize_support_text(incoming.location_address).strip()
        if name and address:
            return f"Location: {name} — {address}"
        if name or address:
            return f"Location: {name or address}"
        if incoming.location_latitude is not None and incoming.location_longitude is not None:
            return f"Location: {incoming.location_latitude:.6f}, {incoming.location_longitude:.6f}"
        return "Location"
    if incoming.text is None:
        return f"[Unsupported WhatsApp {incoming.message_type} message received]"
    if parsed_auth.secret_detected:
        return sanitize_support_text(parsed_auth.redacted_text)
    return sanitize_support_text(incoming.text)


async def _handoff_incoming_without_ai(
    *,
    conversation: Conversation,
    incoming: IncomingWhatsAppMessage,
    parsed_auth: CredentialExtraction,
    reason: str,
    summary: str,
    status_name: str,
) -> Response:
    """Ensure one inbound message is durably owned by human support, without AI.

    Ticket creation is the authoritative boundary. Existing repository uniqueness
    constraints make concurrent/retried calls idempotent. Media is downloaded only
    after the ticket and timeline row exist; metadata alone is enough to identify a
    WhatsApp voice note for routing.
    """
    source_text = _human_inbound_body(incoming, parsed_auth) if incoming.text is not None else None
    try:
        ticket, created = await support_service.finalize_handoff(
            conversation=conversation,
            handoff=HandoffRequest(reason=reason, summary=summary),
            source_whatsapp_message_id=incoming.message_id,
            source_message_text=source_text,
            source_whatsapp_timestamp=incoming.timestamp,
            reply_to_whatsapp_message_id=incoming.context_message_id,
            business_phone_number_id=incoming.business_phone_number_id,
            business_display_phone_number=incoming.business_display_phone_number,
        )
    except Exception:
        logger.exception(
            "Durable human handoff failed before AI routing: customer_ref=%s "
            "message_id=%s reason=%s",
            safe_log_identifier(conversation.phone_number, "wa"),
            incoming.message_id,
            reason,
        )
        return _json_response(503, {"status": "human_handoff_unavailable"})

    # finalize_handoff persists text triggers itself. Media and other non-text
    # messages need their own durable timeline row after the ticket exists.
    if incoming.text is None:
        try:
            message, _message_created = await support_service.route_human_inbound(
                ticket=ticket,
                body=_human_inbound_body(incoming, parsed_auth),
                whatsapp_message_id=incoming.message_id,
                whatsapp_timestamp=incoming.timestamp,
                message_type=incoming.message_type,
                reply_to_whatsapp_message_id=incoming.context_message_id,
                location_latitude=incoming.location_latitude,
                location_longitude=incoming.location_longitude,
                location_name=incoming.location_name,
                location_address=incoming.location_address,
                current_language=conversation.current_language,
                communication_style=conversation.communication_style,
            )
            if incoming.message_type in SUPPORT_MEDIA_MESSAGE_TYPES and incoming.media_id:
                try:
                    await support_service.capture_inbound_media(
                        ticket=ticket,
                        message=message,
                        media_id=incoming.media_id,
                        message_type=incoming.message_type,
                        expected_mime_type=incoming.media_mime_type,
                        filename=incoming.media_filename,
                    )
                except SupportMediaError as exc:
                    if exc.retryable:
                        logger.warning(
                            "Human-handoff media capture will be retried: customer_ref=%s "
                            "message_id=%s type=%s reason=%s error=%s",
                            safe_log_identifier(conversation.phone_number, "wa"),
                            incoming.message_id,
                            incoming.message_type,
                            reason,
                            sanitize_support_text(str(exc))[:300],
                        )
                        return _json_response(503, {"status": "media_capture_retry"})
                    logger.warning(
                        "Human-handoff media could not be stored: customer_ref=%s "
                        "message_id=%s type=%s reason=%s error=%s",
                        safe_log_identifier(conversation.phone_number, "wa"),
                        incoming.message_id,
                        incoming.message_type,
                        reason,
                        sanitize_support_text(str(exc))[:300],
                    )
        except SupportConflictError:
            # An agent may resolve a just-created ticket before this secondary media
            # row is inserted. Retrying the webhook is safer than losing the inbound.
            logger.warning(
                "Human handoff changed state while persisting inbound: customer_ref=%s "
                "message_id=%s reason=%s",
                safe_log_identifier(conversation.phone_number, "wa"),
                incoming.message_id,
                reason,
            )
            return _json_response(503, {"status": "human_message_persist_failed"})
        except Exception:
            logger.exception(
                "Failed to persist inbound after human handoff: customer_ref=%s "
                "message_id=%s reason=%s",
                safe_log_identifier(conversation.phone_number, "wa"),
                incoming.message_id,
                reason,
            )
            return _json_response(503, {"status": "human_message_persist_failed"})

    logger.info(
        "Inbound routed to human without AI: customer_ref=%s message_id=%s "
        "ticket_id=%s reason=%s created=%s",
        safe_log_identifier(conversation.phone_number, "wa"),
        incoming.message_id,
        ticket["id"],
        reason,
        created,
    )
    return _json_response(
        200,
        {"status": status_name, "ticket_id": ticket["id"], "duplicate": not created},
    )


async def _process_incoming_message(
    incoming: IncomingWhatsAppMessage,
    *,
    ai_availability_snapshot: EffectiveAIAvailability | None = None,
) -> Response:
    """
    Process one uniquely claimed inbound WhatsApp message.

    The caller is responsible for inbound message idempotency.
    """

    from_number = incoming.from_number

    # Reactions mutate metadata on an existing message. They must never enter the
    # AI pipeline, create a new ticket, or appear as standalone timeline messages.
    if incoming.message_type == "reaction":
        try:
            reaction = await support_service.route_whatsapp_reaction(
                from_number=incoming.from_number,
                target_whatsapp_message_id=incoming.reaction_message_id,
                emoji=incoming.reaction_emoji,
                provider_reaction_id=incoming.message_id,
                provider_timestamp=incoming.timestamp,
                business_phone_number_id=incoming.business_phone_number_id,
            )
        except Exception:
            logger.exception(
                "Could not persist WhatsApp reaction: message_id=%s target_id=%s",
                incoming.message_id,
                incoming.reaction_message_id,
            )
            return _json_response(503, {"status": "reaction_persist_failed"})
        return _json_response(
            200,
            {
                "status": (
                    "reaction_removed"
                    if reaction is not None and not str(reaction.get("emoji") or "")
                    else "reaction_updated"
                    if reaction is not None
                    else "reaction_target_not_found"
                )
            },
        )

    # Never log inbound body. Password-only messages can be
    # indistinguishable from ordinary text until conversation state
    # is loaded.
    logger.info(
        "Incoming WhatsApp message: "
        "customer_ref=%s message_id=%s type=%s text_chars=%d",
        safe_log_identifier(from_number, "wa"),
        incoming.message_id,
        incoming.message_type,
        len(incoming.text or ""),
    )

    conversation = await conversation_store.get_or_create(
        from_number,
        conversation_key=_conversation_route_key(incoming),
        business_phone_number_id=incoming.business_phone_number_id,
        business_display_phone_number=incoming.business_display_phone_number,
    )

    stale_reason = _stale_inbound_reason(conversation, incoming)
    if stale_reason is not None:
        logger.warning(
            "Stale inbound WhatsApp message ignored: "
            "customer_ref=%s message_id=%s reason=%s timestamp=%s last_timestamp=%s",
            safe_log_identifier(from_number, "wa"),
            incoming.message_id,
            stale_reason,
            incoming.timestamp,
            conversation.last_inbound_timestamp,
        )
        return _json_response(
            200,
            {
                "status": "stale_message_ignored",
                "reason": stale_reason,
            },
        )

    # A pending credential prompt must not trap the customer in authentication. Detect
    # explicit cancellation and clear topic changes before contextual extraction,
    # otherwise ``awaiting_password`` deliberately treats any unlabeled text as the
    # password (including an obvious new shipping question).
    auth_interruption = _pending_auth_interruption_kind(conversation, incoming.text)
    if auth_interruption is not None:
        try:
            await _abandon_pending_auth(conversation, reason=auth_interruption)
        except Exception:
            logger.exception(
                "Could not clear interrupted authentication flow for %s",
                safe_log_identifier(from_number, "wa"),
            )
            return _json_response(503, {"status": "authentication_unavailable"})

    parsed_auth = extract_credentials(
        incoming.text,
        auth_state=conversation.auth_state,
        pending_username=conversation.pending_auth_userid,
        has_pending_password=(conversation.auth_state == "awaiting_username"),
    )

    logger.info(
        "AUTH DEBUG BEFORE HANDLE: phone=%s state=%s pending_userid=%s "
        "parsed_username=%s parsed_password=%s ambiguous=%s",
        safe_log_identifier(from_number, "wa"),
        conversation.auth_state,
        conversation.pending_auth_userid,
        parsed_auth.username_present,
        parsed_auth.password_present,
        parsed_auth.ambiguous,
    )

    if parsed_auth.is_authentication_attempt:
        # Safe parser diagnostics: record only extraction shape, never credential
        # values or the raw inbound text. This makes parser-vs-verifier failures
        # distinguishable in production without leaking secrets.
        logger.info(
            "Conversational auth parsed: customer_ref=%s auth_state=%s "
            "username_present=%s password_present=%s ambiguous=%s",
            safe_log_identifier(from_number, "wa"),
            conversation.auth_state,
            parsed_auth.username_present,
            parsed_auth.password_present,
            parsed_auth.ambiguous,
        )
    safe_turn_text = (
        parsed_auth.redacted_text
        if parsed_auth.is_authentication_attempt
        else redact_sensitive_text(incoming.text)
    )
    _refresh_language_style(conversation, safe_turn_text)

    customer_ref = safe_log_identifier(from_number, "wa")

    # MariaDB ticket state is authoritative.
    try:
        active_ticket = await _get_active_ticket_for_incoming(incoming)

        await support_service.reconcile_conversation_mode(
            conversation,
            active_ticket,
        )

    except Exception:
        logger.exception(
            "Could not determine durable support ownership for %s",
            customer_ref,
        )

        return _json_response(
            503,
            {
                "status": "support_state_unavailable"
            },
        )

    # Existing durable support idempotency can remain as an
    # additional safeguard.
    if (
        active_ticket is None
        and incoming.message_id
    ):
        try:
            previous_support_message = (
                await support_service.get_message_by_whatsapp_id(
                    incoming.message_id
                )
            )

            previous_handoff = (
                await support_service.get_ticket_by_source_message(
                    incoming.message_id
                )
            )

        except Exception:
            logger.exception(
                "Could not check support idempotency for %s",
                customer_ref,
            )

            return _json_response(
                503,
                {
                    "status": "support_state_unavailable"
                },
            )

        if previous_support_message is not None:
            return _json_response(
                200,
                {
                    "status": "duplicate_support_message",
                    "ticket_id": (
                        previous_support_message[
                            "ticket_id"
                        ]
                    ),
                },
            )

        if previous_handoff is not None:
            return _json_response(
                200,
                {
                    "status": "duplicate_handoff_event",
                    "ticket_id": previous_handoff["id"],
                },
            )

    # =========================================================
    # ACTIVE HUMAN SUPPORT
    # =========================================================

    if active_ticket is not None:

        # Staff may see authentication attempts for continuity, but never the
        # password itself. The same formatter is used by automatic handoffs.
        body = _human_inbound_body(incoming, parsed_auth)

        try:
            _message, created = (
                await support_service.route_human_inbound(
                    ticket=active_ticket,
                    body=body,
                    whatsapp_message_id=(
                        incoming.message_id
                    ),
                    whatsapp_timestamp=(
                        incoming.timestamp
                    ),
                    message_type=(
                        incoming.message_type
                    ),
                    reply_to_whatsapp_message_id=incoming.context_message_id,
                    location_latitude=incoming.location_latitude,
                    location_longitude=incoming.location_longitude,
                    location_name=incoming.location_name,
                    location_address=incoming.location_address,
                    current_language=conversation.current_language,
                    communication_style=conversation.communication_style,
                )
            )

            if incoming.message_type in SUPPORT_MEDIA_MESSAGE_TYPES and incoming.media_id:
                try:
                    await support_service.capture_inbound_media(
                        ticket=active_ticket,
                        message=_message,
                        media_id=incoming.media_id,
                        message_type=incoming.message_type,
                        expected_mime_type=incoming.media_mime_type,
                        filename=incoming.media_filename,
                    )
                except SupportMediaError as exc:
                    if exc.retryable:
                        logger.warning(
                            "Inbound WhatsApp media capture will be retried: customer_ref=%s "
                            "message_id=%s type=%s error=%s",
                            customer_ref,
                            incoming.message_id,
                            incoming.message_type,
                            sanitize_support_text(str(exc))[:300],
                        )
                        return _json_response(503, {"status": "media_capture_retry"})
                    logger.warning(
                        "Inbound WhatsApp media could not be stored: customer_ref=%s "
                        "message_id=%s type=%s error=%s",
                        customer_ref,
                        incoming.message_id,
                        incoming.message_type,
                        sanitize_support_text(str(exc))[:300],
                    )

        except SupportConflictError:
            # Resolve-to-AI may have won the row lock after the
            # active-ticket lookup but before message insertion.
            try:
                active_ticket = (
                    await _get_active_ticket_for_incoming(incoming)
                )

                await (
                    support_service
                    .reconcile_conversation_mode(
                        conversation,
                        active_ticket,
                    )
                )

            except Exception:
                logger.exception(
                    "Could not reconcile support state "
                    "after inbound/resolve race"
                )

                return _json_response(
                    503,
                    {
                        "status": (
                            "support_state_unavailable"
                        )
                    },
                )

            if active_ticket is not None:
                return _json_response(
                    503,
                    {
                        "status": (
                            "human_message_persist_failed"
                        )
                    },
                )

        except Exception:
            logger.exception(
                "Failed to persist human-mode "
                "inbound message"
            )

            return _json_response(
                503,
                {
                    "status": (
                        "human_message_persist_failed"
                    )
                },
            )

        else:
            return _json_response(
                200,
                {
                    "status": "held_for_human",
                    "duplicate": not created,
                },
            )

    # =========================================================
    # HIGH-PRIORITY HUMAN MEDIA ROUTING / AI AVAILABILITY
    # =========================================================

    # The customer-service application supports WhatsApp images, videos, audio/voice,
    # PDFs, and customer locations for human agents, but the AI pipeline does not
    # currently process those message types. Route them into the existing human pipeline
    # before evaluating AI availability. The media is then downloaded and attached
    # to the durable ticket so staff can view/play it in the support dashboard.
    #
    # Meta marks push-to-talk voice notes as type=audio with audio.voice=true; that
    # flag only selects the more specific handoff reason/status. No transcription,
    # image analysis, semantic normalization, or other AI call is attempted.
    if incoming.message_type in SUPPORT_MEDIA_MESSAGE_TYPES | {"location", "contacts"}:
        is_voice_note = incoming.message_type == "audio" and incoming.is_voice
        media_label = (
            "shared contact"
            if incoming.message_type == "contacts"
            else "voice note"
            if is_voice_note
            else "image"
            if incoming.message_type == "image"
            else "video"
            if incoming.message_type == "video"
            else "sticker"
            if incoming.message_type == "sticker"
            else "PDF document"
            if incoming.message_type == "document"
            else "location"
            if incoming.message_type == "location"
            else "audio message"
        )
        logger.info(
            "AI-unsupported WhatsApp media received - handing conversation to human: "
            "customer_ref=%s message_id=%s type=%s voice=%s",
            customer_ref,
            incoming.message_id,
            incoming.message_type,
            incoming.is_voice,
        )
        return await _handoff_incoming_without_ai(
            conversation=conversation,
            incoming=incoming,
            parsed_auth=parsed_auth,
            reason=(
                "VOICE_NOTE_UNSUPPORTED"
                if is_voice_note
                else "LOCATION_UNSUPPORTED_BY_AI"
                if incoming.message_type == "location"
                else "MEDIA_UNSUPPORTED_BY_AI"
            ),
            summary=(
                f"Customer sent a WhatsApp {media_label}. "
                "It is available to human support, but AI processing for this message type is disabled."
            ),
            status_name=(
                "handed_off_voice_note"
                if is_voice_note
                else "handed_off_location"
                if incoming.message_type == "location"
                else "handed_off_media"
            ),
        )

    # Persistent MariaDB settings are the deployment-wide source of truth. This
    # check intentionally happens before auth resume, semantic normalization, quota
    # consumption, prompt/model work, or any other OpenAI-backed operation. If the
    # settings store is unavailable, fail closed and let the webhook retry rather
    # than risk an unintended paid AI call.
    try:
        ai_availability = (
            ai_availability_snapshot
            if ai_availability_snapshot is not None
            else await ai_availability_service.get_effective_ai_availability()
        )
    except Exception:
        logger.exception(
            "AI availability evaluation failed closed: customer_ref=%s message_id=%s",
            customer_ref,
            incoming.message_id,
        )
        return _json_response(503, {"status": "ai_availability_unavailable"})

    if not ai_availability.effective_ai_enabled:
        logger.info(
            "AI unavailable globally - handing conversation to human: customer_ref=%s "
            "message_id=%s control_mode=%s effective_reason=%s",
            customer_ref,
            incoming.message_id,
            ai_availability.configuration.control_mode,
            ai_availability.effective_reason,
        )
        return await _handoff_incoming_without_ai(
            conversation=conversation,
            incoming=incoming,
            parsed_auth=parsed_auth,
            reason="AI_DISABLED_GLOBAL",
            summary=(
                "AI assistant is globally unavailable for this inbound message "
                f"({ai_availability.effective_reason})."
            ),
            status_name="handed_off_ai_disabled",
        )

    # =========================================================
    # UNSUPPORTED NON-TEXT
    # =========================================================

    if incoming.text is None:
        return _json_response(
            200,
            {
                "status": "ignored"
            },
        )

    text = incoming.text

    # An explicit cancellation is itself the completed customer intent. A clear topic
    # switch, on the other hand, falls through and is handled as a fresh normal request
    # (for example a public shipping question) without an extra bot message in between.
    if auth_interruption == "cancel":
        _append_customer_history(conversation, text)
        await _send_and_record(
            conversation,
            auth_cancelled(conversation.communication_style),
            source_message_id=incoming.message_id,
        )
        return _json_response(200, {"status": "authentication_cancelled"})

    # =========================================================
    # CONVERSATIONAL CLIENT AUTHENTICATION
    # =========================================================

    try:
        auth_outcome = await _handle_conversational_auth(
            conversation,
            parsed_auth,
            raw_text=text,
            source_message_id=incoming.message_id,
        )
    except Exception:
        # Never log the inbound body here; it may contain a password.
        logger.exception(
            "Conversational authentication processing failed for %s",
            customer_ref,
        )
        return _json_response(503, {"status": "authentication_unavailable"})

    if auth_outcome is not None:
        ok, outcome = auth_outcome
        return _json_response(200 if ok else 500, {"status": outcome})

    # =========================================================
    # DETERMINISTIC PUBLIC SHIPPING QUOTES
    # =========================================================
    # First attempt the shipping pipeline with its semantic model disabled. Pure DB /
    # parser answers remain available even when the customer's AI quota is exhausted,
    # and they do not consume the OpenAI abuse budget.
    try:
        shipping_outcome = await maybe_handle_shipping_quote(
            conversation,
            text,
            semantic_normalizer_enabled=False,
        )
    except Exception:
        logger.exception(
            "Deterministic shipping quote processing failed for %s",
            customer_ref,
        )
        shipping_outcome = None

    if shipping_outcome is not None:
        _append_customer_history(conversation, text)
        await _send_and_record(
            conversation,
            shipping_outcome.reply_text,
            source_message_id=incoming.message_id,
        )
        return _json_response(200, {"status": shipping_outcome.status})

    # =========================================================
    # AI USAGE / WALLET PROTECTION
    # =========================================================
    # Consume one rolling AI-turn quota only when this customer actually needs an
    # AI-backed path. The key intentionally ignores the receiving business number so
    # the same sender cannot bypass limits by contacting another company line.
    decision: AIQuotaDecision = await ai_rate_limiter.consume(from_number)
    if not decision.allowed:
        logger.warning(
            "AI usage quota blocked customer_ref=%s scope=%s",
            customer_ref,
            decision.scope,
        )
        try:
            await conversation_store.save(conversation)
        except Exception:
            logger.exception(
                "Could not persist conversation metadata after AI quota block: %s",
                customer_ref,
            )

        if await ai_rate_limiter.claim_notice(from_number):
            notice = ai_rate_limited(
                conversation.communication_style,
                global_limit=decision.scope in {"global_day", "limiter_unavailable"},
            )
            try:
                await _send_text_once(
                    to=conversation.phone_number,
                    body=notice,
                    source_message_id=incoming.message_id,
                    phone_number_id=conversation.business_phone_number_id,
                )
            except Exception:
                # Never ask Meta to retry a quota-blocked turn. Re-delivery would only
                # create another attempt to reach the same protected AI path.
                logger.exception(
                    "Could not send AI quota notice to customer_ref=%s",
                    customer_ref,
                )
        return _json_response(
            200,
            {"status": "ai_rate_limited", "scope": decision.scope},
        )

    # A narrow semantic shipping normalizer is itself an OpenAI-backed operation, so
    # it is enabled only after the customer has passed the same AI quota gate.
    try:
        shipping_outcome = await maybe_handle_shipping_quote(
            conversation,
            text,
            semantic_normalizer_enabled=True,
        )
    except Exception:
        logger.exception(
            "Semantic shipping quote processing failed for %s",
            customer_ref,
        )
        shipping_outcome = None

    if shipping_outcome is not None:
        _append_customer_history(conversation, text)
        await _send_and_record(
            conversation,
            shipping_outcome.reply_text,
            source_message_id=incoming.message_id,
        )
        return _json_response(200, {"status": shipping_outcome.status})

    # =========================================================
    # NORMAL AI REQUEST
    # =========================================================

    try:
        ok, outcome = await _handle_ai_result(
            conversation=conversation,
            incoming=incoming,
            user_message_for_ticket=text,
        )

    except Exception:
        logger.exception(
            "Error handling message from %s",
            customer_ref,
        )

        try:
            await conversation_store.save(
                conversation
            )

        except Exception:
            logger.exception(
                "Conversation save also failed for %s",
                customer_ref,
            )

        return _json_response(
            500,
            {
                "status": "error"
            },
        )

    return _json_response(
        200 if ok else 500,
        {
            "status": outcome
        },
    )

# =============================================================
# MAIN WHATSAPP WEBHOOK
# =============================================================

@app.post("/webhook")
async def receive_message(
    request: Request,
) -> Response:

    raw_body = await request.body()

    signature = request.headers.get(
        "X-Hub-Signature-256",
        "",
    )

    # =========================================================
    # VERIFY META SIGNATURE
    # =========================================================

    if not verify_meta_signature(
        raw_body,
        signature,
    ):
        logger.warning(
            "Rejected webhook call with "
            "invalid/missing signature"
        )

        return Response(
            status_code=403
        )

    # =========================================================
    # PARSE WEBHOOK
    # =========================================================

    try:
        payload = json.loads(
            raw_body
        )

    except json.JSONDecodeError:
        logger.warning(
            "Received invalid JSON webhook payload"
        )

        return _json_response(
            400,
            {
                "status": "invalid_json"
            },
        )

    return await process_whatsapp_payload(payload, source="cloud")


async def _apply_cloud_delivery_statuses(change: dict, statuses: list) -> None:
    """Show Meta delivered/read receipts as ticks on support messages."""
    line_id = str((change.get("metadata") or {}).get("phone_number_id") or "") or None
    for item in statuses[:100]:
        if not isinstance(item, dict) or item.get("status") not in {"delivered", "read", "failed"} or not item.get("id"):
            continue
        try:
            timestamp_ms = int(item.get("timestamp") or 0) * 1000 or None
            await support_service.apply_delivery_status(
                whatsapp_message_id=str(item["id"]), status=str(item["status"]),
                timestamp_ms=timestamp_ms, business_phone_number_id=line_id,
                phone_number=str(item.get("recipient_id") or "") or None,
            )
        except Exception:
            # Acknowledge only durable receipt handling; Meta can redeliver on 503.
            logger.warning("WhatsApp delivery status could not be recorded: status=%s", item.get("status"))
            raise HTTPException(503, "Delivery status storage is temporarily unavailable.") from None


async def process_whatsapp_payload(payload: dict, *, source: str = "cloud") -> Response:
    """Use the same durable inbound pipeline for both WhatsApp connections."""

    # =========================================================
    # DELIVERY / READ STATUS CALLBACKS
    # =========================================================

    try:
        change = (
            payload["entry"][0]
            ["changes"][0]
            ["value"]
        )

        statuses = change.get(
            "statuses"
        )

        if statuses:
            logger.info(
                "WhatsApp status callback "
                "received: count=%d",
                len(statuses),
            )
            if source == "cloud" and isinstance(statuses, list):
                await _apply_cloud_delivery_statuses(change, statuses)

    except (
        KeyError,
        IndexError,
        TypeError,
    ):
        pass

    # =========================================================
    # EXTRACT INBOUND CUSTOMER MESSAGE
    # =========================================================

    incoming = extract_incoming_message(
        payload
    )

    if incoming is None:
        return _json_response(
            200,
            {
                "status": "ignored"
            },
        )

    if get_transport(incoming.business_phone_number_id) != source:
        return _json_response(200, {"status": "ignored", "reason": "inactive_transport"})

    # =========================================================
    # MANAGED WHATSAPP NUMBER ROUTING
    # =========================================================
    # The database-backed registry is authoritative for dynamic phone-number
    # enablement. New Meta numbers are discovered but stay disabled until a
    # Supervisor/Admin explicitly adds them to the app. The legacy .env number
    # remains enabled as a backwards-compatible seed.
    try:
        line_enabled = await whatsapp_registry_service.observe_inbound(
            waba_id=incoming.business_account_id if source == "cloud" else None,
            phone_number_id=incoming.business_phone_number_id,
            display_phone_number=incoming.business_display_phone_number,
        )
    except WhatsAppRegistryError:
        logger.exception(
            "Could not evaluate inbound WhatsApp number registry: phone_number_id=%s",
            incoming.business_phone_number_id,
        )
        return _json_response(503, {"status": "whatsapp_registry_unavailable"})

    if not line_enabled:
        logger.info(
            "Ignoring inbound message for disabled/unmanaged WhatsApp number: phone_number_id=%s message_id=%s",
            incoming.business_phone_number_id,
            incoming.message_id,
        )
        return _json_response(200, {"status": "ignored", "reason": "whatsapp_number_disabled"})

    try:
        if await _is_inbound_revoked(incoming.message_id):
            return _json_response(200, {"status": "revoked_message_ignored"})
    except Exception:
        logger.exception("Could not check inbound WhatsApp revocation marker: message_id=%s", incoming.message_id)
        return _json_response(503, {"status": "revocation_state_unavailable"})

    # =========================================================
    # GLOBAL INBOUND MESSAGE IDEMPOTENCY
    # =========================================================
    #
    # Meta may deliver exactly the same customer message more than
    # once, including while the original webhook request is still
    # being processed.
    #
    # The WhatsApp message ID must therefore be atomically claimed
    # BEFORE:
    #
    # - authentication
    # - OpenAI
    # - database tools
    # - human-ticket creation
    # - outbound WhatsApp messages
    #
    # Redis SET NX ensures only one concurrent worker wins.
    # =========================================================

    claim_token: str | None = None

    if incoming.message_id:

        try:
            claim_token = (
                await conversation_store
                .claim_incoming_message(
                    incoming.message_id
                )
            )

        except Exception:
            logger.exception(
                "Could not claim inbound WhatsApp "
                "message: message_id=%s",
                incoming.message_id,
            )

            # Fail closed.
            #
            # Processing without idempotency could send duplicate
            # replies or execute a side effect twice.
            return _json_response(
                503,
                {
                    "status": (
                        "message_idempotency_unavailable"
                    )
                },
            )

        if claim_token is None:
            logger.info(
                "Duplicate inbound WhatsApp message observed: message_id=%s",
                incoming.message_id,
            )

            # A retry for the request that originally owned the sender lock can also
            # act as a recovery trigger for already-queued work. Do not enqueue the
            # duplicate again, but opportunistically drain anything still pending.
            try:
                route_key = _conversation_route_key(incoming)
                queued = await conversation_store.pending_incoming_work_count(
                    route_key
                )
                if queued:
                    recovered = await _drive_inbound_queue(route_key)
                    if recovered is not None and _response_is_retryable(recovered):
                        return recovered
            except Exception:
                logger.exception(
                    "Could not recover queued inbound work from duplicate delivery: "
                    "customer_ref=%s",
                    safe_log_identifier(incoming.from_number, "wa"),
                )

            return _json_response(
                200,
                {
                    "status": "duplicate_message",
                    "message_id": incoming.message_id,
                },
            )

    # =========================================================
    # DURABLE PER-SENDER INBOUND QUEUE
    # =========================================================
    # Every unique message is queued *before* competing for the conversation lock.
    # If another turn is already running, this request can acknowledge Meta with 2xx
    # instead of returning 503. The lock owner drains the queue and rapid adjacent
    # text messages are processed as one burst so later details are visible before a
    # clarification reply is generated.
    route_key = _conversation_route_key(incoming)
    try:
        await conversation_store.enqueue_incoming_work(
            route_key,
            _queued_inbound_item(incoming, claim_token),
        )
    except Exception:
        logger.exception(
            "Could not enqueue inbound WhatsApp message: customer_ref=%s message_id=%s",
            safe_log_identifier(incoming.from_number, "wa"),
            incoming.message_id,
        )
        if incoming.message_id and claim_token:
            try:
                await conversation_store.release_incoming_message(
                    incoming.message_id, claim_token
                )
            except Exception:
                logger.exception(
                    "Could not release inbound claim after queue failure: message_id=%s",
                    incoming.message_id,
                )
        return _json_response(503, {"status": "inbound_queue_unavailable"})

    try:
        response = await _drive_inbound_queue(route_key)
    except Exception:
        # The queued item and its idempotency claim are intentionally retained. Meta
        # will retry this webhook because of the 503; the duplicate path above then
        # attempts to resume draining the durable queue without enqueuing it twice.
        logger.exception(
            "Could not drive inbound conversation queue: customer_ref=%s",
            safe_log_identifier(incoming.from_number, "wa"),
        )
        return _json_response(503, {"status": "conversation_queue_unavailable"})

    if response is None:
        logger.info(
            "Inbound WhatsApp message queued behind active turn: customer_ref=%s "
            "message_id=%s",
            safe_log_identifier(incoming.from_number, "wa"),
            incoming.message_id,
        )
        return _json_response(
            200,
            {
                "status": "queued",
                "message_id": incoming.message_id,
            },
        )

    return response
