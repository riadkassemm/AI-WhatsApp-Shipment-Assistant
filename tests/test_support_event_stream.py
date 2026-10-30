"""SSE heartbeat timing and bounded cleanup, with a fake Redis and clock.

No Redis server is used here; tests/test_support_event_stream_redis.py repeats
the leak checks against a disposable local Redis process.
"""
import asyncio
import gc
import json
import logging
import time

import anyio
import pytest
import redis.asyncio as redis_asyncio
from redis.exceptions import ConnectionError as RedisConnectionError

import app.support_events as support_events
from app.support_events import event_stream


OTHER_TICKET = {'type': 'message', 'data': json.dumps({'type': 'ticket_updated', 'ticket_id': 99})}
OWN_TICKET = {'type': 'message', 'data': json.dumps({'type': 'ticket_updated', 'ticket_id': 7})}
MALFORMED = {'type': 'message', 'data': '{not json'}
ACK = {'type': 'unsubscribe', 'data': 0}


class FakeRedis:
    """One client + pubsub per stream; records lifecycle calls."""

    def __init__(self, *, stream_messages=None, cleanup=None, close_errors=None):
        self.stream_messages = stream_messages
        self.cleanup = cleanup or (lambda pubsub: ACK)
        self.close_errors = close_errors or {}
        self.created = 0
        self.closed = []
        self.subscribed = asyncio.Event()
        self.unsubscribed = False

    def install(self, monkeypatch):
        monkeypatch.setattr(redis_asyncio, 'from_url', lambda *args, **kwargs: self.client())

    def client(self):
        fake = self
        fake.created += 1

        class PubSub:
            async def subscribe(self, channel):
                fake.subscribed.set()

            async def unsubscribe(self, channel):
                fake.unsubscribed = True

            async def get_message(self, ignore_subscribe_messages, timeout):
                if fake.unsubscribed:
                    result = fake.cleanup(self)
                    return await result if asyncio.iscoroutine(result) else result
                if fake.stream_messages is None:
                    await asyncio.Event().wait()
                return await fake.stream_messages(timeout)

            async def aclose(self):
                await fake._close('subscription')

        class Client:
            def pubsub(self):
                return PubSub()

            async def aclose(self):
                await fake._close('client')

        return Client()

    async def _close(self, label):
        self.closed.append(label)
        error = self.close_errors.get(label)
        if error == 'hang':
            await asyncio.Event().wait()
        if error is not None:
            raise error


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def capture_loop_errors():
    errors = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
    return errors


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)
    gc.collect()
    await asyncio.sleep(0)


def test_heartbeat_is_not_postponed_by_a_continuous_flow_of_discarded_events(monkeypatch):
    async def run():
        clock, timeouts, validations = Clock(), [], []

        async def messages(timeout):
            timeouts.append(round(timeout, 6))
            clock.now += 2.0  # another staff member's event every 2 seconds
            return OTHER_TICKET

        async def validate():
            validations.append(clock.now)

        async def authorize(payload):
            return payload['ticket_id'] == 7

        FakeRedis(stream_messages=messages).install(monkeypatch)
        stream = event_stream(authorize, validate=validate, clock=clock)
        assert await anext(stream) == 'retry: 3000\n\n'
        chunks = [await asyncio.wait_for(anext(stream), 1) for _ in range(3)]
        await stream.aclose()
        assert chunks == [': keepalive\n\n'] * 3
        # Receiving an event does not restart the 15-second wait.
        assert timeouts[:8] == [15.0, 13.0, 11.0, 9.0, 7.0, 5.0, 3.0, 1.0]
        assert timeouts[8:16] == timeouts[:8]
        # Session revocation is still checked after every Redis message.
        assert len(validations) == 1 + len(timeouts)
    asyncio.run(run())


def test_delivered_events_count_as_output_and_malformed_events_do_not(monkeypatch):
    async def run():
        clock = Clock()
        script = iter([MALFORMED, OWN_TICKET] + [OTHER_TICKET] * 20)

        async def messages(timeout):
            clock.now += 2.0
            return next(script)

        async def authorize(payload):
            return payload['ticket_id'] == 7

        FakeRedis(stream_messages=messages).install(monkeypatch)
        stream = event_stream(authorize, clock=clock)
        await anext(stream)
        delivered = await anext(stream)
        assert delivered.startswith('data: ') and '"authorized": true' in delivered
        assert clock.now == 4.0
        assert await anext(stream) == ': keepalive\n\n'
        assert clock.now == 20.0  # 15 seconds after the delivered event, not after t=0
        await stream.aclose()
    asyncio.run(run())


def test_idle_stream_keeps_one_heartbeat_per_interval(monkeypatch):
    async def run():
        clock, timeouts = Clock(), []

        async def messages(timeout):
            timeouts.append(timeout)
            clock.now += timeout
            return None

        FakeRedis(stream_messages=messages).install(monkeypatch)
        stream = event_stream(clock=clock)
        await anext(stream)
        assert [await anext(stream) for _ in range(3)] == [': keepalive\n\n'] * 3
        assert timeouts == [15.0, 15.0, 15.0]
        await stream.aclose()
    asyncio.run(run())


def test_authorization_failure_still_fails_closed(monkeypatch):
    async def run():
        async def messages(timeout):
            return OWN_TICKET

        async def authorize(payload):
            raise RuntimeError('database unavailable')

        fake = FakeRedis(stream_messages=messages)
        fake.install(monkeypatch)
        stream = event_stream(authorize)
        await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert fake.closed == ['subscription', 'client']
    asyncio.run(run())


def _consume_in_task_group(stream):
    async def consume():
        async for _chunk in stream:
            pass
    return consume


def test_disconnect_cancellation_completes_cleanup_without_orphan_tasks(monkeypatch):
    """Starlette cancels a disconnected stream through an anyio cancel scope."""
    async def run():
        errors = capture_loop_errors()
        fake = FakeRedis()
        fake.install(monkeypatch)
        async with anyio.create_task_group() as group:
            group.start_soon(_consume_in_task_group(event_stream()))
            await fake.subscribed.wait()
            await asyncio.sleep(0)
            group.cancel_scope.cancel()
        assert fake.unsubscribed and fake.closed == ['subscription', 'client']
        await settle()
        assert errors == []
    asyncio.run(run())


def test_connection_closed_during_unsubscribe_is_handled_without_unretrieved_exception(monkeypatch, caplog):
    """The production traceback: Redis closes the connection during cleanup."""
    async def run():
        errors = capture_loop_errors()

        def closed_by_server(pubsub):
            raise RedisConnectionError('Connection closed by server.')

        fake = FakeRedis(cleanup=closed_by_server)
        fake.install(monkeypatch)
        async with anyio.create_task_group() as group:
            group.start_soon(_consume_in_task_group(event_stream()))
            await fake.subscribed.wait()
            await asyncio.sleep(0)
            group.cancel_scope.cancel()
        assert fake.closed == ['subscription', 'client']
        await settle()
        assert errors == []
    with caplog.at_level(logging.WARNING, logger='shipment-bot'):
        asyncio.run(run())
    assert 'Could not confirm support stream unsubscribe: exception_type=ConnectionError' in caplog.text


def test_missing_unsubscribe_acknowledgement_is_bounded(monkeypatch, caplog):
    monkeypatch.setattr(support_events, 'CLEANUP_STEP_SECONDS', 0.05)

    async def run():
        async def never_acknowledged(pubsub):
            await asyncio.Event().wait()

        fake = FakeRedis(cleanup=never_acknowledged)
        fake.install(monkeypatch)
        started = time.monotonic()
        async with anyio.create_task_group() as group:
            group.start_soon(_consume_in_task_group(event_stream()))
            await fake.subscribed.wait()
            await asyncio.sleep(0)
            group.cancel_scope.cancel()
        assert time.monotonic() - started < 1.0
        assert fake.closed == ['subscription', 'client']
    with caplog.at_level(logging.WARNING, logger='shipment-bot'):
        asyncio.run(run())
    assert 'Could not confirm support stream unsubscribe: reason=timeout' in caplog.text


@pytest.mark.parametrize('errors', [
    {'subscription': RedisConnectionError('gone')},
    {'client': OSError('reset')},
    {'subscription': RedisConnectionError('gone'), 'client': OSError('reset')},
    {'subscription': 'hang'},
])
def test_close_failures_are_bounded_and_the_client_is_always_closed(monkeypatch, caplog, errors):
    monkeypatch.setattr(support_events, 'CLEANUP_STEP_SECONDS', 0.05)

    async def run():
        fake = FakeRedis(close_errors=errors)
        fake.install(monkeypatch)
        stream = event_stream()
        await anext(stream)
        started = time.monotonic()
        await stream.aclose()
        assert time.monotonic() - started < 1.0
        assert fake.closed == ['subscription', 'client']
    with caplog.at_level(logging.WARNING, logger='shipment-bot'):
        asyncio.run(run())
    assert 'support stream' in caplog.text.lower()


def test_unexpected_cleanup_errors_are_not_suppressed(monkeypatch):
    async def run():
        fake = FakeRedis(close_errors={'subscription': RuntimeError('bug')})
        fake.install(monkeypatch)
        stream = event_stream()
        await anext(stream)
        with pytest.raises(RuntimeError, match='bug'):
            await stream.aclose()
        assert fake.closed == ['subscription', 'client']
    asyncio.run(run())


def test_shutdown_cancellation_repeated_during_cleanup_still_closes(monkeypatch):
    """Uvicorn cancels open streams natively when graceful shutdown times out."""
    monkeypatch.setattr(support_events, 'CLEANUP_STEP_SECONDS', 0.2)

    async def run():
        acknowledged = asyncio.Event()

        async def slow_acknowledgement(pubsub):
            await acknowledged.wait()
            return ACK

        fake = FakeRedis(cleanup=slow_acknowledgement)
        fake.install(monkeypatch)
        task = asyncio.create_task(_consume_in_task_group(event_stream())())
        await fake.subscribed.wait()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        started = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 1.0
        assert fake.closed == ['subscription', 'client']
    asyncio.run(run())


def test_repeated_streams_release_every_connection(monkeypatch):
    monkeypatch.setattr(support_events, 'CLEANUP_STEP_SECONDS', 0.05)

    async def run():
        errors = capture_loop_errors()
        failures = [None, RedisConnectionError('closed'), 'slow']

        def cleanup(pubsub):
            mode = failures[fake.created % 3]
            if mode is None:
                return ACK
            if mode == 'slow':
                return asyncio.Event().wait()
            raise mode

        fake = FakeRedis(cleanup=cleanup)
        fake.install(monkeypatch)
        for index in range(60):
            fake.unsubscribed = False
            fake.subscribed = asyncio.Event()
            if index % 2:
                async with anyio.create_task_group() as group:
                    group.start_soon(_consume_in_task_group(event_stream()))
                    await fake.subscribed.wait()
                    await asyncio.sleep(0)
                    group.cancel_scope.cancel()
            else:
                stream = event_stream()
                await anext(stream)
                await stream.aclose()
        assert fake.created == 60
        assert fake.closed.count('subscription') == 60 and fake.closed.count('client') == 60
        await settle()
        assert errors == []
        assert asyncio.all_tasks() == {asyncio.current_task()}
    asyncio.run(run())
