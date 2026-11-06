"""Real Redis on a disposable Unix socket. No production DB/provider access."""
import asyncio
import subprocess
import time

import pytest
import redis

from app.conversation_store import RedisConversationStore, _pending_inbound_queue_key, _inflight_inbound_queue_key
from scripts.check_inbound_drain import require_drained


@pytest.fixture
def isolated_redis(tmp_path):
    socket = tmp_path / "redis.sock"
    process = subprocess.Popen(["redis-server", "--port", "0", "--unixsocket", str(socket),
                                "--dir", str(tmp_path), "--save", "", "--appendonly", "no"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = redis.Redis(unix_socket_path=str(socket))
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except redis.RedisError:
                time.sleep(.02)
        else:
            pytest.fail("Disposable Redis failed to start")
        yield client, "unix://" + str(socket)
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)


def test_reservation_survives_client_loss_and_refuses_blind_replay(isolated_redis):
    client, url = isolated_redis
    async def run():
        first = RedisConversationStore(url, 3600)
        message = await first.claim_incoming_message("fixture-message")
        await first.enqueue_incoming_work("fixture", {"message_id": "fixture-message", "claim_token": message})
        assert client.ttl(_pending_inbound_queue_key("fixture")) == -1
        assert client.ttl("whatsapp:inbound:fixture-message") == -1
        owner = await first.claim_conversation_processing("fixture")
        item = await first.pop_incoming_work("fixture", claim_token=owner)
        assert item["message_id"] == "fixture-message"
        assert client.ttl(_inflight_inbound_queue_key("fixture")) == -1
        await first.release_conversation_processing("fixture", owner)
        await first._client.aclose()
        second = RedisConversationStore(url, 3600)
        try:
            with pytest.raises(RuntimeError, match="INBOUND_RECOVERY_REQUIRED"):
                await second.claim_conversation_processing("fixture")
            assert await second.inflight_incoming_work_count("fixture") == 1
            with pytest.raises(RuntimeError, match="INBOUND_DRAIN_UNRESOLVED"):
                require_drained(client, 0)
        finally:
            await second._client.aclose()
    asyncio.run(run())


def test_safe_retry_moves_reservation_atomically_and_ack_removes_only_completed(isolated_redis):
    client, url = isolated_redis
    async def run():
        store = RedisConversationStore(url, 3600)
        try:
            for number in (1, 2):
                await store.enqueue_incoming_work("fixture", {"number": number})
            owner = await store.claim_conversation_processing("fixture")
            item = await store.pop_incoming_work("fixture", claim_token=owner)
            await store.enqueue_incoming_work("fixture", item, front=True)
            assert await store.inflight_incoming_work_count("fixture") == 0
            with pytest.raises(RuntimeError, match="INBOUND_RECEIPT_MISSING"):
                await store.enqueue_incoming_work("fixture", item, front=True)
            assert await store.pending_incoming_work_count("fixture") == 2
            for number in (1, 2):
                item = await store.pop_incoming_work("fixture", claim_token=owner)
                assert item["number"] == number
                await store.acknowledge_incoming_work("fixture", item)
            await store.release_conversation_processing("fixture", owner)
            assert require_drained(client, 0) == {"pending": 0, "inflight": 0, "leases": 0, "invalid": 0}
        finally:
            await store._client.aclose()
    asyncio.run(run())


def test_lost_owner_cannot_remove_pending_work(isolated_redis):
    client, url = isolated_redis
    async def run():
        store = RedisConversationStore(url, 3600)
        try:
            await store.enqueue_incoming_work("fixture", {"number": 1})
            with pytest.raises(Exception, match="INBOUND_OWNER_LOST"):
                await store.pop_incoming_work("fixture", claim_token="wrong")
            assert await store.pending_incoming_work_count("fixture") == 1
        finally:
            await store._client.aclose()
    asyncio.run(run())


def test_malformed_payload_is_retained_and_blocks_takeover(isolated_redis):
    client, url = isolated_redis
    client.rpush(_pending_inbound_queue_key("fixture"), "invalid-json")
    async def run():
        store = RedisConversationStore(url, 3600)
        try:
            owner = await store.claim_conversation_processing("fixture")
            with pytest.raises(RuntimeError, match="INBOUND_PAYLOAD_INVALID"):
                await store.pop_incoming_work("fixture", claim_token=owner)
            assert client.lrange(_inflight_inbound_queue_key("fixture"), 0, -1) == [b"invalid-json"]
        finally:
            await store._client.aclose()
    asyncio.run(run())


def test_drain_gate_never_waits_for_queue_or_lease_expiry(isolated_redis):
    client, _url = isolated_redis
    client.set("conversation:processing:fixture", "processing:test", ex=1)
    with pytest.raises(RuntimeError, match="INBOUND_DRAIN_UNRESOLVED"):
        require_drained(client, 10, sleep=lambda _: pytest.fail("Must abort without waiting for expiry"))
