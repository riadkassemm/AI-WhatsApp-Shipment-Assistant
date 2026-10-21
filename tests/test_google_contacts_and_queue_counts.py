from pathlib import Path

import pytest

from app.google_contacts import GoogleContactsService
from app.support_auth import SupportAgent
from app.support_service import SupportService


def test_google_contact_parser_keeps_whatsapp_capable_phone_entries():
    rows = GoogleContactsService._contact_rows(
        {
            "resourceName": "people/c123",
            "etag": "etag-1",
            "names": [{"displayName": "Test Contact"}],
            "emailAddresses": [{"value": "test@example.com"}],
            "phoneNumbers": [
                {"value": "+961 70 123 456", "formattedType": "Mobile"},
                {"value": "+961 70 123 456", "formattedType": "Other"},
            ],
            "metadata": {},
        }
    )
    assert len(rows) == 1
    assert rows[0]["normalized_phone"] == "96170123456"
    assert rows[0]["display_name"] == "Test Contact"
    assert rows[0]["email"] == "test@example.com"


def test_deleted_google_contact_produces_no_rows():
    assert GoogleContactsService._contact_rows(
        {"resourceName": "people/deleted", "metadata": {"deleted": True}, "phoneNumbers": [{"value": "+96170123456"}]}
    ) == []


@pytest.mark.asyncio
async def test_queue_counts_refresh_every_view(monkeypatch):
    service = SupportService()
    expected = {
        "unassigned": 1,
        "mine": 2,
        "chats": 3,
        "watching": 4,
        "active": 5,
        "waiting": 6,
        "history": 7,
    }

    async def fake_list_tickets(*, view, **kwargs):
        return {"items": [], "total": expected[view], "page": 1, "page_size": 1}

    async def fake_contact_count():
        return 8

    monkeypatch.setattr("app.support_service.support_repository.list_tickets", fake_list_tickets)
    monkeypatch.setattr("app.support_service.google_contacts_service.count_contacts", fake_contact_count)

    counts = await service.queue_counts(SupportAgent(id="5", name="Supervisor", role="supervisor"))
    assert counts == {**expected, "contacts": 8}
    # Active is a monitoring view for Supervisors and Admins only.
    counts = await service.queue_counts(SupportAgent(id="5", name="Agent", role="agent"))
    assert "active" not in counts
    assert counts == {**{view: total for view, total in expected.items() if view != "active"}, "contacts": 8}


def test_support_ui_has_contacts_sync_forward_picker_and_global_counts():
    from app.support_ui import _HTML
    source = _HTML
    assert 'data-view="contacts"' in source
    assert "/api/support/google-contacts/connect" in source
    assert "/api/support/google-contacts/sync" in source
    assert "/api/support/queue-counts" in source
    assert "forwardContactsList" in source
    assert "forward-contact" in source
    assert "refreshAllQueueCounts" in source


def test_vcf_parser_extracts_deduplicates_and_decodes_contacts():
    from app.vcf_contacts import parse_vcf

    raw = b"""BEGIN:VCARD\r\nVERSION:2.1\r\nFN:Alice Example\r\nTEL;CELL:+961 70 123 456\r\nEMAIL:alice@example.com\r\nEND:VCARD\r\nBEGIN:VCARD\r\nVERSION:2.1\r\nFN:Duplicate Alice\r\nTEL;CELL:+96170123456\r\nEND:VCARD\r\nBEGIN:VCARD\r\nVERSION:2.1\r\nFN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=D9=85=D8=AD=D9=85=D8=AF\r\nTEL;CELL:+96171111222\r\nEND:VCARD\r\nBEGIN:VCARD\r\nVERSION:2.1\r\nNOTE:no phone\r\nEND:VCARD\r\n"""
    result = parse_vcf(raw)
    assert result.cards_seen == 4
    assert len(result.contacts) == 2
    assert result.duplicate_phones == 1
    assert result.skipped_without_phone == 1
    contacts = {item.normalized_phone: item for item in result.contacts}
    assert contacts["96170123456"].display_name == "Alice Example"
    assert contacts["96170123456"].email == "alice@example.com"
    assert contacts["96171111222"].display_name == "محمد"


def test_support_ui_has_vcf_import_and_source_badge():
    from app.support_ui import _HTML
    source = _HTML
    assert 'id="importVcfContactsBtn"' in source
    assert 'id="vcfContactsInput"' in source
    assert "/api/support/contacts/import-vcf" in source
    assert "contact.source === 'vcf'" in source
