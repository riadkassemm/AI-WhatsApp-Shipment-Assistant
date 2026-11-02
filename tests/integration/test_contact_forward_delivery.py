"""Contact forwards retain delivery fencing without granting general send rights."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.support_service as service
from app.whatsapp_client import WhatsAppSendError
from tests.integration.test_forward_contact_reuse import AGENT, forward, setup_forward


async def contact_forward(repo, *, ticket_id=43, key="attempt"):
    source, _ = await repo.add_ticket_message(
        ticket_id=42, sender_type="CUSTOMER", direction="INBOUND", body="Forward this",
    )
    row, _ = await repo.add_ticket_message(
        ticket_id=ticket_id, sender_type="AGENT", sender_id="7", sender_name="Agent",
        direction="OUTBOUND", body="Forward this", send_status="PENDING",
        client_message_id=f"contact-forward:42:{source['id']}:{key}",
        is_forwarded=True, forwarded_from_ticket_id=42,
        forwarded_from_message_id=source["id"],
    )
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("ticket_id", [41, 43])
async def test_contact_forward_delivers_into_unassigned_or_other_owned_ticket(infrastructure, ticket_id):
    repo, sql, _ = infrastructure
    before = (await sql("SELECT status,current_assignee_id,active_key FROM support_tickets WHERE id=%s", (ticket_id,)))[0]
    row = await contact_forward(repo, ticket_id=ticket_id)

    token = await repo.claim_delivery(row["id"])
    assert token
    assert await repo.begin_delivery(row["id"], token)
    await repo.finish_delivery(row["id"], token, status="SENT", provider_id="test-contact-forward")

    assert (await repo.get_message(row["id"]))["send_status"] == "SENT"
    assert (await sql("SELECT status,current_assignee_id,active_key FROM support_tickets WHERE id=%s", (ticket_id,)))[0] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["claim", "begin"])
async def test_contact_forward_namespace_requires_complete_trusted_metadata(infrastructure, phase):
    repo, sql, _ = infrastructure
    invalid_updates = [
        ("is_forwarded=0", ()),
        ("forwarded_from_ticket_id=NULL", ()),
        ("forwarded_from_ticket_id=0", ()),
        ("forwarded_from_ticket_id=-1", ()),
        ("forwarded_from_message_id=NULL", ()),
        ("forwarded_from_message_id=0", ()),
        ("forwarded_from_message_id=-1", ()),
        ("forwarded_from_ticket_id=41", ()),
        ("forwarded_from_message_id=forwarded_from_message_id+1", ()),
        ("client_message_id=REPLACE(client_message_id,'contact-forward:','forward:')", ()),
        ("client_message_id=REPLACE(client_message_id,'contact-forward:','CONTACT-FORWARD:')", ()),
        ("client_message_id=CONCAT('contact-forward:',forwarded_from_ticket_id,':',forwarded_from_message_id,':')", ()),
        ("client_message_id=NULL", ()),
        ("sender_id=NULL", ()),
        ("sender_type='CUSTOMER'", ()),
    ]
    for index, (update, args) in enumerate(invalid_updates):
        row = await contact_forward(repo, key=f"invalid-{index}")
        token = await repo.claim_delivery(row["id"]) if phase == "begin" else None
        if phase == "begin":
            assert token
        # All fragments above are fixed test data; no request input reaches SQL.
        await sql(f"UPDATE support_ticket_messages SET {update} WHERE id=%s", (*args, row["id"]))
        if phase == "claim":
            assert await repo.claim_delivery(row["id"]) is None, update
        else:
            assert not await repo.begin_delivery(row["id"], token), update
        assert (await repo.get_message(row["id"]))["send_status"] == "PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["claim", "begin"])
@pytest.mark.parametrize("inactive_update", ["active_key=NULL", "status='RESOLVED'", "status='CLOSED'"])
async def test_contact_forward_cannot_dispatch_into_inactive_ticket(infrastructure, phase, inactive_update):
    repo, sql, _ = infrastructure
    row = await contact_forward(repo)
    token = await repo.claim_delivery(row["id"]) if phase == "begin" else None
    await sql(f"UPDATE support_tickets SET {inactive_update} WHERE id=43")

    if phase == "claim":
        assert await repo.claim_delivery(row["id"]) is None
    else:
        assert token
        assert not await repo.begin_delivery(row["id"], token)


@pytest.mark.asyncio
async def test_contact_forward_lease_and_uncertainty_still_fence_dispatch(infrastructure):
    repo, sql, _ = infrastructure
    row = await contact_forward(repo)
    first = await repo.claim_delivery(row["id"])
    assert first
    assert await repo.claim_delivery(row["id"]) is None
    await sql("UPDATE support_ticket_messages SET delivery_claimed_at=UTC_TIMESTAMP(6)-INTERVAL 6 MINUTE WHERE id=%s", (row["id"],))
    assert not await repo.begin_delivery(row["id"], first)
    second = await repo.claim_delivery(row["id"])
    assert second and second != first
    assert not await repo.begin_delivery(row["id"], first)
    assert await repo.begin_delivery(row["id"], second)
    await repo.finish_delivery(row["id"], second, status="UNCERTAIN", error="Test uncertainty")
    assert await repo.claim_delivery(row["id"], retry=True, expected_token=second) is None
    assert not await repo.begin_delivery(row["id"], second)


@pytest.mark.asyncio
async def test_contact_forward_capability_does_not_enable_unowned_normal_sends(infrastructure):
    repo, _, _ = infrastructure
    for ticket_id in [41, 43]:
        row, _ = await repo.add_ticket_message(
            ticket_id=ticket_id, sender_type="AGENT", sender_id="7", sender_name="Agent",
            direction="OUTBOUND", body="Normal reply", send_status="PENDING",
            client_message_id=f"contact-forward:42:100:forged-{ticket_id}",
        )
        assert await repo.claim_delivery(row["id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SENT", "UNCERTAIN"])
@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("changed_source", [False, True])
async def test_contact_forward_preserves_legacy_attempt_after_upgrade(
    infrastructure, monkeypatch, tmp_path, status, closed, changed_source,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_forward(infrastructure, monkeypatch, tmp_path)
    await sql("UPDATE support_tickets SET current_assignee_id='7' WHERE id=43")
    uncertain_send = AsyncMock(side_effect=WhatsAppSendError("Test ambiguity", delivery_uncertain=True))
    if status == "UNCERTAIN":
        monkeypatch.setattr(service, "whatsapp_client", SimpleNamespace(send_text=uncertain_send))

    # Before this change contact forwards used the same durable request key as
    # direct forwards. An open browser may retry that key after deployment.
    legacy = await service.support_service.forward_message(
        source_ticket_id=42, source_message_id=original["id"], destination_ticket_id=43,
        agent=AGENT, client_message_id="legacy-browser-attempt",
    )
    assert legacy["client_message_id"] == f"forward:42:{original['id']}:legacy-browser-attempt"
    assert legacy["send_status"] == status
    await sql("UPDATE support_tickets SET current_assignee_id='8' WHERE id=43")
    if closed:
        await sql("UPDATE support_tickets SET active_key=NULL,status='RESOLVED' WHERE id=43")
    if changed_source:
        await sql("UPDATE support_ticket_messages SET body='Changed source content' WHERE id=%s", (original["id"],))
    before = await sql("SELECT * FROM support_tickets ORDER BY id")

    if changed_source:
        with pytest.raises(service.SupportConflictError):
            await forward(original, key="legacy-browser-attempt")
    else:
        duplicate = await forward(original, key="legacy-browser-attempt")
        assert duplicate["message"]["id"] == legacy["id"]
        assert duplicate["message"]["send_status"] == status
    assert await sql("SELECT * FROM support_tickets ORDER BY id") == before
    assert len(await repo.list_messages(43)) == 1
    assert len(calls) == (1 if status == "SENT" else 0)
    assert uncertain_send.await_count == (1 if status == "UNCERTAIN" else 0)
