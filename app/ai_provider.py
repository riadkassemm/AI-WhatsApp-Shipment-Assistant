"""OpenAI credentials managed from the dashboard, and the shared client factory.

Admins and Super Admins paste the OpenAI API key in the dashboard. It is stored
encrypted (Fernet, key from ``AI_SECRETS_ENCRYPTION_KEY``), never returned in
full, never logged, and audited by fingerprint only. ``OPENAI_API_KEY`` from the
environment remains a fallback when no dashboard key is saved.

Every OpenAI call obtains its client from ``ai_provider_service.get_client()``.
The credential version is read from MariaDB on each call (one primary-key row),
so a new key applies on every instance immediately and without a restart. The
decrypted key is cached per version. A request already in flight keeps the
client object it started with and finishes on the old key.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiomysql

from app.config import settings
from app.support_auth import SupportAgent

logger = logging.getLogger("shipment-bot")

PROVIDER = "openai"
# OpenAI secret keys: "sk-" plus letters, digits, "-" and "_" (project keys are "sk-proj-...").
_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{20,400}$")
_UNUSABLE_STATUSES = {"INVALID", "QUOTA_EXCEEDED"}
_TEST_MIN_INTERVAL_SECONDS = 5.0
_CLIENT_TIMEOUTS = {
    "chat": lambda: max(1.0, float(settings.openai_request_timeout_seconds)),
    "normalizer": lambda: max(1.0, float(settings.shipping_semantic_normalizer_timeout_seconds)),
}


class AIProviderError(Exception):
    code = "provider_error"


class AIProviderForbiddenError(AIProviderError):
    code = "forbidden"


class AIProviderValidationError(AIProviderError):
    code = "invalid_request"


class AIProviderRateLimitedError(AIProviderError):
    code = "rate_limited"


class AISecretsNotConfiguredError(AIProviderError):
    code = "secrets_not_configured"


class AIProviderUnavailableError(AIProviderError):
    """No usable OpenAI key: AI must hand over to human support."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ActiveCredential:
    source: str  # "dashboard" or "environment"
    version: str
    status: str
    api_key: str = field(repr=False)


def key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def masked_key(last4: str | None) -> str | None:
    return f"sk-…{last4}" if last4 else None


def classify_provider_error(exc: BaseException) -> str:
    """Map an OpenAI SDK/transport error to a stable, secret-free code."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", "") or "")
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        code = code or str((body.get("error") or body).get("code") or "")
    if name in {"AuthenticationError", "PermissionDeniedError"} or status in {401, 403}:
        return "invalid_key"
    if code == "insufficient_quota" or "insufficient_quota" in str(exc):
        return "insufficient_quota"
    if name == "NotFoundError" or status == 404:
        return "model_unavailable"
    if name in {"APIConnectionError", "APITimeoutError", "TimeoutError", "ConnectError"} or isinstance(exc, TimeoutError):
        return "network"
    if name == "RateLimitError" or status == 429:
        return "rate_limited"
    return "provider_error"


_STATUS_FOR_ERROR = {
    "invalid_key": "INVALID",
    "insufficient_quota": "QUOTA_EXCEEDED",
    "model_unavailable": "MODEL_UNAVAILABLE",
}


def _fernet():
    from cryptography.fernet import Fernet, MultiFernet

    raw = str(settings.ai_secrets_encryption_key or "").strip()
    if not raw:
        raise AISecretsNotConfiguredError(
            "Secure key storage is not configured on the server (AI_SECRETS_ENCRYPTION_KEY)."
        )
    try:
        # Several comma-separated keys allow rotating the encryption key itself:
        # the first encrypts, all of them can decrypt.
        return MultiFernet([Fernet(part.strip().encode("ascii")) for part in raw.split(",") if part.strip()])
    except Exception as exc:
        raise AISecretsNotConfiguredError(
            "AI_SECRETS_ENCRYPTION_KEY must contain valid Fernet keys."
        ) from exc


class AIProviderCredentialRepository:
    def __init__(self) -> None:
        self._pool: aiomysql.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await aiomysql.create_pool(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            db=settings.db_name,
            minsize=1,
            maxsize=5,
            autocommit=True,
            charset="utf8mb4",
        )

    async def _pool_ready(self) -> aiomysql.Pool:
        if self._pool is None:
            await self.connect()
        assert self._pool is not None
        return self._pool

    async def get(self, *, include_secret: bool = False) -> dict[str, Any] | None:
        pool = await self._pool_ready()
        columns = (
            "provider, key_last4, key_fingerprint, version, status, last_tested_at, last_error_code, "
            "updated_by_staff_id, updated_by_staff_name, updated_at"
        )
        if include_secret:
            columns += ", api_key_encrypted"
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    f"SELECT {columns} FROM ai_provider_credentials WHERE provider = %s LIMIT 1",
                    (PROVIDER,),
                )
                return await cur.fetchone()

    async def save(
        self,
        *,
        encrypted: str | None,
        last4: str | None,
        fingerprint: str | None,
        status: str,
        actor: SupportAgent,
        action: str,
    ) -> int:
        pool = await self._pool_ready()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT key_last4, key_fingerprint, version, status FROM ai_provider_credentials "
                        "WHERE provider = %s FOR UPDATE",
                        (PROVIDER,),
                    )
                    current = await cur.fetchone()
                    version = int((current or {}).get("version") or 0) + 1
                    await cur.execute(
                        """
                        INSERT INTO ai_provider_credentials (
                            provider, api_key_encrypted, key_last4, key_fingerprint, version, status,
                            last_tested_at, last_error_code, updated_by_staff_id, updated_by_staff_name,
                            updated_at, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s,
                                  IF(%s IN ('VALID'), UTC_TIMESTAMP(6), NULL), NULL, %s, %s,
                                  UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                        ON DUPLICATE KEY UPDATE
                            api_key_encrypted = VALUES(api_key_encrypted),
                            key_last4 = VALUES(key_last4),
                            key_fingerprint = VALUES(key_fingerprint),
                            version = VALUES(version),
                            status = VALUES(status),
                            last_tested_at = VALUES(last_tested_at),
                            last_error_code = NULL,
                            updated_by_staff_id = VALUES(updated_by_staff_id),
                            updated_by_staff_name = VALUES(updated_by_staff_name),
                            updated_at = VALUES(updated_at)
                        """,
                        (PROVIDER, encrypted, last4, fingerprint, version, status, status,
                         int(actor.id), actor.name),
                    )
                    old = {key: (current or {}).get(key) for key in ("key_last4", "key_fingerprint", "version", "status")}
                    new = {"key_last4": last4, "key_fingerprint": fingerprint, "version": version, "status": status}
                    # Audit by fingerprint and last four characters only - never the key.
                    await cur.execute(
                        """
                        INSERT INTO ai_control_settings_audit (
                            settings_id, action, actor_staff_id, actor_staff_name,
                            old_configuration_json, new_configuration_json, created_at
                        ) VALUES (1, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
                        """,
                        (action, int(actor.id), actor.name,
                         json.dumps(old, default=str), json.dumps(new, default=str)),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return version

    async def set_status(self, *, version: int, status: str, error_code: str | None) -> None:
        """Record a test or runtime result, only if the key has not changed since."""
        pool = await self._pool_ready()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE ai_provider_credentials
                    SET status = %s, last_error_code = %s, last_tested_at = UTC_TIMESTAMP(6)
                    WHERE provider = %s AND version = %s AND api_key_encrypted IS NOT NULL
                    """,
                    (status, error_code, PROVIDER, int(version)),
                )


class AIProviderService:
    def __init__(self, repository: AIProviderCredentialRepository | None = None) -> None:
        self.repository = repository or AIProviderCredentialRepository()
        self._decrypted: tuple[str, ActiveCredential] | None = None
        self._clients: dict[tuple[str, str], Any] = {}
        self._environment_status: dict[str, str] = {}
        self._last_test_at = 0.0

    # -- permissions --------------------------------------------------------
    @staticmethod
    def _require_manager(agent: SupportAgent) -> None:
        if not agent.is_supervisor:
            raise AIProviderForbiddenError("Only Super Admins and Admins can manage the OpenAI configuration.")

    # -- active credential ----------------------------------------------------
    def _environment_credential(self) -> ActiveCredential | None:
        key = str(settings.openai_api_key or "").strip()
        if not key:
            return None
        fingerprint = key_fingerprint(key)
        return ActiveCredential(
            source="environment",
            version=f"env:{fingerprint}",
            status=self._environment_status.get(fingerprint, "ENVIRONMENT"),
            api_key=key,
        )

    async def active_credential(self) -> ActiveCredential | None:
        row = await self.repository.get()
        if not row or not row.get("key_fingerprint"):
            return self._environment_credential()
        version = f"db:{int(row.get('version') or 0)}"
        cached = self._decrypted
        if cached is None or cached[0] != version:
            secret_row = await self.repository.get(include_secret=True)
            token = str((secret_row or {}).get("api_key_encrypted") or "")
            try:
                api_key = _fernet().decrypt(token.encode("ascii")).decode("utf-8")
            except AISecretsNotConfiguredError:
                raise
            except Exception as exc:
                raise AIProviderUnavailableError(
                    "API_KEY_UNREADABLE", "The saved OpenAI key cannot be decrypted with the server key.",
                ) from exc
            cached = (version, ActiveCredential("dashboard", version, "UNTESTED", api_key))
            self._decrypted = cached
        return ActiveCredential(
            source="dashboard", version=version, status=str(row.get("status") or "UNTESTED"),
            api_key=cached[1].api_key,
        )

    async def credential_state(self) -> tuple[bool, str | None]:
        """(usable, reason) used by the effective AI availability check."""
        try:
            credential = await self.active_credential()
        except AIProviderError:
            return False, "API_KEY_INVALID"
        if credential is None:
            return False, "API_KEY_MISSING"
        if credential.status in _UNUSABLE_STATUSES:
            return False, "API_KEY_INVALID"
        return True, None

    async def has_api_key(self) -> bool:
        if str(settings.openai_api_key or "").strip():
            return True
        try:
            row = await self.repository.get()
        except Exception:
            return False
        return bool(row and row.get("key_fingerprint"))

    def _build_client(self, api_key: str, *, timeout: float, max_retries: int):
        from openai import AsyncOpenAI

        return AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=max_retries)

    async def get_client(self, purpose: str = "chat"):
        credential = await self.active_credential()
        if credential is None:
            raise AIProviderUnavailableError("API_KEY_MISSING", "No OpenAI API key is configured.")
        cache_key = (credential.version, purpose)
        client = self._clients.get(cache_key)
        if client is None:
            timeout = _CLIENT_TIMEOUTS.get(purpose, _CLIENT_TIMEOUTS["chat"])()
            client = self._build_client(
                credential.api_key, timeout=timeout, max_retries=max(0, int(settings.openai_max_retries)),
            )
            # Drop clients for older keys; requests already running keep their own
            # reference and finish on the old key.
            self._clients = {k: v for k, v in self._clients.items() if k[0] == credential.version}
            self._clients[cache_key] = client
        return client

    async def report_failure(self, exc: BaseException) -> str:
        """Record an authentication/quota rejection seen on a real request."""
        code = classify_provider_error(exc)
        status = _STATUS_FOR_ERROR.get(code)
        if status in _UNUSABLE_STATUSES:
            try:
                credential = await self.active_credential()
                if credential is not None and credential.source == "dashboard":
                    await self.repository.set_status(
                        version=int(credential.version.split(":", 1)[1]), status=status, error_code=code,
                    )
                elif credential is not None:
                    self._environment_status[credential.version.split(":", 1)[1]] = status
            except Exception:
                logger.warning("Could not record OpenAI credential status: code=%s", code)
            logger.error("OpenAI rejected the configured key: code=%s", code)
        return code

    # -- dashboard ---------------------------------------------------------------
    async def status_payload(self, agent: SupportAgent) -> dict[str, Any]:
        row = await self.repository.get()
        environment = self._environment_credential()
        dashboard = bool(row and row.get("key_fingerprint"))
        status = (
            str(row.get("status") or "UNTESTED") if dashboard
            else (environment.status if environment else "NOT_CONFIGURED")
        )
        usable = (dashboard or environment is not None) and status not in _UNUSABLE_STATUSES
        payload: dict[str, Any] = {
            "configured": dashboard or environment is not None,
            "status": status,
            "usable": usable,
        }
        if not agent.is_supervisor:
            return payload
        payload.update({
            "source": "dashboard" if dashboard else ("environment" if environment else None),
            "masked": masked_key(row.get("key_last4")) if dashboard else (
                masked_key(environment.api_key[-4:]) if environment else None
            ),
            "last_tested_at": _iso(row.get("last_tested_at")) if dashboard else None,
            "last_error_code": row.get("last_error_code") if dashboard else None,
            "updated_at": _iso(row.get("updated_at")) if dashboard else None,
            "updated_by": (
                {"id": str(row["updated_by_staff_id"]), "name": row.get("updated_by_staff_name")}
                if dashboard and row.get("updated_by_staff_id") else None
            ),
            "secure_storage_configured": bool(str(settings.ai_secrets_encryption_key or "").strip()),
            "model": settings.openai_model,
        })
        return payload

    async def _probe(self, api_key: str) -> str | None:
        """Cost-free authenticated check: retrieve the configured model's metadata."""
        client = self._build_client(api_key, timeout=10.0, max_retries=0)
        try:
            await client.models.retrieve(settings.openai_model)
        except Exception as exc:
            return classify_provider_error(exc)
        return None

    async def save_key(self, agent: SupportAgent, api_key: str, *, test_first: bool = True) -> dict[str, Any]:
        self._require_manager(agent)
        key = str(api_key or "").strip()
        if not _KEY_RE.fullmatch(key):
            raise AIProviderValidationError("This does not look like an OpenAI API key (it should start with sk-).")
        fernet = _fernet()
        status = "UNTESTED"
        if test_first:
            error = await self._probe(key)
            if error is not None:
                raise AIProviderValidationError(_test_message(error) + " The key was not saved.")
            status = "VALID"
        fingerprint = key_fingerprint(key)
        await self.repository.save(
            encrypted=fernet.encrypt(key.encode("utf-8")).decode("ascii"),
            last4=key[-4:], fingerprint=fingerprint, status=status, actor=agent, action="OPENAI_KEY_SAVED",
        )
        logger.info("OpenAI key saved: actor_staff_id=%s fingerprint=%s tested=%s", agent.id, fingerprint, test_first)
        return await self.status_payload(agent)

    async def delete_key(self, agent: SupportAgent) -> dict[str, Any]:
        self._require_manager(agent)
        await self.repository.save(
            encrypted=None, last4=None, fingerprint=None, status="NOT_CONFIGURED", actor=agent,
            action="OPENAI_KEY_REMOVED",
        )
        logger.info("OpenAI key removed: actor_staff_id=%s", agent.id)
        return await self.status_payload(agent)

    async def test_connection(self, agent: SupportAgent) -> dict[str, Any]:
        self._require_manager(agent)
        now = time.monotonic()
        if now - self._last_test_at < _TEST_MIN_INTERVAL_SECONDS:
            raise AIProviderRateLimitedError("Please wait a few seconds before testing again.")
        self._last_test_at = now
        credential = await self.active_credential()
        if credential is None:
            raise AIProviderValidationError("No OpenAI API key is configured yet.")
        error = await self._probe(credential.api_key)
        status = "VALID" if error is None else _STATUS_FOR_ERROR.get(error, credential.status)
        if credential.source == "dashboard":
            if error is None or error in _STATUS_FOR_ERROR:
                await self.repository.set_status(
                    version=int(credential.version.split(":", 1)[1]), status=status, error_code=error,
                )
        elif error is None or error in _STATUS_FOR_ERROR:
            self._environment_status[credential.version.split(":", 1)[1]] = status
        logger.info("OpenAI connection test: actor_staff_id=%s ok=%s code=%s", agent.id, error is None, error)
        payload = await self.status_payload(agent)
        payload["test"] = {"ok": error is None, "error_code": error, "message": _test_message(error)}
        return payload


def _test_message(error: str | None) -> str:
    return {
        None: "Connection to OpenAI works.",
        "invalid_key": "OpenAI rejected this key.",
        "insufficient_quota": "This OpenAI account has no remaining quota or billing.",
        "model_unavailable": f"This key cannot use the configured model ({settings.openai_model}).",
        "network": "OpenAI could not be reached. Try again shortly.",
        "rate_limited": "OpenAI is rate limiting requests. Try again shortly.",
    }.get(error, "OpenAI returned an unexpected error.")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        from datetime import timezone

        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(value)


ai_provider_service = AIProviderService()
