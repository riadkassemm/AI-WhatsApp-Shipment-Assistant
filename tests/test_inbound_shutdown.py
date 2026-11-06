import asyncio

import pytest

from app import main
from app.conversation_store import InMemoryConversationStore
from app.whatsapp_client import IncomingWhatsAppMessage


def test_cancelled_http_request_does_not_cancel_reserved_work(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        entered, finish = asyncio.Event(), asyncio.Event()
        async def process(_incoming):
            entered.set()
            await finish.wait()
            return main._json_response(200, {"status": "ok"})
        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main, "_process_incoming_message", process)
        monkeypatch.setattr(main.settings, "whatsapp_message_burst_debounce_seconds", 0)
        monkeypatch.setattr(main, "_inbound_stopping", False)
        message = IncomingWhatsAppMessage("15550000001", "fixture", "text", "hello", "1")
        claim = await store.claim_incoming_message("fixture")
        await store.enqueue_incoming_work(message.from_number, main._queued_inbound_item(message, claim))
        request = asyncio.create_task(main._drive_inbound_queue(message.from_number))
        await entered.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert await store.inflight_incoming_work_count(message.from_number) == 1
        shutdown = asyncio.create_task(main._finish_inbound_tasks(1))
        await asyncio.sleep(0)
        assert not shutdown.done()
        finish.set()
        await shutdown
        assert await store.inflight_incoming_work_count(message.from_number) == 0
        assert await store.pending_incoming_work_count(message.from_number) == 0
        assert await store.claim_incoming_message("fixture") is None
    asyncio.run(run())


def test_shutdown_deadline_retains_uncertain_work_without_requeue(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        entered = asyncio.Event()
        async def process(_incoming):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main, "_process_incoming_message", process)
        monkeypatch.setattr(main.settings, "whatsapp_message_burst_debounce_seconds", 0)
        monkeypatch.setattr(main, "_inbound_stopping", False)
        message = IncomingWhatsAppMessage("15550000001", "fixture", "text", "hello", "1")
        await store.enqueue_incoming_work(message.from_number, main._queued_inbound_item(message, None))
        request = asyncio.create_task(main._drive_inbound_queue(message.from_number))
        await entered.wait()
        with pytest.raises(RuntimeError, match="INBOUND_SHUTDOWN_INCOMPLETE"):
            await main._finish_inbound_tasks(0)
        with pytest.raises(asyncio.CancelledError):
            await request
        assert await store.inflight_incoming_work_count(message.from_number) == 1
        assert await store.pending_incoming_work_count(message.from_number) == 0
        with pytest.raises(RuntimeError, match="INBOUND_RECOVERY_REQUIRED"):
            await main._drive_inbound_queue(message.from_number)
    asyncio.run(run())


def test_completion_failure_retains_reservation(monkeypatch):
    async def run():
        store = InMemoryConversationStore()
        monkeypatch.setattr(main, "conversation_store", store)
        async def failed(_incoming):
            raise OSError("fixture failure")
        monkeypatch.setattr(main, "_mark_inbound_completed", failed)
        message = IncomingWhatsAppMessage("15550000001", "fixture", "text", "hello", "1")
        await store.enqueue_incoming_work(message.from_number, main._queued_inbound_item(message, None))
        item = await store.pop_incoming_work(message.from_number)
        with pytest.raises(OSError):
            await main._complete_queued_inbound_items(message.from_number, [item])
        assert await store.inflight_incoming_work_count(message.from_number) == 1
    asyncio.run(run())
