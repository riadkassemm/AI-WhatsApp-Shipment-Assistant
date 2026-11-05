"""Forward resolution preserves source-office identity under real DB races."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import app.support_service as service
from app.whatsapp_client import WhatsAppSendError
from tests.integration.test_forward_contact_reuse import AGENT, PHONE, api_client, forward, setup_forward
from tests.integration.test_quick_reply_pdfs import create_sessions


async def setup_offices(infrastructure, monkeypatch, tmp_path, *, state="create", kind="text"):
    repo, sql, _ = infrastructure
    original, calls = await setup_forward(infrastructure, monkeypatch, tmp_path, kind=kind)
    # The selected conversation is assigned to this agent, but uses another office.
    await sql("""UPDATE support_tickets SET business_phone_number_id='90006',
        active_key='90006:155500043',current_assignee_id='7' WHERE id=43""")
    if state != "create":
        await sql("""UPDATE support_tickets SET whatsapp_phone=%s,
            active_key=%s,status=%s,conversation_origin='STAFF',current_assignee_id='8'
            WHERE id=41""", (PHONE, "90005:" + PHONE if state == "existing" else None,
                            "ASSIGNED" if state == "existing" else "CLOSED"))
    media = {
        "image": (b"synthetic-jpeg", "image/jpeg", "photo.jpg"),
        "video": (b"synthetic-mp4", "video/mp4", "video.mp4"),
        "sticker": (b"synthetic-webp", "image/webp", "sticker.webp"),
        "audio": (b"synthetic-ogg", "audio/ogg", "voice.ogg"),
    }
    if kind in media:
        content, mime, filename = media[kind]
        service.support_media_store.save(ticket_id=42, message_id=original["id"],
            content=content, mime_type=mime, filename=filename)
        if kind == "audio":
            monkeypatch.setattr(service.support_media_store, "prepare_voice_note",
                AsyncMock(return_value=(content, "audio/ogg; codecs=opus", filename, True)))
    return original, calls


async def forward_via(route, original, *, key="source-office-forward"):
    if route == "contact":
        return (await forward(original, key=key))["message"]
    return await service.support_service.forward_message(
        source_ticket_id=42, source_message_id=original["id"], destination_ticket_id=43,
        agent=AGENT, client_message_id=key,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["contact", "conversation"])
@pytest.mark.parametrize("state", ["existing", "create", "reopen"])
async def test_forward_resolves_recipient_in_source_office(
    infrastructure, monkeypatch, tmp_path, route, state,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_offices(infrastructure, monkeypatch, tmp_path, state=state)
    other_office = await repo.get_ticket(43)
    message = await forward_via(route, original)
    duplicate = await forward_via(route, original)

    assert duplicate["id"] == message["id"]
    assert message["ticket_id"] != 43 and message["send_status"] == "SENT"
    destination = await repo.get_ticket(message["ticket_id"])
    assert destination["whatsapp_phone"] == PHONE
    assert destination["business_phone_number_id"] == "90005"
    assert destination["active_key"] == "90005:" + PHONE
    assert destination["current_assignee_id"] == ("8" if state == "existing" else "7")
    assert (message["ticket_id"] == 41) is (state != "create")
    assert len(await sql("SELECT id FROM support_tickets")) == (4 if state == "create" else 3)
    assert await repo.get_ticket(43) == other_office
    assert not await repo.list_messages(43)
    assert len(calls) == 1
    assert calls[0]["phone_number_id"] == "90005" and calls[0]["to"] == PHONE


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["existing", "create"])
async def test_conversation_forward_endpoint_preserves_source_office(
    infrastructure, monkeypatch, tmp_path, state,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_offices(infrastructure, monkeypatch, tmp_path, state=state)
    await create_sessions(sql)
    other_office = await repo.get_ticket(43)
    path = f'/api/support/tickets/42/messages/{original["id"]}/forward'
    async with api_client() as client:
        responses = [await client.post(path, json={
            "destination_ticket_id": 43, "client_message_id": "http-source-office",
        }) for _ in range(2)]
        assert all(response.status_code == 200 for response in responses), [
            response.text for response in responses
        ]
        message = responses[0].json()["message"]
        assert responses[1].json()["message"]["id"] == message["id"]
        if state == "existing":
            # Sending this specific forward does not grant access to the
            # source-office conversation assigned to another staff member.
            assert (await client.get("/api/support/tickets/41")).status_code == 403
    assert message["ticket_id"] != 43 and message["send_status"] == "SENT"
    assert (await repo.get_ticket(message["ticket_id"]))["business_phone_number_id"] == "90005"
    assert await repo.get_ticket(43) == other_office
    assert len(calls) == 1 and calls[0]["phone_number_id"] == "90005"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["contact", "conversation"])
@pytest.mark.parametrize("kind", ["text", "image", "video", "audio", "sticker", "document", "location"])
async def test_all_forward_types_send_and_upload_through_source_office(
    infrastructure, monkeypatch, tmp_path, route, kind,
):
    repo, _, _ = infrastructure
    original, calls = await setup_offices(
        infrastructure, monkeypatch, tmp_path, state="existing", kind=kind,
    )
    message = await forward_via(route, original)

    assert message["ticket_id"] == 41 and message["message_type"] == kind
    assert message["send_status"] == "SENT" and len(calls) == 1
    assert calls[0]["phone_number_id"] == "90005" and calls[0]["to"] == PHONE
    assert message["forwarded_from_ticket_id"] == 42
    assert message["forwarded_from_message_id"] == original["id"]
    assert not await repo.list_messages(43)
    upload = service.whatsapp_client.upload_media
    if kind in service.SUPPORT_MEDIA_MESSAGE_TYPES:
        upload.assert_awaited_once()
        assert upload.await_args.kwargs["phone_number_id"] == "90005"
    else:
        upload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["contact", "conversation"])
@pytest.mark.parametrize("same_line_exists", [False, True])
async def test_forward_ignores_unbound_legacy_conversation(
    infrastructure, monkeypatch, tmp_path, route, same_line_exists,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_offices(
        infrastructure, monkeypatch, tmp_path,
        state="existing" if same_line_exists else "create",
    )
    await sql("""UPDATE support_tickets SET business_phone_number_id=NULL,
        active_key=%s WHERE id=43""", (PHONE,))
    legacy = await repo.get_ticket(43)
    # Default behavior remains compatible with inbound legacy adoption.
    assert (await repo.find_active_ticket_by_phone(PHONE, "90005"))["id"] == 43
    strict = await repo.find_active_ticket_by_phone(PHONE, "90005", include_legacy=False)
    assert (strict["id"] if strict else None) == (41 if same_line_exists else None)

    message = await forward_via(route, original)
    assert message["ticket_id"] != 43
    assert (await repo.get_ticket(message["ticket_id"]))["business_phone_number_id"] == "90005"
    assert len(calls) == 1 and calls[0]["phone_number_id"] == "90005"
    assert await repo.get_ticket(43) == legacy


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["contact", "conversation"])
@pytest.mark.parametrize("state", ["create", "reopen"])
@pytest.mark.parametrize("same_request", [False, True])
async def test_concurrent_forwards_share_one_source_office_conversation(
    infrastructure, monkeypatch, tmp_path, route, state, same_request,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_offices(infrastructure, monkeypatch, tmp_path, state=state)
    other_office = await repo.get_ticket(43)
    find_active = repo.find_active_ticket_by_phone
    arrivals = 0
    absent_lookups = asyncio.Event()

    async def synchronized_lookup(phone, line=None, *, include_legacy=True):
        nonlocal arrivals, absent_lookups
        found = await find_active(phone, line, include_legacy=include_legacy)
        if phone == PHONE and line == "90005" and not include_legacy and found is None:
            # Both requests observe the miss before either creates a row. In
            # create cases, also synchronize the repository's two prechecks.
            # Each pair gets its own event; asyncio.Barrier requires Python 3.11.
            round_complete = absent_lookups
            arrivals += 1
            if arrivals == 2:
                arrivals = 0
                absent_lookups = asyncio.Event()
                round_complete.set()
            await asyncio.wait_for(round_complete.wait(), 5)
        return found

    monkeypatch.setattr(repo, "find_active_ticket_by_phone", synchronized_lookup)
    keys = ["concurrent-forward", "concurrent-forward" if same_request else "other-forward"]
    results = await asyncio.wait_for(asyncio.gather(*(
        forward_via(route, original, key=key) for key in keys
    )), 15)

    active = await sql("""SELECT id FROM support_tickets WHERE whatsapp_phone=%s
        AND business_phone_number_id='90005' AND active_key IS NOT NULL""", (PHONE,))
    assert len(active) == 1
    destination_id = active[0]["id"]
    assert {row["ticket_id"] for row in results} == {destination_id}
    assert len({row["id"] for row in results}) == (1 if same_request else 2)
    assert len(await repo.list_messages(destination_id)) == (1 if same_request else 2)
    assert len(calls) == (1 if same_request else 2)
    assert all(call["phone_number_id"] == "90005" for call in calls)
    assert await repo.get_ticket(43) == other_office
    assert not await repo.list_messages(43)
    # A later repeat also observes the original durable attempt, after both
    # competing requests and provider acknowledgements have completed.
    assert (await forward_via(route, original, key=keys[0]))["id"] == results[0]["id"]
    assert len(calls) == (1 if same_request else 2)


@pytest.mark.asyncio
async def test_concurrent_contact_and_conversation_selectors_share_one_attempt(
    infrastructure, monkeypatch, tmp_path,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_offices(infrastructure, monkeypatch, tmp_path, state="existing")
    await sql("UPDATE support_tickets SET current_assignee_id='7' WHERE id=41")
    add_message = repo.add_ticket_message
    arrivals = 0
    both_prepared = asyncio.Event()

    async def synchronize_insert(**kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            both_prepared.set()
        # Both routes have missed their prior-attempt lookup. The actual
        # repository lock must deduplicate their different client namespaces.
        await asyncio.wait_for(both_prepared.wait(), 5)
        return await add_message(**kwargs)

    monkeypatch.setattr(repo, "add_ticket_message", synchronize_insert)
    results = await asyncio.wait_for(asyncio.gather(
        forward_via("contact", original, key="concurrent-selectors"),
        service.support_service.forward_message(
            source_ticket_id=42, source_message_id=original["id"], destination_ticket_id=41,
            agent=AGENT, client_message_id="concurrent-selectors",
        ),
    ), 15)

    assert arrivals == 2
    assert results[0]["id"] == results[1]["id"]
    assert len(await repo.list_messages(41)) == 1
    assert len(calls) == 1 and calls[0]["phone_number_id"] == "90005"


@pytest.mark.asyncio
async def test_forward_namespace_alias_preserves_payload_identity(infrastructure):
    repo, _, _ = infrastructure
    fields = dict(ticket_id=43, sender_type="AGENT", sender_id="7", direction="OUTBOUND",
        body="Original", is_forwarded=True, forwarded_from_ticket_id=42,
        forwarded_from_message_id=1, send_status="UNCERTAIN")
    original, _ = await repo.add_ticket_message(**fields,
        client_message_id="forward:42:1:alias-payload", request_fingerprint="original")

    with pytest.raises(service.SupportConflictError):
        await repo.add_ticket_message(**fields,
            client_message_id="contact-forward:42:1:alias-payload", request_fingerprint="changed")

    assert len(await repo.list_messages(43)) == 1
    assert (await repo.get_message(original["id"]))["send_status"] == "UNCERTAIN"


@pytest.mark.asyncio
@pytest.mark.parametrize("untrusted", ["not_forwarded", "wrong_source", "inbound", "other_sender"])
async def test_forward_alias_never_matches_untrusted_provenance(infrastructure, untrusted):
    repo, _, _ = infrastructure
    fields = dict(ticket_id=43, sender_type="AGENT", sender_id="7", direction="OUTBOUND",
        body="Original", is_forwarded=True, forwarded_from_ticket_id=42,
        forwarded_from_message_id=1, request_fingerprint="same-payload")
    old_fields = {**fields, **{
        "not_forwarded": {"is_forwarded": False},
        "wrong_source": {"forwarded_from_ticket_id": 41},
        "inbound": {"direction": "INBOUND"},
        "other_sender": {"sender_id": "8"},
    }[untrusted]}
    original, _ = await repo.add_ticket_message(**old_fields,
        client_message_id="forward:42:1:alias-provenance")

    forwarded, created = await repo.add_ticket_message(**fields,
        client_message_id="contact-forward:42:1:alias-provenance")

    assert created and forwarded["id"] != original["id"]
    assert forwarded["is_forwarded"] and forwarded["sender_id"] == "7"
    assert len(await repo.list_messages(43)) == 2


@pytest.mark.asyncio
async def test_normal_message_cannot_borrow_forward_alias(infrastructure):
    repo, _, _ = infrastructure
    fields = dict(ticket_id=43, sender_type="AGENT", sender_id="7", direction="OUTBOUND",
        body="Original", forwarded_from_ticket_id=42,
        forwarded_from_message_id=1, request_fingerprint="same-payload")
    forwarded, _ = await repo.add_ticket_message(**fields, is_forwarded=True,
        client_message_id="contact-forward:42:1:not-a-forward")

    normal, created = await repo.add_ticket_message(**fields, is_forwarded=False,
        client_message_id="forward:42:1:not-a-forward")

    assert created and normal["id"] != forwarded["id"]
    assert not normal["is_forwarded"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["contact", "conversation"])
@pytest.mark.parametrize("status", ["SENT", "UNCERTAIN", "DISPATCHING"])
async def test_old_wrong_office_direct_attempt_cannot_be_rerouted_and_resent(
    infrastructure, monkeypatch, tmp_path, route, status,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_offices(infrastructure, monkeypatch, tmp_path)
    old, _ = await repo.add_ticket_message(
        ticket_id=43, sender_type="AGENT", sender_id="7", sender_name="Agent",
        direction="OUTBOUND", body=original["body"], send_status=status,
        client_message_id=f"forward:42:{original['id']}:old-wrong-office",
        is_forwarded=True, forwarded_from_ticket_id=42,
        forwarded_from_message_id=original["id"],
    )
    before = await sql("SELECT * FROM support_tickets ORDER BY id")

    with pytest.raises(service.SupportConflictError):
        await forward_via(route, original, key="old-wrong-office")

    assert not calls
    assert await sql("SELECT * FROM support_tickets ORDER BY id") == before
    assert (await repo.get_message(old["id"]))["send_status"] == status
    assert len(await repo.list_messages(43)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_route", ["contact", "conversation"])
@pytest.mark.parametrize("status", ["SENT", "UNCERTAIN"])
async def test_retry_selecting_source_office_keeps_original_forward(
    infrastructure, monkeypatch, tmp_path, initial_route, status,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_offices(infrastructure, monkeypatch, tmp_path, state="existing")
    await sql("UPDATE support_tickets SET current_assignee_id='7' WHERE id=41")
    uncertain_send = AsyncMock(side_effect=WhatsAppSendError("Synthetic ambiguity", delivery_uncertain=True))
    if status == "UNCERTAIN":
        monkeypatch.setattr(service.whatsapp_client, "send_text", uncertain_send)
    first = await forward_via(initial_route, original, key="changed-selector")
    assert first["ticket_id"] == 41 and first["send_status"] == status

    retried = await service.support_service.forward_message(
        source_ticket_id=42, source_message_id=original["id"], destination_ticket_id=41,
        agent=AGENT, client_message_id="changed-selector",
    )

    assert retried["id"] == first["id"] and retried["send_status"] == status
    assert len(await repo.list_messages(41)) == 1
    assert not await repo.list_messages(43)
    assert len(calls) == (1 if status == "SENT" else 0)
    assert uncertain_send.await_count == (1 if status == "UNCERTAIN" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SENT", "UNCERTAIN", "DISPATCHING"])
async def test_old_wrong_office_attempt_is_fenced_after_selecting_correct_office(
    infrastructure, monkeypatch, tmp_path, status,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_offices(infrastructure, monkeypatch, tmp_path, state="existing")
    await sql("UPDATE support_tickets SET current_assignee_id='7' WHERE id=41")
    old, _ = await repo.add_ticket_message(
        ticket_id=43, sender_type="AGENT", sender_id="7", sender_name="Agent",
        direction="OUTBOUND", body=original["body"], send_status=status,
        client_message_id=f"forward:42:{original['id']}:old-corrected-selector",
        is_forwarded=True, forwarded_from_ticket_id=42,
        forwarded_from_message_id=original["id"],
    )

    with pytest.raises(service.SupportConflictError):
        await service.support_service.forward_message(
            source_ticket_id=42, source_message_id=original["id"], destination_ticket_id=41,
            agent=AGENT, client_message_id="old-corrected-selector",
        )

    assert not calls and not await repo.list_messages(41)
    assert (await repo.get_message(old["id"]))["send_status"] == status
    assert len(await repo.list_messages(43)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["contact", "conversation"])
@pytest.mark.parametrize("boundary", ["disabled", "source_not_visible"])
async def test_forward_cannot_use_unauthorized_source_office(
    infrastructure, monkeypatch, tmp_path, route, boundary,
):
    repo, sql, _ = infrastructure
    original, calls = await setup_offices(infrastructure, monkeypatch, tmp_path)
    if boundary == "disabled":
        service.whatsapp_registry_service.is_number_enabled.return_value = False
    else:
        await sql("UPDATE support_tickets SET current_assignee_id='8' WHERE id=42")
    before = await sql("SELECT * FROM support_tickets ORDER BY id")

    with pytest.raises((service.SupportConflictError, service.SupportForbiddenError)):
        await forward_via(route, original)

    assert not calls
    assert await sql("SELECT * FROM support_tickets ORDER BY id") == before
    assert not await repo.list_messages(43)
