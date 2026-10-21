from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

# Keep this unit-test module independent from production-only services in the
# artifact validation environment. The application still uses the real packages
# in production/CI when requirements.txt is installed.
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

from app import main, openai_service, shipping_intent_normalizer, support_api, support_ui
from app.ai_availability import (
    AIAvailabilityService,
    AIControlConfiguration,
    AIControlForbiddenError,
    AIControlValidationError,
    evaluate_ai_availability,
)
from app.ai_rate_limit import AIQuotaDecision
from app.conversation_store import Conversation, InMemoryConversationStore
from app.support_auth import SupportAgent, get_current_agent
from app.whatsapp_client import IncomingWhatsAppMessage, extract_incoming_message


def _configuration(
    *,
    mode: str = "MANUAL",
    manual_enabled: bool = True,
    start: str = "08:00",
    end: str = "18:00",
    timezone_name: str = "UTC",
    version: int = 1,
) -> AIControlConfiguration:
    return AIControlConfiguration(
        control_mode=mode,  # type: ignore[arg-type]
        manual_ai_enabled=manual_enabled,
        schedule_start_time=start,
        schedule_end_time=end,
        schedule_timezone=timezone_name,
        version=version,
    )


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 12, hour, minute, tzinfo=timezone.utc)


def test_manual_mode_evaluation() -> None:
    on = evaluate_ai_availability(_configuration(manual_enabled=True), now=_at(12))
    off = evaluate_ai_availability(_configuration(manual_enabled=False), now=_at(12))
    assert on.effective_ai_enabled is True
    assert on.effective_reason == "MANUAL_ON"
    assert off.effective_ai_enabled is False
    assert off.effective_reason == "MANUAL_OFF"


@pytest.mark.parametrize(
    ("hour", "minute", "enabled"),
    [
        (7, 59, False),
        (8, 0, True),
        (12, 0, True),
        (17, 59, True),
        (18, 0, False),
        (21, 0, False),
    ],
)
def test_normal_schedule_boundaries(hour: int, minute: int, enabled: bool) -> None:
    result = evaluate_ai_availability(
        _configuration(mode="SCHEDULED", start="08:00", end="18:00"),
        now=_at(hour, minute),
    )
    assert result.effective_ai_enabled is enabled
    assert result.effective_reason == ("WITHIN_SCHEDULE" if enabled else "OUTSIDE_SCHEDULE")


@pytest.mark.parametrize(
    ("hour", "minute", "enabled"),
    [
        (19, 0, False),
        (20, 0, True),
        (23, 0, True),
        (2, 0, True),
        (5, 59, True),
        (6, 0, False),
        (10, 0, False),
    ],
)
def test_overnight_schedule_boundaries(hour: int, minute: int, enabled: bool) -> None:
    result = evaluate_ai_availability(
        _configuration(mode="SCHEDULED", start="20:00", end="06:00"),
        now=_at(hour, minute),
    )
    assert result.effective_ai_enabled is enabled


def test_schedule_uses_iana_timezone_not_server_timezone() -> None:
    # On 2026-09-12 Beirut is UTC+3. 05:00 UTC is therefore exactly 08:00 local.
    result = evaluate_ai_availability(
        _configuration(
            mode="SCHEDULED",
            start="08:00",
            end="18:00",
            timezone_name="Asia/Beirut",
        ),
        now=_at(5, 0),
    )
    assert result.effective_ai_enabled is True
    assert result.next_transition_at is not None


class _RecordingAIRepository:
    def __init__(self) -> None:
        self.configuration = _configuration()
        self.update_calls: list[dict] = []

    async def get(self) -> AIControlConfiguration:
        return self.configuration

    async def update(self, **kwargs) -> AIControlConfiguration:
        self.update_calls.append(dict(kwargs))
        self.configuration = replace(
            self.configuration,
            control_mode=kwargs["control_mode"],
            manual_ai_enabled=kwargs["manual_ai_enabled"],
            schedule_start_time=kwargs["schedule_start_time"],
            schedule_end_time=kwargs["schedule_end_time"],
            schedule_timezone=kwargs["schedule_timezone"],
            version=self.configuration.version + 1,
            updated_by_staff_id=int(kwargs["actor"].id),
            updated_by_staff_name=kwargs["actor"].name,
        )
        return self.configuration


def _actor(role: str) -> SupportAgent:
    return SupportAgent(id="7", name="Manager", role=role)


@pytest.mark.parametrize("role", ["admin", "supervisor"])
def test_admin_and_supervisor_can_update_ai_settings(role: str) -> None:
    async def run() -> None:
        repository = _RecordingAIRepository()
        service = AIAvailabilityService(repository)  # type: ignore[arg-type]
        payload = await service.update_settings(
            actor=_actor(role),
            expected_version=1,
            control_mode="SCHEDULED",
            manual_ai_enabled=True,
            schedule_start_time="20:00",
            schedule_end_time="06:00",
            schedule_timezone="Asia/Beirut",
            now=_at(20),
        )
        assert payload["control_mode"] == "SCHEDULED"
        assert payload["schedule"]["timezone"] == "Asia/Beirut"
        assert len(repository.update_calls) == 1

    asyncio.run(run())


def test_agent_cannot_update_ai_settings() -> None:
    async def run() -> None:
        service = AIAvailabilityService(_RecordingAIRepository())  # type: ignore[arg-type]
        with pytest.raises(AIControlForbiddenError):
            await service.update_settings(
                actor=_actor("agent"),
                expected_version=1,
                control_mode="MANUAL",
                manual_ai_enabled=False,
                schedule_start_time="08:00",
                schedule_end_time="18:00",
                schedule_timezone="UTC",
            )

    asyncio.run(run())


def test_ai_update_api_returns_403_for_agent() -> None:
    async def run() -> None:
        payload = support_api.AISettingsUpdateRequest(
            control_mode="MANUAL",
            manual_ai_enabled=False,
            schedule={"start_time": "08:00", "end_time": "18:00", "timezone": "UTC"},
            version=1,
        )
        with pytest.raises(HTTPException) as caught:
            await support_api.update_ai_settings(payload, agent=_actor("agent"))
        assert caught.value.status_code == 403

    asyncio.run(run())


def test_unauthenticated_support_request_is_rejected() -> None:
    async def run() -> None:
        request = Request({"type": "http", "method": "PUT", "path": "/api/support/ai-settings", "headers": []})
        with pytest.raises(HTTPException) as caught:
            await get_current_agent(request)
        assert caught.value.status_code == 401

    asyncio.run(run())


@pytest.mark.parametrize(
    ("start", "end", "timezone_name"),
    [
        ("08:00", "08:00", "UTC"),
        ("25:00", "18:00", "UTC"),
        ("08:00", "18:00", "Not/A_Real_Timezone"),
    ],
)
def test_invalid_schedule_is_rejected(start: str, end: str, timezone_name: str) -> None:
    async def run() -> None:
        service = AIAvailabilityService(_RecordingAIRepository())  # type: ignore[arg-type]
        with pytest.raises(AIControlValidationError):
            await service.update_settings(
                actor=_actor("admin"),
                expected_version=1,
                control_mode="SCHEDULED",
                manual_ai_enabled=True,
                schedule_start_time=start,
                schedule_end_time=end,
                schedule_timezone=timezone_name,
            )

    asyncio.run(run())


class _AllowQuota:
    def __init__(self) -> None:
        self.calls = 0

    async def consume(self, _key: str) -> AIQuotaDecision:
        self.calls += 1
        return AIQuotaDecision(True, None)

    async def claim_notice(self, _key: str) -> bool:
        return True


async def _exercise_inbound(
    monkeypatch,
    *,
    incoming: IncomingWhatsAppMessage,
    availability,
    active_ticket: dict | None = None,
    preexisting_human_mode: bool = False,
) -> tuple[object, dict[str, object]]:
    store = InMemoryConversationStore()
    if preexisting_human_mode:
        await store.save(
            Conversation(
                phone_number=incoming.from_number,
                mode="human",
                active_ticket_id=str((active_ticket or {}).get("id") or 51),
                business_phone_number_id=incoming.business_phone_number_id,
            )
        )

    observations: dict[str, object] = {
        "ai_calls": 0,
        "shipping_calls": [],
        "handoffs": [],
        "human_routes": 0,
        "availability_calls": 0,
        "media_captures": 0,
        "sent": [],
        "provider_calls": 0,
    }
    quota = _AllowQuota()

    class _ProviderResponses:
        async def create(self, **_kwargs):
            observations["provider_calls"] = int(observations["provider_calls"]) + 1
            raise AssertionError("An AI provider request was made on a protected routing path.")

    class _ProviderClient:
        responses = _ProviderResponses()

    provider_client = _ProviderClient()

    async def get_ticket(*_args, **_kwargs):
        return active_ticket

    async def reconcile(conversation, ticket):
        if ticket is not None:
            conversation.mode = "human"
            conversation.active_ticket_id = str(ticket["id"])
            await store.save(conversation)

    async def no_support_message(*_args, **_kwargs):
        return None

    async def fake_availability():
        observations["availability_calls"] = int(observations["availability_calls"]) + 1
        return availability

    async def no_auth(*_args, **_kwargs):
        return None

    async def shipping(_conversation, _text, *, semantic_normalizer_enabled=True):
        casted = observations["shipping_calls"]
        assert isinstance(casted, list)
        casted.append(bool(semantic_normalizer_enabled))
        return None

    async def ai_handler(**_kwargs):
        observations["ai_calls"] = int(observations["ai_calls"]) + 1
        return True, "handled"

    async def finalize_handoff(*, handoff, conversation, **_kwargs):
        casted = observations["handoffs"]
        assert isinstance(casted, list)
        casted.append(handoff.reason)
        conversation.mode = "human"
        conversation.active_ticket_id = "51"
        await store.save(conversation)
        return {
            "id": 51,
            "ticket_reference": "CS-TEST",
            "status": "NEW",
            "current_assignee_id": None,
        }, True

    async def route_human_inbound(**_kwargs):
        observations["human_routes"] = int(observations["human_routes"]) + 1
        return {"id": 401}, True

    async def capture_inbound_media(**_kwargs):
        observations["media_captures"] = int(observations["media_captures"]) + 1
        return object()

    async def send_text_once(*, body: str, **_kwargs):
        casted = observations["sent"]
        assert isinstance(casted, list)
        casted.append(body)
        return {"messages": [{"id": "wamid.out"}]}

    monkeypatch.setattr(main, "conversation_store", store)
    monkeypatch.setattr(main, "ai_rate_limiter", quota)
    # Explicit provider-level spies prove that protected routes never reach either
    # OpenAI-backed path even if provider clients are otherwise available.
    monkeypatch.setattr(openai_service, "_get_client", lambda: provider_client)
    monkeypatch.setattr(shipping_intent_normalizer, "_get_client", lambda: provider_client)
    monkeypatch.setattr(main.support_service, "get_active_ticket", get_ticket)
    monkeypatch.setattr(main.support_service, "reconcile_conversation_mode", reconcile)
    monkeypatch.setattr(main.support_service, "get_message_by_whatsapp_id", no_support_message)
    monkeypatch.setattr(main.support_service, "get_ticket_by_source_message", no_support_message)
    monkeypatch.setattr(main.support_service, "finalize_handoff", finalize_handoff)
    monkeypatch.setattr(main.support_service, "route_human_inbound", route_human_inbound)
    monkeypatch.setattr(main.support_service, "capture_inbound_media", capture_inbound_media)
    monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", fake_availability)
    monkeypatch.setattr(main, "_handle_conversational_auth", no_auth)
    monkeypatch.setattr(main, "maybe_handle_shipping_quote", shipping)
    monkeypatch.setattr(main, "_handle_ai_result", ai_handler)
    monkeypatch.setattr(main, "_send_text_once", send_text_once)

    response = await main._process_incoming_message(incoming)
    observations["quota_calls"] = quota.calls
    return response, observations


def test_manual_ai_on_text_continues_existing_ai_flow(monkeypatch) -> None:
    async def run() -> None:
        availability = evaluate_ai_availability(_configuration(manual_enabled=True), now=_at(12))
        response, observed = await _exercise_inbound(
            monkeypatch,
            incoming=IncomingWhatsAppMessage("96170123456", "wamid.on", "text", "Hello", str(int(time.time()))),
            availability=availability,
        )
        assert response.status_code == 200
        assert observed["ai_calls"] == 1
        assert observed["handoffs"] == []
        assert observed["shipping_calls"] == [False, True]
        assert observed["quota_calls"] == 1

    asyncio.run(run())


def test_manual_ai_off_text_handoffs_before_any_ai_backed_path(monkeypatch) -> None:
    async def run() -> None:
        availability = evaluate_ai_availability(_configuration(manual_enabled=False), now=_at(12))
        response, observed = await _exercise_inbound(
            monkeypatch,
            incoming=IncomingWhatsAppMessage("96170123456", "wamid.off", "text", "Hello", str(int(time.time()))),
            availability=availability,
        )
        payload = json.loads(response.body)
        assert response.status_code == 200
        assert payload["status"] == "handed_off_ai_disabled"
        assert observed["handoffs"] == ["AI_DISABLED_GLOBAL"]
        assert observed["ai_calls"] == 0
        assert observed["shipping_calls"] == []
        assert observed["quota_calls"] == 0
        assert observed["provider_calls"] == 0
        assert observed["sent"] == [], "a handoff sends no message to the customer"

    asyncio.run(run())


def test_scheduled_off_text_handoffs_before_ai(monkeypatch) -> None:
    async def run() -> None:
        availability = evaluate_ai_availability(
            _configuration(mode="SCHEDULED", start="08:00", end="18:00"),
            now=_at(21),
        )
        response, observed = await _exercise_inbound(
            monkeypatch,
            incoming=IncomingWhatsAppMessage("96170123456", "wamid.soff", "text", "Hello", str(int(time.time()))),
            availability=availability,
        )
        assert response.status_code == 200
        assert observed["handoffs"] == ["AI_DISABLED_GLOBAL"]
        assert observed["ai_calls"] == 0
        assert observed["shipping_calls"] == []
        assert observed["provider_calls"] == 0

    asyncio.run(run())


def test_scheduled_active_text_continues_existing_ai_flow(monkeypatch) -> None:
    async def run() -> None:
        availability = evaluate_ai_availability(
            _configuration(mode="SCHEDULED", start="08:00", end="18:00"),
            now=_at(12),
        )
        response, observed = await _exercise_inbound(
            monkeypatch,
            incoming=IncomingWhatsAppMessage("96170123456", "wamid.son", "text", "Hello", str(int(time.time()))),
            availability=availability,
        )
        assert response.status_code == 200
        assert observed["ai_calls"] == 1
        assert observed["handoffs"] == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "availability",
    [
        evaluate_ai_availability(_configuration(manual_enabled=True), now=_at(12)),
        evaluate_ai_availability(_configuration(manual_enabled=False), now=_at(12)),
        evaluate_ai_availability(_configuration(mode="SCHEDULED"), now=_at(12)),
    ],
)
def test_voice_note_always_handoffs_without_ai(monkeypatch, availability) -> None:
    async def run() -> None:
        response, observed = await _exercise_inbound(
            monkeypatch,
            incoming=IncomingWhatsAppMessage(
                "96170123456",
                "wamid.voice",
                "audio",
                None,
                str(int(time.time())),
                media_id="media-voice",
                media_mime_type="audio/ogg; codecs=opus",
                is_voice=True,
            ),
            availability=availability,
        )
        payload = json.loads(response.body)
        assert response.status_code == 200
        assert payload["status"] == "handed_off_voice_note"
        assert observed["handoffs"] == ["VOICE_NOTE_UNSUPPORTED"]
        assert observed["ai_calls"] == 0
        assert observed["shipping_calls"] == []
        assert observed["quota_calls"] == 0
        assert observed["availability_calls"] == 0  # voice has higher routing priority
        assert observed["human_routes"] == 1
        assert observed["media_captures"] == 1
        assert observed["provider_calls"] == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    ("message_type", "is_voice", "expected_status", "expected_reason"),
    [
        ("image", False, "handed_off_media", "MEDIA_UNSUPPORTED_BY_AI"),
        ("audio", False, "handed_off_media", "MEDIA_UNSUPPORTED_BY_AI"),
        ("document", False, "handed_off_media", "MEDIA_UNSUPPORTED_BY_AI"),
        ("video", False, "handed_off_media", "MEDIA_UNSUPPORTED_BY_AI"),
    ],
)
def test_human_supported_media_handoffs_and_is_never_sent_to_ai(
    monkeypatch,
    message_type: str,
    is_voice: bool,
    expected_status: str,
    expected_reason: str,
) -> None:
    async def run() -> None:
        availability = evaluate_ai_availability(_configuration(manual_enabled=True), now=_at(12))
        response, observed = await _exercise_inbound(
            monkeypatch,
            incoming=IncomingWhatsAppMessage(
                "96170123456",
                f"wamid.{message_type}",
                message_type,
                None,
                str(int(time.time())),
                media_id=f"media-{message_type}",
                media_mime_type=(
                    "image/jpeg"
                    if message_type == "image"
                    else "application/pdf"
                    if message_type == "document"
                    else "video/mp4"
                    if message_type == "video"
                    else "audio/mpeg"
                ),
                media_caption=("Package photo" if message_type in {"image", "video"} else None),
                media_filename=("shipment.pdf" if message_type == "document" else None),
                is_voice=is_voice,
            ),
            availability=availability,
        )
        payload = json.loads(response.body)
        assert response.status_code == 200
        assert payload["status"] == expected_status
        assert observed["handoffs"] == [expected_reason]
        assert observed["human_routes"] == 1
        assert observed["media_captures"] == 1
        assert observed["sent"] == []
        assert observed["ai_calls"] == 0
        assert observed["shipping_calls"] == []
        assert observed["quota_calls"] == 0
        assert observed["availability_calls"] == 0
        assert observed["provider_calls"] == 0

    asyncio.run(run())


def test_customer_location_handoffs_without_ai_and_keeps_location_metadata(monkeypatch) -> None:
    async def run() -> None:
        availability = evaluate_ai_availability(_configuration(manual_enabled=True), now=_at(12))
        response, observed = await _exercise_inbound(
            monkeypatch,
            incoming=IncomingWhatsAppMessage(
                "96170123456",
                "wamid.location",
                "location",
                None,
                str(int(time.time())),
                location_latitude=33.8938,
                location_longitude=35.5018,
                location_name="Beirut",
                location_address="Beirut, Lebanon",
            ),
            availability=availability,
        )
        payload = json.loads(response.body)
        assert response.status_code == 200
        assert payload["status"] == "handed_off_location"
        assert observed["handoffs"] == ["LOCATION_UNSUPPORTED_BY_AI"]
        assert observed["human_routes"] == 1
        assert observed["media_captures"] == 0
        assert observed["ai_calls"] == 0
        assert observed["shipping_calls"] == []
        assert observed["availability_calls"] == 0
        assert observed["provider_calls"] == 0

    asyncio.run(run())


def test_existing_human_image_is_stored_without_creating_another_handoff(monkeypatch) -> None:
    async def run() -> None:
        ticket = {
            "id": 89,
            "status": "ASSIGNED",
            "current_assignee_id": "22",
            "business_phone_number_id": None,
        }
        availability = evaluate_ai_availability(_configuration(manual_enabled=True), now=_at(12))
        response, observed = await _exercise_inbound(
            monkeypatch,
            incoming=IncomingWhatsAppMessage(
                "96170123456",
                "wamid.human-image",
                "image",
                None,
                str(int(time.time())),
                media_id="media-human-image",
                media_mime_type="image/jpeg",
                media_caption="Photo for the agent",
            ),
            availability=availability,
            active_ticket=ticket,
            preexisting_human_mode=True,
        )
        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "held_for_human"
        assert observed["human_routes"] == 1
        assert observed["media_captures"] == 1
        assert observed["handoffs"] == []
        assert observed["ai_calls"] == 0
        assert observed["availability_calls"] == 0
        assert observed["provider_calls"] == 0

    asyncio.run(run())


def test_existing_human_assignment_is_never_reclaimed_by_ai(monkeypatch) -> None:
    async def run() -> None:
        ticket = {
            "id": 88,
            "status": "ASSIGNED",
            "current_assignee_id": "22",
            "business_phone_number_id": None,
        }
        availability = evaluate_ai_availability(_configuration(manual_enabled=True), now=_at(12))
        response, observed = await _exercise_inbound(
            monkeypatch,
            incoming=IncomingWhatsAppMessage("96170123456", "wamid.human", "text", "Still here", str(int(time.time()))),
            availability=availability,
            active_ticket=ticket,
            preexisting_human_mode=True,
        )
        assert response.status_code == 200
        assert json.loads(response.body)["status"] == "held_for_human"
        assert observed["human_routes"] == 1
        assert observed["ai_calls"] == 0
        assert observed["availability_calls"] == 0

    asyncio.run(run())


def test_customer_handed_off_while_off_stays_human_after_ai_turns_on(monkeypatch) -> None:
    # Durable ticket ownership, not the current global switch, is authoritative.
    async def run() -> None:
        ticket = {"id": 91, "status": "NEW", "current_assignee_id": None, "business_phone_number_id": None}
        availability = evaluate_ai_availability(_configuration(manual_enabled=True), now=_at(12))
        response, observed = await _exercise_inbound(
            monkeypatch,
            incoming=IncomingWhatsAppMessage("96170123456", "wamid.after-on", "text", "Another message", str(int(time.time()))),
            availability=availability,
            active_ticket=ticket,
            preexisting_human_mode=True,
        )
        assert response.status_code == 200
        assert observed["human_routes"] == 1
        assert observed["handoffs"] == []
        assert observed["ai_calls"] == 0

    asyncio.run(run())


def test_duplicate_whatsapp_webhook_does_not_process_handoff_twice(monkeypatch) -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        incoming = IncomingWhatsAppMessage("96170123456", "wamid.duplicate", "text", "Hello", "100")
        process_calls = 0

        class FakeRequest:
            headers = {"X-Hub-Signature-256": "sha256=fake"}

            async def body(self) -> bytes:
                return b'{"entry": []}'

        async def fake_process(_incoming):
            nonlocal process_calls
            process_calls += 1
            return main._json_response(200, {"status": "handed_off_ai_disabled"})

        async def no_ticket(*_args, **_kwargs):
            return None

        async def enabled_number(**_kwargs):
            return True

        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main, "verify_meta_signature", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(main, "extract_incoming_message", lambda _payload: incoming)
        monkeypatch.setattr(main, "_process_incoming_message", fake_process)
        monkeypatch.setattr(main.support_service, "get_active_ticket", no_ticket)
        monkeypatch.setattr(main.whatsapp_registry_service, "observe_inbound", enabled_number)
        monkeypatch.setattr(main.settings, "whatsapp_message_burst_debounce_seconds", 0)
        monkeypatch.setattr(main.settings, "whatsapp_message_burst_max_wait_seconds", 0)

        first = await main.receive_message(FakeRequest())  # type: ignore[arg-type]
        second = await main.receive_message(FakeRequest())  # type: ignore[arg-type]
        assert first.status_code == 200
        assert second.status_code == 200
        assert json.loads(second.body)["status"] == "duplicate_message"
        assert process_calls == 1

    asyncio.run(run())


def test_two_quick_off_messages_create_one_handoff_assignment(monkeypatch) -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        phone = "96170123456"
        ticket: dict | None = None
        finalize_calls = 0
        human_routes = 0
        availability = evaluate_ai_availability(_configuration(manual_enabled=False), now=_at(12))

        async def get_ticket(*_args, **_kwargs):
            return ticket

        async def reconcile(conversation, active):
            if active is not None:
                conversation.mode = "human"
                conversation.active_ticket_id = str(active["id"])
                await store.save(conversation)

        async def no_support_message(*_args, **_kwargs):
            return None

        async def fake_availability():
            return availability

        async def finalize_handoff(*, conversation, **_kwargs):
            nonlocal ticket, finalize_calls
            finalize_calls += 1
            if ticket is None:
                ticket = {"id": 501, "ticket_reference": "CS-501", "status": "NEW", "current_assignee_id": None}
            conversation.mode = "human"
            conversation.active_ticket_id = "501"
            await store.save(conversation)
            return ticket, finalize_calls == 1

        async def route_human(**_kwargs):
            nonlocal human_routes
            human_routes += 1
            return {"id": 700 + human_routes}, True

        async def fake_send(*_args, **_kwargs):
            return {"messages": [{"id": "out"}]}

        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main.support_service, "get_active_ticket", get_ticket)
        monkeypatch.setattr(main.support_service, "reconcile_conversation_mode", reconcile)
        monkeypatch.setattr(main.support_service, "get_message_by_whatsapp_id", no_support_message)
        monkeypatch.setattr(main.support_service, "get_ticket_by_source_message", no_support_message)
        monkeypatch.setattr(main.support_service, "finalize_handoff", finalize_handoff)
        monkeypatch.setattr(main.support_service, "route_human_inbound", route_human)
        monkeypatch.setattr(main.ai_availability_service, "get_effective_ai_availability", fake_availability)
        monkeypatch.setattr(main, "_send_text_once", fake_send)

        items = []
        now = int(time.time())
        for index, text in enumerate(("First", "Second"), start=1):
            incoming = IncomingWhatsAppMessage(phone, f"wamid.quick-{index}", "text", text, str(now + index))
            claim = await store.claim_incoming_message(incoming.message_id or "")
            assert claim is not None
            items.append(main._queued_inbound_item(incoming, claim))

        response = await main._process_inbound_burst(phone, items)
        assert response.status_code == 200
        assert finalize_calls == 1
        assert human_routes == 1  # second message follows existing human-owned path

    asyncio.run(run())


def test_voice_note_detection_uses_meta_audio_voice_flag() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "business-a"},
                            "messages": [
                                {
                                    "from": "96170123456",
                                    "id": "wamid.voice-detect",
                                    "timestamp": "123",
                                    "type": "audio",
                                    "audio": {
                                        "id": "media-audio",
                                        "mime_type": "audio/ogg; codecs=opus",
                                        "voice": True,
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    incoming = extract_incoming_message(payload)
    assert incoming is not None
    assert incoming.message_type == "audio"
    assert incoming.is_voice is True


def test_ai_control_migration_preserves_ai_on_default_and_audits_changes() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "20260912_ai_control_settings.sql"
    ).read_text()
    assert "INSERT IGNORE INTO ai_control_settings" in migration
    assert "'MANUAL'" in migration
    assert "ai_control_settings_audit" in migration
    assert "'AI_SETTINGS_UPDATED'" not in migration  # audit rows are runtime changes only





def test_assigned_staff_can_send_native_voice_note_without_ai(monkeypatch, outbound_unit) -> None:
    import app.support_service as support_service_module

    async def run() -> None:
        ticket = {
            "id": 77,
            "status": "ASSIGNED",
            "active_key": "business:96170123456",
            "current_assignee_id": "7",
            "whatsapp_phone": "96170123456",
            "business_phone_number_id": "business",
        }
        sent: dict = {}

        async def get_ticket(_ticket_id):
            return dict(ticket)

        async def no_existing(_client_id, **_scope):
            return None

        async def add_message(**kwargs):
            sent["stored"] = kwargs
            return {"id": 901, "ticket_id": 77, "transport_client_id": "unit-wire-id", **kwargs}, True

        async def prepare(content, mime_type):
            assert content.startswith(b"OggS")
            assert "audio/ogg" in mime_type
            return content, "audio/ogg; codecs=opus", "voice-note.ogg", True

        real_save = support_service_module.support_media_store.save

        def save(**kwargs):
            sent["saved"] = kwargs
            return real_save(**kwargs)

        async def upload_media(**kwargs):
            sent["upload"] = kwargs
            return "meta-media-id"

        async def send_media(**kwargs):
            sent["send"] = kwargs
            return {"messages": [{"id": "wamid.out.voice"}]}

        async def update_send_state(message_id, **kwargs):
            sent["send_state"] = {"message_id": message_id, **kwargs}

        async def mark_reply(*_args, **_kwargs):
            return None

        async def get_message(message_id):
            return {"id": message_id, "ticket_id": 77, "message_type": "audio", "send_status": "SENT"}

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
        monkeypatch.setattr(support_service_module.support_repository, "get_message_by_client_id", no_existing)
        monkeypatch.setattr(support_service_module.support_repository, "add_ticket_message", add_message)
        monkeypatch.setattr(support_service_module.support_repository, "update_message_send_state", update_send_state)
        monkeypatch.setattr(support_service_module.support_repository, "mark_agent_reply_started", mark_reply)
        monkeypatch.setattr(support_service_module.support_repository, "get_message", get_message)
        monkeypatch.setattr(support_service_module.support_media_store, "prepare_voice_note", prepare)
        monkeypatch.setattr(support_service_module.support_media_store, "save", save)
        monkeypatch.setattr(support_service_module.whatsapp_client, "upload_media", upload_media)
        monkeypatch.setattr(support_service_module.whatsapp_client, "send_media", send_media)

        result = await support_service_module.support_service.send_agent_media(
            77,
            agent=_actor("agent"),
            media_type="audio",
            content=b"OggSvoice",
            content_type="audio/ogg; codecs=opus",
            filename="voice.ogg",
            caption=None,
            client_message_id="client-voice-1",
        )

        assert result["send_status"] == "SENT"
        assert sent["stored"]["require_assignee_id"] == "7"
        assert sent["upload"]["mime_type"] == "audio/ogg; codecs=opus"
        assert sent["send"]["media_type"] == "audio"
        assert sent["send"]["voice"] is True
        assert sent["send"]["to"] == "96170123456"

    asyncio.run(run())

def test_ticket_detail_exposes_recoverable_media_url_from_durable_provider_reference(monkeypatch) -> None:
    import app.support_service as support_service_module

    async def run() -> None:
        ticket = {
            "id": 44,
            "status": "NEW",
            "active_key": "business:96170123456",
            "current_assignee_id": None,
            "whatsapp_phone": "96170123456",
            "business_phone_number_id": "business",
            "customer_id": None,
            "tracking_reference": None,
            "context_json": None,
        }
        message = {
            "id": 55,
            "ticket_id": 44,
            "message_type": "audio",
            "body": "Voice note",
            "provider_media_id": "meta-audio-id",
            "provider_media_mime_type": "audio/ogg; codecs=opus",
        }

        async def get_ticket(_ticket_id):
            return dict(ticket)

        async def list_messages(_ticket_id, *, limit=150, user_id=None):
            return [dict(message)]

        async def list_events(_ticket_id, *, limit=150):
            return []

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
        monkeypatch.setattr(support_service_module.support_repository, "list_messages", list_messages)
        monkeypatch.setattr(support_service_module.support_repository, "list_events", list_events)
        monkeypatch.setattr(support_service_module.support_media_store, "get", lambda *_args: None)
        monkeypatch.setattr(
            support_service_module.support_media_store,
            "get_remote_reference",
            lambda *_args: None,
        )

        detail = await support_service_module.support_service.ticket_detail(44, _actor("supervisor"))
        rendered = detail["messages"][0]
        assert rendered["media_available"] is True
        assert rendered["media_cached"] is False
        assert rendered["media_url"] == "/api/support/tickets/44/messages/55/media"
        assert rendered["media_mime_type"] == "audio/ogg"
        assert "provider_media_id" not in rendered
        assert "provider_media_mime_type" not in rendered

    asyncio.run(run())

def test_support_media_recovery_migration_is_additive() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "20260912_support_media_recovery.sql"
    ).read_text()
    assert "ALTER TABLE support_ticket_messages" in migration
    assert "ADD COLUMN IF NOT EXISTS provider_media_id" in migration
    assert "ADD COLUMN IF NOT EXISTS provider_media_mime_type" in migration
    assert "DROP " not in migration.upper()

def test_frontend_exposes_ai_status_to_staff_and_role_gates_editing() -> None:
    html = support_ui._HTML
    assert 'id="manageAiBtn"' not in html
    assert 'id="aiHeaderBtn"' in html
    assert 'id="aiHeaderStatus">—</span>' in html
    assert "$('aiHeaderBtn').addEventListener('click', openAiSettingsModal)" in html
    assert 'id="aiSettingsModal"' in html
    assert 'value="MANUAL"' in html and 'value="SCHEDULED"' in html
    assert 'id="aiScheduleStart"' in html and 'id="aiScheduleEnd"' in html
    assert 'id="aiScheduleTimezone"' in html
    assert "state.agent.can_manage_ai" in html
    assert "$('saveAiSettingsBtn').classList.toggle('hidden', !editable)" in html
    assert "await api('/api/support/ai-settings', {method:'PUT'" in html
    assert "await loadAiSettings();" in html
    assert "start === end" in html
    assert "validIanaTimezone" in html


def test_whatsapp_message_features_migration_is_additive_and_keeps_reply_location_document_metadata() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "20260917_whatsapp_message_features.sql"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS reply_to_whatsapp_message_id" in migration
    assert "ADD COLUMN IF NOT EXISTS location_latitude" in migration
    assert "ADD COLUMN IF NOT EXISTS location_longitude" in migration
    assert "ADD COLUMN IF NOT EXISTS location_name" in migration
    assert "ADD COLUMN IF NOT EXISTS location_address" in migration
    assert "ADD COLUMN IF NOT EXISTS provider_media_filename" in migration
    assert "DROP " not in migration.upper()


def test_latency_defaults_reduce_deliberate_wait_without_lowering_ai_reasoning() -> None:
    from app.config import settings

    assert settings.whatsapp_message_burst_debounce_seconds <= 0.65
    assert settings.whatsapp_message_burst_max_wait_seconds <= 2.0
    assert settings.support_queue_poll_seconds <= 2
    assert settings.openai_reasoning_effort == "medium"
    assert settings.max_conversation_messages >= 30


def test_human_ticket_inbound_skips_burst_debounce(monkeypatch) -> None:
    async def run() -> None:
        store = InMemoryConversationStore()
        await store.enqueue_incoming_work("business:customer", {"message_id": "one"})
        calls = []

        async def active_ticket(phone, *, business_phone_number_id=None):
            calls.append((phone, business_phone_number_id))
            return {"id": 42}

        async def unexpected_sleep(_seconds):
            raise AssertionError("Human ticket should not wait for AI burst debounce")

        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main.support_service, "get_active_ticket", active_ticket)
        monkeypatch.setattr(main.settings, "whatsapp_message_burst_debounce_seconds", 0.65)
        monkeypatch.setattr(main.asyncio, "sleep", unexpected_sleep)
        items = await main._collect_inbound_burst("business:customer")
        assert [item["message_id"] for item in items] == ["one"]
        assert calls == [("customer", "business")]

    asyncio.run(run())


def test_ai_quality_prompt_requires_complete_grounded_answers() -> None:
    from app.ai_config import CHATBOT_INSTRUCTIONS

    assert "identify every explicit question or request" in CHATBOT_INSTRUCTIONS
    assert "address every supported part" in CHATBOT_INSTRUCTIONS
    assert "instead of guessing" in CHATBOT_INSTRUCTIONS
    assert "fresh tool output controls facts" in CHATBOT_INSTRUCTIONS


def test_assigned_staff_can_send_pdf_with_reply_context(monkeypatch, outbound_unit) -> None:
    import app.support_service as support_service_module

    async def run() -> None:
        ticket = {
            "id": 78,
            "status": "IN_PROGRESS",
            "active_key": "business:96170123456",
            "current_assignee_id": "7",
            "whatsapp_phone": "96170123456",
            "business_phone_number_id": "business",
        }
        sent: dict = {}

        async def get_ticket(_ticket_id):
            return dict(ticket)

        async def no_existing(_client_id, **_scope):
            return None

        async def get_message(message_id):
            if message_id == 44:
                return {"id": 44, "ticket_id": 78, "whatsapp_message_id": "wamid.customer.pdf"}
            return {"id": message_id, "ticket_id": 78, "message_type": "document", "send_status": "SENT"}

        async def add_message(**kwargs):
            sent["stored"] = kwargs
            return {"id": 902, "ticket_id": 78, "transport_client_id": "unit-wire-id", **kwargs}, True

        real_save = support_service_module.support_media_store.save

        def save(**kwargs):
            sent["saved"] = kwargs
            return real_save(**kwargs)

        async def upload_media(**kwargs):
            sent["upload"] = kwargs
            return "meta-pdf-id"

        async def send_media(**kwargs):
            sent["send"] = kwargs
            return {"messages": [{"id": "wamid.out.pdf"}]}

        async def update_send_state(message_id, **kwargs):
            sent["send_state"] = {"message_id": message_id, **kwargs}

        monkeypatch.setattr(support_service_module.support_repository, "get_ticket", get_ticket)
        monkeypatch.setattr(support_service_module.support_repository, "get_message_by_client_id", no_existing)
        monkeypatch.setattr(support_service_module.support_repository, "get_message", get_message)
        monkeypatch.setattr(support_service_module.support_repository, "add_ticket_message", add_message)
        monkeypatch.setattr(support_service_module.support_repository, "update_message_send_state", update_send_state)
        monkeypatch.setattr(support_service_module.support_media_store, "save", save)
        monkeypatch.setattr(support_service_module.whatsapp_client, "upload_media", upload_media)
        monkeypatch.setattr(support_service_module.whatsapp_client, "send_media", send_media)

        result = await support_service_module.support_service.send_agent_media(
            78,
            agent=_actor("agent"),
            media_type="document",
            content=b"%PDF-1.4 fake",
            content_type="application/pdf",
            filename="invoice.pdf",
            caption="Invoice copy",
            client_message_id="client-pdf-1",
            reply_to_message_id=44,
        )

        assert result["send_status"] == "SENT"
        assert sent["stored"]["message_type"] == "document"
        assert sent["stored"]["reply_to_whatsapp_message_id"] == "wamid.customer.pdf"
        assert sent["upload"]["mime_type"] == "application/pdf"
        assert sent["send"]["media_type"] == "document"
        assert sent["send"]["filename"] == "invoice.pdf"
        assert sent["send"]["context_message_id"] == "wamid.customer.pdf"

    asyncio.run(run())


def test_ai_handoff_sends_no_message_to_customer(monkeypatch) -> None:
    from app.support_models import AIReplyResult, HandoffRequest

    async def run() -> None:
        store = InMemoryConversationStore()
        conversation = Conversation(phone_number="96170123456", business_phone_number_id="123")
        sent: list[str] = []
        handoffs: list[str] = []

        async def ai_reply(*_args, **_kwargs):
            return AIReplyResult(reply_text="", handoff=HandoffRequest(reason="CUSTOMER_REQUEST"))

        async def finalize_handoff(*, handoff, **_kwargs):
            handoffs.append(handoff.reason)
            return {"id": 61}, True

        async def send_text_once(*, body: str, **_kwargs):
            sent.append(body)

        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main, "_get_ai_reply_with_deadline", ai_reply)
        monkeypatch.setattr(main.support_service, "finalize_handoff", finalize_handoff)
        monkeypatch.setattr(main, "_send_text_once", send_text_once)

        handled, status = await main._handle_ai_result(
            conversation=conversation,
            incoming=IncomingWhatsAppMessage("96170123456", "wamid.human", "text", "Agent please", str(int(time.time()))),
            user_message_for_ticket="Agent please",
        )
        assert (handled, status) == (True, "handed_off")
        assert handoffs == ["CUSTOMER_REQUEST"]
        assert sent == []

    asyncio.run(run())
