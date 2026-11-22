"""Phase 1a: batched Cloud webhooks, shared contacts in the queue, credential-free logs."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import types

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

from app import main  # noqa: E402
from app.client_chat_auth import extract_credentials  # noqa: E402
from app.conversation_store import Conversation, InMemoryConversationStore  # noqa: E402
from app.whatsapp_client import (  # noqa: E402
    IncomingWhatsAppMessage,
    extract_incoming_message,
    extract_incoming_messages,
)

LINE = "1234567890"
OTHER_LINE = "1234567891"


def _message(message_id: str, sender: str, body: str, timestamp: str = "1760000000") -> dict:
    return {
        "from": sender,
        "id": message_id,
        "timestamp": timestamp,
        "type": "text",
        "text": {"body": body},
    }


def _change(line: str, messages: list[dict] | None = None, statuses: list[dict] | None = None) -> dict:
    value: dict = {"metadata": {"phone_number_id": line, "display_phone_number": f"+{line}"}}
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {"value": value}


def _batch_payload() -> dict:
    return {
        "entry": [
            {"id": "waba-1", "changes": [
                _change(LINE, [_message("wamid.1", "96170000001", "first"),
                               _message("wamid.2", "96170000002", "second")]),
            ]},
            {"id": "waba-2", "changes": [
                _change(OTHER_LINE, [_message("wamid.3", "96170000003", "third")]),
            ]},
        ]
    }


def _patch_pipeline(monkeypatch, store, processed: list[str], *, fail_ids: set[str] | None = None):
    fail_ids = fail_ids or set()

    async def process(incoming, **_kwargs):
        if incoming.message_id in fail_ids:
            return main._json_response(503, {"status": "support_state_unavailable"})
        processed.append(incoming.message_id)
        return main._json_response(200, {"status": "handled"})

    async def enabled(**_kwargs):
        return True

    async def no_ticket(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "conversation_store", store)
    monkeypatch.setattr(main, "_process_incoming_message", process)
    monkeypatch.setattr(main, "get_transport", lambda _line: "cloud")
    monkeypatch.setattr(main.whatsapp_registry_service, "observe_inbound", enabled)
    monkeypatch.setattr(main.support_service, "get_active_ticket", no_ticket)
    monkeypatch.setattr(main.settings, "whatsapp_message_burst_debounce_seconds", 0)
    monkeypatch.setattr(main.settings, "whatsapp_message_burst_max_wait_seconds", 0)
    monkeypatch.setattr(main, "_inbound_stopping", False)


def test_extract_incoming_messages_reads_every_entry_change_and_message():
    messages = extract_incoming_messages(_batch_payload())
    assert [m.message_id for m in messages] == ["wamid.1", "wamid.2", "wamid.3"]
    assert [m.business_phone_number_id for m in messages] == [LINE, LINE, OTHER_LINE]
    assert [m.business_account_id for m in messages] == ["waba-1", "waba-1", "waba-2"]
    assert [m.text for m in messages] == ["first", "second", "third"]


def test_extract_incoming_messages_skips_malformed_items_without_hiding_siblings():
    payload = _batch_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"].insert(0, {"id": "no-sender", "type": "text"})
    payload["entry"].append("not-an-entry")
    assert [m.message_id for m in extract_incoming_messages(payload)] == ["wamid.1", "wamid.2", "wamid.3"]
    assert extract_incoming_messages({}) == []
    assert extract_incoming_messages({"entry": "bad"}) == []


def test_single_message_parser_keeps_first_message_semantics():
    payload = _batch_payload()
    assert extract_incoming_message(payload).message_id == "wamid.1"
    payload["entry"][0]["changes"][0]["value"]["messages"][0] = {"id": "no-sender", "type": "text"}
    # The bridge contract is one message per callback: a malformed first item is
    # still rejected rather than silently replaced by a sibling.
    assert extract_incoming_message(payload) is None


def test_batched_cloud_webhook_queues_and_processes_every_message(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        processed: list[str] = []
        _patch_pipeline(monkeypatch, store, processed)
        response = await main.process_whatsapp_payload(_batch_payload(), source="cloud")
        assert response.status_code == 200
        assert json.loads(response.body) == {"status": "batch_processed", "count": 3}
        assert sorted(processed) == ["wamid.1", "wamid.2", "wamid.3"]
        for message_id in ("wamid.1", "wamid.2", "wamid.3"):
            # Every message owns its own completed idempotency claim.
            assert await store.claim_incoming_message(message_id) is None
    asyncio.run(run())


def test_batch_retry_after_partial_failure_does_not_duplicate(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        processed: list[str] = []
        _patch_pipeline(monkeypatch, store, processed, fail_ids={"wamid.2"})
        first = await main.process_whatsapp_payload(_batch_payload(), source="cloud")
        assert first.status_code == 503
        # wamid.3 sits on another line, so it is still accepted despite wamid.2.
        assert sorted(processed) == ["wamid.1", "wamid.3"]

        # Meta retries the same payload once the dependency recovers.
        _patch_pipeline(monkeypatch, store, processed)
        second = await main.process_whatsapp_payload(_batch_payload(), source="cloud")
        assert second.status_code == 200
        assert sorted(processed) == ["wamid.1", "wamid.2", "wamid.3"]
    asyncio.run(run())


def test_single_message_cloud_webhook_response_is_unchanged(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        processed: list[str] = []
        _patch_pipeline(monkeypatch, store, processed)
        payload = {"entry": [{"id": "waba-1", "changes": [
            _change(LINE, [_message("wamid.solo", "96170000009", "hi")]),
        ]}]}
        response = await main.process_whatsapp_payload(payload, source="cloud")
        assert response.status_code == 200
        # Same response the pre-batch code produced for one queued message.
        assert json.loads(response.body) == {"status": "queue_drained"}
        assert processed == ["wamid.solo"]
    asyncio.run(run())


def test_statuses_in_every_change_are_applied(monkeypatch):
    seen: list[str] = []

    async def apply(**kwargs):
        seen.append(kwargs["whatsapp_message_id"])
        return True

    monkeypatch.setattr(main.support_service, "apply_delivery_status", apply)
    payload = {"entry": [
        {"changes": [
            _change(LINE, statuses=[{"id": "wamid.A", "status": "delivered", "timestamp": "1760000001"}]),
            _change(OTHER_LINE, statuses=[{"id": "wamid.B", "status": "read", "timestamp": "1760000002"}]),
        ]},
        {"changes": [
            _change(LINE, statuses=[{"id": "wamid.C", "status": "failed", "timestamp": "1760000003"}]),
        ]},
    ]}
    response = asyncio.run(main.process_whatsapp_payload(payload, source="cloud"))
    assert response.status_code == 200
    assert seen == ["wamid.A", "wamid.B", "wamid.C"]


def test_linked_device_path_still_reads_one_message(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        processed: list[str] = []
        _patch_pipeline(monkeypatch, store, processed)
        monkeypatch.setattr(main, "get_transport", lambda _line: "linked_device")
        response = await main.process_whatsapp_payload(_batch_payload(), source="linked_device")
        assert response.status_code == 200
        assert processed == ["wamid.1"]
    asyncio.run(run())


def test_shared_contacts_survive_the_durable_queue():
    cards = [{"name": {"formatted_name": "Maya Example"}, "phones": [{"phone": "+961 70 123456"}]}]
    incoming = IncomingWhatsAppMessage(
        "96170000001", "wamid.contact", "contacts", None, "1760000000",
        business_phone_number_id=LINE, contacts=cards,
    )
    item = main._queued_inbound_item(incoming, "claim")
    restored = main._incoming_from_queued_item(json.loads(json.dumps(item)))
    assert restored.contacts == cards
    assert main._human_inbound_body(restored, None) == "Maya Example\nPhone: +961 70 123456"


def test_items_queued_before_the_contacts_field_still_load():
    incoming = IncomingWhatsAppMessage("96170000001", "wamid.old", "contacts", None, "1760000000")
    item = main._queued_inbound_item(incoming, None)
    item.pop("contacts")
    assert main._incoming_from_queued_item(item).contacts is None
    assert main._incoming_from_queued_item({**item, "contacts": "garbage"}).contacts is None
    assert main._incoming_from_queued_item({**item, "contacts": ["x", {"name": {}}]}).contacts == [{"name": {}}]


def test_auth_flow_logs_never_contain_user_id_or_password_length(monkeypatch, caplog):
    user_id = "LB98765"
    password = "Zx81-qq-Lm4"

    async def run():
        store = InMemoryConversationStore()
        monkeypatch.setattr(main, "conversation_store", store)
        sent: list[str] = []

        async def send_once(**kwargs):
            sent.append(kwargs["body"])
            return {}

        async def rejected(*_args, **_kwargs):
            return None

        monkeypatch.setattr(main, "_send_text_once", send_once)
        monkeypatch.setattr(main, "authenticate_customer", rejected, raising=False)
        conversation = Conversation(phone_number="96170000001")
        conversation.auth_state = "awaiting_credentials"
        await store.save(conversation)

        first = extract_credentials(
            user_id, auth_state="awaiting_credentials", pending_username=None, has_pending_password=False,
        )
        await main._handle_conversational_auth(
            conversation, first, raw_text=user_id, source_message_id="wamid.u",
        )
        assert conversation.auth_state == "awaiting_password"
        second = extract_credentials(
            password, auth_state=conversation.auth_state,
            pending_username=conversation.pending_auth_userid, has_pending_password=False,
        )
        await main._handle_conversational_auth(
            conversation, second, raw_text=password, source_message_id="wamid.p",
        )
        await store.save(conversation)
        await store.get_or_create("96170000001")

    caplog.set_level(logging.DEBUG)
    asyncio.run(run())
    text = caplog.text
    assert user_id not in text
    assert password not in text
    assert "password_length" not in text
    # Both credential steps really ran and logged their (redacted) state.
    assert "AUTH STATE SET: pending_userid_present=True" in text
    assert "AUTH PASSWORD STEP: pending_userid_present=True" in text


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **_kwargs):
        self.values[key] = value
        return True


def test_redis_store_logs_have_no_phone_user_id_or_customer_id(caplog):
    from app.conversation_store import RedisConversationStore

    store = RedisConversationStore.__new__(RedisConversationStore)
    store._client = _FakeRedis()
    store._ttl_seconds = 60

    async def run():
        conversation = Conversation(
            phone_number="96170111222", conversation_key=f"{LINE}:96170111222",
            business_phone_number_id=LINE,
        )
        conversation.pending_auth_userid = "LB98765"
        conversation.verified_customer_id = "4242"
        await store.save(conversation)
        await store.get_or_create("96170111222", conversation_key=f"{LINE}:96170111222")

    caplog.set_level(logging.DEBUG)
    asyncio.run(run())
    for secret in ("96170111222", "LB98765", "4242"):
        assert secret not in caplog.text
    assert "pending_userid_present=True" in caplog.text
