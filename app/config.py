from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-6-sol"
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    openai_summarizer_reasoning_effort: Literal[
        "none", "low", "medium", "high", "xhigh", "max"
    ] = "low"
    # Bound provider retries and the total work for one inbound turn so an old
    # request cannot finish much later and send an out-of-context reply.
    openai_request_timeout_seconds: float = 45.0
    openai_max_retries: int = 1
    openai_turn_timeout_seconds: float = 90.0

    # Narrow structured normalizer for public shipping-catalog requests. It converts
    # multilingual customer wording into explicit English fields only; database
    # validation, matching, and arithmetic remain deterministic backend operations.
    shipping_semantic_normalizer_enabled: bool = True
    shipping_semantic_normalizer_model: str = ""
    shipping_semantic_normalizer_timeout_seconds: float = 10.0
    shipping_catalog_cache_seconds: int = 60

    # WhatsApp
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_api_version: str = "v20.0"
    whatsapp_app_secret: str = ""
    # Cloud remains the default; additional linked-device routes are opt-in.
    whatsapp_transport: Literal["cloud", "linked_device"] = "cloud"
    whatsapp_linked_device_enabled: bool = False
    whatsapp_linked_device_default_line_id: str = ""
    whatsapp_linked_device_url: str = "http://127.0.0.1:3001"
    whatsapp_linked_device_secret: str = ""
    whatsapp_linked_device_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    # Keep existing numeric business-number IDs as routing keys. Values pin a
    # bridge session to its expected phone number; an empty value permits pairing.
    whatsapp_linked_device_lines: dict[str, str] = Field(default_factory=dict)
    whatsapp_linked_device_callback_url: str = "http://127.0.0.1:8000/api/whatsapp/linked-device/events"
    whatsapp_linked_device_data_directory: str = "whatsapp_bridge/data"

    @field_validator("whatsapp_linked_device_lines")
    @classmethod
    def validate_linked_device_lines(cls, value: dict[str, str]) -> dict[str, str]:
        for line_id, phone in value.items():
            if not re.fullmatch(r"[0-9]{5,32}", line_id):
                raise ValueError("Linked-device line IDs must contain 5 to 32 digits.")
            if phone and (
                not re.fullmatch(r"\+?[0-9 ()-]+", phone)
                or not 5 <= len(re.sub(r"[^0-9]", "", phone)) <= 15
            ):
                raise ValueError("Linked-device expected phone numbers must be valid phone strings.")
        return value
    # Ignore a uniquely delivered inbound message when Meta delivers it long after
    # the customer sent it. Set to 0 to disable the age check.
    whatsapp_max_inbound_age_seconds: int = 15 * 60
    # Customers often split one thought across several WhatsApp messages. Hold the
    # per-sender queue very briefly before starting a turn so adjacent messages can
    # be processed together instead of producing an obsolete clarification reply.
    whatsapp_message_burst_debounce_seconds: float = 0.65
    # Keep extending the quiet-period debounce when new messages arrive, but never
    # delay the first response indefinitely while a customer is actively typing.
    whatsapp_message_burst_max_wait_seconds: float = 2.0
    # Bound one combined AI turn. Additional queued messages are handled by the same
    # serialized worker in the next burst rather than being rejected with HTTP 503.
    whatsapp_message_burst_max_messages: int = 8

    # MariaDB
    db_host: str = "127.0.0.1"
    db_port: int = 3307
    db_name: str = "mikexport"
    db_user: str = "whatsapp_bot"
    db_password: str = ""
    shipping_db_schema: str = "shipping_db"

    # Human handoff notification. This is only a secondary alert; durable
    # support state lives in MariaDB.
    human_handoff_webhook_url: str | None = None

    # Kept for backwards-compatible configuration parsing. Durable tickets do
    # NOT auto-expire based on this value; an unresolved ticket remains human-
    # controlled until an authorized agent explicitly resolves it.
    human_mode_timeout_seconds: int = 1800

    # Support dashboard/API.
    support_ui_enabled: bool = True
    support_queue_poll_seconds: int = 2
    support_session_ttl_seconds: int = 8 * 60 * 60
    support_cookie_name: str = "support_session"
    support_csrf_cookie_name: str = "support_csrf"
    support_cookie_secure: bool = True
    support_cookie_samesite: Literal["lax", "strict", "none"] = "strict"
    support_login_max_failures: int = 5
    support_login_lock_seconds: int = 15 * 60

    # Google Contacts OAuth / People API. OAuth refresh tokens are encrypted at
    # rest with the Fernet key below. Configure the redirect URI exactly in the
    # Google Cloud OAuth client (for example https://support.example.com/api/support/google-contacts/callback).
    google_contacts_client_id: str = ""
    google_contacts_client_secret: str = ""
    google_contacts_redirect_uri: str = ""
    google_contacts_token_encryption_key: str = ""

    # Media exchanged while a human agent owns the WhatsApp conversation. Files
    # are kept outside the public web root and are served only through authenticated
    # support endpoints. Browser microphone recordings are normalized to Ogg/Opus
    # with ffmpeg before being uploaded to WhatsApp.
    support_media_directory: str = "~/.local/share/whatsapp_chatbot/support_media"
    support_image_max_bytes: int = 5 * 1024 * 1024
    # WhatsApp Cloud API sticker uploads are WebP. The larger animated-sticker
    # ceiling is used here; Meta remains authoritative for format/dimension checks.
    support_sticker_max_bytes: int = 500 * 1024
    support_audio_max_bytes: int = 16 * 1024 * 1024
    support_document_max_bytes: int = 25 * 1024 * 1024
    support_media_download_timeout_seconds: float = 20.0
    support_media_transcode_timeout_seconds: float = 30.0
    support_media_ffmpeg_path: str = "ffmpeg"

    # Legacy generic message-rate settings are retained for backwards-compatible
    # environment parsing. OpenAI cost protection is enforced by the Redis-backed
    # rolling AI-turn quotas below instead of returning retryable webhook 429s.
    rate_limit_max_messages: int = 20
    rate_limit_window_seconds: int = 60

    # OpenAI abuse / wallet protection. A "turn" is one customer request that is
    # allowed to enter an AI-backed path. Rapid WhatsApp messages are already grouped
    # into one burst before this quota is consumed. Per-customer limits span every
    # business WhatsApp number on this deployment so switching lines cannot bypass
    # them. The global daily limit is a final circuit breaker for distributed abuse.
    ai_rate_limit_enabled: bool = True
    ai_rate_limit_turns_per_minute: int = 6
    ai_rate_limit_turns_per_hour: int = 30
    ai_rate_limit_turns_per_day: int = 120
    ai_rate_limit_global_turns_per_day: int = 2000
    ai_rate_limit_notice_cooldown_seconds: int = 60
    # Cost protection should fail closed if the quota backend cannot be checked.
    ai_rate_limit_fail_closed: bool = True

    # Bound generative output even for an allowed turn. This is a second cost guard
    # against prompts that try to force unusually long model responses.
    openai_max_output_tokens: int = 1400

    # Conversational customer-login brute-force protection. This is separate
    # from general message throughput so repeated credential verification can
    # be throttled more aggressively without penalizing ordinary chat.
    client_auth_rate_limit_max_attempts: int = 5
    client_auth_rate_limit_window_seconds: int = 5 * 60
    client_auth_pending_secret_ttl_seconds: int = 5 * 60

    # Conversation
    redis_url: str = "redis://localhost:6379/0"
    conversation_ttl_seconds: int = 259200
    shipping_quote_context_ttl_seconds: int = 30 * 60
    # Serialize all messages for one WhatsApp sender. This must be longer than
    # openai_turn_timeout_seconds.
    conversation_processing_lock_ttl_seconds: int = 3 * 60
    # Keep a send guard for each inbound WhatsApp message so webhook retries cannot
    # send a second customer-facing reply after an uncertain provider outcome.
    outbound_reply_dedupe_ttl_seconds: int = 7 * 24 * 60 * 60

    # History
    max_conversation_messages: int = 30
    summarizer_model: str = "gpt-6-luna"

    log_level: str = "INFO"


settings = Settings()


def linked_device_data_directory() -> Path:
    """Bridge state paths are rooted at the project, independent of process cwd."""
    directory = Path(settings.whatsapp_linked_device_data_directory)
    if not directory.is_absolute():
        directory = Path(__file__).resolve().parent.parent / directory
    return directory


def _manifest_linked_device_routing() -> tuple[dict[str, str], set[str]]:
    manifest_path = linked_device_data_directory() / "lines.json"
    try:
        with manifest_path.open("rb") as manifest_file:
            raw = manifest_file.read(128 * 1024 + 1)
    except FileNotFoundError:
        return {}, set()
    except OSError as exc:
        raise ValueError("The linked-device line manifest could not be read.") from exc
    if len(raw) > 128 * 1024:
        raise ValueError("The linked-device line manifest exceeds the size limit.")
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("The linked-device line manifest is invalid.") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) - {"version", "lines", "requests", "removed_line_ids", "removed_request_ids"}
        or type(manifest.get("version", 1)) is not int
        or manifest.get("version", 1) != 1
        or not isinstance(manifest.get("lines"), dict)
        or ("requests" in manifest and not isinstance(manifest["requests"], dict))
        or not isinstance(manifest.get("removed_line_ids", []), list)
        or not isinstance(manifest.get("removed_request_ids", []), list)
    ):
        raise ValueError("The linked-device line manifest has an unsupported structure.")
    lines = manifest["lines"]
    if any(not isinstance(line_id, str) or not isinstance(phone, str) for line_id, phone in lines.items()):
        raise ValueError("The linked-device line manifest requires numeric IDs and phone strings.")
    removed = manifest.get("removed_line_ids", [])
    if any(not isinstance(line_id, str) or not re.fullmatch(r"[0-9]{5,32}", line_id) for line_id in removed):
        raise ValueError("The linked-device line manifest has invalid removed IDs.")
    if any(not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", request_id) for request_id in manifest.get("removed_request_ids", [])):
        raise ValueError("The linked-device line manifest has invalid removed request IDs.")
    return Settings.validate_linked_device_lines(lines), set(removed)


def linked_device_routing_configuration() -> tuple[dict[str, str], set[str]]:
    """Read active routes and deletion tombstones from one manifest snapshot."""
    # Node atomically publishes the manifest after registration. Read each time
    # so every Python worker observes newly added/removed routes without restart.
    manifest_lines, removed = _manifest_linked_device_routing()
    lines = {line_id: phone for line_id, phone in manifest_lines.items() if line_id not in removed}
    environment_lines = Settings.validate_linked_device_lines(dict(settings.whatsapp_linked_device_lines))
    for line_id, phone in environment_lines.items():
        if line_id in removed:
            continue
        if line_id in lines and re.sub(r"[^0-9]", "", lines[line_id]) != re.sub(r"[^0-9]", "", phone):
            raise ValueError("The linked-device line manifest conflicts with an environment route.")
        lines[line_id] = phone
    # Deleting the last configured route must not activate the Cloud production
    # number as a bridge fallback. Tombstones also suppress the legacy default.
    if not (manifest_lines or environment_lines or removed) and settings.whatsapp_phone_number_id:
        lines[str(settings.whatsapp_phone_number_id).strip()] = ""
    return Settings.validate_linked_device_lines(lines), removed


def configured_linked_device_lines() -> dict[str, str]:
    """Return current allowed bridge routes, including newly registered numbers."""
    return linked_device_routing_configuration()[0]


def removed_linked_device_line_ids() -> set[str]:
    return linked_device_routing_configuration()[1]
