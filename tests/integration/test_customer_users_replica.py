"""Separate users source using disposable MariaDB and a SELECT-only login."""
import os
import secrets
import uuid

import aiomysql
import pytest
import pytest_asyncio

from app import customer_auth
from app.config import settings
from app.customer_directory import CustomerDirectory
from app.customer_users import CustomerUsersRepository
from app.shipment_client import ShipmentClient, ShipmentDBError


@pytest_asyncio.fixture
async def customer_databases(monkeypatch):
    if not os.environ.get("SUPPORT_INTEGRATION"):
        pytest.skip("Requires disposable MariaDB")
    options = {
        "user": os.environ.get("TEST_DB_USER", "root"),
        "password": os.environ.get("TEST_DB_PASSWORD", ""),
        "autocommit": True,
        "charset": "utf8mb4",
    }
    if os.environ.get("TEST_DB_SOCKET"):
        options["unix_socket"] = os.environ["TEST_DB_SOCKET"]
    else:
        options.update(host=os.environ.get("TEST_DB_HOST", "127.0.0.1"),
                       port=int(os.environ.get("TEST_DB_PORT", "3306")))
    suffix = uuid.uuid4().hex
    main_db, replica_db = "users_main_" + suffix, "users_replica_" + suffix
    reader = "users_" + suffix[:20]
    password = secrets.token_urlsafe(24)
    admin = await aiomysql.connect(**options)
    main_pool = None
    repository = CustomerUsersRepository()
    created_databases = []
    created_reader = False

    async def execute(query, params=()):
        async with admin.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute(query, params)
            return await cursor.fetchall()

    try:
        for database in (main_db, replica_db):
            await execute(f"CREATE DATABASE `{database}`")
            created_databases.append(database)
            await execute(f"""CREATE TABLE `{database}`.users (
                id INT PRIMARY KEY, uuid CHAR(36), userid VARCHAR(100) UNIQUE,
                password VARCHAR(100), name VARCHAR(255), mobile VARCHAR(255),
                email VARCHAR(255), status VARCHAR(10)
            )""")
        await execute(f"""INSERT INTO `{main_db}`.users VALUES
            (7,'wrong-customer','wrong-user','wrong-pass','Wrong Name','15550000009',NULL,'1'),
            (42,'correct-customer','old-code','old-pass','Old Name','15550000001',NULL,'1')""")
        await execute(f"""INSERT INTO `{replica_db}`.users VALUES
            (7,'correct-customer','live-code','test-password','Live Name','15550000001',NULL,'1'),
            (8,'new-customer','new-code','test-password','New Name','15550000002',NULL,'1')""")
        await execute(f"""CREATE TABLE `{main_db}`.user_wallet (
            id INT PRIMARY KEY, user_id INT, ballance INT, ballancet VARCHAR(10)
        )""")
        await execute(f"INSERT INTO `{main_db}`.user_wallet VALUES (1,7,999,'USD'),(2,42,12,'USD')")
        await execute(f"CREATE USER '{reader}'@'%' IDENTIFIED BY %s", (password,))
        created_reader = True
        await execute(f"GRANT SELECT ON `{replica_db}`.users TO '{reader}'@'%'")
        reader_options = {**options, "user": reader, "password": password, "db": replica_db}
        for key, value in {
            "customer_users_db_host": "127.0.0.1",
            "customer_users_db_name": replica_db,
            "customer_users_db_user": reader,
            "customer_users_db_password": password,
        }.items():
            monkeypatch.setattr(settings, key, value)
        # The fixture can use a disposable socket, unlike production TCP settings.
        monkeypatch.setattr(customer_auth, "customer_connection_options", lambda: reader_options)
        repository._pool = await aiomysql.create_pool(**reader_options, minsize=1, maxsize=2)
        main_pool = await aiomysql.create_pool(**options, db=main_db, minsize=1, maxsize=2)
        import app.customer_directory as directory_module
        import app.shipment_client as shipment_module
        monkeypatch.setattr(directory_module, "customer_users", repository)
        monkeypatch.setattr(shipment_module, "customer_users", repository)
        client = ShipmentClient()
        client._pool = main_pool
        yield execute, repository, client, main_db, replica_db
    finally:
        await repository.close()
        if main_pool is not None:
            main_pool.close()
            await main_pool.wait_closed()
        if created_reader:
            await execute(f"DROP USER '{reader}'@'%'")
        for database in created_databases:
            await execute(f"DROP DATABASE `{database}`")
        admin.close()


@pytest.mark.asyncio
async def test_live_login_and_phone_lookup_with_select_only_access(customer_databases):
    execute, repository, _client, _main, replica = customer_databases
    customer = await customer_auth.authenticate_customer("live-code", "test-password")
    assert customer.customer_id == "replica:7"
    assert customer.name == "Live Name"
    stored = await execute(f"SELECT password FROM `{replica}`.users WHERE id=7")
    assert stored[0]["password"] == "test-password"
    # Confirm that the application login really lacks write privileges.
    async with repository._pool.acquire() as connection:
        async with connection.cursor() as cursor:
            with pytest.raises(aiomysql.OperationalError) as error:
                await cursor.execute("UPDATE users SET name=name WHERE id=7")
            assert error.value.args[0] == 1142
    directory = CustomerDirectory()
    assert (await directory._registered_user("15550000001"))["name"] == "Live Name"
    await execute(f"UPDATE `{replica}`.users SET name='Updated Name',status='0' WHERE id=7")
    assert (await directory._registered_user("15550000001"))["name"] == "Updated Name"
    assert await customer_auth.authenticate_customer("live-code", "test-password") is None
    assert await customer_auth.authenticate_customer("wrong-user", "wrong-pass") is None


@pytest.mark.asyncio
async def test_wallet_uses_uuid_cross_reference_and_blocks_unmapped_users(customer_databases):
    _execute, _repository, client, _main, _replica = customer_databases
    balance = await client.get_customer_balance("replica:7")
    assert balance["balance"] == 12
    assert balance["name"] == "Live Name"
    assert balance["userid"] == "live-code"
    with pytest.raises(ShipmentDBError, match="Historical shipment data is unavailable"):
        await client.get_customer_balance("replica:8")
    with pytest.raises(ShipmentDBError, match="sign in again"):
        await client.get_customer_balance("7")
