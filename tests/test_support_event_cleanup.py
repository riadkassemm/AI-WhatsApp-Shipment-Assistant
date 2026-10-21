"""Deterministic cleanup ordering; real Redis coverage lives in integration tests."""
import asyncio
import json

from app.support_events import event_stream


def test_revoked_stream_discards_queued_data_and_waits_for_unsubscribe_ack(monkeypatch):
    async def run():
        unsubscribed = asyncio.Event()
        ack_allowed = asyncio.Event()
        closed = []
        validating = 0
        messages = [None, {'type': 'message', 'data': json.dumps({'private': 'never emitted'})}]

        class PubSub:
            async def subscribe(self, channel):
                return None
            async def unsubscribe(self, channel):
                unsubscribed.set()
            async def get_message(self, **kwargs):
                if messages:
                    return messages.pop(0)
                await ack_allowed.wait()
                return {'type': 'unsubscribe', 'data': 0}
            async def aclose(self):
                closed.append('subscription')

        pubsub = PubSub()
        class Client:
            def pubsub(self):
                return pubsub
            async def aclose(self):
                closed.append('client')

        import redis.asyncio as redis
        monkeypatch.setattr(redis, 'from_url', lambda *args, **kwargs: Client())
        async def validate():
            nonlocal validating
            validating += 1
            if validating > 1:
                raise RuntimeError('revoked')

        stream = event_stream(validate=validate)
        assert await anext(stream) == 'retry: 3000\n\n'
        task = asyncio.create_task(anext(stream))
        await asyncio.wait_for(unsubscribed.wait(), 1)
        assert not task.done() and closed == []
        ack_allowed.set()
        try:
            await asyncio.wait_for(task, 1)
            raise AssertionError('Revoked stream emitted protected data')
        except StopAsyncIteration:
            pass
        assert closed == ['subscription', 'client']
    asyncio.run(run())
