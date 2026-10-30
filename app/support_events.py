from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import AsyncIterator, Any

import anyio

from app.config import settings

logger = logging.getLogger("shipment-bot")
CHANNEL = "support:events:v1"
# Comfortably inside the proxy's 60-second read timeout.
HEARTBEAT_SECONDS = 15.0
# Upper bound for each cleanup step (unsubscribe confirmation, each close).
CLEANUP_STEP_SECONDS = 1.0


async def publish_support_event(event_type: str, **payload: Any) -> None:
    client = None
    try:
        import redis.asyncio as redis

        data = {"type": event_type, **payload}
        client = redis.from_url(
            settings.redis_url, decode_responses=True,
            socket_connect_timeout=0.5, socket_timeout=0.5,
        )
        await asyncio.wait_for(
            client.publish(CHANNEL, json.dumps(data, ensure_ascii=False, default=str)),
            timeout=0.75,
        )
    except Exception:
        logger.warning("Could not publish support realtime event: type=%s", event_type)
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), timeout=0.5)
            except Exception:
                logger.warning("Could not close support realtime publisher")


EventAuthorizer = Callable[[dict[str, Any]], Awaitable[bool]]


async def event_stream(
    authorize: EventAuthorizer | None = None,
    *, validate: Callable[[], Awaitable[None]] | None = None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    clock: Callable[[], float] | None = None,
) -> AsyncIterator[str]:
    """Stream support events after applying per-session authorization.

    Redis is an internal fan-out bus, not an authorization boundary. Ticket/chat
    events must be checked for each connected staff session before any payload is
    written to that browser's SSE response.
    """
    import redis.asyncio as redis

    now = clock or asyncio.get_running_loop().time
    client = redis.from_url(settings.redis_url, decode_responses=True)
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(CHANNEL)
        if validate is not None:
            await validate()
        yield "retry: 3000\n\n"
        # The heartbeat follows the last chunk actually written. Discarded events
        # (another staff member's ticket, malformed data) are not output, so a
        # steady flow of them can no longer starve the proxy's read timeout.
        last_output = now()
        while True:
            remaining = max(0.0, heartbeat_seconds - (now() - last_output))
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining)
            if validate is not None:
                try:
                    await validate()
                except Exception:
                    logger.info("Closing support stream after session validation failed")
                    return
            chunk = None
            if message and message.get("type") == "message":
                payload = _decode_event(message)
                if payload is not None:
                    allowed = True
                    if authorize is not None:
                        try:
                            allowed = await authorize(payload)
                        except Exception:
                            # Authorization failures must fail closed. In particular,
                            # never fall back to forwarding the raw Redis payload.
                            logger.exception(
                                "Support realtime event authorization failed: type=%s",
                                payload.get("type"),
                            )
                            return
                        if allowed:
                            # The browser only plays ticket notification sounds for
                            # events explicitly approved at this delivery boundary.
                            payload["authorized"] = True
                    if allowed:
                        chunk = f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            if chunk is None and now() - last_output >= heartbeat_seconds:
                chunk = ": keepalive\n\n"
            if chunk is not None:
                yield chunk
                last_output = now()
            await asyncio.sleep(0)
    finally:
        await _close_subscription(pubsub, client)


def _decode_event(message: dict[str, Any]) -> dict[str, Any] | None:
    try:
        payload = json.loads(str(message.get("data") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Discarding malformed support realtime event")
        return None
    return payload if isinstance(payload, dict) else None


async def _close_subscription(pubsub: Any, client: Any) -> None:
    """Release one stream's subscription and Redis connection within a bound.

    This runs on normal exit, revocation, client disconnect and server shutdown.
    Starlette cancels a disconnected stream and Uvicorn cancels open streams at
    shutdown; the shield lets cleanup finish under that cancellation, while each
    step's time limit keeps cleanup from holding the cancellation open. Pending
    events are discarded here, never emitted after revocation.
    """
    from redis.exceptions import RedisError

    with anyio.CancelScope(shield=True):
        try:
            with anyio.move_on_after(CLEANUP_STEP_SECONDS) as bound:
                await pubsub.unsubscribe(CHANNEL)
                # unsubscribe() only writes the command. Wait for Redis to confirm
                # removal before declaring the subscription closed.
                while True:
                    reply = await pubsub.get_message(
                        ignore_subscribe_messages=False, timeout=CLEANUP_STEP_SECONDS,
                    )
                    if reply and reply.get("type") == "unsubscribe" and reply.get("data") == 0:
                        break
            if bound.cancelled_caught:
                logger.warning("Could not confirm support stream unsubscribe: reason=timeout")
        except (RedisError, OSError) as exc:
            # Redis may be unreachable. Closing the connection below still ends
            # the subscription on the Redis side.
            logger.warning(
                "Could not confirm support stream unsubscribe: exception_type=%s", type(exc).__name__,
            )
        finally:
            try:
                await _release("subscription", pubsub.aclose)
            finally:
                await _release("client", client.aclose)


async def _release(label: str, close: Callable[[], Awaitable[None]]) -> None:
    from redis.exceptions import RedisError

    try:
        with anyio.move_on_after(CLEANUP_STEP_SECONDS) as bound:
            await close()
        if bound.cancelled_caught:
            logger.warning("Support stream %s close timed out", label)
    except (RedisError, OSError) as exc:
        logger.warning("Could not close support stream %s: exception_type=%s", label, type(exc).__name__)
