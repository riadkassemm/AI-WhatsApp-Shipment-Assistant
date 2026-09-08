from __future__ import annotations

import asyncio
import re
import sys
import types

# Lightweight stubs for production-only dependencies in the artifact test runtime.
fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {})
)
sys.modules["aiomysql"] = fake_aiomysql

fake_openai = sys.modules.get("openai") or types.ModuleType("openai")


class _AsyncOpenAI:
    def __init__(self, *_args, **_kwargs) -> None:
        pass


fake_openai.AsyncOpenAI = getattr(fake_openai, "AsyncOpenAI", _AsyncOpenAI)
sys.modules["openai"] = fake_openai

from app.conversation_store import Conversation
from app.customer_reply_guard import (
    render_customer_shipments,
    validate_customer_reply,
)
from app import main
from app import openai_service


_ARABIC_SCRIPT_RE = re.compile(r"[\u0600-\u06FF]")
_INCIDENT_TEXT = (
    'L2et 10 orders. 2elle ayya wa7ad بدك? Wait Arabic forbidden! '
    'Need all Latin. "baddak". list. Need no Arabic. Include identifiers. '
    'Maybe order IDs.'
)


def test_exact_incident_is_rejected_for_multiple_independent_reasons() -> None:
    validation = validate_customer_reply(_INCIDENT_TEXT, "leb_arabizi")
    assert validation.safe is False
    assert "arabic_script_in_arabizi" in validation.reasons
    assert "internal_note_leak" in validation.reasons


def test_clean_arabizi_reply_is_accepted() -> None:
    reply = "L2et 2 orders. B3atle l order ID li baddak shouf details taba3a."
    assert validate_customer_reply(reply, "leb_arabizi").safe is True


def test_phase_aware_extraction_uses_only_final_answer() -> None:
    response = types.SimpleNamespace(
        output_text=_INCIDENT_TEXT + "\nSAFE FINAL",
        output=[
            types.SimpleNamespace(
                type="message",
                phase="commentary",
                content=[types.SimpleNamespace(text=_INCIDENT_TEXT)],
            ),
            types.SimpleNamespace(
                type="message",
                phase="final_answer",
                content=[types.SimpleNamespace(text="L2et 2 recent orders.")],
            ),
        ],
    )
    assert openai_service._text_from_response(response) == "L2et 2 recent orders."


def test_commentary_only_response_is_not_treated_as_output_text() -> None:
    response = types.SimpleNamespace(
        output_text=_INCIDENT_TEXT,
        output=[
            types.SimpleNamespace(
                type="message",
                phase="commentary",
                content=[types.SimpleNamespace(text=_INCIDENT_TEXT)],
            )
        ],
    )
    assert openai_service._text_from_response(response) == ""


def test_authoritative_order_renderer_lists_identifiers_in_latin_only() -> None:
    rendered = render_customer_shipments(
        {
            "found": True,
            "count": 2,
            "shipments": [
                {
                    "order_id": "ORD-77",
                    "tracking_number": "TRK-77",
                    "shipment_id": "SHP-77",
                    "status": "IN_TRANSIT",
                },
                {
                    "order_id": "ORD-88",
                    "tracking_number": "TRK-88",
                    "shipment_id": "SHP-88",
                    "status": "RECEIVED",
                },
            ],
        },
        "leb_arabizi",
    )
    assert rendered is not None
    assert "ORD-77" in rendered
    assert "TRK-88" in rendered
    assert "SHP-88" in rendered
    assert not _ARABIC_SCRIPT_RE.search(rendered)
    assert validate_customer_reply(rendered, "leb_arabizi").safe is True


def test_unsafe_tool_reply_uses_deterministic_order_renderer_without_repair_call() -> None:
    async def run() -> None:
        conversation = Conversation(
            phone_number="96170123456",
            communication_style="leb_arabizi",
            current_language="ar-LB",
        )
        original_repair = openai_service._repair_customer_reply

        async def must_not_run(**_kwargs) -> str:
            raise AssertionError("deterministic tool renderer should run before model repair")

        openai_service._repair_customer_reply = must_not_run  # type: ignore[assignment]
        try:
            reply = await openai_service._prepare_customer_reply(
                candidate=_INCIDENT_TEXT,
                conversation=conversation,
                tool_results=[
                    {
                        "name": "get_customer_shipments",
                        "result": {
                            "found": True,
                            "count": 1,
                            "shipments": [
                                {
                                    "order_id": "ORD-99",
                                    "tracking_number": "TRK-99",
                                    "shipment_id": "SHP-99",
                                    "status": "IN_TRANSIT",
                                }
                            ],
                        },
                    }
                ],
            )
        finally:
            openai_service._repair_customer_reply = original_repair  # type: ignore[assignment]

        assert "ORD-99" in reply
        assert "TRK-99" in reply
        assert "Wait Arabic forbidden" not in reply
        assert not _ARABIC_SCRIPT_RE.search(reply)

    asyncio.run(run())


def test_final_whatsapp_boundary_replaces_unsafe_mocked_reply() -> None:
    conversation = Conversation(
        phone_number="96170123456",
        communication_style="leb_arabizi",
        current_language="ar-LB",
    )
    conversation.messages.append({"role": "assistant", "content": _INCIDENT_TEXT})

    guarded = main._guard_ai_reply_for_send(conversation, _INCIDENT_TEXT)

    assert guarded != _INCIDENT_TEXT
    assert "Wait Arabic forbidden" not in guarded
    assert not _ARABIC_SCRIPT_RE.search(guarded)
    assert conversation.messages[-1]["content"] == guarded


def test_unsafe_freeform_reply_is_repaired_with_structured_output() -> None:
    async def run() -> None:
        conversation = Conversation(
            phone_number="96170123456",
            communication_style="leb_arabizi",
            current_language="ar-LB",
        )
        captured: dict = {}

        class _Responses:
            async def create(self, **kwargs):
                captured.update(kwargs)
                return types.SimpleNamespace(
                    output=[
                        types.SimpleNamespace(
                            type="message",
                            phase="final_answer",
                            content=[
                                types.SimpleNamespace(
                                    text=(
                                        '{"reply_text":"L2et 10 orders. '
                                        'B3atle l order ID li baddak check."}'
                                    )
                                )
                            ],
                        )
                    ]
                )

        fake_client = types.SimpleNamespace(responses=_Responses())
        original_get_client = openai_service._get_client
        openai_service._get_client = lambda: fake_client  # type: ignore[assignment]
        try:
            reply = await openai_service._prepare_customer_reply(
                candidate=_INCIDENT_TEXT,
                conversation=conversation,
                tool_results=[],
            )
        finally:
            openai_service._get_client = original_get_client  # type: ignore[assignment]

        assert reply == "L2et 10 orders. B3atle l order ID li baddak check."
        assert captured["reasoning"] == {"effort": "none"}
        assert captured["text"]["format"]["type"] == "json_schema"
        assert captured["text"]["format"]["strict"] is True
        assert validate_customer_reply(reply, "leb_arabizi").safe is True

    asyncio.run(run())
