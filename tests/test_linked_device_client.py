from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings, configured_linked_device_lines, linked_device_data_directory, removed_linked_device_line_ids, settings
from app.linked_device_client import LinkedDeviceWhatsAppClient
from app.whatsapp_client import (
    RoutedWhatsAppClient, WhatsAppClient, WhatsAppMediaError, WhatsAppSendError,
    create_whatsapp_client, get_transport, is_linked_device_message,
)


MESSAGE = "ld:10001:ABC123"
MEDIA = "ldmedia:10001:ce82b74b-1763-494a-9b79-3f99b994c354"


@pytest.mark.parametrize("first_result", ["success", "failure", "cancelled"])
def test_attachment_upload_slot_precedes_allocation_and_releases_on_every_outcome(monkeypatch, first_result):
    from app import support_delivery

    async def run():
        monkeypatch.setattr(support_delivery, "_media_upload_slot", asyncio.Semaphore(1))
        entered, release, second_claimed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        reads, sends, interrupted, finished = [], [], [], {}
        active = maximum_active = 0

        class Repository:
            async def claim_delivery(self, message_id, **_kwargs):
                if message_id == 2:
                    second_claimed.set()
                return str(message_id)

            async def begin_delivery(self, *_args):
                return True

            async def interrupt_delivery(self, message_id, _token):
                interrupted.append(message_id)

            async def finish_delivery(self, message_id, _token, **kwargs):
                finished[message_id] = kwargs

            async def get_message(self, message_id):
                return {"id": message_id}

        def asset(_ticket_id, message_id):
            def read():
                reads.append(message_id)
                return str(message_id).encode()
            return SimpleNamespace(path=SimpleNamespace(read_bytes=read), mime_type="video/mp4", filename="synthetic.mp4")

        async def upload_media(*, content, **_kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                if content == b"1":
                    entered.set()
                    await release.wait()
                    if first_result == "failure":
                        raise WhatsAppMediaError("Synthetic upload failure", retryable=True)
                return content.decode()
            finally:
                active -= 1

        async def send_media(*, media_id, client_message_id, **_kwargs):
            sends.append((media_id, client_message_id))
            return {"messages": [{"id": "provider-" + media_id}]}

        repository = Repository()
        transport = SimpleNamespace(upload_media=upload_media, send_media=send_media)
        ticket = {"id": 42, "whatsapp_phone": "96170123456", "business_phone_number_id": "10001"}
        def deliver(message_id):
            message = {"id": message_id, "message_type": "video", "body": "Video",
                       "transport_client_id": "stable-" + str(message_id)}
            return support_delivery.deliver(repository, transport, SimpleNamespace(get=asset), ticket, message)

        first = asyncio.create_task(deliver(1))
        await entered.wait()
        second = asyncio.create_task(deliver(2))
        await second_claimed.wait()
        assert reads == [1], "another claimed reply must wait before allocating attachment bytes"
        assert sends == [], "uploading cannot bypass the existing dispatch claim"
        if first_result == "cancelled":
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            release.set()
            await first
        await second
        assert reads == [1, 2] and maximum_active == 1 and active == 0
        assert finished[2]["status"] == "SENT"
        assert sends == ([("1", "stable-1"), ("2", "stable-2")] if first_result == "success" else [("2", "stable-2")])
        if first_result == "failure":
            assert finished[1]["status"] == "FAILED"
        if first_result == "cancelled":
            assert interrupted == [1] and 1 not in finished

    asyncio.run(run())


def test_delivery_pressure_preflight_prevents_attachment_reads_uploads_and_dispatch(bridge):
    from app.support_delivery import deliver

    requests = bridge(lambda request: httpx.Response(200, json={"lines": [], "storage": {
        "level": "critical", "issues": [{"code": "DISK_SPACE_LOW", "level": "critical", "line_id": None}],
    }}))
    finished = []

    class Repository:
        async def claim_delivery(self, *_args, **_kwargs):
            return "claim"

        async def begin_delivery(self, *_args):
            pytest.fail("storage pressure must be rejected before dispatch admission")

        async def finish_delivery(self, *_args, **kwargs):
            finished.append(kwargs)

        async def get_message(self, message_id):
            return {"id": message_id}

    def forbidden_read():
        pytest.fail("storage pressure must be rejected before allocating attachment bytes")

    asset = SimpleNamespace(path=SimpleNamespace(read_bytes=forbidden_read), mime_type="application/pdf", filename="test.pdf")
    media_store = SimpleNamespace(get=lambda *_args: asset)
    ticket = {"id": 1, "whatsapp_phone": "96170123456", "business_phone_number_id": "10001"}
    message = {"id": 2, "message_type": "document", "body": "Document", "transport_client_id": "stable-send-key"}

    async def run():
        for _ in range(3):
            await deliver(Repository(), RoutedWhatsAppClient(), media_store, ticket, message, retry=True)

    asyncio.run(run())
    assert len(requests) == 3
    assert all(request.method == "GET" and request.url.path == "/v1/status" for request in requests)
    assert all(result["status"] == "FAILED" for result in finished)


@pytest.mark.parametrize("storage", [None, {"level": "warning", "issues": []}, {
    "level": "critical", "issues": [{"code": "JOURNAL_LIMIT", "level": "critical", "line_id": "10002"}],
}])
def test_media_preflight_preserves_rollback_and_other_line_availability(bridge, storage):
    payload = {"lines": []}
    if storage is not None:
        payload["storage"] = storage
    requests = bridge(lambda _: httpx.Response(200, json=payload))
    asyncio.run(RoutedWhatsAppClient().check_media_storage(phone_number_id="10001"))
    assert len(requests) == 1


@pytest.mark.parametrize("storage", [{"level": [], "issues": []}, {"level": "ok", "issues": None}, []])
def test_media_preflight_fails_closed_on_malformed_storage_health(bridge, storage):
    bridge(lambda _: httpx.Response(200, json={"storage": storage}))
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().check_media_storage(phone_number_id="10001"))
    assert caught.value.bridge_code == "STORAGE_PRESSURE"
    assert not caught.value.delivery_uncertain


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "whatsapp_transport", "cloud")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "70001")
    monkeypatch.setattr(settings, "whatsapp_linked_device_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_linked_device_default_line_id", "10001")
    monkeypatch.setattr(settings, "whatsapp_linked_device_url", "http://127.0.0.1:3001")
    monkeypatch.setattr(settings, "whatsapp_linked_device_secret", "s" * 40)
    monkeypatch.setattr(settings, "whatsapp_linked_device_lines", {"10001": "+96170111111", "10002": "+96170222222"})
    monkeypatch.setattr(settings, "whatsapp_linked_device_data_directory", str(tmp_path / "bridge-data"))
    real_client = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def install(handler):
        def record(request):
            requests.append(request)
            return handler(request)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(record), **kwargs))
        return requests
    return install


def test_hybrid_transport_keeps_cloud_and_routes_two_test_lines(bridge):
    def respond(request):
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"messages": [{"id": "wamid.cloud"}]})
        line = request.url.path.split("/")[3]
        assert request.headers["authorization"] == "Bearer " + "s" * 40
        return httpx.Response(200, json={"messages": [{"id": f"ld:{line}:OUTBOUND"}]})
    requests = bridge(respond)

    async def run():
        routed = RoutedWhatsAppClient()
        assert get_transport() == "cloud"
        assert get_transport("70001") == "cloud"
        assert get_transport("10002") == "linked_device"
        assert isinstance(create_whatsapp_client(), WhatsAppClient)
        await routed.send_text("96171111111", "Production")
        await routed.send_text("96171111111", "Test A", phone_number_id="10001")
        await routed.send_location("96171111111", latitude=33.89, longitude=35.5, phone_number_id="10002")

    asyncio.run(run())
    assert requests[0].url.path.endswith("/70001/messages")
    assert requests[1].url.path == "/v1/lines/10001/messages"
    assert requests[2].url.path == "/v1/lines/10002/messages"
    assert json.loads(requests[2].content)["location"] == {"latitude": 33.89, "longitude": 35.5}


def test_global_linked_default_uses_test_line_not_production(bridge, monkeypatch):
    requests = bridge(lambda _: httpx.Response(200, json={"messages": [{"id": MESSAGE}]}))
    monkeypatch.setattr(settings, "whatsapp_transport", "linked_device")
    assert isinstance(create_whatsapp_client(), LinkedDeviceWhatsAppClient)
    asyncio.run(RoutedWhatsAppClient().send_text("96171111111", "Test"))
    assert requests[0].url.path == "/v1/lines/10001/messages"


def test_edit_and_delete_require_provider_confirmation_and_preserve_operation_id(bridge):
    requests = bridge(lambda _: httpx.Response(200, json={"confirmed": True, "id": MESSAGE, "message_id": "OP123", "timestamp": 1780000000000}))

    async def run():
        client = RoutedWhatsAppClient()
        edited = await client.edit_message("96171111111", MESSAGE, "Corrected", phone_number_id="10001", operation_id="operation-1")
        deleted = await client.delete_message("96171111111", MESSAGE, phone_number_id="10001", operation_id="operation-2")
        assert edited["confirmed"] is True and deleted["id"] == MESSAGE
    asyncio.run(run())
    assert requests[0].url.path == f"/v1/lines/10001/messages/{MESSAGE}/edit"
    assert json.loads(requests[0].content) == {"to": "96171111111", "body": "Corrected", "operation_id": "operation-1"}
    assert requests[1].url.path.endswith("/delete")
    assert json.loads(requests[1].content)["operation_id"] == "operation-2"


@pytest.mark.parametrize("status, payload, uncertain", [
    (409, {"error": "expired"}, False),
    (503, {"error": "disconnected"}, True),
    (500, {"error": "known rejection", "delivery_uncertain": False}, False),
    (409, {"error": "waiting", "delivery_uncertain": True}, True),
])
def test_provider_rejection_is_distinct_from_uncertain_delivery(bridge, status, payload, uncertain):
    bridge(lambda _: httpx.Response(status, json=payload))
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().delete_message("96171111111", MESSAGE, operation_id="stable-op"))
    assert caught.value.delivery_uncertain is uncertain
    assert caught.value.definitive_failure is not uncertain
    assert caught.value.http_status == status


@pytest.mark.parametrize(("payload", "code"), [
    ({"delivery_uncertain": False, "error": {"code": "NOT_CONNECTED", "message": "reconnecting"}}, "NOT_CONNECTED"),
    ({"delivery_uncertain": False, "error": {"code": "not a code!", "message": "x"}}, None),
    ({"error": "disconnected"}, None),
])
def test_bridge_error_code_identifies_an_offline_line(bridge, payload, code):
    bridge(lambda _: httpx.Response(503, json=payload))
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().send_text("96171111111", "Hello", phone_number_id="10001", client_message_id="request-1"))
    assert caught.value.bridge_code == code
    if code:
        assert caught.value.delivery_uncertain is False  # nothing was sent, so it may be held and resent


def test_network_timeout_leaves_mutation_uncertain(bridge):
    def fail(request):
        raise httpx.ReadTimeout("Timeout", request=request)
    bridge(fail)
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().edit_message("96171111111", MESSAGE, "New", operation_id="stable-op"))
    assert caught.value.delivery_uncertain


@pytest.mark.parametrize('failure,uncertain', [
    (httpx.ConnectError, False), (httpx.ConnectTimeout, False), (httpx.PoolTimeout, False),
    (httpx.ReadTimeout, True), (httpx.WriteTimeout, True), (httpx.WriteError, True),
    (httpx.RemoteProtocolError, True),
])
def test_only_typed_pre_request_failure_is_safe_to_hold(bridge, failure, uncertain, caplog):
    import errno

    def fail(request):
        try:
            raise OSError(errno.ECONNREFUSED, 'private-secret-path')
        except OSError as cause:
            raise failure('private-secret-provider-message', request=request) from cause

    requests = bridge(fail)
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().send_text(
            '96171111111', 'Synthetic', phone_number_id='10001', client_message_id='stable-send-key'))
    assert len(requests) == 1, 'the HTTP adapter must not automatically replay an uncertain mutation'
    assert caught.value.delivery_uncertain is uncertain
    assert caught.value.bridge_code == ('BRIDGE_TRANSPORT_UNKNOWN' if uncertain else 'BRIDGE_UNAVAILABLE')
    assert 'ECONNREFUSED' in caplog.text
    assert 'private-secret' not in caplog.text
    assert 'stable-send-key' not in caplog.text


def test_media_upload_retains_safe_connection_failure_code(bridge):
    def fail(request):
        raise httpx.ConnectError('synthetic connection refusal', request=request)

    bridge(fail)
    with pytest.raises(WhatsAppMediaError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().upload_media(
            content=b'synthetic-media', mime_type='application/pdf', filename='test.pdf', phone_number_id='10001'))
    assert caught.value.bridge_code == 'BRIDGE_UNAVAILABLE'
    assert caught.value.retryable is True


@pytest.mark.parametrize("payload", [
    {"confirmed": False, "id": MESSAGE, "message_id": "OP123"},
    {"confirmed": True, "id": "ld:10001:OTHER", "message_id": "OP123"},
    {"confirmed": True, "id": MESSAGE},
    {"confirmed": "true", "id": MESSAGE, "message_id": "OP123"},
])
def test_unverified_success_cannot_change_local_ticket(bridge, payload):
    bridge(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().delete_message("96171111111", MESSAGE, operation_id="stable-op"))
    assert caught.value.delivery_uncertain


def test_old_cloud_ids_and_wrong_lines_never_reach_bridge(bridge):
    requests = bridge(lambda _: pytest.fail("Invalid reference reached the bridge"))

    async def run():
        client = LinkedDeviceWhatsAppClient()
        actions = [
            client.send_text("96171111111", "Reply", context_message_id="wamid.old"),
            client.send_reaction(to="96171111111", message_id="wamid.old", emoji="👍"),
            client.edit_message("96171111111", "wamid.old", "New", operation_id="op1"),
            client.delete_message("96171111111", MESSAGE, phone_number_id="10002", operation_id="op2"),
            client.send_media(to="96171111111", media_type="image", media_id="old-cloud-media"),
            client.delete_message("96171111111", MESSAGE),
        ]
        for action in actions:
            with pytest.raises(WhatsAppSendError) as caught:
                await action
            assert not caught.value.delivery_uncertain
        with pytest.raises(WhatsAppMediaError) as caught:
            await client.download_media("old-cloud-media")
        assert not caught.value.retryable
    asyncio.run(run())
    assert requests == []


def test_disabled_linked_references_never_get_sent_to_cloud(bridge, monkeypatch):
    requests = bridge(lambda _: pytest.fail("Disabled linked reference reached a provider"))
    monkeypatch.setattr(settings, "whatsapp_linked_device_enabled", False)
    with pytest.raises(WhatsAppSendError):
        asyncio.run(RoutedWhatsAppClient().send_text("96171111111", "Reply", context_message_id=MESSAGE))
    assert not requests


@pytest.mark.parametrize('filename', ['voice.ogg', 'voice.opus'])
def test_media_upload_download_and_send_remain_scoped_to_line(bridge, filename):
    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"audio-payload", headers={"content-type": "audio/ogg"})
        if request.url.path.endswith("/media"):
            payload = json.loads(request.content)
            assert base64.b64decode(payload["content_base64"]) == b"audio-payload"
            return httpx.Response(200, json={"id": MEDIA})
        return httpx.Response(200, json={"messages": [{"id": MESSAGE}]})
    requests = bridge(respond)

    async def run():
        client = RoutedWhatsAppClient()
        media_id = await client.upload_media(content=b"audio-payload", mime_type="audio/ogg; codecs=opus", filename=filename, phone_number_id="10001")
        await client.send_media(to="96171111111", media_type="audio", media_id=media_id, voice=True, phone_number_id="10001")
        media = await client.download_media(media_id, phone_number_id="10001")
        assert media.content == b"audio-payload"
        assert media.mime_type == "audio/ogg"
        assert media.file_size == 13
        with pytest.raises(WhatsAppSendError):
            await client.send_media(to="96171111111", media_type="audio", media_id=media_id, phone_number_id="10002")
    asyncio.run(run())
    assert len(requests) == 3
    assert json.loads(requests[0].content)["mime_type"] == "audio/ogg; codecs=opus"
    assert json.loads(requests[0].content)["filename"] == filename
    assert json.loads(requests[1].content)["audio"] == {"id": MEDIA, "voice": True}


@pytest.mark.parametrize("url,secret", [
    ("http://example.com:3001", "s" * 40),
    ("http://user:password@127.0.0.1:3001", "s" * 40),
    ("http://127.0.0.1:3001?secret=bad", "s" * 40),
    ("http://[::1]:99999", "s" * 40),
    ("http://127.0.0.1:3001", "short"),
])
def test_unsafe_or_unauthenticated_bridge_configuration_fails_closed(bridge, monkeypatch, url, secret):
    requests = bridge(lambda _: pytest.fail("Unsafe configuration sent a request"))
    monkeypatch.setattr(settings, "whatsapp_linked_device_url", url)
    monkeypatch.setattr(settings, "whatsapp_linked_device_secret", secret)
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().send_text("96171111111", "Hello"))
    assert not caught.value.delivery_uncertain
    assert requests == []


def test_redirects_do_not_forward_bridge_secret(bridge):
    requests = bridge(lambda _: httpx.Response(307, headers={"location": "https://another.example/steal"}))
    with pytest.raises(WhatsAppSendError):
        asyncio.run(LinkedDeviceWhatsAppClient().send_text("96171111111", "Hello"))
    assert len(requests) == 1


def test_download_response_bound_is_enforced(bridge, monkeypatch):
    bridge(lambda _: httpx.Response(200, content=b"oversized"))
    for field in ["support_audio_max_bytes", "support_image_max_bytes", "support_document_max_bytes", "support_sticker_max_bytes", "support_video_max_bytes"]:
        monkeypatch.setattr(settings, field, 2)
    with pytest.raises(WhatsAppMediaError):
        asyncio.run(LinkedDeviceWhatsAppClient().download_media(MEDIA))


@pytest.mark.parametrize("status,retryable", [(404, False), (429, True), (503, True)])
def test_media_failure_retryability_is_safe_and_explicit(bridge, status, retryable):
    bridge(lambda _: httpx.Response(status, json={"error": "Unavailable"}))
    with pytest.raises(WhatsAppMediaError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().download_media(MEDIA))
    assert caught.value.retryable is retryable


def test_pairing_status_works_before_enabling_linked_sends(bridge, monkeypatch):
    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"lines": [
                {"line_id": "10001", "display_phone_number": "+96170111111", "status": "qr", "qr_data_url": "data:image/png;base64,aGVsbG8="},
                {"line_id": "99999", "status": "qr", "qr_data_url": "private-other-account"},
            ]})
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {}
        return httpx.Response(200, json={"status": "connecting"})
    requests = bridge(respond)
    monkeypatch.setattr(settings, "whatsapp_linked_device_enabled", False)

    async def run():
        client = LinkedDeviceWhatsAppClient()
        status = await client.get_status()
        assert status["lines"][0]["status"] == "qr"
        assert len(status["lines"]) == 1
        assert "private-other-account" not in str(status)
        await client.connect("10001")
        await client.disconnect("10001")
    asyncio.run(run())
    assert [request.url.path for request in requests] == ["/v1/status", "/v1/lines/10001/connect", "/v1/lines/10001/disconnect"]


def test_reference_detection_and_config_fallback(monkeypatch, tmp_path):
    assert is_linked_device_message(MESSAGE, "10001")
    assert not is_linked_device_message(MESSAGE, "10002")
    assert not is_linked_device_message("wamid.original")
    assert not is_linked_device_message("ld:10001:has:colon")
    assert not is_linked_device_message("ld:10001:" + "A" * 191)
    monkeypatch.setattr(settings, "whatsapp_linked_device_lines", {})
    monkeypatch.setattr(settings, "whatsapp_linked_device_data_directory", str(tmp_path))
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "12345")
    assert configured_linked_device_lines() == {"12345": ""}
    with pytest.raises(ValueError):
        Settings(_env_file=None, whatsapp_linked_device_lines={"not-numeric": ""})


def test_cloud_cannot_claim_remote_edit_or_delete_succeeded():
    async def run():
        client = WhatsAppClient()
        with pytest.raises(WhatsAppSendError) as edit:
            await client.edit_message("96171111111", "wamid.old", "New", operation_id="op1")
        with pytest.raises(WhatsAppSendError) as delete:
            await client.delete_message("96171111111", "wamid.old", operation_id="op2")
        assert edit.value.definitive_failure and delete.value.definitive_failure
    asyncio.run(run())


def test_manifest_routes_are_read_fresh_and_environment_pins_are_preserved(bridge):
    bridge(lambda _: httpx.Response(200, json={"lines": []}))
    directory = linked_device_data_directory()
    directory.mkdir(parents=True)
    manifest = directory / "lines.json"
    assert get_transport("90002") == "cloud"
    manifest.write_text(json.dumps({"version": 1, "lines": {"90002": "", "10001": "96170111111"}, "requests": {}}))
    assert get_transport("90002") == "linked_device"
    assert configured_linked_device_lines()["10001"] == "+96170111111"
    assert configured_linked_device_lines()["90002"] == ""
    manifest.write_text(json.dumps({"lines": {"90003": ""}}))
    assert get_transport("90002") == "cloud"
    assert get_transport("90003") == "linked_device"
    manifest.write_text(json.dumps({"version": 1, "lines": {"10001": "96179999999"}}))
    with pytest.raises(ValueError, match="conflicts"):
        configured_linked_device_lines()
    with pytest.raises(WhatsAppSendError):
        asyncio.run(LinkedDeviceWhatsAppClient().get_status())


@pytest.mark.parametrize("manifest", [
    [], {"version": 2, "lines": {}}, {"version": True, "lines": {}},
    {"version": 1, "lines": []}, {"version": 1, "lines": {"bad": ""}},
    {"version": 1, "lines": {"90001": None}},
    {"version": 1, "lines": {}, "requests": []},
    {"version": 1, "lines": {}, "unexpected": "field"},
    {"version": 1, "lines": {}, "removed_line_ids": "90001"},
    {"version": 1, "lines": {}, "removed_line_ids": ["invalid"]},
    {"version": 1, "lines": {}, "removed_request_ids": {}},
    {"version": 1, "lines": {}, "removed_request_ids": ["invalid"]},
])
def test_invalid_manifest_cannot_authorize_dynamic_routes(bridge, manifest):
    bridge(lambda _: pytest.fail("Invalid manifest reached provider"))
    directory = linked_device_data_directory()
    directory.mkdir(parents=True)
    (directory / "lines.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        configured_linked_device_lines()
    with pytest.raises(WhatsAppSendError):
        asyncio.run(LinkedDeviceWhatsAppClient().send_text("96171111111", "Hello", phone_number_id="90001"))


def test_manifest_path_does_not_depend_on_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "whatsapp_linked_device_data_directory", "var/whatsapp-linked-device")
    import app.config as config_module
    assert linked_device_data_directory() == Path(config_module.__file__).resolve().parent.parent / "var/whatsapp-linked-device"


def test_manifest_size_is_bounded_before_parsing(bridge):
    bridge(lambda _: pytest.fail("Invalid manifest reached provider"))
    directory = linked_device_data_directory()
    directory.mkdir(parents=True)
    (directory / "lines.json").write_bytes(b" " * (128 * 1024 + 1))
    with pytest.raises(ValueError, match="size limit"):
        configured_linked_device_lines()


def test_registering_a_line_refreshes_routes_without_restarting_existing_clients(bridge):
    request_id = str(uuid.uuid4())
    directory = linked_device_data_directory()
    directory.mkdir(parents=True)
    def respond(request):
        if request.url.path == "/v1/lines":
            payload = json.loads(request.content)
            assert payload == {"request_id": request_id, "expected_phone": "96170333333", "reserved_line_ids": ["90001", "12345"]}
            (directory / "lines.json").write_text(json.dumps({"version": 1, "lines": {"90002": "96170333333"}, "requests": {request_id: {"line_id": "90002", "expected_phone": "96170333333"}}}))
            return httpx.Response(200, json={"line_id": "90002", "display_phone_number": "96170333333", "status": "disconnected"})
        if request.url.path.endswith("/connect"):
            assert json.loads(request.content) == {}
            return httpx.Response(200, json={"line_id": "90002", "status": "connecting"})
        line = request.url.path.split("/")[3]
        return httpx.Response(200, json={"messages": [{"id": f"ld:{line}:OUTBOUND"}]})
    requests = bridge(respond)
    routed = RoutedWhatsAppClient()
    async def run():
        client = LinkedDeviceWhatsAppClient()
        assert get_transport("90002") == "cloud"
        added = await client.add_line(request_id, "+961 70 333 333", reserved_line_ids=["90001", "12345"])
        assert added["line_id"] == "90002"
        assert get_transport("90002") == "linked_device"
        await client.connect("90002")
        await routed.send_text("96171111111", "New line", phone_number_id="90002")
        await routed.send_text("96171111111", "Existing line", phone_number_id="10001")
        # The same request UUID is sent unchanged after an uncertain response.
        await client.add_line(request_id, "+961 70 333 333", reserved_line_ids=["90001", "12345"])
    asyncio.run(run())
    assert len(requests) == 5


def test_first_dynamic_line_can_be_registered_when_no_routes_exist(bridge, monkeypatch):
    bridge(lambda _: httpx.Response(200, json={"line_id": "90001", "status": "disconnected", "display_phone_number": ""}))
    monkeypatch.setattr(settings, "whatsapp_linked_device_lines", {})
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "")
    assert configured_linked_device_lines() == {}
    response = asyncio.run(LinkedDeviceWhatsAppClient().add_line(str(uuid.uuid4())))
    assert response["line_id"] == "90001"
    with pytest.raises(WhatsAppSendError):
        asyncio.run(LinkedDeviceWhatsAppClient().connect("90001"))


@pytest.mark.parametrize("payload", [{"status": "disconnected"}, {"line_id": "invalid", "status": "disconnected"}, {"line_id": "90001", "status": "invalid"}])
def test_invalid_registration_response_is_uncertain(bridge, payload):
    bridge(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().add_line(str(uuid.uuid4())))
    assert caught.value.delivery_uncertain


def test_invalid_registration_request_never_reaches_bridge(bridge):
    requests = bridge(lambda _: pytest.fail("Invalid add-line request reached bridge"))
    async def run():
        client = LinkedDeviceWhatsAppClient()
        actions = [
            client.add_line("not-uuid"),
            client.add_line(str(uuid.uuid4()), expected_phone="not-phone"),
            client.add_line(str(uuid.uuid4()), reserved_line_ids=["invalid"]),
        ]
        for action in actions:
            with pytest.raises(WhatsAppSendError) as caught:
                await action
            assert not caught.value.delivery_uncertain
    asyncio.run(run())
    assert requests == []


def test_removal_tombstones_suppress_environment_routes_and_prevent_cloud_fallback(bridge, monkeypatch):
    requests = bridge(lambda _: pytest.fail("Removed route reached a provider"))
    directory = linked_device_data_directory()
    directory.mkdir(parents=True)
    (directory / "lines.json").write_text(json.dumps({"version": 1, "lines": {}, "requests": {}, "removed_line_ids": ["10001", "10002"]}))
    assert configured_linked_device_lines() == {}
    assert removed_linked_device_line_ids() == {"10001", "10002"}
    assert get_transport("10001") == "removed"
    assert get_transport("70001") == "cloud"

    async def run():
        client = RoutedWhatsAppClient()
        with pytest.raises(WhatsAppSendError) as text_error:
            await client.send_text("96171111111", "Hello", phone_number_id="10001")
        assert text_error.value.definitive_failure
        with pytest.raises(WhatsAppSendError):
            await client.send_text("96171111111", "Reply", context_message_id=MESSAGE)
        with pytest.raises(WhatsAppMediaError):
            await client.upload_media(content=b"media", mime_type="image/png", filename="media.png", phone_number_id="10001")
        with pytest.raises(WhatsAppSendError):
            await LinkedDeviceWhatsAppClient().connect("10001")
    asyncio.run(run())
    monkeypatch.setattr(settings, "whatsapp_linked_device_enabled", False)
    assert get_transport("10001") == "removed"
    assert requests == []


def test_removed_legacy_default_is_never_reactivated(bridge, monkeypatch):
    bridge(lambda _: pytest.fail("Removed default reached a provider"))
    directory = linked_device_data_directory()
    directory.mkdir(parents=True)
    (directory / "lines.json").write_text(json.dumps({"lines": {}, "removed_line_ids": ["70001"]}))
    monkeypatch.setattr(settings, "whatsapp_linked_device_lines", {})
    assert configured_linked_device_lines() == {}
    assert get_transport() == "removed"
    with pytest.raises(WhatsAppSendError):
        create_whatsapp_client()
    with pytest.raises(WhatsAppSendError):
        asyncio.run(RoutedWhatsAppClient().send_text("96171111111", "Hello"))


def test_complete_line_removal_is_confirmed_and_retryable_after_tombstone(bridge):
    directory = linked_device_data_directory()
    directory.mkdir(parents=True)
    def respond(request):
        assert request.method == "DELETE"
        assert request.url.path == "/v1/lines/10001"
        (directory / "lines.json").write_text(json.dumps({"version": 1, "lines": {}, "requests": {}, "removed_line_ids": ["10001"], "removed_request_ids": [str(uuid.uuid4())]}))
        return httpx.Response(200, json={"line_id": "10001", "removed": True, "unlinked": True})
    requests = bridge(respond)
    async def run():
        client = LinkedDeviceWhatsAppClient()
        response = await client.remove_line("10001")
        assert response["removed"] is True and response["unlinked"] is True
        assert get_transport("10001") == "removed"
        assert "10001" not in configured_linked_device_lines()
        assert (await client.remove_line("10001"))["removed"] is True
    asyncio.run(run())
    assert len(requests) == 2
    assert requests[0].extensions["timeout"]["read"] == 75.0


@pytest.mark.parametrize("response", [
    {"line_id": "10001", "removed": True, "unlinked": False},
    {"line_id": "10001", "removed": True},
    {"line_id": "10002", "removed": True, "unlinked": True},
    {"line_id": "10001", "removed": "true", "unlinked": True},
])
def test_partial_line_removal_is_not_reported_as_success(bridge, response):
    bridge(lambda _: httpx.Response(200, json=response))
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().remove_line("10001"))
    assert caught.value.delivery_uncertain
    assert "10001" in configured_linked_device_lines()


def test_unknown_line_removal_never_reaches_bridge(bridge):
    requests = bridge(lambda _: pytest.fail("Unknown removal reached the bridge"))
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().remove_line("90009"))
    assert caught.value.definitive_failure
    assert requests == []


def test_removed_registration_uuid_is_exposed_as_terminal_conflict(bridge):
    bridge(lambda _: httpx.Response(409, json={"error": {"code": "REGISTRATION_REMOVED", "message": "This registration was removed."}, "delivery_uncertain": False}))
    with pytest.raises(WhatsAppSendError) as caught:
        asyncio.run(LinkedDeviceWhatsAppClient().add_line(str(uuid.uuid4())))
    assert caught.value.http_status == 409
    assert caught.value.definitive_failure


def test_delivery_lookup_uses_read_only_status_endpoint_and_checks_line(bridge):
    requests = bridge(lambda _: httpx.Response(200, json={'status': 'confirmed', 'message_id': MESSAGE, 'timestamp': 1792660000000}))
    result = asyncio.run(LinkedDeviceWhatsAppClient().get_delivery_status(
        phone='96171111111', client_message_id='transport-key', phone_number_id='10001'))
    assert result['message_id'] == MESSAGE
    assert len(requests) == 1 and requests[0].url.path == '/v1/lines/10001/messages/status'
    assert json.loads(requests[0].content) == {'phone': '96171111111', 'client_message_id': 'transport-key'}


@pytest.mark.parametrize('payload', [{'status': 'sent'}, {'status': 'confirmed', 'message_id': 'ld:10002:OTHER'}])
def test_delivery_lookup_rejects_invalid_evidence(bridge, payload):
    bridge(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(WhatsAppSendError):
        asyncio.run(LinkedDeviceWhatsAppClient().get_delivery_status(
            phone='96171111111', client_message_id='transport-key', phone_number_id='10001'))


def test_quarantine_blocks_sends_but_preserves_local_media(bridge, monkeypatch, tmp_path):
    import json
    from app.config import quarantined_linked_device_line_ids
    directory = tmp_path / "quarantined"
    directory.mkdir()
    monkeypatch.setattr(settings, "whatsapp_linked_device_data_directory", str(directory))
    (directory / "lines.json").write_text(json.dumps({"version": 1, "lines": {}, "quarantined_lines": {"10001": {
        "line_id": "10001", "state": "permanently_decommissioned", "reuse_allowed": False, "remote_unlinked": False}}}))
    def respond(request):
        assert request.method == "GET"
        assert request.url.path.endswith(MEDIA)
        return httpx.Response(200, content=b"preserved image", headers={"content-type": "image/jpeg"})
    requests = bridge(respond)
    async def run():
        client = RoutedWhatsAppClient()
        assert quarantined_linked_device_line_ids() == {"10001"}
        assert get_transport("10001") == "removed"
        media = await client.download_media(MEDIA, phone_number_id="10001")
        assert media.content == b"preserved image"
        with pytest.raises(WhatsAppMediaError):
            await client.download_media(MEDIA, phone_number_id="10002")
        with pytest.raises(WhatsAppSendError):
            await client.send_text("96170000000", "blocked", phone_number_id="10001")
    asyncio.run(run())
    assert len(requests) == 1


def test_decommission_client_accepts_explicit_local_result_without_claiming_unlink(bridge):
    import json
    def respond(request):
        assert request.method == "POST" and request.url.path == "/v1/lines/10001/decommission"
        assert json.loads(request.content) == {"confirm_line_id": "10001", "actor": "7"}
        return httpx.Response(200, json={"line_id": "10001", "removed": True, "unlinked": False, "quarantined": True, "reuse_allowed": False})
    bridge(respond)
    result = asyncio.run(LinkedDeviceWhatsAppClient().remove_line("10001", decommission=True, actor="7"))
    assert result["unlinked"] is False
