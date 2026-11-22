"""Real Redis on a disposable Unix socket. No production DB/provider access."""
import asyncio
import json
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
            assert require_drained(client, 0) == {"pending": 0, "inflight": 0, "leases": 0, "invalid": 0, "dead_letter": 0}
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


def test_turn_started_marker_is_durable_hashed_and_expires(isolated_redis):
    client, url = isolated_redis
    async def run():
        first = RedisConversationStore(url, 3600)
        try:
            assert not await first.inbound_turn_started(["wamid.t1", "wamid.t2"])
            await first.mark_inbound_turn_started(["wamid.t1", "wamid.t2", ""])
        finally:
            await first._client.aclose()
        # A new client (for example after an API restart) still sees the marker.
        second = RedisConversationStore(url, 3600)
        try:
            assert await second.inbound_turn_started(["wamid.other", "wamid.t2"])
            assert not await second.inbound_turn_started(["wamid.other"])
            assert not await second.inbound_turn_started([])
        finally:
            await second._client.aclose()
        keys = [key.decode() for key in client.scan_iter(match="whatsapp:inbound-turn:*")]
        assert len(keys) == 2
        # Keys never contain the raw WhatsApp message ID, and they expire.
        assert all("wamid" not in key for key in keys)
        assert all(0 < client.ttl(key) <= 7 * 24 * 60 * 60 for key in keys)
    asyncio.run(run())


def _age_inflight(client, route: str, seconds: float) -> None:
    from app.conversation_store import _inflight_since_key
    client.set(_inflight_since_key(route), repr(time.time() - seconds))


def test_abandoned_reservations_recover_in_order_after_grace(isolated_redis):
    client, url = isolated_redis
    from app.conversation_store import inbound_recovery_grace_seconds
    async def run():
        store = RedisConversationStore(url, 3600)
        try:
            for number in (1, 2, 3):
                await store.enqueue_incoming_work("fixture", {"message_id": None, "number": number})
            owner = await store.claim_conversation_processing("fixture")
            await store.pop_incoming_work("fixture", claim_token=owner)
            await store.pop_incoming_work("fixture", claim_token=owner)
            await store.release_conversation_processing("fixture", owner)

            # Fresh in-flight work may still be live: the lock is refused.
            with pytest.raises(RuntimeError, match="INBOUND_RECOVERY_REQUIRED"):
                await store.claim_conversation_processing("fixture")

            _age_inflight(client, "fixture", inbound_recovery_grace_seconds() + 5)
            owner = await store.claim_conversation_processing("fixture")
            assert owner
            with pytest.raises(RuntimeError, match="INBOUND_OWNER_LOST"):
                await store.recover_inflight_incoming_work("fixture", "not-the-owner")
            assert await store.recover_inflight_incoming_work("fixture", owner) == 2
            assert await store.inflight_incoming_work_count("fixture") == 0
            assert not client.exists("conversation:inflight-since:" + _digest("fixture"))
            order = []
            for _ in range(3):
                item = await store.pop_incoming_work("fixture", claim_token=owner)
                order.append((item["number"], bool(item.get("recovered")), item.get("attempts")))
                await store.acknowledge_incoming_work("fixture", item)
            assert order == [(1, True, 1), (2, True, 1), (3, False, None)]
            await store.release_conversation_processing("fixture", owner)
            assert require_drained(client, 0)["inflight"] == 0
        finally:
            await store._client.aclose()
    asyncio.run(run())


def _digest(route: str) -> str:
    import hashlib
    return hashlib.sha256(route.encode()).hexdigest()


def test_unreadable_reservation_is_parked_not_blocking(isolated_redis):
    client, url = isolated_redis
    from app.conversation_store import inbound_recovery_grace_seconds
    client.rpush(_pending_inbound_queue_key("fixture"), "invalid-json")
    async def run():
        store = RedisConversationStore(url, 3600)
        try:
            owner = await store.claim_conversation_processing("fixture")
            with pytest.raises(RuntimeError, match="INBOUND_PAYLOAD_INVALID"):
                await store.pop_incoming_work("fixture", claim_token=owner)
            await store.release_conversation_processing("fixture", owner)
            _age_inflight(client, "fixture", inbound_recovery_grace_seconds() + 5)
            owner = await store.claim_conversation_processing("fixture")
            assert await store.recover_inflight_incoming_work("fixture", owner) == 1
            assert await store.inflight_incoming_work_count("fixture") == 0
            assert await store.dead_letter_incoming_work_count("fixture") == 1
        finally:
            await store._client.aclose()
    asyncio.run(run())


def test_dead_letter_is_reported_but_does_not_block_drain_and_can_be_replayed(isolated_redis):
    client, url = isolated_redis
    from scripts.check_inbound_drain import inspect
    from scripts.replay_dead_inbound import list_dead_letters, replay
    async def run():
        store = RedisConversationStore(url, 3600)
        try:
            await store.ping()
            await store.enqueue_incoming_work("fixture", {
                "message_id": "wamid.dead", "text": "private customer text", "attempts": 20,
            })
            assert [head["message_id"] for head in await store.scan_inbound_queue_heads()] == ["wamid.dead"]
            owner = await store.claim_conversation_processing("fixture")
            item = await store.pop_incoming_work("fixture", claim_token=owner)
            await store.dead_letter_incoming_work("fixture", item, "support_state_unavailable")
            with pytest.raises(RuntimeError, match="INBOUND_RECEIPT_MISSING"):
                await store.dead_letter_incoming_work("fixture", item, "again")
            await store.release_conversation_processing("fixture", owner)
            assert await store.scan_inbound_queue_heads() == []
        finally:
            await store._client.aclose()
    asyncio.run(run())
    counts = inspect(client)
    assert counts["dead_letter"] == 1 and counts["inflight"] == 0 and counts["pending"] == 0
    assert require_drained(client, 0)["dead_letter"] == 1  # reported, not blocking

    listing = list_dead_letters(client)
    assert listing[0]["digest"] == _digest("fixture")
    assert listing[0]["items"][0]["dead_letter_reason"] == "support_state_unavailable"
    assert "private customer text" not in json.dumps(listing)  # metadata only

    with pytest.raises(SystemExit):
        replay(client, "not-a-digest")
    assert replay(client, _digest("fixture")) == 1
    assert inspect(client)["dead_letter"] == 0
    restored = json.loads(client.lindex(_pending_inbound_queue_key("fixture"), 0))
    assert restored["recovered"] is True and restored["attempts"] == 0
    assert "dead_lettered_at" not in restored
