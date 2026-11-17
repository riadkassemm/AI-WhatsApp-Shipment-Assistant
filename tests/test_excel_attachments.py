"""Excel workbooks (.xlsx/.xls) travel as WhatsApp documents, like PDFs."""
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
fake_aiomysql.IntegrityError = getattr(fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {}))
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
from app import main, support_api
from app.client_chat_auth import extract_credentials
from app.config import settings
from app.support_auth import SupportAgent
from app.support_media import (
    XLS_MIME_TYPE,
    XLSX_MIME_TYPE,
    SupportMediaError,
    SupportMediaStore,
    document_label,
    has_spreadsheet_signature,
    media_extension,
    media_kind_for_mime,
    normalize_mime_type,
    spreadsheet_mime_for_file,
)
from app.support_service import SupportValidationError
from app.support_ui import _HTML
from app.whatsapp_client import DownloadedWhatsAppMedia, IncomingWhatsAppMessage

AGENT = SupportAgent(id="7", name="Rana", role="agent")
XLSX_BYTES = b"PK\x03\x04" + b"\x00" * 60
XLS_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 60


def test_excel_types_are_documents_with_their_own_extensions() -> None:
    assert media_kind_for_mime(XLSX_MIME_TYPE) == "document"
    assert media_kind_for_mime(XLS_MIME_TYPE) == "document"
    assert media_extension(XLSX_MIME_TYPE) == ".xlsx"
    assert media_extension(XLS_MIME_TYPE) == ".xls"
    assert normalize_mime_type("application/x-excel") == XLS_MIME_TYPE
    # Macro-enabled workbooks stay unsupported.
    assert media_kind_for_mime("application/vnd.ms-excel.sheet.macroenabled.12") is None
    assert document_label(XLSX_MIME_TYPE) == "Excel spreadsheet"
    assert document_label("application/pdf") == "PDF document"


def test_generic_labels_are_trusted_only_with_extension_and_signature() -> None:
    assert has_spreadsheet_signature(XLSX_BYTES) and has_spreadsheet_signature(XLS_BYTES)
    assert not has_spreadsheet_signature(b"%PDF-1.7")
    assert spreadsheet_mime_for_file("application/octet-stream", "Rates.XLSX", XLSX_BYTES) == XLSX_MIME_TYPE
    assert spreadsheet_mime_for_file("application/zip", "rates.xlsx", XLSX_BYTES) == XLSX_MIME_TYPE
    assert spreadsheet_mime_for_file(None, "old.xls", XLS_BYTES) == XLS_MIME_TYPE
    # A password-protected .xlsx is an OLE2 container.
    assert spreadsheet_mime_for_file("application/octet-stream", "locked.xlsx", XLS_BYTES) == XLSX_MIME_TYPE
    assert spreadsheet_mime_for_file("application/octet-stream", "rates.xlsx", b"<html>") is None
    assert spreadsheet_mime_for_file("application/octet-stream", "rates.zip", XLSX_BYTES) is None
    assert spreadsheet_mime_for_file("text/csv", "rates.xlsx", XLSX_BYTES) is None


def test_customer_excel_without_a_name_is_labelled_as_a_spreadsheet() -> None:
    def body(mime: str) -> str:
        incoming = IncomingWhatsAppMessage(
            "96170123456", "wamid.x", "document", None, "1", media_id="media-x", media_mime_type=mime,
        )
        return main._human_inbound_body(incoming, extract_credentials(""))

    assert body(XLSX_MIME_TYPE) == "Excel spreadsheet"
    assert body("application/pdf") == "PDF document"


def _stub_agent_send(monkeypatch) -> dict:
    ticket = {"id": 77, "status": "ASSIGNED", "active_key": "business:96170123456", "current_assignee_id": "7",
              "whatsapp_phone": "96170123456", "business_phone_number_id": "business"}
    sent: dict = {}

    async def get_ticket(_ticket_id):
        return dict(ticket)

    async def no_existing(_client_id, **_scope):
        return None

    async def add_message(**kwargs):
        sent["stored"] = kwargs
        return {"id": 902, "ticket_id": 77, "transport_client_id": "unit-wire-id", **kwargs}, True

    real_save = support_service_module.support_media_store.save

    def save(**kwargs):
        sent["saved"] = kwargs
        return real_save(**kwargs)

    async def upload_media(**kwargs):
        sent["upload"] = kwargs
        return "meta-media-id"

    async def send_media(**kwargs):
        sent["send"] = kwargs
        return {"messages": [{"id": "wamid.out.xlsx"}]}

    async def update_send_state(message_id, **kwargs):
        sent["send_state"] = kwargs

    async def nothing(*_args, **_kwargs):
        return None

    async def get_message(message_id):
        return {"id": message_id, "ticket_id": 77, "message_type": "document", "send_status": "SENT"}

    repo = support_service_module.support_repository
    monkeypatch.setattr(repo, "get_ticket", get_ticket)
    monkeypatch.setattr(repo, "get_message_by_client_id", no_existing)
    monkeypatch.setattr(repo, "add_ticket_message", add_message)
    monkeypatch.setattr(repo, "update_message_send_state", update_send_state)
    monkeypatch.setattr(repo, "mark_agent_reply_started", nothing)
    monkeypatch.setattr(repo, "get_message", get_message)
    monkeypatch.setattr(support_service_module.support_media_store, "save", save)
    monkeypatch.setattr(support_service_module.whatsapp_client, "upload_media", upload_media)
    monkeypatch.setattr(support_service_module.whatsapp_client, "send_media", send_media)
    return sent


@pytest.mark.parametrize(("content", "mime", "filename", "expected_name"), [
    (XLSX_BYTES, XLSX_MIME_TYPE, "Rates 2026.xlsx", "Rates 2026.xlsx"),
    (XLSX_BYTES, XLSX_MIME_TYPE, "Rates", "Rates.xlsx"),
    (XLS_BYTES, XLS_MIME_TYPE, None, "document.xls"),
])
def test_staff_can_send_an_excel_workbook(monkeypatch, outbound_unit, content, mime, filename, expected_name) -> None:
    sent = _stub_agent_send(monkeypatch)
    result = asyncio.run(support_service_module.support_service.send_agent_media(
        77, agent=AGENT, media_type="document", content=content, content_type=mime,
        filename=filename, caption="", client_message_id="client-xlsx-" + expected_name,
    ))
    assert result["send_status"] == "SENT"
    assert sent["stored"]["message_type"] == "document"
    assert sent["stored"]["body"] == expected_name
    assert sent["saved"]["mime_type"] == mime
    assert sent["upload"]["mime_type"] == mime
    assert sent["upload"]["filename"] == expected_name
    assert sent["send"]["media_type"] == "document"
    assert sent["send"]["filename"] == expected_name


@pytest.mark.parametrize(("content", "mime", "message"), [
    (b"<table></table>", XLSX_MIME_TYPE, "not a valid Excel workbook"),
    (b"", XLSX_MIME_TYPE, "Excel spreadsheet is empty"),
    (b"a,b\n1,2\n", "text/csv", "PDF documents and Excel spreadsheets"),
])
def test_invalid_workbooks_are_rejected_before_anything_is_stored(monkeypatch, content, mime, message) -> None:
    sent = _stub_agent_send(monkeypatch)
    with pytest.raises(SupportValidationError, match=message):
        asyncio.run(support_service_module.support_service.send_agent_media(
            77, agent=AGENT, media_type="document", content=content, content_type=mime,
            filename="rates.xlsx", caption=None, client_message_id="client-xlsx-bad",
        ))
    assert "stored" not in sent and "send" not in sent


@pytest.fixture
def capture(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "support_media_directory", str(tmp_path / "media"))
    store = SupportMediaStore()
    monkeypatch.setattr(support_service_module, "support_media_store", store)
    state: dict[str, Any] = {"result": None}

    async def nothing(*_args, **_kwargs):
        return None

    async def download(_media_id, **_kwargs):
        return state["result"]

    monkeypatch.setattr(support_service_module.support_repository, "set_message_media_reference", nothing)
    monkeypatch.setattr(support_service_module.whatsapp_client, "download_media", download)
    monkeypatch.setattr(support_service_module, "publish_support_event", nothing)

    def run(*, expected_mime: str | None, filename: str | None, message_id: int = 41):
        return asyncio.run(support_service_module.SupportService().capture_inbound_media(
            ticket={"id": 7, "business_phone_number_id": "90052"},
            message={"id": message_id, "ticket_id": 7, "message_type": "document", "provider_media_id": "m"},
            media_id="m", message_type="document", expected_mime_type=expected_mime, filename=filename,
        ))
    return state, run


def test_customer_workbook_is_stored_under_its_name(capture) -> None:
    state, run = capture
    state["result"] = DownloadedWhatsAppMedia(content=XLSX_BYTES, mime_type=XLSX_MIME_TYPE, file_size=64, sha256=None)
    stored = run(expected_mime=XLSX_MIME_TYPE, filename="Invoice.xlsx")
    assert stored.mime_type == XLSX_MIME_TYPE and stored.filename == "Invoice.xlsx"
    assert stored.path.name == "media.xlsx"

    state["result"] = DownloadedWhatsAppMedia(content=XLS_BYTES, mime_type=XLS_MIME_TYPE, file_size=64, sha256=None)
    assert run(expected_mime=XLS_MIME_TYPE, filename=None, message_id=42).filename == "customer-document.xls"


def test_generically_labelled_customer_workbook_is_recognised(capture) -> None:
    state, run = capture
    state["result"] = DownloadedWhatsAppMedia(
        content=XLSX_BYTES, mime_type="application/octet-stream", file_size=64, sha256=None,
    )
    stored = run(expected_mime="application/octet-stream", filename="Rates.xlsx")
    assert stored.mime_type == XLSX_MIME_TYPE and stored.path.name == "media.xlsx"


def test_generic_file_that_is_not_a_workbook_stays_unsupported(capture) -> None:
    state, run = capture
    state["result"] = DownloadedWhatsAppMedia(
        content=b"MZ\x90\x00", mime_type="application/octet-stream", file_size=4, sha256=None,
    )
    with pytest.raises(SupportMediaError, match="unsupported media format"):
        run(expected_mime="application/octet-stream", filename="Rates.xlsx")


def _media_response(monkeypatch, tmp_path, mime: str, filename: str):
    path = tmp_path / "media.bin"
    path.write_bytes(b"x")
    asset = types.SimpleNamespace(path=path, mime_type=mime, filename=filename)

    async def get_ticket_media(*_args, **_kwargs):
        return asset

    monkeypatch.setattr(support_api.support_service, "get_ticket_media", get_ticket_media)
    return asyncio.run(support_api.ticket_message_media(1, 2, None, AGENT))


def test_workbooks_download_under_their_name_and_pdfs_stay_inline(monkeypatch, tmp_path) -> None:
    response = _media_response(monkeypatch, tmp_path, XLSX_MIME_TYPE, "Rates 2026.xlsx")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;") and "Rates%202026.xlsx" in disposition
    assert response.headers["x-content-type-options"] == "nosniff"

    response = _media_response(monkeypatch, tmp_path, "application/pdf", "guide.pdf")
    assert "content-disposition" not in response.headers


def test_dashboard_accepts_and_downloads_workbooks() -> None:
    assert ".xlsx,.xls," in _HTML and XLSX_MIME_TYPE in _HTML
    assert "Attach image, video, sticker, PDF, or Excel file" in _HTML
    assert "view.download = m.media_filename || 'spreadsheet';" in _HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_attach_picker_stages_workbooks_as_documents() -> None:
    picker = _HTML[_HTML.index("const VIDEO_MAX_BYTES = "): _HTML.index("async function sendMediaMessage(")]
    script = r"""
const assert = require('node:assert/strict');
const state = {ticketId: 42, composerEnabled: true};
const input = {files: [], value: ''};
const $ = id => ({imageInput: input, message: {focus() {}}})[id];
const calls = [];
const showError = message => calls.push(['error', message]);
const setPendingMedia = (kind, file) => calls.push(['pending', kind, file.name, file.type]);
const setAppStatus = () => {};
const formatBytes = bytes => bytes + ' B';
""" + picker.replace("'__DOCUMENT_MAX_BYTES__'", "'1000'") + r"""
const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
assert.equal(attachComposerFile(new File(['PK'], 'rates.xlsx', {type: XLSX})), true);
assert.deepEqual(calls.pop(), ['pending', 'document', 'rates.xlsx', XLSX]);
assert.equal(attachComposerFile(new File(['x'], 'old.XLS', {type: 'application/octet-stream'})), true);
assert.deepEqual(calls.pop(), ['pending', 'document', 'old.XLS', 'application/vnd.ms-excel']);
assert.equal(attachComposerFile(new File(['PK'], 'rates.xlsx', {type: 'application/zip'})), true);
assert.deepEqual(calls.pop(), ['pending', 'document', 'rates.xlsx', XLSX]);
// A ZIP label is reinterpreted only for .xlsx names.
assert.equal(attachComposerFile(new File(['PK'], 'photo.jpg', {type: 'application/zip'})), false);
assert.match(calls.pop()[1], /Excel \(XLSX\/XLS\)/);
assert.equal(attachComposerFile(new File(['x'.repeat(1001)], 'big.xlsx', {type: XLSX})), false);
assert.match(calls.pop()[1], /exceeds the 1000 B limit/);
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
