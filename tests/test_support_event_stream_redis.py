"""SSE heartbeat and cleanup against a disposable local Redis process.

Each test starts its own redis-server on a free loopback port with persistence
disabled, checks that the server it reaches is that child process, and stops it
afterwards. It never uses REDIS_URL or any shared Redis instance. Skipped when
redis-server is not installed.
"""
import asyncio
import gc
import json
import shutil
import socket
import subprocess
import time

import anyio
import pytest
import redis
import redis.asyncio as redis_asyncio

import app.support_events as support_events
from app.support_events import CHANNEL, event_stream


@pytest.fixture
def disposable_redis(tmp_path, monkeypatch):
    binary = shutil.which('redis-server')
    if not binary:
        pytest.skip('redis-server is not installed')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [binary, '--port', str(port), '--bind', '127.0.0.1', '--save', '', '--appendonly', 'no',
         '--dir', str(tmp_path), '--protected-mode', 'yes'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f'redis://127.0.0.1:{port}/0'
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                info = redis.Redis.from_url(url).info('server')
                break
            except redis.exceptions.ConnectionError:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise
                time.sleep(0.05)
        assert int(info['process_id']) == process.pid, 'connected to an unexpected Redis server'
        monkeypatch.setattr(support_events.settings, 'redis_url', url)
        yield url, process
    finally:
        process.kill()
        process.wait(5)


async def _client_count(control):
    return len([client for client in await control.client_list() if client.get('cmd') != 'client|list'])


def _loop_errors():
    errors = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
    return errors


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0.01)
    gc.collect()
    await asyncio.sleep(0.01)


def test_flood_of_discarded_events_still_produces_heartbeats(disposable_redis):
    url, _process = disposable_redis

    async def run():
        control = redis_asyncio.from_url(url, decode_responses=True)
        stop = asyncio.Event()

        async def flood():
            while not stop.is_set():
                await control.publish(CHANNEL, json.dumps({'type': 'ticket_updated', 'ticket_id': 99}))
                await asyncio.sleep(0.02)

        async def authorize(payload):
            return False

        stream = event_stream(authorize, heartbeat_seconds=0.3)
        assert await anext(stream) == 'retry: 3000\n\n'
        publisher = asyncio.create_task(flood())
        chunks, started = [], time.monotonic()
        while time.monotonic() - started < 1.0:
            chunks.append(await asyncio.wait_for(anext(stream), 1))
        stop.set()
        await publisher
        await stream.aclose()
        await control.aclose()
        assert chunks and set(chunks) == {': keepalive\n\n'}
        assert len(chunks) >= 2
    asyncio.run(run())


def test_disconnects_and_shutdown_cancellation_leave_no_subscription_or_connection(disposable_redis):
    url, _process = disposable_redis

    async def run():
        errors = _loop_errors()
        control = redis_asyncio.from_url(url, decode_responses=True)
        baseline = await _client_count(control)

        async def consume(stream):
            async for _chunk in stream:
                pass

        for cycle in range(30):
            stream = event_stream()
            if cycle % 2:
                # Client disconnect: Starlette cancels through an anyio cancel scope.
                async with anyio.create_task_group() as group:
                    group.start_soon(consume, stream)
                    while (await control.pubsub_numsub(CHANNEL))[0][1] == 0:
                        await asyncio.sleep(0.005)
                    group.cancel_scope.cancel()
            else:
                # Shutdown timeout: Uvicorn cancels the request task directly.
                task = asyncio.create_task(consume(stream))
                while (await control.pubsub_numsub(CHANNEL))[0][1] == 0:
                    await asyncio.sleep(0.005)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert (await control.pubsub_numsub(CHANNEL))[0][1] == 0
        await _settle()
        assert await _client_count(control) == baseline
        await control.aclose()
        assert errors == []
    asyncio.run(run())


def test_redis_outage_ends_the_stream_promptly_and_cleanly(disposable_redis):
    url, process = disposable_redis

    async def run():
        errors = _loop_errors()
        stream = event_stream(heartbeat_seconds=0.2)
        assert await anext(stream) == 'retry: 3000\n\n'
        assert await asyncio.wait_for(anext(stream), 2) == ': keepalive\n\n'
        process.kill()
        process.wait(5)
        started = time.monotonic()
        with pytest.raises(redis.exceptions.ConnectionError):
            while True:
                await asyncio.wait_for(anext(stream), 10)
        # Cleanup against a dead server is bounded by the per-step limit.
        assert time.monotonic() - started < 6
        await _settle()
        assert errors == []
    asyncio.run(run())
