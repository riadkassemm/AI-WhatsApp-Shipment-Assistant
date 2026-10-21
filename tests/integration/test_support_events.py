import asyncio
import json

import pytest

from app.support_api import support_events
from app.support_auth import SupportAgent
from app.support_events import CHANNEL
import app.support_auth as auth


async def session(sql):
    await sql("""INSERT INTO staff_sessions (id,staff_id,token_hash,csrf_hash,expires_at,created_at,last_seen_at)
                 VALUES (1,8,REPEAT('a',64),REPEAT('b',64),UTC_TIMESTAMP(6)+INTERVAL 1 HOUR,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""")
    response = await support_events(SupportAgent(id='8', name='Other', role='supervisor', session_id=1))
    stream = response.body_iterator
    assert await stream.__anext__() == 'retry: 3000\n\n'
    return stream


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    "UPDATE staff_sessions SET revoked_at=UTC_TIMESTAMP(6)",
    "UPDATE staff_sessions SET expires_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND",
    "UPDATE staff SET is_active=0 WHERE id=8",
    "UPDATE staff SET role='invalid' WHERE id=8",
])
@pytest.mark.parametrize('protected_event', [False, True])
async def test_live_stream_revocation_and_idle_cleanup(infrastructure, mutation, protected_event):
    _, sql, redis = infrastructure
    stream = await session(sql)
    assert (await redis.pubsub_numsub(CHANNEL))[0][1] == 1
    await sql(mutation)
    if protected_event:
        await redis.publish(CHANNEL, json.dumps({'type':'outbound_message','ticket_id':42,'body':'private'}))
    with pytest.raises(StopAsyncIteration):
        # The subscription acknowledgement also exercises an idle iteration;
        # no sleeps or dependence on a heartbeat timer.
        await asyncio.wait_for(stream.__anext__(), 3)
    assert (await redis.pubsub_numsub(CHANNEL))[0][1] == 0


@pytest.mark.asyncio
async def test_current_role_and_ticket_access_used(infrastructure):
    _, sql, redis = infrastructure
    stream = await session(sql)
    await sql("UPDATE staff SET role='agent' WHERE id=8")
    await redis.publish(CHANNEL, json.dumps({'type':'ticket_updated','ticket_id':42,'secret':'must not leak'}))
    await redis.publish(CHANNEL, json.dumps({'type':'ticket_updated','ticket_id':43,'marker':'owned'}))
    async def next_data():
        while True:
            chunk = await stream.__anext__()
            if chunk.startswith('data:'): return chunk
    chunk = await asyncio.wait_for(next_data(), 3)
    assert 'owned' in chunk and 'must not leak' not in chunk
    # Resolved history remains shared by policy.
    await sql("UPDATE support_tickets SET status='RESOLVED',active_key=NULL WHERE id=42")
    await redis.publish(CHANNEL, json.dumps({'type':'ticket_updated','ticket_id':42,'marker':'history'}))
    assert 'history' in await asyncio.wait_for(next_data(), 3)
    await stream.aclose()
    assert (await redis.pubsub_numsub(CHANNEL))[0][1] == 0


@pytest.mark.asyncio
async def test_validation_failure_terminates_without_payload(infrastructure, monkeypatch):
    _, sql, redis = infrastructure
    stream = await session(sql)
    async def fail(*args): raise RuntimeError('database unavailable')
    monkeypatch.setattr(auth.staff_repository, 'get_active_session_by_id', fail)
    await redis.publish(CHANNEL, json.dumps({'type':'ticket_updated','ticket_id':42}))
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), 3)
    assert (await redis.pubsub_numsub(CHANNEL))[0][1] == 0
