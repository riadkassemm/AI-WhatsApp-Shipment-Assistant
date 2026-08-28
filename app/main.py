from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import FastAPI, Request, Response

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
    is_logout_intent,
    redact_sensitive_text,
)
from app.openai_service import ai_unavailable_message, get_ai_reply
from app.language import (
    already_authenticated,
    auth_clarification,
    auth_failure,
    auth_prompt,
    auth_rate_limited,
    auth_unavailable,
    detect_communication_style,
    handoff_confirmation,
    login_confirmation,
    logout_confirmation,
    pending_request_unavailable,
    password_prompt,
    userid_prompt,
)
from app.shipping_quote_service import maybe_handle_shipping_quote
from app.security import (
    client_auth_rate_limiter,
    rate_limiter,
    safe_log_identifier,
    verify_meta_signature,
)
from app.support_api import router as support_api_router
from app.support_repository import SupportConflictError
from app.support_service import sanitize_support_text, support_service
from app.support_ui import router as support_ui_router
from app.whatsapp_client import (
    IncomingWhatsAppMessage,
    WhatsAppSendError,
    extract_incoming_message,
    whatsapp_client,
)


logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("shipment-bot")

app = FastAPI(title="AI Shipment Assistant - WhatsApp Backend")
app.include_router(support_api_router)
app.include_router(support_ui_router)


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

    if (
        conversation.last_inbound_timestamp is not None
        and timestamp < int(conversation.last_inbound_timestamp)
    ):
        return "out_of_order"
    return None


async def _mark_inbound_completed(incoming: IncomingWhatsAppMessage) -> None:
    """Persist a small sender watermark after successful processing."""
    conversation = await conversation_store.get_or_create(incoming.from_number)
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


def _reply_idempotency_key(source_message_id: str) -> str:
    return f"primary-customer-reply:{source_message_id}"


async def _send_text_once(
    *,
    to: str,
    body: str,
    source_message_id: str | None,
) -> dict:
    """Attempt at most one primary customer reply per inbound WhatsApp ID."""
    if not source_message_id:
        return await whatsapp_client.send_text(to=to, body=body)

    key = _reply_idempotency_key(source_message_id)
    token = await conversation_store.claim_outbound_reply(key)
    if token is None:
        logger.info(
            "Outbound reply suppressed by idempotency guard: message_id=%s",
            source_message_id,
        )
        return {"deduplicated": True}

    try:
        response = await whatsapp_client.send_text(to=to, body=body)
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
    await conversation_store.clear_pending_auth_password(conversation.phone_number)

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
        await conversation_store.clear_pending_auth_password(conversation.phone_number)
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
        await conversation_store.clear_pending_auth_password(conversation.phone_number)
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
            await conversation_store.clear_pending_auth_password(conversation.phone_number)
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
        await conversation_store.clear_pending_auth_password(conversation.phone_number)
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
                conversation.phone_number
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
            conversation.phone_number,
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
            )
            return True, "handoff_failed_notified"

        handoff_text = guarded_reply or handoff_confirmation(
            conversation.communication_style
        )
        await _send_text_once(
            to=conversation.phone_number,
            body=handoff_text,
            source_message_id=source_message_id,
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
        )
        return True, "handled"

    fallback = ai_unavailable_message(conversation.communication_style)
    conversation.messages.append({"role": "assistant", "content": fallback})
    await conversation_store.save(conversation)
    await _send_text_once(
        to=conversation.phone_number,
        body=fallback,
        source_message_id=source_message_id,
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
            )
            return True, "authenticated_handoff_failed_notified"

        conversation.pending_request = None
        conversation.pending_tool_name = None
        conversation.pending_tool_arguments = None
        await conversation_store.save(conversation)
        await _send_text_once(
            to=conversation.phone_number,
            body=(
                guarded_reply
                or handoff_confirmation(conversation.communication_style)
            ),
            source_message_id=source_message_id,
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

async def _process_incoming_message(
    incoming: IncomingWhatsAppMessage,
) -> Response:
    """
    Process one uniquely claimed inbound WhatsApp message.

    The caller is responsible for inbound message idempotency.
    """

    from_number = incoming.from_number

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
        from_number
    )

    stale_reason = _stale_inbound_reason(conversation, incoming)
    if stale_reason is not None:
        logger.warning(
            "Stale/out-of-order inbound WhatsApp message ignored: "
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
        active_ticket = await support_service.get_active_ticket(
            from_number
        )

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

        if incoming.text is None:
            body = (
                f"[Unsupported WhatsApp "
                f"{incoming.message_type} message received]"
            )

        elif parsed_auth.secret_detected:
            # Staff may see the customer's authentication message for continuity,
            # but never the password itself.
            body = sanitize_support_text(parsed_auth.redacted_text)

        else:
            body = sanitize_support_text(
                incoming.text
            )

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
                    current_language=conversation.current_language,
                    communication_style=conversation.communication_style,
                )
            )

        except SupportConflictError:
            # Resolve-to-AI may have won the row lock after the
            # active-ticket lookup but before message insertion.
            try:
                active_ticket = (
                    await support_service.get_active_ticket(
                        from_number
                    )
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
    # UNSUPPORTED NON-TEXT
    # =========================================================

    if incoming.text is None:
        return _json_response(
            200,
            {
                "status": "ignored"
            },
        )

    # =========================================================
    # RATE LIMIT
    # =========================================================

    if not rate_limiter.allow(
        from_number
    ):
        logger.warning(
            "Rate limit hit for %s",
            customer_ref,
        )

        return _json_response(
            429,
            {
                "status": "rate_limited"
            },
        )

    text = incoming.text

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
    # Route/category/weight slots are backend-owned, so numeric and country-change
    # follow-ups cannot lose the context established on the preceding turn.
    try:
        shipping_outcome = await maybe_handle_shipping_quote(conversation, text)
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
                "Duplicate inbound WhatsApp message "
                "ignored: message_id=%s",
                incoming.message_id,
            )

            return _json_response(
                200,
                {
                    "status": "duplicate_message",
                    "message_id": (
                        incoming.message_id
                    ),
                },
            )

    # =========================================================
    # SERIALIZE ONE SENDER'S CONVERSATION
    # =========================================================
    # Per-message idempotency does not prevent two different message IDs from the same
    # sender being processed concurrently. Without this lock, an older slow AI call can
    # finish after a newer one and send a seemingly random reply.
    conversation_claim_token: str | None = None
    try:
        conversation_claim_token = await conversation_store.claim_conversation_processing(
            incoming.from_number
        )
    except Exception:
        logger.exception(
            "Could not claim conversation processing lock: customer_ref=%s",
            safe_log_identifier(incoming.from_number, "wa"),
        )
        if incoming.message_id and claim_token:
            try:
                await conversation_store.release_incoming_message(
                    incoming.message_id,
                    claim_token,
                )
            except Exception:
                logger.exception(
                    "Could not release inbound claim after conversation-lock failure: message_id=%s",
                    incoming.message_id,
                )
        return _json_response(503, {"status": "conversation_lock_unavailable"})

    if conversation_claim_token is None:
        logger.info(
            "Conversation is already processing another message: customer_ref=%s",
            safe_log_identifier(incoming.from_number, "wa"),
        )
        if incoming.message_id and claim_token:
            try:
                await conversation_store.release_incoming_message(
                    incoming.message_id,
                    claim_token,
                )
            except Exception:
                logger.exception(
                    "Could not release inbound claim for busy conversation: message_id=%s",
                    incoming.message_id,
                )
        return _json_response(503, {"status": "conversation_busy"})

    try:
        # =========================================================
        # PROCESS UNIQUE, SERIALIZED MESSAGE
        # =========================================================
        try:
            response = await _process_incoming_message(incoming)
        except Exception:
            logger.exception(
                "Unhandled error while processing WhatsApp message_id=%s",
                incoming.message_id,
            )
            if incoming.message_id and claim_token:
                try:
                    await conversation_store.release_incoming_message(
                        incoming.message_id,
                        claim_token,
                    )
                except Exception:
                    logger.exception(
                        "Failed to release inbound message claim: message_id=%s",
                        incoming.message_id,
                    )
            return _json_response(500, {"status": "error"})

        # =========================================================
        # FINALIZE IDEMPOTENCY + COMPLETION WATERMARK
        # =========================================================
        retryable = response.status_code == 429 or response.status_code >= 500
        if retryable:
            if incoming.message_id and claim_token:
                try:
                    await conversation_store.release_incoming_message(
                        incoming.message_id,
                        claim_token,
                    )
                except Exception:
                    logger.exception(
                        "Could not release retryable inbound claim: message_id=%s",
                        incoming.message_id,
                    )
        else:
            try:
                await _mark_inbound_completed(incoming)
            except Exception:
                # The customer-facing work already completed. Return 2xx so Meta does
                # not redeliver a message whose reply may already have been accepted.
                logger.exception(
                    "Could not persist inbound completion watermark: message_id=%s",
                    incoming.message_id,
                )

            if incoming.message_id and claim_token:
                try:
                    await conversation_store.complete_incoming_message(
                        incoming.message_id,
                        claim_token,
                    )
                except Exception:
                    logger.exception(
                        "Could not finalize inbound message idempotency: message_id=%s",
                        incoming.message_id,
                    )

        return response
    finally:
        try:
            await conversation_store.release_conversation_processing(
                incoming.from_number,
                conversation_claim_token,
            )
        except Exception:
            logger.exception(
                "Could not release conversation processing lock: customer_ref=%s",
                safe_log_identifier(incoming.from_number, "wa"),
            )

