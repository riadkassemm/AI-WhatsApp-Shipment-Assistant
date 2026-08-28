from __future__ import annotations

import asyncio
import os
import re

import pytest

os.environ.setdefault("REDIS_URL", "")

from app import shipping_quote_service, staff_management
from app.conversation_store import Conversation
from app.language import detect_communication_style
from app.shipment_client import ShipmentClient, shipment_client
from app.shipping_quote_service import maybe_handle_shipping_quote
from app.support_api import UpdateStaffRequest
from app.support_auth import SupportAgent
from app.support_ui import _HTML


RATE_ROWS = [
    {
        "id": 1,
        "destination_id": 1,
        "origin": "UAE",
        "destination": "Lebanon",
        "shipping_method": "Air (Daily)",
        "goods_type": "Cosmetics",
        "price": "Pickup: $12.30/kg, Delivery: $12.80/kg",
        "transit_time": "2-3 business days",
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
    {
        "id": 12,
        "destination_id": 1,
        "origin": "Turkey",
        "destination": "Lebanon",
        "shipping_method": "Air (Every Friday)",
        "goods_type": "Normal (Clothes & Accessories)",
        "price": "$7.00/kg",
        "transit_time": "2-3 business days",
    },
    {
        "id": 13,
        "destination_id": 1,
        "origin": "Turkey",
        "destination": "Lebanon",
        "shipping_method": "Sea (Every Friday)",
        "goods_type": "Normal (General)",
        "price": "$5.50/kg",
        "transit_time": "Approx 15 business days",
    },
]


def _client_with_rows(rows: list[dict]) -> ShipmentClient:
    client = ShipmentClient()

    async def _load() -> list[dict]:
        return rows

    client._load_shipping_rates = _load  # type: ignore[method-assign]
    return client


def test_terkiya_typo_matches_english_turkey_clothes_row() -> None:
    async def run() -> None:
        result = await _client_with_rows(RATE_ROWS).get_shipping_price(
            origin="terkiya",
            destination="lebnen",
            weight_kg=50,
            goods_type="tyeb",
        )
        assert result["found"] is True
        assert result["matched_requested_filters"] is True
        assert result["origin"] == "Turkey"
        assert result["goods_type"] == "Normal (Clothes & Accessories)"
        assert result["calculated_total"] == "$350.00"

    asyncio.run(run())


def test_reported_two_turn_turkey_quote_uses_backend_state() -> None:
    async def run() -> None:
        original = shipment_client._load_shipping_rates

        async def _load() -> list[dict]:
            return RATE_ROWS

        shipment_client._load_shipping_rates = _load  # type: ignore[method-assign]
        try:
            message = "bade esh7an tyeb mn terkiya 3a lebnen adesh betkallef"
            profile = detect_communication_style(message)
            conversation = Conversation(
                phone_number="96170000001",
                current_language=profile.language,
                communication_style=profile.style,
            )
            first = await maybe_handle_shipping_quote(conversation, message)
            assert first is not None
            assert first.status == "shipping_quote_waiting_weight_kg"
            assert first.reply_text == "2adde wazna bel kg?"
            assert conversation.pending_shipping_quote == {
                "origin": "turkey",
                "destination": "lebanon",
                "goods_type": "clothes",
                "shipping_method_explicit": False,
            }

            second = await maybe_handle_shipping_quote(conversation, "50")
            assert second is not None
            assert second.status == "shipping_quote_completed"
            assert "$350.00" in second.reply_text
            assert "$7.00/kg" in second.reply_text
            assert not re.search(r"[\u0600-\u06FF]", second.reply_text)
            assert conversation.pending_shipping_quote is None
            assert conversation.last_shipping_quote["origin"] == "Turkey"
        finally:
            shipment_client._load_shipping_rates = original  # type: ignore[method-assign]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("message", "weight_reply", "expected_style"),
    [
        (
            "I want to ship clothes from Turkey to Lebanon. How much does it cost?",
            "50 kg",
            "en",
        ),
        (
            "Je veux expédier des vêtements de Turquie au Liban, combien ça coûte ?",
            "50",
            "fr",
        ),
        (
            "بدي اشحن تياب من تركيا على لبنان قديش بتكلف",
            "٥٠",
            "leb_ar",
        ),
        (
            "bade esh7an tyeb mn terkiya 3a lebnen adesh betkallef",
            "50",
            "leb_arabizi",
        ),
    ],
)
def test_same_quote_across_supported_styles(
    message: str,
    weight_reply: str,
    expected_style: str,
) -> None:
    async def run() -> None:
        original = shipment_client._load_shipping_rates

        async def _load() -> list[dict]:
            return RATE_ROWS

        shipment_client._load_shipping_rates = _load  # type: ignore[method-assign]
        try:
            profile = detect_communication_style(message)
            assert profile.style == expected_style
            conversation = Conversation(
                phone_number="96170000002",
                current_language=profile.language,
                communication_style=profile.style,
            )
            first = await maybe_handle_shipping_quote(conversation, message)
            assert first is not None
            second = await maybe_handle_shipping_quote(conversation, weight_reply)
            assert second is not None
            assert "$350.00" in second.reply_text
            if expected_style == "leb_arabizi":
                assert not re.search(r"[\u0600-\u06FF]", second.reply_text)
            if expected_style in {"ar", "leb_ar"}:
                assert re.search(r"[\u0600-\u06FF]", second.reply_text)
        finally:
            shipment_client._load_shipping_rates = original  # type: ignore[method-assign]

    asyncio.run(run())


def test_origin_followup_reuses_weight_goods_not_previous_db_method() -> None:
    async def run() -> None:
        original = shipment_client._load_shipping_rates

        async def _load() -> list[dict]:
            return RATE_ROWS

        shipment_client._load_shipping_rates = _load  # type: ignore[method-assign]
        try:
            conversation = Conversation(
                phone_number="96170000003",
                current_language="ar-LB",
                communication_style="leb_arabizi",
                last_shipping_quote={
                    "origin": "UAE",
                    "destination": "Lebanon",
                    "goods_type": "Cosmetics",
                    "weight_kg": "20",
                    "shipping_method_explicit": False,
                },
            )
            outcome = await maybe_handle_shipping_quote(
                conversation, "tyb w eza mn el su3udiye"
            )
            assert outcome is not None
            assert "$105.00" in outcome.reply_text
            assert "$115.00" in outcome.reply_text
            assert "bel barr" in outcome.reply_text
            assert conversation.last_shipping_quote["origin"] == "KSA"
        finally:
            shipment_client._load_shipping_rates = original  # type: ignore[method-assign]

    asyncio.run(run())


def test_expired_pending_context_does_not_capture_bare_number(monkeypatch) -> None:
    async def run() -> None:
        original = shipment_client._load_shipping_rates

        async def _load() -> list[dict]:
            return RATE_ROWS

        shipment_client._load_shipping_rates = _load  # type: ignore[method-assign]
        try:
            conversation = Conversation(
                phone_number="96170000004",
                communication_style="leb_arabizi",
                pending_shipping_quote={
                    "origin": "Turkey",
                    "destination": "Lebanon",
                    "goods_type": "clothes",
                },
                pending_shipping_quote_updated_at=100.0,
            )
            monkeypatch.setattr(shipping_quote_service.time, "time", lambda: 100000.0)
            outcome = await maybe_handle_shipping_quote(conversation, "50")
            assert outcome is None
            assert conversation.pending_shipping_quote is None
        finally:
            shipment_client._load_shipping_rates = original  # type: ignore[method-assign]

    asyncio.run(run())


def test_duplicate_email_conflict_proposes_role_change() -> None:
    async def run() -> None:
        actor = SupportAgent(id="1", name="Supervisor", role="supervisor")
        existing = {
            "id": 9,
            "name": "Test Agent",
            "email": "test@gmail.com",
            "email_normalized": "test@gmail.com",
            "role": "agent",
            "is_active": 1,
        }
        repo = staff_management.staff_repository
        original = repo.get_staff_by_email

        async def get_existing(_email: str):
            return existing

        repo.get_staff_by_email = get_existing  # type: ignore[method-assign]
        try:
            with pytest.raises(staff_management.StaffManagementConflictError) as caught:
                await staff_management.create_staff_account(
                    actor,
                    name="Duplicate Admin",
                    email=" Test@Gmail.com ",
                    password="LongInitialPass123!",
                    role="admin",
                )
        finally:
            repo.get_staff_by_email = original  # type: ignore[method-assign]

        detail = caught.value.detail()
        assert detail["code"] == "email_conflict"
        assert detail["existing_staff"]["id"] == "9"
        assert detail["requested_role"] == "admin"
        assert detail["suggested_action"] == "change_role"

    asyncio.run(run())


def test_supervisor_updates_credentials_role_activation_and_deletes_agent() -> None:
    async def run() -> None:
        actor = SupportAgent(id="1", name="Supervisor", role="supervisor")
        target = {
            "id": 12,
            "name": "Agent One",
            "email": "agent@example.com",
            "email_normalized": "agent@example.com",
            "password_hash": "old",
            "role": "agent",
            "is_active": 1,
        }
        calls: dict[str, object] = {}
        repo = staff_management.staff_repository
        original_get = repo.get_staff_by_id
        original_update = repo.update_staff
        original_delete = repo.soft_delete_staff

        async def get_target(_staff_id: int):
            return target

        async def update_target(staff_id: int, **kwargs):
            calls["update"] = (staff_id, kwargs)
            return {
                **target,
                "name": kwargs.get("name") or target["name"],
                "email": kwargs.get("email") or target["email"],
                "role": kwargs.get("role") or target["role"],
                "is_active": (
                    kwargs.get("is_active")
                    if kwargs.get("is_active") is not None
                    else target["is_active"]
                ),
            }

        async def delete_target(staff_id: int):
            calls["delete"] = staff_id
            return {**target, "is_active": 0}

        repo.get_staff_by_id = get_target  # type: ignore[method-assign]
        repo.update_staff = update_target  # type: ignore[method-assign]
        repo.soft_delete_staff = delete_target  # type: ignore[method-assign]
        try:
            updated = await staff_management.update_staff_account(
                actor,
                12,
                name="Agent Updated",
                email="updated@example.com",
                password="NewLongPassword123!",
                role="admin",
                is_active=True,
            )
            deleted = await staff_management.delete_staff_account(actor, 12)
        finally:
            repo.get_staff_by_id = original_get  # type: ignore[method-assign]
            repo.update_staff = original_update  # type: ignore[method-assign]
            repo.soft_delete_staff = original_delete  # type: ignore[method-assign]

        staff_id, kwargs = calls["update"]  # type: ignore[misc]
        assert staff_id == 12
        assert kwargs["email_normalized"] == "updated@example.com"
        assert kwargs["password_hash"] != "NewLongPassword123!"
        assert kwargs["role"] == "admin"
        assert kwargs["revoke_sessions"] is True
        assert updated["role"] == "admin"
        assert calls["delete"] == 12
        assert deleted["deleted"] is True
        assert deleted["is_active"] is False

    asyncio.run(run())


def test_self_delete_and_supervisor_structural_changes_are_blocked() -> None:
    async def run() -> None:
        actor = SupportAgent(id="1", name="Supervisor", role="supervisor")
        target = {
            "id": 1,
            "name": "Supervisor",
            "email": "supervisor@example.com",
            "email_normalized": "supervisor@example.com",
            "password_hash": "old",
            "role": "supervisor",
            "is_active": 1,
        }
        repo = staff_management.staff_repository
        original_get = repo.get_staff_by_id

        async def get_target(_staff_id: int):
            return target

        repo.get_staff_by_id = get_target  # type: ignore[method-assign]
        try:
            with pytest.raises(staff_management.StaffManagementConflictError):
                await staff_management.delete_staff_account(actor, 1)
            with pytest.raises(staff_management.StaffManagementConflictError):
                await staff_management.update_staff_account(actor, 1, is_active=False)
        finally:
            repo.get_staff_by_id = original_get  # type: ignore[method-assign]

    asyncio.run(run())


def test_staff_api_and_ui_expose_full_lifecycle() -> None:
    payload = UpdateStaffRequest(
        name="Updated Agent",
        email="updated@example.com",
        password="LongUpdatedPass123!",
        role="admin",
        is_active=False,
    )
    assert payload.role == "admin"
    api_source = open("app/support_api.py", encoding="utf-8").read()
    assert '@router.patch("/staff/{staff_id}")' in api_source
    assert '@router.delete("/staff/{staff_id}")' in api_source
    assert "method:editing?'PATCH':'POST'" in _HTML
    assert "method:'DELETE'" in _HTML
    assert "Edit the existing account" in _HTML
    assert "Duplicate email" in _HTML
