"""Step 2b: the OpenAI API key is managed from the dashboard.

The key is stored encrypted, never returned or logged in full, takes effect on
the next request without a restart, and a missing or rejected key sends
conversations to human support instead of an automated guess.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import types
from datetime import datetime, timezone

import pytest

os.environ["REDIS_URL"] = ""

fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {})
)
sys.modules["aiomysql"] = fake_aiomysql

fake_bcrypt = sys.modules.get("bcrypt") or types.ModuleType("bcrypt")
fake_bcrypt.checkpw = getattr(fake_bcrypt, "checkpw", lambda *_args, **_kwargs: False)
fake_bcrypt.hashpw = getattr(fake_bcrypt, "hashpw", lambda *_args, **_kwargs: b"hash")
fake_bcrypt.gensalt = getattr(fake_bcrypt, "gensalt", lambda *_args, **_kwargs: b"salt")
sys.modules["bcrypt"] = fake_bcrypt

fake_openai = sys.modules.get("openai") or types.ModuleType("openai")
if not hasattr(fake_openai, "AsyncOpenAI"):
    class _AsyncOpenAI:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    fake_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules["openai"] = fake_openai

from cryptography.fernet import Fernet  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import ai_provider, main, openai_service, support_api  # noqa: E402
from app.ai_availability import AIAvailabilityService, AIControlConfiguration, evaluate_ai_availability  # noqa: E402
from app.ai_provider import (  # noqa: E402
    AIProviderForbiddenError,
    AIProviderRateLimitedError,
    AIProviderService,
    AIProviderUnavailableError,
    AIProviderValidationError,
    AISecretsNotConfiguredError,
    classify_provider_error,
)
from app.conversation_store import Conversation  # noqa: E402
from app.support_auth import SupportAgent  # noqa: E402
from app.support_ui import _HTML  # noqa: E402

AGENT = SupportAgent(id="7", name="Agent", role="agent")
ADMIN = SupportAgent(id="6", name="Admin", role="admin")
KEY_A = "sk-proj-" + "A" * 40 + "wxyz"
KEY_B = "sk-proj-" + "B" * 40 + "9876"
ENV_KEY = "sk-env-" + "E" * 40 + "eeee"


class AuthenticationError(Exception):
    status_code = 401


class RateLimitError(Exception):
    status_code = 429

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotFoundError(Exception):
    status_code = 404


class APIConnectionError(Exception):
    pass


class FakeCredentialRepository:
    """In-memory stand-in for ai_provider_credentials + its audit rows."""

    def __init__(self) -> None:
        self.row: dict | None = None
        self.audit: list[tuple[str, dict, dict]] = []

    async def get(self, *, include_secret: bool = False):
        if self.row is None:
            return None
        row = dict(self.row)
        if not include_secret:
            row.pop("api_key_encrypted", None)
        return row

    async def save(self, *, encrypted, last4, fingerprint, status, actor, action):
        old = dict(self.row or {})
        version = int(old.get("version") or 0) + 1
        self.row = {
            "provider": "openai", "api_key_encrypted": encrypted, "key_last4": last4,
            "key_fingerprint": fingerprint, "version": version, "status": status,
            "last_tested_at": datetime.now(timezone.utc) if status == "VALID" else None,
            "last_error_code": None, "updated_by_staff_id": int(actor.id),
            "updated_by_staff_name": actor.name, "updated_at": datetime.now(timezone.utc),
        }
        self.audit.append((action, {k: old.get(k) for k in ("key_last4", "key_fingerprint", "version")},
                           {"key_last4": last4, "key_fingerprint": fingerprint, "version": version}))
        return version

    async def set_status(self, *, version, status, error_code):
        if self.row and self.row["version"] == version and self.row.get("api_key_encrypted"):
            self.row.update(status=status, last_error_code=error_code, last_tested_at=datetime.now(timezone.utc))


class FakeClient:
    def __init__(self, api_key: str, error: Exception | None = None) -> None:
        self.api_key = api_key
        self.error = error
        outer = self

        class Models:
            async def retrieve(self, _model):
                if outer.error is not None:
                    raise outer.error
                return {"id": _model}

        self.models = Models()


def _service(monkeypatch, *, probe_error: Exception | None = None, env_key: str = ""):
    monkeypatch.setattr(ai_provider.settings, "ai_secrets_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(ai_provider.settings, "openai_api_key", env_key)
    repository = FakeCredentialRepository()
    service = AIProviderService(repository)
    built: list[FakeClient] = []

    def build(api_key, *, timeout, max_retries):
        client = FakeClient(api_key, probe_error)
        built.append(client)
        return client

    monkeypatch.setattr(service, "_build_client", build)
    return service, repository, built


# ---------------------------------------------------------------------------
# Saving, masking, permissions
# ---------------------------------------------------------------------------

def test_only_admins_save_and_the_key_is_stored_encrypted_and_masked(monkeypatch):
    service, repository, _ = _service(monkeypatch)
    with pytest.raises(AIProviderForbiddenError):
        asyncio.run(service.save_key(AGENT, KEY_A))
    assert repository.row is None

    payload = asyncio.run(service.save_key(ADMIN, KEY_A))
    stored = repository.row
    assert stored["api_key_encrypted"] and KEY_A not in stored["api_key_encrypted"]
    assert Fernet(ai_provider.settings.ai_secrets_encryption_key.encode()).decrypt(
        stored["api_key_encrypted"].encode()).decode() == KEY_A
    assert stored["key_last4"] == "wxyz" and stored["status"] == "VALID"
    assert payload["masked"] == "sk-…wxyz" and payload["source"] == "dashboard" and payload["usable"] is True
    assert KEY_A not in json.dumps(payload, default=str)
    action, _old, new = repository.audit[-1]
    assert action == "OPENAI_KEY_SAVED" and KEY_A not in json.dumps(new)


def test_agents_see_only_whether_ai_has_a_usable_key(monkeypatch):
    service, _, _ = _service(monkeypatch)
    asyncio.run(service.save_key(ADMIN, KEY_A))
    payload = asyncio.run(service.status_payload(AGENT))
    assert payload == {"configured": True, "status": "VALID", "usable": True}


def test_malformed_keys_and_missing_server_secret_are_refused(monkeypatch):
    service, repository, _ = _service(monkeypatch)
    for bad in ("", "not-a-key", "sk-short", "sk-" + "x" * 30 + " spaces"):
        with pytest.raises(AIProviderValidationError):
            asyncio.run(service.save_key(ADMIN, bad))
    monkeypatch.setattr(ai_provider.settings, "ai_secrets_encryption_key", "")
    with pytest.raises(AISecretsNotConfiguredError):
        asyncio.run(service.save_key(ADMIN, KEY_A, test_first=False))
    assert repository.row is None


def test_a_key_rejected_by_openai_is_not_saved_when_testing_first(monkeypatch):
    service, repository, _ = _service(monkeypatch, probe_error=AuthenticationError("bad key"))
    with pytest.raises(AIProviderValidationError, match="rejected this key"):
        asyncio.run(service.save_key(ADMIN, KEY_A))
    assert repository.row is None
    # Saving without testing is allowed and marked untested.
    asyncio.run(service.save_key(ADMIN, KEY_A, test_first=False))
    assert repository.row["status"] == "UNTESTED"


# ---------------------------------------------------------------------------
# Client factory and rotation
# ---------------------------------------------------------------------------

def test_new_key_applies_to_the_next_request_while_in_flight_clients_keep_theirs(monkeypatch):
    service, _, _ = _service(monkeypatch)
    asyncio.run(service.save_key(ADMIN, KEY_A))
    first = asyncio.run(service.get_client("chat"))
    assert first.api_key == KEY_A
    assert asyncio.run(service.get_client("chat")) is first  # cached per key version

    asyncio.run(service.save_key(ADMIN, KEY_B))
    second = asyncio.run(service.get_client("chat"))
    assert second.api_key == KEY_B and second is not first
    assert first.api_key == KEY_A  # a request that already holds it finishes on the old key


def test_environment_key_is_the_fallback_and_missing_key_raises(monkeypatch):
    service, _, _ = _service(monkeypatch, env_key=ENV_KEY)
    assert asyncio.run(service.get_client()).api_key == ENV_KEY
    payload = asyncio.run(service.status_payload(ADMIN))
    assert payload["source"] == "environment" and payload["masked"] == "sk-…eeee"

    asyncio.run(service.save_key(ADMIN, KEY_A))
    assert asyncio.run(service.get_client()).api_key == KEY_A  # dashboard key wins
    asyncio.run(service.delete_key(ADMIN))
    assert asyncio.run(service.get_client()).api_key == ENV_KEY

    empty, _, _ = _service(monkeypatch)
    with pytest.raises(AIProviderUnavailableError) as missing:
        asyncio.run(empty.get_client())
    assert missing.value.code == "API_KEY_MISSING"


def test_credential_state_drives_effective_availability(monkeypatch):
    service, repository, _ = _service(monkeypatch)
    assert asyncio.run(service.credential_state()) == (False, "API_KEY_MISSING")
    asyncio.run(service.save_key(ADMIN, KEY_A))
    assert asyncio.run(service.credential_state()) == (True, None)
    repository.row["status"] = "INVALID"
    assert asyncio.run(service.credential_state()) == (False, "API_KEY_INVALID")
    # A key encrypted with a different server secret cannot be used.
    repository.row["status"] = "VALID"
    service._decrypted = None
    monkeypatch.setattr(ai_provider.settings, "ai_secrets_encryption_key", Fernet.generate_key().decode())
    assert asyncio.run(service.credential_state()) == (False, "API_KEY_INVALID")


def test_effective_ai_is_off_without_a_usable_key_even_if_the_number_is_on():
    class Repo:
        async def get(self):
            return AIControlConfiguration("MANUAL", True, "08:00", "18:00", "UTC", 1)

        async def get_line_ai_enabled(self, _line):
            return True

        async def count_ai_enabled_lines(self):
            return 1

    async def missing():
        return False, "API_KEY_MISSING"

    service = AIAvailabilityService(Repo(), credential_state=missing)
    result = asyncio.run(service.get_effective_ai_availability(line_id="90052"))
    assert (result.effective_ai_enabled, result.effective_reason) == (False, "API_KEY_MISSING")
    payload = asyncio.run(service.get_settings_payload())
    assert payload["effective_ai_enabled"] is True  # global setting itself is on
    assert payload["provider"] == {"usable": False, "reason": "API_KEY_MISSING"}


# ---------------------------------------------------------------------------
# Test connection and runtime rejection
# ---------------------------------------------------------------------------

def test_connection_test_updates_status_and_is_rate_limited(monkeypatch):
    service, repository, built = _service(monkeypatch)
    asyncio.run(service.save_key(ADMIN, KEY_A, test_first=False))
    with pytest.raises(AIProviderForbiddenError):
        asyncio.run(service.test_connection(AGENT))
    result = asyncio.run(service.test_connection(ADMIN))
    assert result["test"]["ok"] is True and repository.row["status"] == "VALID"
    with pytest.raises(AIProviderRateLimitedError):
        asyncio.run(service.test_connection(ADMIN))

    service._last_test_at = time.monotonic() - 60
    monkeypatch.setattr(service, "_build_client",
                        lambda key, **_kw: FakeClient(key, RateLimitError("insufficient_quota")))
    result = asyncio.run(service.test_connection(ADMIN))
    assert result["test"] == {"ok": False, "error_code": "insufficient_quota",
                              "message": "This OpenAI account has no remaining quota or billing."}
    assert repository.row["status"] == "QUOTA_EXCEEDED" and result["usable"] is False


def test_runtime_rejection_marks_the_key_unusable(monkeypatch):
    service, repository, _ = _service(monkeypatch)
    asyncio.run(service.save_key(ADMIN, KEY_A))
    assert asyncio.run(service.report_failure(AuthenticationError("401"))) == "invalid_key"
    assert repository.row["status"] == "INVALID"
    # A transient error never marks the key unusable.
    asyncio.run(service.save_key(ADMIN, KEY_B))
    assert asyncio.run(service.report_failure(APIConnectionError("reset"))) == "network"
    assert repository.row["status"] == "VALID"


def test_provider_errors_are_classified_without_secrets():
    assert classify_provider_error(AuthenticationError("x")) == "invalid_key"
    assert classify_provider_error(RateLimitError("insufficient_quota")) == "insufficient_quota"
    assert classify_provider_error(RateLimitError("rate_limit_exceeded")) == "rate_limited"
    assert classify_provider_error(NotFoundError("x")) == "model_unavailable"
    assert classify_provider_error(APIConnectionError("x")) == "network"
    assert classify_provider_error(asyncio.TimeoutError()) == "network"
    assert classify_provider_error(ValueError("x")) == "provider_error"


def test_rejected_key_during_a_turn_hands_off_instead_of_replying(monkeypatch):
    reported = []

    class Responses:
        async def create(self, **_kwargs):
            raise AuthenticationError("Incorrect API key provided")

    class Client:
        responses = Responses()

    async def report(exc):
        reported.append(type(exc).__name__)
        return "invalid_key"

    monkeypatch.setattr(openai_service, "_get_client", lambda: Client())
    monkeypatch.setattr(openai_service.ai_provider_service, "report_failure", report)
    result = asyncio.run(openai_service.get_ai_reply(Conversation(phone_number="96170000001"), "hello"))
    assert result.reply_text == ""
    assert result.handoff is not None and result.handoff.reason == "AI_PROVIDER_UNAVAILABLE"
    assert reported == ["AuthenticationError"]


def test_routing_hands_off_with_provider_reason_when_no_key(monkeypatch):
    from app.conversation_store import InMemoryConversationStore
    from app.whatsapp_client import IncomingWhatsAppMessage

    handoffs = []
    base = evaluate_ai_availability(AIControlConfiguration("MANUAL", True, "08:00", "18:00", "UTC", 1))

    async def availability(**_kwargs):
        return type(base)(base.configuration, False, "API_KEY_MISSING", None)

    async def none(*_args, **_kwargs):
        return None

    async def finalize(*, handoff, **_kwargs):
        handoffs.append(handoff.reason)
        return {"id": 3, "ticket_reference": "CS-T", "status": "NEW"}, True

    monkeypatch.setattr(main, "conversation_store", InMemoryConversationStore())
    monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", availability)
    for name in ("get_active_ticket", "reconcile_conversation_mode", "get_message_by_whatsapp_id", "get_ticket_by_source_message"):
        monkeypatch.setattr(main.support_service, name, none)
    monkeypatch.setattr(main.support_service, "finalize_handoff", finalize)
    incoming = IncomingWhatsAppMessage("96170000001", "wamid.k", "text", "hi", str(int(time.time())),
                                       business_phone_number_id="90052")
    asyncio.run(main._process_incoming_message(incoming))
    assert handoffs == ["AI_PROVIDER_UNAVAILABLE"]


# ---------------------------------------------------------------------------
# API and secret hygiene
# ---------------------------------------------------------------------------

def test_request_model_never_echoes_the_key():
    model = support_api.OpenAIKeyRequest(api_key=KEY_A)
    assert KEY_A not in repr(model) and model.api_key.get_secret_value() == KEY_A
    assert model.test_first is True
    with pytest.raises(ValidationError) as error:
        support_api.OpenAIKeyRequest(api_key=KEY_A, extra_field=KEY_A)
    assert KEY_A not in str(error.value)


def test_errors_map_to_safe_http_statuses():
    assert support_api._translate_error(AIProviderForbiddenError("x")).status_code == 403
    assert support_api._translate_error(AIProviderValidationError("x")).status_code == 422
    assert support_api._translate_error(AIProviderRateLimitedError("x")).status_code == 429
    assert support_api._translate_error(AISecretsNotConfiguredError("x")).status_code == 503
    assert support_api._translate_error(AIProviderUnavailableError("API_KEY_MISSING")).status_code == 503


def test_openai_routes_require_csrf_for_changes():
    routes = {(tuple(sorted(r.methods)), r.path): r for r in support_api.router.routes if "ai-settings/openai" in getattr(r, "path", "")}
    assert {path for _methods, path in routes} == {"/api/support/ai-settings/openai", "/api/support/ai-settings/openai/test"}
    for (methods, _path), route in routes.items():
        dependencies = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
        if methods == ("GET",):
            assert "get_current_agent" in dependencies
        else:
            assert "require_support_csrf" in dependencies


def test_key_never_appears_in_logs(monkeypatch, caplog):
    service, _, _ = _service(monkeypatch)
    caplog.set_level(logging.DEBUG)
    asyncio.run(service.save_key(ADMIN, KEY_A))
    asyncio.run(service.test_connection(ADMIN))
    asyncio.run(service.report_failure(AuthenticationError(f"Incorrect API key provided: {KEY_A[:12]}")))
    assert KEY_A not in caplog.text and "wxyz" not in caplog.text


def test_dashboard_tab_is_for_managers_and_never_keeps_the_key():
    assert 'id="aiTabOpenAI"' in _HTML and 'id="aiOpenAIKey"' in _HTML and 'type="password"' in _HTML
    opener = _HTML[_HTML.index("async function openAiSettingsModal("):]
    assert "$('aiTabOpenAI').classList.toggle('hidden', !canManageAiSettings())" in opener
    saver = _HTML[_HTML.index("async function saveOpenAIKey("): _HTML.index("async function testOpenAIConnection(")]
    assert "input.value = '';" in saver[saver.index("finally"):]
    assert "'/api/support/ai-settings/openai'" in saver
