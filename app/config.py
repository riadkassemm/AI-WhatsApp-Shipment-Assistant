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

    # Rate limiting
    rate_limit_max_messages: int = 20
    rate_limit_window_seconds: int = 60

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
