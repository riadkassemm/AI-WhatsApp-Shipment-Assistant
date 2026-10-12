from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import AsyncIterator, Any

from app.config import settings

logger = logging.getLogger("shipment-bot")
CHANNEL = "support:events:v1"


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
) -> AsyncIterator[str]:
    """Stream support events after applying per-session authorization.

    Redis is an internal fan-out bus, not an authorization boundary. Ticket/chat
    events must be checked for each connected staff session before any payload is
    written to that browser's SSE response.
    """
    import redis.asyncio as redis

    client = redis.from_url(settings.redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe(CHANNEL)
    try:
        yield "retry: 3000\n\n"
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
            if message and message.get("type") == "message":
                try:
                    payload = json.loads(str(message.get("data") or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    logger.warning("Discarding malformed support realtime event")
                    continue
                if not isinstance(payload, dict):
                    continue
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
                        continue
                    if not allowed:
                        continue
                    # The browser only plays ticket notification sounds for events
                    # explicitly approved at this backend delivery boundary.
                    payload["authorized"] = True
                yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(0)
    finally:
        try:
            await pubsub.unsubscribe(CHANNEL)
        finally:
            await pubsub.aclose()
            await client.aclose()
