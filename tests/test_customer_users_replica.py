from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import customer_auth
from app.config import Settings, settings
from app.conversation_store import Conversation
from app.customer_directory import CustomerDirectory
from app.customer_users import (
    CustomerUsersRepository,
    customer_connection_options,
    customer_identity_id,
    customer_users,
)
from app.shipment_client import ShipmentClient, ShipmentDBError


@pytest.fixture
def replica(monkeypatch):
    for key, value in {
        "customer_users_db_host": "replica.invalid",
        "customer_users_db_port": 3310,
        "customer_users_db_name": "replica_test",
        "customer_users_db_user": "users_reader",
        "customer_users_db_password": "test-only-replica-secret",
    }.items():
        monkeypatch.setattr(settings, key, value)


def test_replica_configuration_is_complete_and_credentials_are_separate(replica):
    options = customer_connection_options()
    assert (options["host"], options["port"], options["db"], options["user"]) == (
        "replica.invalid", 3310, "replica_test", "users_reader",
    )
    assert options["password"] == "test-only-replica-secret"
    assert "test-only-replica-secret" not in repr(settings)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, customer_users_db_host="replica.invalid")


def test_default_source_keeps_existing_database_options():
    options = customer_connection_options()
    assert options["db"] == settings.db_name
    assert options["port"] == settings.db_port
    assert options["user"] == settings.db_user
    assert customer_identity_id(7) == "7"


@pytest.mark.parametrize("stored_password", ["test-password", "$2y$test-hash"])
def test_replica_authentication_never_migrates_passwords(monkeypatch, replica, stored_password):
    calls = []
    connection_options = []

    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, query, params):
            assert query.lstrip().startswith("SELECT")
            calls.append((query, params))

        async def fetchone(self):
            return {"id": 7, "userid": "live-user", "name": "Live Name",
                    "password": stored_password, "status": "1"}

    class Connection:
        closed = False

        def cursor(self, *_args):
            return Cursor()

        def close(self):
            self.closed = True

    connection = Connection()

    async def connect(**kwargs):
        connection_options.append(kwargs)
        return connection

    def checkpw(password, hashed):
        assert (password, hashed) == (b"test-password", b"$2b$test-hash")
        return True

    def forbid_hash(*_args, **_kwargs):
        pytest.fail("Replica authentication must not attempt password migration")

    monkeypatch.setattr(customer_auth, "aiomysql", SimpleNamespace(connect=connect, DictCursor=object))
    monkeypatch.setattr(customer_auth, "bcrypt", SimpleNamespace(checkpw=checkpw, hashpw=forbid_hash))
    customer = asyncio.run(customer_auth.authenticate_customer("live-user", "test-password"))
    assert customer.customer_id == "replica:7"
    assert customer.name == "Live Name"
    assert len(calls) == 1
    assert connection_options == [customer_connection_options()]
    assert connection.closed


@pytest.mark.parametrize("status,password,found", [("0", "test-password", True),
                                                       ("1", "wrong", True),
                                                       ("1", "test-password", False)])
def test_replica_rejects_disabled_missing_and_invalid_accounts(monkeypatch, replica, status, password, found):
    class Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, query, params):
            assert query.lstrip().startswith("SELECT")

        async def fetchone(self):
            return ({"id": 7, "userid": "live-user", "name": "Live Name",
                     "password": "test-password", "status": status} if found else None)

    async def connect():
        return SimpleNamespace(cursor=lambda *_args: Cursor(), close=lambda: None)

    monkeypatch.setattr(customer_auth, "_get_db_connection", connect)
    assert asyncio.run(customer_auth.authenticate_customer("live-user", password)) is None


def test_replica_auth_outage_is_unavailable_without_fallback(monkeypatch, replica, caplog):
    async def connect():
        raise RuntimeError("sensitive backend detail")

    monkeypatch.setattr(customer_auth, "_get_db_connection", connect)
    with pytest.raises(customer_auth.CustomerAuthenticationUnavailable):
        asyncio.run(customer_auth.authenticate_customer("live-user", "test-password"))
    assert "sensitive backend detail" not in caplog.text
    assert "test-password" not in caplog.text


def test_directory_reads_replica_and_keeps_saved_names_local(monkeypatch, replica):
    directory = CustomerDirectory()
    reads = []
    writes = []

    async def replica_rows(query, params):
        assert "FROM users" in query
        reads.append(params)
        return [{"id": 7, "userid": "live-user", "name": "Live Name", "mobile": "15550000001"}]

    async def local_one(query, params):
        assert "FROM users" not in query
        if "FROM support_contacts" in query:
            return {"id": 2, "display_name": "Saved Name"}
        return None

    async def local_write(query, params):
        assert "INSERT INTO support_contacts" in query
        writes.append(params)
        return 1

    async def stale_rows(*_args):
        pytest.fail("Must not read stale local users")

    monkeypatch.setattr(customer_users, "fetch_all", replica_rows)
    monkeypatch.setattr(directory, "_fetchone", local_one)
    monkeypatch.setattr(directory, "_fetchall", stale_rows)
    monkeypatch.setattr(directory, "_execute", local_write)
    identity = asyncio.run(directory.save_contact(phone="+15550000001", display_name="Saved Name", staff_id="1"))
    assert (identity.user_id, identity.userid, identity.name, identity.account_name) == (
        "replica:7", "live-user", "Saved Name", "Live Name",
    )
    assert len(reads) == len(writes) == 1


def test_directory_replica_outage_does_not_use_local_users(monkeypatch, replica):
    directory = CustomerDirectory()

    async def unavailable(*_args):
        raise ConnectionError("replica unavailable")

    async def no_contact(*_args):
        return None

    monkeypatch.setattr(customer_users, "fetch_all", unavailable)
    monkeypatch.setattr(directory, "_fetchone", no_contact)
    with pytest.raises(ConnectionError):
        asyncio.run(directory.resolve("15550000001"))


@pytest.mark.parametrize("replica_enabled,stored_id,expected", [
    (True, "7", None), (True, "replica:7", "replica:7"),
    (False, "replica:7", None), (False, "7", "7"),
])
def test_session_cutover_requires_fresh_login(monkeypatch, replica_enabled, stored_id, expected):
    monkeypatch.setattr(settings, "customer_users_db_host", "replica.invalid" if replica_enabled else "")
    original = Conversation(phone_number="15550000001", verified_customer_id=stored_id,
                            authenticated_userid="old-user", active_ticket_id="41",
                            pending_tool_name="get_customer_balance")
    restored = Conversation.from_json(original.to_json())
    assert restored.verified_customer_id == expected
    assert restored.active_ticket_id == "41"
    if expected is None:
        assert restored.authenticated_userid is None
        assert restored.pending_tool_name is None
        assert restored.auth_state == "guest"


def test_balance_maps_uuid_instead_of_reusing_colliding_numeric_id(monkeypatch, replica):
    client = ShipmentClient()

    async def live_user(identity):
        assert identity == "replica:7"
        return {"id": 7, "uuid": "live-uuid", "userid": "live-user", "name": "Live Name"}

    async def local_rows(query, params):
        assert "WHERE uuid = %s" in query and params == ("live-uuid",)
        return [{"id": 42}]

    async def wallet(query, params):
        assert "FROM user_wallet" in query and params == ("42",)
        return {"ballance": 12, "ballancet": "USD"}

    monkeypatch.setattr(customer_users, "get_by_identity", live_user)
    monkeypatch.setattr(client, "_fetchall", local_rows)
    monkeypatch.setattr(client, "_fetchone", wallet)
    result = asyncio.run(client.get_customer_balance("replica:7"))
    assert result == {"customer_id": "replica:7", "userid": "live-user", "name": "Live Name",
                      "balance": 12, "balance_type": "USD"}


@pytest.mark.parametrize("matches", [[], [{"id": 7}, {"id": 8}]])
def test_unmapped_or_ambiguous_uuid_never_reads_wallet(monkeypatch, replica, matches):
    client = ShipmentClient()

    async def live_user(_identity):
        return {"id": 7, "uuid": "different-uuid", "userid": "live-user", "name": "Live Name"}

    async def local_rows(query, params):
        assert params == ("different-uuid",)
        return matches

    async def forbid_wallet(*_args):
        pytest.fail("Must not reuse numeric ID after UUID mapping fails")

    monkeypatch.setattr(customer_users, "get_by_identity", live_user)
    monkeypatch.setattr(client, "_fetchall", local_rows)
    monkeypatch.setattr(client, "_fetchone", forbid_wallet)
    with pytest.raises(ShipmentDBError, match="Historical shipment data is unavailable"):
        asyncio.run(client.get_customer_balance("replica:7"))


def test_shipment_reads_and_writes_use_mapped_owner(monkeypatch, replica):
    client = ShipmentClient()
    calls = []

    async def live_user(_identity):
        return {"id": 7, "uuid": "live-uuid"}

    async def local_rows(query, params):
        if "WHERE uuid = %s" in query:
            return [{"id": 42}]
        assert "FROM orders_new" in query and params == ("42",)
        calls.append("list")
        return []

    async def local_one(query, params):
        assert "FROM orders_new" in query
        assert params[0] == "42"
        if len(params) == 4:
            calls.append("track")
            return None
        calls.append("mode-read")
        return {"id": 1, "trackid": "TRACK", "pod": "1", "customer_id": 42}

    async def local_write(query, params):
        assert "UPDATE orders_new" in query and params == ("2", "42", "TRACK")
        calls.append("mode-write")
        return 1

    monkeypatch.setattr(customer_users, "get_by_identity", live_user)
    monkeypatch.setattr(client, "_fetchall", local_rows)
    monkeypatch.setattr(client, "_fetchone", local_one)
    monkeypatch.setattr(client, "_execute", local_write)

    async def run():
        await client.track_shipment("replica:7", "TRACK")
        await client.get_customer_shipments("replica:7")
        result = await client.update_shipment_mode("replica:7", "TRACK", "delivery")
        assert result["updated"] is True

    asyncio.run(run())
    assert calls == ["track", "list", "mode-read", "mode-write"]


def test_old_customer_ids_cannot_query_the_new_source(monkeypatch, replica):
    repository = CustomerUsersRepository()

    async def forbid_query(*_args):
        pytest.fail("Old identity must be rejected before opening a connection")

    monkeypatch.setattr(repository, "fetch_all", forbid_query)
    assert asyncio.run(repository.get_by_identity("7")) is None
    with pytest.raises(ShipmentDBError, match="sign in again"):
        asyncio.run(ShipmentClient().track_shipment("7", "TRACK"))
