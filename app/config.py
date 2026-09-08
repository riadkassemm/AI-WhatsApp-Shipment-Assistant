from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6"
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
    # Ignore a uniquely delivered inbound message when Meta delivers it long after
    # the customer sent it. Set to 0 to disable the age check.
    whatsapp_max_inbound_age_seconds: int = 15 * 60
    # Customers often split one thought across several WhatsApp messages. Hold the
    # per-sender queue very briefly before starting a turn so adjacent messages can
    # be processed together instead of producing an obsolete clarification reply.
    whatsapp_message_burst_debounce_seconds: float = 1.25
    # Keep extending the quiet-period debounce when new messages arrive, but never
    # delay the first response indefinitely while a customer is actively typing.
    whatsapp_message_burst_max_wait_seconds: float = 4.0
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
    support_queue_poll_seconds: int = 5
    support_session_ttl_seconds: int = 8 * 60 * 60
    support_cookie_name: str = "support_session"
    support_csrf_cookie_name: str = "support_csrf"
    support_cookie_secure: bool = True
    support_cookie_samesite: Literal["lax", "strict", "none"] = "strict"
    support_login_max_failures: int = 5
    support_login_lock_seconds: int = 15 * 60

    # Media exchanged while a human agent owns the WhatsApp conversation. Files
    # are kept outside the public web root and are served only through authenticated
    # support endpoints. Browser microphone recordings are normalized to Ogg/Opus
    # with ffmpeg before being uploaded to WhatsApp.
    support_media_directory: str = "~/.local/share/whatsapp_chatbot/support_media"
    support_image_max_bytes: int = 5 * 1024 * 1024
    support_audio_max_bytes: int = 16 * 1024 * 1024
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
    max_conversation_messages: int = 20
    summarizer_model: str = "gpt-5.6"

    log_level: str = "INFO"


settings = Settings()
