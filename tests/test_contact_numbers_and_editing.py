"""Phone numbers for new chats and editing every kind of contact."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import types
from typing import Any

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
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass
    fake_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules["openai"] = fake_openai

import app.support_service as support_service_module
from app import google_contacts, support_api
from app.customer_directory import ContactConflictError, ContactNotFoundError, CustomerDirectory
from app.linked_device_client import LinkedDeviceWhatsAppClient
from app.phone_numbers import PhoneNumberError, region_for_number, to_whatsapp_digits
from app.support_auth import SupportAgent, require_support_csrf
from app.support_repository import SupportConflictError, SupportNotFoundError
from app.support_service import SupportService, SupportValidationError
from app.support_ui import _HTML

AGENT = SupportAgent(id="7", name="Rana", role="agent")


# --- phone numbers -----------------------------------------------------------

@pytest.mark.parametrize(("raw", "region", "expected"), [
    ("03 512 345", "LB", "9613512345"),          # Lebanese mobile written locally
    ("76612345", "LB", "96176612345"),
    ("0532 123 45 67", "TR", "905321234567"),    # Turkish mobile written locally
    ("+961 3 512 345", "TR", "9613512345"),      # international stays international
    ("0096170123456", None, "96170123456"),
    ("96170123456", "LB", "96170123456"),        # already has the country code
    ("905321234567", "LB", "905321234567"),      # foreign number without '+'
])
def test_numbers_are_turned_into_international_digits(raw, region, expected) -> None:
    assert to_whatsapp_digits(raw, default_region=region) == expected


@pytest.mark.parametrize(("raw", "region"), [("76612345", None), ("035 12345", None), ("12", "LB"), ("", "LB"), ("+961 12", None)])
def test_numbers_without_a_usable_country_code_are_rejected(raw, region) -> None:
    with pytest.raises(PhoneNumberError, match="country code"):
        to_whatsapp_digits(raw, default_region=region)


def test_line_region_comes_from_its_display_number() -> None:
    assert region_for_number("+961 1 234 567") == "LB"
    assert region_for_number("90 212 555 1234") == "TR"
    assert region_for_number("") is None


# --- chat numbers per WhatsApp line ---------------------------------------------

def _line_number(monkeypatch, *, transport: str, lookup=None):
    monkeypatch.setattr(support_service_module, "get_transport", lambda _line: transport)
    if lookup is not None:
        monkeypatch.setattr(support_service_module.linked_device_client, "check_number", lookup)

    def run(raw: str, display: str = "+961 1 234 567") -> str:
        return asyncio.run(SupportService()._whatsapp_number_for_line(raw, line_id="90012", line_display_phone=display))
    return run


def test_local_number_is_read_in_the_lines_country_and_checked_on_whatsapp(monkeypatch) -> None:
    asked = []

    async def lookup(phone, *, phone_number_id):
        asked.append((phone, phone_number_id))
        return {"exists": True, "whatsapp_phone": phone}

    run = _line_number(monkeypatch, transport="linked_device", lookup=lookup)
    assert run("76 612 345") == "96176612345"
    assert asked == [("96176612345", "90012")]
    assert run("0532 123 45 67", display="+90 212 555 1234") == "905321234567"


def test_number_without_whatsapp_is_refused(monkeypatch) -> None:
    async def lookup(phone, *, phone_number_id):
        return {"exists": False, "whatsapp_phone": phone}

    run = _line_number(monkeypatch, transport="linked_device", lookup=lookup)
    with pytest.raises(SupportValidationError, match="not on WhatsApp"):
        run("03 512 345")


def test_unavailable_lookup_does_not_block_and_cloud_lines_skip_it(monkeypatch) -> None:
    async def broken(phone, *, phone_number_id):
        raise RuntimeError("bridge down")

    assert _line_number(monkeypatch, transport="linked_device", lookup=broken)("03 512 345") == "9613512345"

    async def never(phone, *, phone_number_id):
        raise AssertionError("Cloud API lines have no lookup")

    assert _line_number(monkeypatch, transport="cloud", lookup=never)("03 512 345") == "9613512345"
    with pytest.raises(SupportValidationError, match="country code"):
        _line_number(monkeypatch, transport="cloud")("12")


def test_start_chat_refuses_a_number_that_is_not_on_whatsapp_before_creating_anything(monkeypatch) -> None:
    created = []

    async def enabled(_line):
        return True

    async def lines(_agent):
        return [{"phone_number_id": "90012", "display_phone_number": "+961 1 234 567"}]

    async def lookup(phone, *, phone_number_id):
        return {"exists": False, "whatsapp_phone": phone}

    async def create(**kwargs):
        created.append(kwargs)

    registry = support_service_module.whatsapp_registry_service
    monkeypatch.setattr(registry, "is_number_enabled", enabled)
    monkeypatch.setattr(registry, "list_enabled_numbers", lines)
    monkeypatch.setattr(support_service_module, "get_transport", lambda _line: "linked_device")
    monkeypatch.setattr(support_service_module.linked_device_client, "check_number", lookup)
    monkeypatch.setattr(support_service_module.support_repository, "create_staff_chat", create)
    with pytest.raises(SupportValidationError, match="not on WhatsApp"):
        asyncio.run(SupportService().start_chat(agent=AGENT, phone_number="035 12345", business_phone_number_id="90012", body="Hello"))
    assert created == []


def test_linked_device_client_asks_the_bridge_without_sending(monkeypatch) -> None:
    client = LinkedDeviceWhatsAppClient()
    calls = []

    async def request(method, path, *, payload=None, mutating=False, timeout_seconds=None):
        calls.append((method, path, payload, mutating))
        return {"phone": payload["phone"], "exists": True, "whatsapp_phone": "96176612345"}

    monkeypatch.setattr(client, "_phone_number_id", lambda line: line)
    monkeypatch.setattr(client, "_json_request", request)
    result = asyncio.run(client.check_number("96176612345", phone_number_id="90012"))
    assert result == {"exists": True, "whatsapp_phone": "96176612345"}
    assert calls == [("POST", "/v1/lines/90012/contacts/check", {"phone": "96176612345"}, False)]


# --- editing contacts --------------------------------------------------------

class FakeDirectoryDb:
    def __init__(self) -> None:
        self.contacts: dict[int, dict[str, Any]] = {1: {"id": 1, "normalized_phone": "96170000001", "display_name": "Saved", "replaces_phone": None}}
        self.google = {5: "03512345"}
        self.vcf = {9: "96170000009"}
        self.registered = {"96170999999"}
        self.next_id = 2

    def install(self, directory: CustomerDirectory) -> None:
        async def fetchone(query: str, params: tuple):
            sql = " ".join(query.split())
            if sql.startswith("SELECT normalized_phone FROM support_contacts WHERE id"):
                row = self.contacts.get(params[0])
                return {"normalized_phone": row["normalized_phone"]} if row else None
            if "FROM support_google_contacts gc" in sql and "gc.id = %s" in sql:
                return {"normalized_phone": self.google[params[0]]} if params[0] in self.google else None
            if sql.startswith("SELECT normalized_phone FROM support_vcf_contacts"):
                return {"normalized_phone": self.vcf[params[0]]} if params[0] in self.vcf else None
            if sql.startswith("SELECT id FROM support_contacts WHERE normalized_phone = %s AND id <> %s"):
                return next(({"id": c["id"]} for c in self.contacts.values() if c["normalized_phone"] == params[0] and c["id"] != params[1]), None)
            if sql.startswith("SELECT id FROM support_contacts WHERE normalized_phone = %s"):
                return next(({"id": c["id"]} for c in self.contacts.values() if c["normalized_phone"] == params[0]), None)
            raise AssertionError(sql)

        async def execute(query: str, params: tuple) -> int:
            sql = " ".join(query.split())
            if sql.startswith("UPDATE support_contacts"):
                name, phone, _staff, contact_id = params
                self.contacts[contact_id].update(display_name=name, normalized_phone=phone)
                return 1
            if sql.startswith("INSERT INTO support_contacts"):
                phone, name, replaces, _a, _b = params
                existing = next((c for c in self.contacts.values() if c["normalized_phone"] == phone), None)
                if existing:
                    existing.update(display_name=name, replaces_phone=replaces or existing["replaces_phone"])
                else:
                    self.contacts[self.next_id] = {"id": self.next_id, "normalized_phone": phone, "display_name": name, "replaces_phone": replaces}
                    self.next_id += 1
                return 1
            raise AssertionError(sql)

        async def registered(phone: str):
            return {"id": 1} if phone in self.registered else None

        directory._fetchone = fetchone  # type: ignore[method-assign]
        directory._execute = execute  # type: ignore[method-assign]
        directory._registered_user = registered  # type: ignore[method-assign]


def _directory() -> tuple[CustomerDirectory, FakeDirectoryDb]:
    directory = CustomerDirectory()
    db = FakeDirectoryDb()
    db.install(directory)
    return directory, db


def test_saved_contact_is_edited_in_place() -> None:
    directory, db = _directory()
    result = asyncio.run(directory.edit_contact(contact_key="manual:1", display_name="  Maya   Khoury ", phone="96170000002", staff_id="7"))
    assert result == {"contact_key": "manual:1", "source": "manual", "display_name": "Maya Khoury", "normalized_phone": "96170000002"}
    assert db.contacts[1]["normalized_phone"] == "96170000002"


def test_google_contact_edit_replaces_its_outdated_entry_without_touching_google() -> None:
    directory, db = _directory()
    result = asyncio.run(directory.edit_contact(contact_key="google:5", display_name="Ali", phone="9613512345", staff_id="7"))
    saved = db.contacts[2]
    assert result["contact_key"] == "manual:2"
    assert saved == {"id": 2, "normalized_phone": "9613512345", "display_name": "Ali", "replaces_phone": "03512345"}
    assert db.google == {5: "03512345"}, "Google data is never changed"


def test_vcf_contact_name_only_edit_overrides_by_number() -> None:
    directory, db = _directory()
    asyncio.run(directory.edit_contact(contact_key="vcf:9", display_name="New name", phone="96170000009", staff_id="7"))
    assert db.contacts[2]["replaces_phone"] is None
    assert db.vcf == {9: "96170000009"}


@pytest.mark.parametrize(("key", "phone", "error"), [
    ("manual:404", "96170000003", ContactNotFoundError),
    ("google:404", "96170000003", ContactNotFoundError),
    ("bogus:1", "96170000003", ContactNotFoundError),
])
def test_contact_edit_refuses_missing_contacts_and_registered_numbers(key, phone, error) -> None:
    directory, _db = _directory()
    with pytest.raises(error):
        asyncio.run(directory.edit_contact(contact_key=key, display_name="X", phone=phone, staff_id="7"))


def test_saved_contact_cannot_take_another_saved_contacts_number() -> None:
    directory, db = _directory()
    db.contacts[2] = {"id": 2, "normalized_phone": "96170000002", "display_name": "Other", "replaces_phone": None}
    with pytest.raises(ContactConflictError, match="Another saved contact"):
        asyncio.run(directory.edit_contact(contact_key="manual:1", display_name="X", phone="96170000002", staff_id="7"))


def test_google_or_vcf_contacts_name_the_customer_and_are_editable() -> None:
    directory = CustomerDirectory()

    async def none(_phone):
        return None

    async def google(_phone):
        return {"source": "google", "id": 5, "display_name": "Ali from Google"}

    directory._registered_user = none  # type: ignore[method-assign]
    directory._saved_contact = none  # type: ignore[method-assign]
    directory._directory_contact = google  # type: ignore[method-assign]
    identity = asyncio.run(directory.resolve("9613512345"))
    assert (identity.source, identity.name, identity.contact_key) == ("google_contact", "Ali from Google", "google:5")

    async def broken(_phone):
        raise RuntimeError("table missing")

    directory._directory_contact = broken  # type: ignore[method-assign]
    assert asyncio.run(directory.resolve("9613512345")).source == "guest"


def test_service_requires_a_country_code_and_maps_errors(monkeypatch) -> None:
    edits = []

    async def edit_contact(**kwargs):
        edits.append(kwargs)
        if kwargs["contact_key"] == "manual:404":
            raise ContactNotFoundError("Contact not found.")
        if kwargs["contact_key"] == "manual:2":
            raise ContactConflictError("Another saved contact already uses this number.")
        return {"contact_key": "manual:2", "source": "manual", "display_name": kwargs["display_name"], "normalized_phone": kwargs["phone"]}

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(support_service_module.customer_directory, "edit_contact", edit_contact)
    monkeypatch.setattr(support_service_module.support_repository, "update_customer_name_for_phone", nothing)
    monkeypatch.setattr(support_service_module, "publish_support_event", nothing)
    service = SupportService()
    result = asyncio.run(service.edit_contact(agent=AGENT, contact_key="google:5", display_name="Ali", phone="+961 3 512 345"))
    assert result["normalized_phone"] == "9613512345"
    with pytest.raises(SupportValidationError, match="country code"):
        asyncio.run(service.edit_contact(agent=AGENT, contact_key="google:5", display_name="Ali", phone="03 512 345"))
    with pytest.raises(SupportNotFoundError):
        asyncio.run(service.edit_contact(agent=AGENT, contact_key="manual:404", display_name="Ali", phone="+9613512345"))
    with pytest.raises(SupportConflictError):
        asyncio.run(service.edit_contact(agent=AGENT, contact_key="manual:2", display_name="Ali", phone="+9613512345"))
    with pytest.raises(SupportValidationError, match="name"):
        asyncio.run(service.edit_contact(agent=AGENT, contact_key="google:5", display_name="  ", phone="+9613512345"))


def test_edit_route_needs_csrf_and_list_hides_replaced_entries() -> None:
    route = next(r for r in support_api.router.routes if getattr(r, "path", "") == "/api/support/contacts" and "PUT" in r.methods)
    assert require_support_csrf in {dep.call for dep in route.dependant.dependencies}
    import inspect
    source = inspect.getsource(google_contacts.GoogleContactsService.list_contacts) + inspect.getsource(google_contacts.GoogleContactsService.count_contacts)
    assert source.count("replaces_phone = gc.normalized_phone") == 2
    assert source.count("replaces_phone = vc.normalized_phone") == 2


# --- dashboard ---------------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_every_contact_row_can_be_edited_and_saves_through_the_api() -> None:
    row_code = _HTML[_HTML.index("function createContactRow("): _HTML.index("function createTicketRow(")]
    keys = _HTML[_HTML.index("function handleQueueKeydown("): _HTML.index("function setMobileTicketOpen(")]
    modal = _HTML[_HTML.index("const CONTACT_SOURCE_NOTES = {"): _HTML.index("function renderStartChatLinePreview(")]
    script = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.dataset = {}; this.style = {}; this.value = ''; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  focus() {}
  closest() { return this; }
}
const elements = {};
const $ = id => (elements[id] ||= new Element('div'));
const document = {createElement: tag => new Element(tag), createTextNode: value => ({textContent: String(value)})};
const text = (element, value) => { element.textContent = value ?? ''; };
const createIcon = () => new Element('svg');
const initials = () => 'AB';
const calls = [];
const openStartChatModal = contact => calls.push(['chat', contact.contact_key]);
const openDialog = (id, focus) => calls.push(['open', id, focus]);
const closeDialog = id => calls.push(['close', id]);
const setButtonBusy = () => {};
const notify = message => calls.push(['notify', message]);
const loadQueue = async () => calls.push(['reload']);
const api = async (path, options) => { calls.push(['api', path, options.method, JSON.parse(options.body)]); return {}; };
const state = {authenticated: true, activeModal: null, view: 'contacts', contactEditTarget: null, contactEditBusy: false};
""" + row_code + keys + modal + r"""
(async () => {
  for (const [key, label] of [['manual:1', 'dashboard'], ['google:5', 'Google Contacts is not changed'], ['vcf:9', 'VCF']]) {
    const item = createContactRow({contact_key: key, source: key.split(':')[0], display_name: 'Ali', normalized_phone: key === 'google:5' ? '03512345' : '96170000009'});
    const [row, edit] = item.children;
    assert.equal(item.className, 'contact-item');
    assert.equal(edit.attributes['aria-label'], 'Edit contact Ali');
    calls.length = 0;
    edit.onclick({stopPropagation() {}});
    assert.deepEqual(calls[0], ['open', 'contactEditModal', 'contactEditName']);
    assert.match($('contactEditNote').textContent, new RegExp(label));
    state.activeModal = null;
  }
  assert.equal($('contactEditPhone').value, '+96170000009');

  // A local number from Google is shown without a fake '+' so staff add the country code.
  const google = createContactRow({contact_key: 'google:5', display_name: 'Ali', normalized_phone: '03512345'});
  const googleCopy = google.children[0].children[1];
  assert.equal(googleCopy.children[1].textContent, '03512345', 'local numbers are not shown with a fake +');
  google.children[0].onclick();
  assert.deepEqual(calls.at(-1), ['chat', 'google:5'], 'clicking the row still starts a chat');
  calls.length = 0;
  let prevented = false;
  handleQueueKeydown({key: 'e', target: google.children[0], preventDefault() { prevented = true; }});
  assert.ok(prevented);
  assert.equal($('contactEditPhone').value, '03512345');

  $('contactEditName').value = 'Ali Haddad';
  $('contactEditPhone').value = '+961 3 512 345';
  await saveContactEdit({preventDefault() {}});
  assert.deepEqual(calls.filter(call => call[0] !== 'open'), [
    ['api', '/api/support/contacts', 'PUT', {contact_key: 'google:5', display_name: 'Ali Haddad', phone: '+961 3 512 345'}],
    ['close', 'contactEditModal'], ['notify', 'Contact saved.'], ['reload'],
  ]);

  state.contactEditTarget = {contact_key: 'manual:1'};
  $('contactEditPhone').value = '';
  calls.length = 0;
  await saveContactEdit({preventDefault() {}});
  assert.equal($('contactEditError').textContent, 'Enter the WhatsApp number.');
  assert.equal(calls.length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_ticket_panel_edits_google_and_vcf_contacts_too() -> None:
    assert "identity.contact_key || identity.registered_user ? 'Edit contact' : 'Save contact'" in _HTML
    assert "identity?.source === 'google_contact' ? 'Google contact'" in _HTML


# --- registered customers ---------------------------------------------------------

def test_registered_customer_can_get_a_dashboard_name_without_changing_the_account() -> None:
    directory, db = _directory()
    db.vcf[10] = "96170999999"  # this number belongs to a registered customer
    result = asyncio.run(directory.edit_contact(contact_key="vcf:10", display_name="Rana (VIP)", phone="96170999999", staff_id="7"))
    assert result["display_name"] == "Rana (VIP)"

    async def user(_phone):
        return {"id": 900, "userid": "rana01", "name": "Rana Khoury"}

    async def contact(_phone):
        return {"id": 12, "display_name": "Rana (VIP)"}

    async def nothing(_phone):
        return None

    resolver = CustomerDirectory()
    resolver._registered_user = user  # type: ignore[method-assign]
    resolver._saved_contact = contact  # type: ignore[method-assign]
    resolver._directory_contact = nothing  # type: ignore[method-assign]
    identity = asyncio.run(resolver.resolve("96170999999"))
    assert identity.source == "registered_user"
    assert (identity.name, identity.account_name, identity.contact_key) == ("Rana (VIP)", "Rana Khoury", "manual:12")
    assert (identity.user_id, identity.userid) == ("900", "rana01"), "the account link is kept"

    resolver._saved_contact = nothing  # type: ignore[method-assign]
    plain = asyncio.run(resolver.resolve("96170999999"))
    assert (plain.name, plain.account_name, plain.contact_key) == ("Rana Khoury", "Rana Khoury", None)


def test_ticket_panel_lets_staff_rename_registered_customers() -> None:
    assert "if (identity && permissions && permissions.can_operate) {" in _HTML
    assert "identity.contact_key || identity.registered_user ? 'Edit contact' : 'Save contact'" in _HTML
    assert "The customer account" in _HTML and "is not changed." in _HTML
    assert "['Account name', identity?.registered_user" in _HTML


@pytest.mark.parametrize(("verified_id", "authenticated_name", "dashboard_name", "expected"), [
    ("900", "Rana Khoury", "Rana (VIP)", "Rana (VIP)"),     # same customer: the dashboard name wins
    ("555", "Other Account", "Rana (VIP)", "Other Account"),  # signed in to another account from this phone
    (None, None, "Rana (VIP)", "Rana (VIP)"),
    ("900", "Rana Khoury", None, "Rana Khoury"),             # never renamed
])
def test_new_tickets_use_the_dashboard_name_for_the_same_registered_customer(monkeypatch, verified_id, authenticated_name, dashboard_name, expected) -> None:
    from app.conversation_store import Conversation
    from app.customer_directory import CustomerIdentity
    from app.support_models import HandoffRequest

    class Stop(Exception):
        pass

    created = {}

    async def resolve(_phone):
        return CustomerIdentity(
            source="registered_user", phone="96170999999", name=dashboard_name or "Rana Khoury",
            user_id="900", userid="rana01", account_name="Rana Khoury",
            contact_key="manual:12" if dashboard_name else None,
        )

    async def create_ticket(**kwargs):
        created.update(kwargs)
        raise Stop()

    monkeypatch.setattr(support_service_module.customer_directory, "resolve", resolve)
    monkeypatch.setattr(support_service_module.support_repository, "create_ticket", create_ticket)
    monkeypatch.setattr(support_service_module, "get_transport", lambda _line: "cloud")
    conversation = Conversation(phone_number="96170999999", authenticated_name=authenticated_name, verified_customer_id=verified_id)
    with pytest.raises(Stop):
        asyncio.run(SupportService().finalize_handoff(
            conversation=conversation, handoff=HandoffRequest(reason="Help"),
            source_whatsapp_message_id=None, source_message_text=None,
        ))
    assert created["customer_name"] == expected
