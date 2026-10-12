"""Read-only startup checks for the database, Redis, and optional app schema.

Run from any directory with the project's Python environment. No credentials,
provider requests, migrations, or application writes are involved.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import math
import os
from pathlib import Path
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ATTEMPT_TIMEOUT_SECONDS = 5.0
RETRY_INTERVAL_SECONDS = 1.0


class NotReady(Exception):
    """Only fixed, public diagnostic text belongs in this exception."""


def load_configuration() -> Any:
    # Settings reads .env relative to cwd. Environment variables keep their
    # normal precedence; do not parse or export the file ourselves.
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.config import settings

    return settings


class _SingleConnectionPool:
    """Let the existing read-only schema guard reuse our one connection."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class _ReadOnlyRegistryRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        import aiomysql

        async with self.connection.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute(query, params)
            return list(await cursor.fetchall())


async def check_schema(connection: Any) -> None:
    from app.support_repository import SupportRepository, SupportSchemaError
    from app.whatsapp_registry import WhatsAppRegistrySchemaError, WhatsAppRegistryService

    try:
        await SupportRepository()._validate_schema(_SingleConnectionPool(connection))
    except SupportSchemaError:
        raise NotReady("support database migrations required") from None
    try:
        await WhatsAppRegistryService(_ReadOnlyRegistryRepository(connection))._ensure_schema()
    except WhatsAppRegistrySchemaError:
        raise NotReady("WhatsApp number registry migration required") from None


async def check_database(configuration: Any, *, schema: bool, timeout: float) -> None:
    import aiomysql

    # Own the connection before starting its handshake. aiomysql 0.3.2's
    # connect() factory does not close a partially connected socket when its
    # handshake is cancelled. Ownership here lets finally close that socket
    # even when a deadline cancels the check. No production pool is created.
    connection = aiomysql.Connection(
        host=configuration.db_host,
        port=configuration.db_port,
        user=configuration.db_user,
        password=configuration.db_password,
        db=configuration.db_name,
        connect_timeout=timeout,
        autocommit=True,
        charset="utf8mb4",
    )
    try:
        await connection._connect()
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1")
            row = await cursor.fetchone()
            if not row or row[0] != 1:
                raise NotReady("database unavailable")
        if schema:
            await check_schema(connection)
    finally:
        connection.close()


async def check_redis(configuration: Any, *, timeout: float) -> None:
    from redis.asyncio import Redis

    # Reserve part of this check's budget for closing its connection, including
    # the timeout path. The client owns at most one connection.
    cleanup_timeout = min(0.1, timeout / 5)
    request_timeout = timeout - cleanup_timeout
    client = Redis.from_url(
        configuration.redis_url,
        socket_connect_timeout=request_timeout,
        socket_timeout=request_timeout,
        max_connections=1,
    )
    try:
        if await asyncio.wait_for(client.ping(), timeout=request_timeout) is not True:
            raise NotReady("Redis unavailable")
    finally:
        await asyncio.wait_for(
            client.aclose(close_connection_pool=True), timeout=cleanup_timeout,
        )


async def check_once(configuration: Any, *, schema: bool, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    try:
        await asyncio.wait_for(
            check_database(configuration, schema=schema, timeout=timeout),
            timeout=timeout,
        )
    except NotReady as exc:
        return str(exc)
    except Exception:
        return "database unavailable"

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "Redis unavailable"
    try:
        await asyncio.wait_for(
            check_redis(configuration, timeout=remaining), timeout=remaining,
        )
    except Exception:
        return "Redis unavailable"
    return None


async def wait_until_ready(
    configuration: Any,
    *,
    wait: float,
    schema: bool,
    probe=None,
    clock=None,
    sleep=None,
) -> str | None:
    """Return a safe failure reason, or None when all checks pass together."""
    probe = probe or check_once
    clock = clock or time.monotonic
    sleep = sleep or asyncio.sleep
    deadline = clock() + wait
    reason = "runtime dependencies unavailable"
    while True:
        remaining = deadline - clock() if wait else ATTEMPT_TIMEOUT_SECONDS
        if remaining <= 0:
            return reason
        reason = await probe(
            configuration, schema=schema,
            timeout=min(ATTEMPT_TIMEOUT_SECONDS, remaining),
        )
        if reason is None or not wait:
            return reason
        remaining = deadline - clock()
        if remaining <= 0:
            return reason
        await sleep(min(RETRY_INTERVAL_SECONDS, remaining))


def _wait_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a nonnegative number of seconds") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("must be a finite, nonnegative number of seconds")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wait", type=_wait_seconds, default=0.0, metavar="SECONDS",
        help="Retry for at most this many seconds (default: one bounded attempt).",
    )
    parser.add_argument("--schema", action="store_true", help="Check support and WhatsApp registry migrations.")
    args = parser.parse_args(argv)
    try:
        configuration = load_configuration()
    except Exception:
        print("Runtime not ready: configuration invalid.", file=sys.stderr)
        return 1
    try:
        reason = asyncio.run(wait_until_ready(configuration, wait=args.wait, schema=args.schema))
    except KeyboardInterrupt:
        print("Runtime check interrupted.", file=sys.stderr)
        return 1
    except Exception:
        print("Runtime not ready: dependency check failed.", file=sys.stderr)
        return 1
    if reason:
        print(f"Runtime not ready: {reason}.", file=sys.stderr)
        return 1
    print("Runtime ready: database, Redis" + (", and application schema." if args.schema else "."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
