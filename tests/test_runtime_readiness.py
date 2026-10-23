from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import aiomysql
import pytest
from redis.asyncio import Redis

from app.support_repository import _SUPPORT_SCHEMA_MIGRATIONS
from app.whatsapp_registry import _REQUIRED_TABLES
from scripts import check_runtime


@pytest.fixture
def dependencies(monkeypatch):
    columns: dict[str, set[str]] = {}
    for _migration, requirements in _SUPPORT_SCHEMA_MIGRATIONS:
        for table, names in requirements.items():
            columns.setdefault(table, set()).update(names)
    state = SimpleNamespace(
        connections=[], redis_clients=[], queries=[], columns=columns,
        tables=set(_REQUIRED_TABLES),
        database_error=None, redis_error=None, database_hangs=False,
        redis_hangs=False, redis_pong=True, redis_timeout=None,
    )

    class Cursor:
        def __init__(self):
            self.query = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, query, params=()):
            self.query = query
            state.queries.append(query)

        async def fetchone(self):
            return (1,)

        async def fetchall(self):
            if "INFORMATION_SCHEMA.STATISTICS" in self.query:
                return [{"index_name": name, "column_name": column, "non_unique": 0}
                        for name, columns in {
                            "uq_support_transport_client": ["transport_client_id"],
                            "uq_support_delivery_replacement": ["replacement_for_message_id"],
                            "uq_support_scoped_client": ["ticket_id", "sender_type", "sender_scope", "client_message_id"],
                        }.items() for column in columns]
            if "INFORMATION_SCHEMA.COLUMNS" in self.query:
                return [
                    {"table_name": table, "column_name": column}
                    for table, columns in state.columns.items() for column in columns
                ]
            return [{"table_name": table} for table in state.tables]

    class Connection:
        def __init__(self, **kwargs):
            self.closed = False
            self.kwargs = kwargs
            state.connections.append(self)

        async def _connect(self):
            if state.database_hangs:
                await asyncio.Event().wait()
            if state.database_error:
                raise state.database_error

        def cursor(self, *args):
            return Cursor()

        def close(self):
            self.closed = True

    class RedisClient:
        def __init__(self, *args, **kwargs):
            self.closed = False
            state.redis_timeout = kwargs["socket_timeout"]
            state.redis_clients.append(self)

        async def ping(self):
            if state.redis_hangs:
                await asyncio.Event().wait()
            if state.redis_error:
                raise state.redis_error
            return state.redis_pong

        async def aclose(self, *, close_connection_pool):
            assert close_connection_pool is True
            self.closed = True

    monkeypatch.setattr(aiomysql, "Connection", Connection, raising=False)
    monkeypatch.setattr(Redis, "from_url", RedisClient)
    configuration = SimpleNamespace(
        db_host="localhost", db_port=3307, db_name="runtime_database",
        db_user="private_user", db_password="PRIVATE_PASSWORD",
        redis_url="redis://private_user:PRIVATE_PASSWORD@localhost:6379/0",
    )
    monkeypatch.setattr(check_runtime, "load_configuration", lambda: configuration)
    return configuration, state


def run_check(configuration, *, schema=False, timeout=0.5):
    return asyncio.run(check_runtime.check_once(configuration, schema=schema, timeout=timeout))


def test_ready_checks_schema_read_only_with_one_database_connection(dependencies):
    configuration, state = dependencies
    assert run_check(configuration, schema=True) is None
    assert len(state.connections) == len(state.redis_clients) == 1
    assert state.connections[0].closed and state.redis_clients[0].closed
    assert state.queries[0] == "SELECT 1"
    assert len(state.queries) == 6
    assert all(query.lstrip().upper().startswith("SELECT ") for query in state.queries)


def test_database_failure_never_reports_ready_and_closes_connection(dependencies):
    configuration, state = dependencies
    state.database_error = RuntimeError("PRIVATE_PASSWORD SQL metadata private_user")
    assert run_check(configuration) == "database unavailable"
    assert state.connections[0].closed
    assert not state.redis_clients


@pytest.mark.parametrize("failure", ["exception", "false_pong"])
def test_redis_failure_never_reports_ready_and_closes_all_clients(dependencies, failure):
    configuration, state = dependencies
    if failure == "exception":
        state.redis_error = RuntimeError("redis://private_user:PRIVATE_PASSWORD@localhost")
    else:
        state.redis_pong = False
    assert run_check(configuration) == "Redis unavailable"
    assert state.connections[0].closed and state.redis_clients[0].closed


def test_missing_support_migration_prevents_startup(dependencies):
    configuration, state = dependencies
    state.columns["support_ticket_messages"].remove("whatsapp_change_timestamp")
    assert run_check(configuration, schema=True) == "support database migrations required"
    assert state.connections[0].closed
    assert not state.redis_clients


def test_missing_registry_migration_prevents_startup(dependencies):
    configuration, state = dependencies
    state.tables.remove("whatsapp_integration_audit")
    assert run_check(configuration, schema=True) == "WhatsApp number registry migration required"
    assert state.connections[0].closed
    assert not state.redis_clients


@pytest.mark.parametrize("dependency", ["database", "redis"])
def test_hanging_dependency_obeys_deadline_and_cleanup(dependencies, dependency):
    configuration, state = dependencies
    setattr(state, f"{dependency}_hangs", True)
    started = time.monotonic()
    reason = run_check(configuration, timeout=0.04)
    assert reason == ("database unavailable" if dependency == "database" else "Redis unavailable")
    assert time.monotonic() - started < 0.2
    assert state.connections[0].closed
    assert all(client.closed for client in state.redis_clients)


def test_retry_budget_limits_last_attempt_and_sleeps_without_real_waiting(dependencies):
    configuration, _state = dependencies
    elapsed = 0.0
    timeouts = []

    async def probe(configuration, *, schema, timeout):
        nonlocal elapsed
        timeouts.append(timeout)
        elapsed += min(1.2, timeout)
        return "database unavailable"

    async def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    reason = asyncio.run(check_runtime.wait_until_ready(
        configuration, wait=2.4, schema=True,
        probe=probe, clock=lambda: elapsed, sleep=sleep,
    ))
    assert reason == "database unavailable"
    assert elapsed == pytest.approx(2.4)
    assert timeouts == pytest.approx([2.4, 0.2])


def test_recovery_requires_all_dependencies_and_stops_retrying(dependencies):
    configuration, state = dependencies
    state.database_error = ConnectionError("PRIVATE_PASSWORD")
    elapsed = 0.0

    async def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds
        state.database_error = None

    assert asyncio.run(check_runtime.wait_until_ready(
        configuration, wait=20, schema=True, clock=lambda: elapsed, sleep=sleep,
    )) is None
    assert len(state.connections) == 2 and len(state.redis_clients) == 1
    assert all(connection.closed for connection in state.connections)
    assert elapsed == 1


def test_cli_failure_diagnostics_never_expose_secrets(dependencies, capsys):
    _configuration, state = dependencies
    state.redis_error = RuntimeError("redis://private_user:PRIVATE_PASSWORD@localhost SELECT secret_table")
    assert check_runtime.main(["--wait", "0", "--schema"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Runtime not ready: Redis unavailable.\n"


def test_cli_configuration_failure_diagnostics_never_expose_secrets(monkeypatch, capsys):
    def invalid_configuration():
        raise ValueError("input_value='PRIVATE_PASSWORD'")

    monkeypatch.setattr(check_runtime, "load_configuration", invalid_configuration)
    assert check_runtime.main([]) == 1
    assert capsys.readouterr().err == "Runtime not ready: configuration invalid.\n"


def test_configuration_loads_root_env_from_other_cwd_and_preserves_env_precedence(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("DB_NAME=runtime_env_database\nDB_PORT=3307\n", encoding="utf-8")
    unrelated = tmp_path / "other"
    unrelated.mkdir()
    (unrelated / ".env").write_text("DB_NAME=wrong_database\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("DB_NAME", None)
    environment["DB_PORT"] = "3311"
    code = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from scripts import check_runtime
check_runtime.ROOT = Path(sys.argv[2])
configuration = check_runtime.load_configuration()
assert Path.cwd() == check_runtime.ROOT
assert configuration.db_name == 'runtime_env_database'
assert configuration.db_port == 3311
"""
    subprocess.run(
        [sys.executable, "-c", code, str(check_runtime.ROOT), str(project)],
        cwd=unrelated, env=environment, check=True, capture_output=True, text=True, timeout=10,
    )


@pytest.mark.parametrize("value", ["-1", "inf", "nan", "invalid"])
def test_cli_rejects_invalid_retry_budgets(value):
    with pytest.raises(SystemExit) as exc:
        check_runtime.main(["--wait", value])
    assert exc.value.code == 2
