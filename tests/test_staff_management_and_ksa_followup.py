from __future__ import annotations

import asyncio
import os
import re
import sys
import types

import pytest
from pydantic import ValidationError

os.environ.setdefault("REDIS_URL", "")

# Production DB/hash dependencies are not installed in this artifact-validation
# runtime. The tests replace all I/O and exercise application behavior only.
fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql,
    "IntegrityError",
    type("IntegrityError", (Exception,), {}),
)
sys.modules["aiomysql"] = fake_aiomysql

fake_bcrypt = sys.modules.get("bcrypt") or types.ModuleType("bcrypt")
fake_bcrypt.checkpw = getattr(fake_bcrypt, "checkpw", lambda *_args, **_kwargs: False)
fake_bcrypt.hashpw = getattr(
    fake_bcrypt,
    "hashpw",
    lambda password, _salt: b"$2b$12$artifact-test-password-hash",
)
fake_bcrypt.gensalt = getattr(fake_bcrypt, "gensalt", lambda *_args, **_kwargs: b"salt")
sys.modules["bcrypt"] = fake_bcrypt

fake_openai = sys.modules.get("openai") or types.ModuleType("openai")


class _AsyncOpenAI:
    def __init__(self, *_args, **_kwargs) -> None:
        pass


fake_openai.AsyncOpenAI = getattr(fake_openai, "AsyncOpenAI", _AsyncOpenAI)
sys.modules["openai"] = fake_openai

from app import openai_service, staff_management
from app.ai_tools import dispatch_tool_call, shipment_client
from app.conversation_store import Conversation
from app.customer_reply_guard import render_shipping_price, validate_customer_reply
from app.language import detect_communication_style
from app.shipment_client import ShipmentClient, detect_shipping_method_in_text
from app.support_api import CreateStaffRequest
from app.support_auth import SupportAgent
from app.support_ui import _HTML


KSA_ROWS = [
    {
        "id": 4,
        "destination_id": 1,
        "origin": "KSA",
        "destination": "Lebanon",
        "shipping_method": "Land (Every Thursday)",
        "goods_type": "Normal (General)",
        "price": "Pickup: $4.50/kg, Delivery: $5.00/kg",
        "transit_time": "20-25 days from departure",
    },
    {
        "id": 5,
        "destination_id": 1,
        "origin": "KSA",
        "destination": "Lebanon",
        "shipping_method": "Land (Every Thursday)",
        "goods_type": "Cosmetics",
        "price": "Pickup: $5.25/kg, Delivery: $5.75/kg",
        "transit_time": "20-25 days from departure",
    },
]


def _client_with_rows(rows: list[dict]) -> ShipmentClient:
    client = ShipmentClient()

    async def _load() -> list[dict]:
        return rows

    client._load_shipping_rates = _load  # type: ignore[method-assign]
    return client


def test_reported_ksa_arabizi_quote_matches_cosmetics_and_multiplies_per_kg() -> None:
    async def run() -> None:
        result = await _client_with_rows(KSA_ROWS).get_shipping_price(
            origin="el su3udiye",
            destination="lebnen",
            weight_kg=20,
            goods_type="mawed tejmil",
            shipping_method=None,
        )
        assert result["found"] is True
        assert result["matched_requested_filters"] is True
        assert result["origin"] == "KSA"
        assert result["goods_type"] == "Cosmetics"
        assert result["shipping_method"] == "Land (Every Thursday)"
        assert result["calculated_totals"] == {
            "pickup": "$105.00",
            "delivery": "$115.00",
        }

    asyncio.run(run())


def test_followup_without_method_drops_model_inherited_air_filter() -> None:
    captured: dict[str, object] = {}

    async def fake_lookup(**kwargs):
        captured.update(kwargs)
        return {"found": False, "query": kwargs}

    async def run() -> None:
        original = shipment_client.get_shipping_price
        shipment_client.get_shipping_price = fake_lookup  # type: ignore[method-assign]
        try:
            await dispatch_tool_call(
                "get_shipping_price",
                {
                    "origin": "el su3udiye",
                    "destination": "lebnen",
                    "weight_kg": 20,
                    "goods_type": "mawed tejmil",
                    # This simulates an LLM copying "air" from the previous UAE
                    # database result even though the active follow-up did not say it.
                    "shipping_method": "air",
                },
                verified_customer_id=None,
                current_user_message="tyb w eza mn el su3udiye",
            )
        finally:
            shipment_client.get_shipping_price = original  # type: ignore[method-assign]

        assert captured["shipping_method"] is None

    asyncio.run(run())


def test_explicit_method_in_active_turn_is_kept() -> None:
    assert detect_shipping_method_in_text("eza mn el su3udiye bl barr") == "land"
    assert detect_shipping_method_in_text("par avion") == "air"
    assert detect_shipping_method_in_text("بالجو") == "air"
    assert detect_shipping_method_in_text("tyb w eza mn el su3udiye") is None


def test_reported_messages_remain_lebanese_arabizi() -> None:
    messages = [
        "bade esh7an 20 kg mawed tejmil mn emarat 3a lebnen adesh betkallef",
        "tyb w eza mn el su3udiye",
        "eza bade esh7an mn el su3udiye",
    ]
    for message in messages:
        profile = detect_communication_style(message)
        assert profile.language == "ar-LB"
        assert profile.style == "leb_arabizi"


def test_short_shipping_followups_keep_arabic_french_and_english_styles() -> None:
    assert detect_communication_style("طيب وإذا من السعودية").style == "leb_ar"
    assert (
        detect_communication_style("Et si c'est depuis l'Arabie saoudite ?").style
        == "fr"
    )
    assert detect_communication_style("What about Saudi Arabia?").style == "en"


def test_ksa_quote_renderer_is_exact_and_all_latin_for_arabizi() -> None:
    async def run() -> None:
        result = await _client_with_rows(KSA_ROWS).get_shipping_price(
            origin="su3udiye",
            destination="lebnen",
            weight_kg=20,
            goods_type="mawed tejmil",
        )
        rendered = render_shipping_price(result, "leb_arabizi")
        assert rendered is not None
        assert "$105.00" in rendered
        assert "$115.00" in rendered
        assert "$5.25/kg" in rendered
        assert "$5.75/kg" in rendered
        assert "bel barr" in rendered
        assert "20-25 yom" in rendered
        assert not re.search(r"[\u0600-\u06FF]", rendered)
        assert validate_customer_reply(rendered, "leb_arabizi").safe is True

    asyncio.run(run())


def test_shipping_tool_output_is_preferred_over_a_safe_but_wrong_model_draft() -> None:
    async def run() -> None:
        result = await _client_with_rows(KSA_ROWS).get_shipping_price(
            origin="su3udiye",
            destination="lebnen",
            weight_kg=20,
            goods_type="mawed tejmil",
        )
        conversation = Conversation(
            phone_number="96170000000",
            current_language="ar-LB",
            communication_style="leb_arabizi",
        )
        reply = await openai_service._prepare_customer_reply(
            candidate="Ma fi se3er msajjal.",
            conversation=conversation,
            tool_results=[{"name": "get_shipping_price", "result": result}],
        )
        assert "$105.00" in reply
        assert "$115.00" in reply
        assert "Ma fi se3er msajjal" not in reply

    asyncio.run(run())


def test_unknown_goods_category_lists_available_rates_instead_of_misquoting_one() -> None:
    async def run() -> None:
        result = await _client_with_rows(KSA_ROWS).get_shipping_price(
            origin="su3udiye",
            destination="lebnen",
            weight_kg=20,
            goods_type="documents",
        )
        assert result["matched_requested_filters"] is False
        assert result["unmatched_filters"] == ["goods_type"]
        rendered = render_shipping_price(result, "leb_arabizi")
        assert rendered is not None
        assert "exact" not in rendered.casefold() or "Ma l2et" in rendered
        assert "aghrad 3adiye" in rendered
        assert "mawed tejmil" in rendered
        assert "$90.00" in rendered
        assert "$105.00" in rendered
        assert not re.search(r"[\u0600-\u06FF]", rendered)

    asyncio.run(run())


def test_supervisor_can_create_agent_or_admin_without_exposing_hash() -> None:
    async def run(role: str) -> None:
        actor = SupportAgent(id="7", name="Supervisor", role="supervisor")
        calls: dict[str, object] = {}

        async def get_by_email(email_normalized: str):
            calls["lookup"] = email_normalized
            return None

        async def create_staff(**kwargs):
            calls["create"] = kwargs
            return 44

        async def get_public(staff_id: int):
            assert staff_id == 44
            return {
                "id": 44,
                "name": "New Staff",
                "email": "new@example.com",
                "role": role,
                "is_active": 1,
                "last_login_at": None,
                "created_at": None,
                "updated_at": None,
            }

        repo = staff_management.staff_repository
        original_lookup = repo.get_staff_by_email
        original_create = repo.create_staff
        original_public = repo.get_public_staff_by_id
        repo.get_staff_by_email = get_by_email  # type: ignore[method-assign]
        repo.create_staff = create_staff  # type: ignore[method-assign]
        repo.get_public_staff_by_id = get_public  # type: ignore[method-assign]
        try:
            result = await staff_management.create_staff_account(
                actor,
                name="New Staff",
                email="New@Example.com",
                password="LongInitialPass123!",
                role=role,
            )
        finally:
            repo.get_staff_by_email = original_lookup  # type: ignore[method-assign]
            repo.create_staff = original_create  # type: ignore[method-assign]
            repo.get_public_staff_by_id = original_public  # type: ignore[method-assign]

        assert result["role"] == role
        assert result["email"] == "new@example.com"
        assert "password_hash" not in result
        assert calls["lookup"] == "new@example.com"
        create_args = calls["create"]
        assert isinstance(create_args, dict)
        assert create_args["password_hash"] != "LongInitialPass123!"
        assert create_args["role"] == role

    asyncio.run(run("agent"))
    asyncio.run(run("admin"))


def test_agent_cannot_manage_staff_and_supervisor_role_cannot_be_created() -> None:
    agent = SupportAgent(id="8", name="Agent", role="agent")

    async def run() -> None:
        with pytest.raises(staff_management.StaffManagementForbiddenError):
            await staff_management.list_staff_accounts(agent)

    asyncio.run(run())

    with pytest.raises(ValidationError):
        CreateStaffRequest(
            name="Another Supervisor",
            email="supervisor@example.com",
            password="LongInitialPass123!",
            role="supervisor",  # type: ignore[arg-type]
        )


def test_support_ui_exposes_staff_management_only_through_supervisor_gate() -> None:
    assert 'id="manageStaffBtn"' in _HTML
    assert "canManageStaff()" in _HTML
    assert "'/api/support/staff'" in _HTML
    assert 'value="agent"' in _HTML
    assert 'value="admin"' in _HTML
    assert 'value="supervisor"' not in _HTML
