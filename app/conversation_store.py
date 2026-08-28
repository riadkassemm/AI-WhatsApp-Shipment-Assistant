"""
Conversation state persisted in Redis.

Authentication state is stored as part of the conversation:

    verified_customer_id
    authenticated_userid
    authenticated_name

A temporary pending userid may be stored while waiting for the
customer to provide their password.

Passwords are NEVER stored in conversation state.

Inbound WhatsApp message IDs are also claimed atomically so duplicate
Meta webhook deliveries cannot execute authentication, AI calls, tools,
or outbound messages more than once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.ai_config import CHATBOT_INSTRUCTIONS
from app.client_chat_auth import AuthState, redact_sensitive_text
from app.config import settings
from app.security import safe_log_identifier

logger = logging.getLogger("shipment-bot")


Mode = Literal["ai", "human"]


# A webhook should normally finish long before this expires.
#
# If the process dies while handling a message, the processing claim
# eventually expires and the message may be retried.
INBOUND_PROCESSING_TTL_SECONDS = 15 * 60

# Remember successfully processed WhatsApp IDs long enough to prevent
# later webhook redeliveries from executing again.
INBOUND_COMPLETED_TTL_SECONDS = 7 * 24 * 60 * 60

# The outbound reply guard uses an at-most-once send attempt per inbound message.
# On a network timeout Meta may already have accepted the send, so retaining the
# guard is safer than retrying and producing a random duplicate later.
OUTBOUND_REPLY_GUARD_PREFIX = "whatsapp:outbound-reply"


SYSTEM_PROMPT = CHATBOT_INSTRUCTIONS


@dataclass
class Conversation:
    phone_number: str

    mode: Mode = "ai"

    # Cached durable support ticket ID.
    # MariaDB remains authoritative.
    active_ticket_id: str | None = None

    # Database users.id.
    verified_customer_id: str | None = None

    # Database users.userid.
    authenticated_userid: str | None = None

    # Optional display name.
    authenticated_name: str | None = None

    # Explicit conversational client-auth state. Authorization still derives only
    # from verified_customer_id established by backend credential verification.
    auth_state: AuthState = "guest"

    # Temporary userid waiting for password. Passwords are NEVER serialized into
    # this conversation record; a password-first flow uses a separate short-lived
    # one-time secret store below.
    pending_auth_userid: str | None = None

    # Non-secret original customer request that was blocked by authentication.
    # This lets the chatbot resume the request automatically after login.
    pending_request: str | None = None

    # Exact protected application action that returned not_authenticated. These
    # arguments never contain the authenticated customer identity or password; the
    # backend injects identity only when replaying the action after verification.
    pending_tool_name: str | None = None
    pending_tool_arguments: dict[str, Any] | None = None

    # Backend-owned public shipping quote context. It contains catalog lookup slots
    # only, never customer secrets. Timestamps prevent stale bare-number follow-ups.
    pending_shipping_quote: dict[str, Any] | None = None
    last_shipping_quote: dict[str, Any] | None = None
    pending_shipping_quote_updated_at: float | None = None
    last_shipping_quote_updated_at: float | None = None

    # Updated from every non-credential customer turn. These are lightweight
    # presentation hints, not authorization or factual sources.
    current_language: str = "en"
    communication_style: str = "en"

    # Completed inbound watermark. It prevents a uniquely identified old webhook
    # from being answered after a newer customer turn was already completed.
    last_inbound_timestamp: int | None = None
    recent_inbound_message_ids: list[str] = field(default_factory=list)

    summary: str = ""

    messages: list[dict] = field(
        default_factory=lambda: [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            }
        ]
    )

    def to_json(self) -> str:
        data = asdict(self)
        data["summary"] = redact_sensitive_text(data.get("summary"))
        safe_messages: list[dict] = []
        for message in data.get("messages", []):
            item = dict(message)
            if isinstance(item.get("content"), str):
                item["content"] = redact_sensitive_text(item["content"])
            safe_messages.append(item)
        data["messages"] = safe_messages
        if data.get("pending_request"):
            data["pending_request"] = redact_sensitive_text(data["pending_request"])
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(
        cls,
        raw: str,
    ) -> "Conversation":
        data = json.loads(raw)

        # Backwards compatibility.
        data.setdefault(
            "active_ticket_id",
            None,
        )

        data.setdefault(
            "verified_customer_id",
            None,
        )

        data.setdefault(
            "authenticated_userid",
            None,
        )

        data.setdefault(
            "authenticated_name",
            None,
        )

        data.setdefault(
            "pending_auth_userid",
            None,
        )

        data.setdefault(
            "pending_request",
            None,
        )
        data.setdefault(
            "pending_tool_name",
            None,
        )
        data.setdefault(
            "pending_tool_arguments",
            None,
        )
        if not isinstance(data.get("pending_tool_arguments"), (dict, type(None))):
            data["pending_tool_arguments"] = None
        data.setdefault("pending_shipping_quote", None)
        data.setdefault("last_shipping_quote", None)
        data.setdefault("pending_shipping_quote_updated_at", None)
        data.setdefault("last_shipping_quote_updated_at", None)
        if not isinstance(data.get("pending_shipping_quote"), (dict, type(None))):
            data["pending_shipping_quote"] = None
        if not isinstance(data.get("last_shipping_quote"), (dict, type(None))):
            data["last_shipping_quote"] = None
        for timestamp_key in (
            "pending_shipping_quote_updated_at",
            "last_shipping_quote_updated_at",
        ):
            raw_timestamp = data.get(timestamp_key)
            if raw_timestamp is None:
                continue
            try:
                data[timestamp_key] = float(raw_timestamp)
            except (TypeError, ValueError):
                data[timestamp_key] = None

        if data.get("verified_customer_id") is not None:
            data["auth_state"] = "authenticated"
        else:
            data.setdefault(
                "auth_state",
                "awaiting_password" if data.get("pending_auth_userid") else "guest",
            )

        data.setdefault(
            "current_language",
            "en",
        )

        data.setdefault(
            "communication_style",
            "en",
        )

        data.setdefault(
            "last_inbound_timestamp",
            None,
        )
        data.setdefault(
            "recent_inbound_message_ids",
            [],
        )
        if not isinstance(data.get("recent_inbound_message_ids"), list):
            data["recent_inbound_message_ids"] = []
        data["recent_inbound_message_ids"] = [
            str(value) for value in data["recent_inbound_message_ids"][-50:] if value
        ]

        data.setdefault(
            "summary",
            "",
        )

        if "messages" not in data:
            data["messages"] = [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                }
            ]

        # Always replace an old stored system prompt with the
        # current application prompt.
        if data["messages"]:
            if data["messages"][0].get("role") == "system":
                data["messages"][0] = {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                }

        # Historical Redis records may predate credential redaction. Scrub them
        # defensively before they can be shown to staff or sent to OpenAI again.
        for message in data.get("messages", [])[1:]:
            if isinstance(message.get("content"), str):
                message["content"] = redact_sensitive_text(message["content"])
        data["summary"] = redact_sensitive_text(data.get("summary"))
        if data.get("pending_request"):
            data["pending_request"] = redact_sensitive_text(data["pending_request"])

        return cls(**data)


def _redis_key(
    phone_number: str,
) -> str:
    return f"conversation:{phone_number}"


def _inbound_message_key(
    message_id: str,
) -> str:
    return f"whatsapp:inbound:{message_id}"


def _sender_digest(phone_number: str) -> str:
    return hashlib.sha256(str(phone_number).encode("utf-8")).hexdigest()


def _pending_auth_secret_key(phone_number: str) -> str:
    # Do not expose the customer phone number in the transient-secret key name.
    return f"conversation:auth-secret:{_sender_digest(phone_number)}"


def _conversation_processing_key(phone_number: str) -> str:
    return f"conversation:processing:{_sender_digest(phone_number)}"


def _outbound_reply_key(idempotency_key: str) -> str:
    digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()
    return f"{OUTBOUND_REPLY_GUARD_PREFIX}:{digest}"


class BaseConversationStore:

    async def get_or_create(
        self,
        phone_number: str,
    ) -> Conversation:
        raise NotImplementedError

    async def save(
        self,
        conversation: Conversation,
    ) -> None:
        raise NotImplementedError

    # =========================================================
    # INBOUND MESSAGE IDEMPOTENCY
    # =========================================================

    async def claim_incoming_message(
        self,
        message_id: str,
    ) -> str | None:
        """
        Atomically claim a WhatsApp inbound message.

        Returns a unique claim token for the worker that successfully
        claimed it.

        Returns None when the message is already processing or has
        already been completed.
        """
        raise NotImplementedError

    async def complete_incoming_message(
        self,
        message_id: str,
        claim_token: str,
    ) -> None:
        """
        Mark a successfully handled WhatsApp message as completed.

        The claim token prevents an old/stale worker from completing
        a claim currently owned by another worker.
        """
        raise NotImplementedError

    async def release_incoming_message(
        self,
        message_id: str,
        claim_token: str,
    ) -> None:
        """
        Release a processing claim after a retryable failure.
        """
        raise NotImplementedError

    async def claim_conversation_processing(
        self,
        phone_number: str,
    ) -> str | None:
        """Serialize all inbound work for one WhatsApp sender."""
        raise NotImplementedError

    async def release_conversation_processing(
        self,
        phone_number: str,
        claim_token: str,
    ) -> None:
        raise NotImplementedError

    async def claim_outbound_reply(
        self,
        idempotency_key: str,
    ) -> str | None:
        """Reserve the single customer-facing reply for one inbound message."""
        raise NotImplementedError

    async def complete_outbound_reply(
        self,
        idempotency_key: str,
        claim_token: str,
    ) -> None:
        raise NotImplementedError

    async def release_outbound_reply(
        self,
        idempotency_key: str,
        claim_token: str,
    ) -> None:
        """Release only when the provider definitively rejected the send."""
        raise NotImplementedError

    async def stash_pending_auth_password(
        self,
        phone_number: str,
        password: str,
    ) -> None:
        """Temporarily hold a password-first credential outside chat history."""
        raise NotImplementedError

    async def pop_pending_auth_password(
        self,
        phone_number: str,
    ) -> str | None:
        """Atomically return and delete the one-time pending password."""
        raise NotImplementedError

    async def clear_pending_auth_password(
        self,
        phone_number: str,
    ) -> None:
        raise NotImplementedError

    async def set_mode(
        self,
        phone_number: str,
        mode: Mode,
    ) -> None:
        conversation = await self.get_or_create(
            phone_number
        )

        conversation.mode = mode

        await self.save(
            conversation
        )

    async def authenticate(
        self,
        phone_number: str,
        customer_id: str,
        userid: str,
        name: str | None = None,
    ) -> Conversation:
        conversation = await self.get_or_create(
            phone_number
        )

        conversation.verified_customer_id = str(
            customer_id
        )

        conversation.authenticated_userid = str(
            userid
        )

        conversation.authenticated_name = name

        conversation.pending_auth_userid = None
        conversation.auth_state = "authenticated"

        await self.clear_pending_auth_password(phone_number)
        await self.save(
            conversation
        )

        return conversation

    async def set_pending_auth_userid(
        self,
        phone_number: str,
        userid: str,
    ) -> Conversation:
        conversation = await self.get_or_create(
            phone_number
        )

        conversation.pending_auth_userid = str(
            userid
        )
        conversation.auth_state = "awaiting_password"

        await self.save(
            conversation
        )

        return conversation

    async def clear_pending_auth(
        self,
        phone_number: str,
    ) -> Conversation:
        conversation = await self.get_or_create(
            phone_number
        )

        conversation.pending_auth_userid = None
        conversation.auth_state = (
            "authenticated" if conversation.verified_customer_id is not None else "guest"
        )
        await self.clear_pending_auth_password(phone_number)

        await self.save(
            conversation
        )

        return conversation

    async def logout(
        self,
        phone_number: str,
    ) -> Conversation:
        conversation = await self.get_or_create(
            phone_number
        )

        conversation.verified_customer_id = None
        conversation.authenticated_userid = None
        conversation.authenticated_name = None
        conversation.pending_auth_userid = None
        conversation.pending_request = None
        conversation.pending_tool_name = None
        conversation.pending_tool_arguments = None
        conversation.auth_state = "guest"
        await self.clear_pending_auth_password(phone_number)

        await self.save(
            conversation
        )

        return conversation

    async def reset(
        self,
        phone_number: str,
    ) -> None:
        await self.save(
            Conversation(
                phone_number=phone_number
            )
        )


class InMemoryConversationStore(
    BaseConversationStore
):

    def __init__(self) -> None:
        self._conversations: dict[
            str,
            Conversation,
        ] = {}

        # message_id -> (state, token, expires_at)
        self._inbound_messages: dict[
            str,
            tuple[str, str, float],
        ] = {}

        # sender/idempotency key -> (state, token, expires_at)
        self._conversation_claims: dict[str, tuple[str, str, float]] = {}
        self._outbound_replies: dict[str, tuple[str, str, float]] = {}

        # Short-lived password-first auth values. These never enter Conversation
        # JSON/history and are popped immediately when the missing userid arrives.
        self._pending_auth_passwords: dict[str, tuple[str, float]] = {}

    async def get_or_create(
        self,
        phone_number: str,
    ) -> Conversation:
        if phone_number not in self._conversations:
            self._conversations[
                phone_number
            ] = Conversation(
                phone_number=phone_number
            )

        return self._conversations[
            phone_number
        ]

    async def save(
        self,
        conversation: Conversation,
    ) -> None:
        self._conversations[
            conversation.phone_number
        ] = conversation

    async def stash_pending_auth_password(
        self,
        phone_number: str,
        password: str,
    ) -> None:
        self._pending_auth_passwords[phone_number] = (
            password,
            time.monotonic() + settings.client_auth_pending_secret_ttl_seconds,
        )

    async def pop_pending_auth_password(
        self,
        phone_number: str,
    ) -> str | None:
        existing = self._pending_auth_passwords.pop(phone_number, None)
        if not existing:
            return None
        password, expires_at = existing
        if expires_at <= time.monotonic():
            return None
        return password

    async def clear_pending_auth_password(
        self,
        phone_number: str,
    ) -> None:
        self._pending_auth_passwords.pop(phone_number, None)

    async def claim_incoming_message(
        self,
        message_id: str,
    ) -> str | None:
        now = time.monotonic()

        existing = self._inbound_messages.get(
            message_id
        )

        if existing is not None:
            _state, _token, expires_at = existing

            if expires_at > now:
                return None

            # Expired processing/completed record.
            self._inbound_messages.pop(
                message_id,
                None,
            )

        token = secrets.token_urlsafe(24)

        self._inbound_messages[
            message_id
        ] = (
            "processing",
            token,
            now + INBOUND_PROCESSING_TTL_SECONDS,
        )

        return token

    async def complete_incoming_message(
        self,
        message_id: str,
        claim_token: str,
    ) -> None:
        existing = self._inbound_messages.get(
            message_id
        )

        if existing is None:
            return

        state, token, _expires_at = existing

        if (
            state != "processing"
            or token != claim_token
        ):
            return

        self._inbound_messages[
            message_id
        ] = (
            "completed",
            "",
            time.monotonic()
            + INBOUND_COMPLETED_TTL_SECONDS,
        )

    async def release_incoming_message(
        self,
        message_id: str,
        claim_token: str,
    ) -> None:
        existing = self._inbound_messages.get(
            message_id
        )

        if existing is None:
            return

        state, token, _expires_at = existing

        if (
            state == "processing"
            and token == claim_token
        ):
            self._inbound_messages.pop(
                message_id,
                None,
            )


    @staticmethod
    def _claim_memory_guard(
        store: dict[str, tuple[str, str, float]],
        key: str,
        ttl_seconds: int,
    ) -> str | None:
        now = time.monotonic()
        existing = store.get(key)
        if existing is not None and existing[2] > now:
            return None
        if existing is not None:
            store.pop(key, None)
        token = secrets.token_urlsafe(24)
        store[key] = ("processing", token, now + max(1, ttl_seconds))
        return token

    @staticmethod
    def _release_memory_guard(
        store: dict[str, tuple[str, str, float]],
        key: str,
        claim_token: str,
    ) -> None:
        existing = store.get(key)
        if existing and existing[0] == "processing" and existing[1] == claim_token:
            store.pop(key, None)

    async def claim_conversation_processing(self, phone_number: str) -> str | None:
        return self._claim_memory_guard(
            self._conversation_claims,
            phone_number,
            max(
                30,
                int(settings.conversation_processing_lock_ttl_seconds),
                int(float(settings.openai_turn_timeout_seconds)) + 30,
            ),
        )

    async def release_conversation_processing(
        self,
        phone_number: str,
        claim_token: str,
    ) -> None:
        self._release_memory_guard(self._conversation_claims, phone_number, claim_token)

    async def claim_outbound_reply(self, idempotency_key: str) -> str | None:
        return self._claim_memory_guard(
            self._outbound_replies,
            idempotency_key,
            max(60, int(settings.outbound_reply_dedupe_ttl_seconds)),
        )

    async def complete_outbound_reply(
        self,
        idempotency_key: str,
        claim_token: str,
    ) -> None:
        existing = self._outbound_replies.get(idempotency_key)
        if existing and existing[0] == "processing" and existing[1] == claim_token:
            self._outbound_replies[idempotency_key] = (
                "completed",
                "",
                time.monotonic() + max(60, int(settings.outbound_reply_dedupe_ttl_seconds)),
            )

    async def release_outbound_reply(
        self,
        idempotency_key: str,
        claim_token: str,
    ) -> None:
        self._release_memory_guard(self._outbound_replies, idempotency_key, claim_token)


class RedisConversationStore(
    BaseConversationStore
):

    _POP_PENDING_SECRET_SCRIPT = """
    local value = redis.call('GET', KEYS[1])
    if value then
        redis.call('DEL', KEYS[1])
    end
    return value
    """

    _COMPLETE_MESSAGE_SCRIPT = """
    local current = redis.call('GET', KEYS[1])

    if current == ARGV[1] then
        redis.call(
            'SET',
            KEYS[1],
            'completed',
            'EX',
            ARGV[2]
        )
        return 1
    end

    return 0
    """

    _RELEASE_MESSAGE_SCRIPT = """
    local current = redis.call('GET', KEYS[1])

    if current == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    end

    return 0
    """

    _COMPLETE_GUARD_SCRIPT = """
    local current = redis.call('GET', KEYS[1])

    if current == ARGV[1] then
        redis.call('SET', KEYS[1], 'completed', 'EX', ARGV[2])
        return 1
    end

    return 0
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
    ) -> None:
        import redis.asyncio as redis

        self._client = redis.from_url(
            redis_url,
            decode_responses=True,
        )

        self._ttl_seconds = ttl_seconds

    async def get_or_create(
        self,
        phone_number: str,
    ) -> Conversation:
        key = _redis_key(phone_number)

        raw = await self._client.get(key)

        logger.info(
            "REDIS LOAD key=%s exists=%s",
            key,
            raw is not None,
        )

        if raw is None:
            logger.warning(
                "REDIS MISS creating new conversation phone=%s",
                safe_log_identifier(phone_number, "wa"),
            )
            return Conversation(phone_number=phone_number)

        if raw is None:
            return Conversation(phone_number=phone_number)

        # redis-py may return bytes or str depending on configuration; ensure str
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode()

        conversation = Conversation.from_json(raw)

        logger.info(
            "REDIS CONVERSATION RESTORED state=%s pending_userid=%s verified=%s",
            conversation.auth_state,
            conversation.pending_auth_userid,
            conversation.verified_customer_id,
        )

        return conversation

    async def save(
        self,
        conversation: Conversation,
    ) -> None:

        logger.info(
            "REDIS SAVE state=%s pending_userid=%s",
            conversation.auth_state,
            conversation.pending_auth_userid,
        )

        await self._client.set(
            _redis_key(conversation.phone_number),
            conversation.to_json(),
            ex=self._ttl_seconds,
        )

    async def stash_pending_auth_password(
        self,
        phone_number: str,
        password: str,
    ) -> None:
        await self._client.set(
            _pending_auth_secret_key(phone_number),
            password,
            ex=max(30, int(settings.client_auth_pending_secret_ttl_seconds)),
        )

    async def pop_pending_auth_password(
        self,
        phone_number: str,
    ) -> str | None:
        value = await self._client.eval(
            self._POP_PENDING_SECRET_SCRIPT,
            1,
            _pending_auth_secret_key(phone_number),
        )
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8")
        return str(value) if value is not None else None

    async def clear_pending_auth_password(
        self,
        phone_number: str,
    ) -> None:
        await self._client.delete(_pending_auth_secret_key(phone_number))

    async def claim_conversation_processing(self, phone_number: str) -> str | None:
        token = secrets.token_urlsafe(24)
        claimed = await self._client.set(
            _conversation_processing_key(phone_number),
            f"processing:{token}",
            nx=True,
            ex=max(
                30,
                int(settings.conversation_processing_lock_ttl_seconds),
                int(float(settings.openai_turn_timeout_seconds)) + 30,
            ),
        )
        return token if claimed else None

    async def release_conversation_processing(
        self,
        phone_number: str,
        claim_token: str,
    ) -> None:
        await self._client.eval(
            self._RELEASE_MESSAGE_SCRIPT,
            1,
            _conversation_processing_key(phone_number),
            f"processing:{claim_token}",
        )

    async def claim_outbound_reply(self, idempotency_key: str) -> str | None:
        token = secrets.token_urlsafe(24)
        claimed = await self._client.set(
            _outbound_reply_key(idempotency_key),
            f"processing:{token}",
            nx=True,
            ex=max(60, int(settings.outbound_reply_dedupe_ttl_seconds)),
        )
        return token if claimed else None

    async def complete_outbound_reply(
        self,
        idempotency_key: str,
        claim_token: str,
    ) -> None:
        await self._client.eval(
            self._COMPLETE_GUARD_SCRIPT,
            1,
            _outbound_reply_key(idempotency_key),
            f"processing:{claim_token}",
            str(max(60, int(settings.outbound_reply_dedupe_ttl_seconds))),
        )

    async def release_outbound_reply(
        self,
        idempotency_key: str,
        claim_token: str,
    ) -> None:
        await self._client.eval(
            self._RELEASE_MESSAGE_SCRIPT,
            1,
            _outbound_reply_key(idempotency_key),
            f"processing:{claim_token}",
        )

    async def claim_incoming_message(
        self,
        message_id: str,
    ) -> str | None:
        """
        Redis SET NX makes this claim atomic across concurrent
        FastAPI workers/processes.
        """

        token = secrets.token_urlsafe(24)

        value = f"processing:{token}"

        claimed = await self._client.set(
            _inbound_message_key(
                message_id
            ),
            value,
            nx=True,
            ex=INBOUND_PROCESSING_TTL_SECONDS,
        )

        if not claimed:
            return None

        return token

    async def complete_incoming_message(
        self,
        message_id: str,
        claim_token: str,
    ) -> None:
        """
        Only the worker that owns the processing token may mark
        the message completed.
        """

        expected = (
            f"processing:{claim_token}"
        )

        await self._client.eval(
            self._COMPLETE_MESSAGE_SCRIPT,
            1,
            _inbound_message_key(
                message_id
            ),
            expected,
            str(
                INBOUND_COMPLETED_TTL_SECONDS
            ),
        )

    async def release_incoming_message(
        self,
        message_id: str,
        claim_token: str,
    ) -> None:
        """
        Only delete the claim if this worker still owns it.
        """

        expected = (
            f"processing:{claim_token}"
        )

        await self._client.eval(
            self._RELEASE_MESSAGE_SCRIPT,
            1,
            _inbound_message_key(
                message_id
            ),
            expected,
        )


def _build_store() -> BaseConversationStore:
    if settings.redis_url:
        return RedisConversationStore(
            settings.redis_url,
            settings.conversation_ttl_seconds,
        )

    logger.warning(
        "REDIS_URL not set -- using in-memory conversation store."
    )

    return InMemoryConversationStore()


conversation_store = _build_store()