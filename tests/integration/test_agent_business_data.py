"""Execute the adapter's real SQL against disposable business views only."""
import os

import aiomysql
import pytest

from app.agent.business_data import BusinessDataError, ReplicaBusinessDataSource
from app.customer_ai_identity import AIIdentity
from app.customer_users import customer_users


@pytest.mark.asyncio
async def test_owned_read_and_freshness_on_real_mariadb(infrastructure, monkeypatch):
    repository, sql, _ = infrastructure
    await sql("CREATE TABLE ai_data_freshness (dataset VARCHAR(30) PRIMARY KEY, as_of DATETIME(6))")
    await sql("CREATE TABLE ai_orders (customer_uuid VARCHAR(50), order_id VARCHAR(50), tracking_number VARCHAR(50), shipment_id VARCHAR(50), status VARCHAR(50), created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    await sql("CREATE TABLE ai_wallets (customer_uuid VARCHAR(50), balance DECIMAL(12,2), currency VARCHAR(3))")
    await sql("INSERT INTO ai_data_freshness VALUES ('orders', UTC_TIMESTAMP(6)), ('wallet', UTC_TIMESTAMP(6))")
    await sql("INSERT INTO ai_orders (customer_uuid,order_id,tracking_number,shipment_id,status) VALUES ('owner-a','ORDER-A','TRACK-A',NULL,'Transit'), ('owner-b','ORDER-B','TRACK-B',NULL,'Delivered')")
    await sql("INSERT INTO ai_wallets VALUES ('owner-a',12.30,'USD'), ('owner-b',900.00,'USD')")
    # A separate session configured exactly like production is read-only even
    # though the test administrator itself has write access to this scratch DB.
    from app.config import settings
    options = dict(db=settings.db_name, user=os.environ.get('TEST_DB_USER', 'root'),
                   password=os.environ.get('TEST_DB_PASSWORD', ''), autocommit=False,
                   init_command='SET SESSION TRANSACTION READ ONLY')
    if os.environ.get('TEST_DB_SOCKET'):
        options['unix_socket'] = os.environ['TEST_DB_SOCKET']
    else:
        options.update(host=os.environ.get('TEST_DB_HOST', '127.0.0.1'), port=int(os.environ.get('TEST_DB_PORT', '3306')))
    source = ReplicaBusinessDataSource()
    source._pool = await aiomysql.create_pool(minsize=1, maxsize=2, **options)

    async def account(customer_id):
        assert customer_id == 'replica:1'
        return {'uuid': 'owner-a'}
    monkeypatch.setattr(customer_users, 'get_by_identity', account)
    l1 = AIIdentity(level='L1', reason='TEST', customer_id='replica:1')
    l2 = AIIdentity(level='L2', reason='TEST', customer_id='replica:1')
    try:
        result = await source.read('get_customer_shipments', {}, l1)
        assert [item['order_id'] for item in result.payload['shipments']] == ['ORDER-A']
        with pytest.raises(BusinessDataError, match='BUSINESS_DATA_UNAVAILABLE'):
            await source.read('track_shipment', {'reference': 'TRACK-B'}, l1)
        wallet = await source.read('get_customer_balance', {}, l2)
        assert wallet.payload == {'balance': '12.30', 'currency': 'USD'}
        with pytest.raises(BusinessDataError, match='IDENTITY_VERIFICATION_REQUIRED'):
            await source.read('get_customer_balance', {}, l1)
        async with source._pool.acquire() as conn:
            try:
                await conn.begin()
                async with conn.cursor() as cur:
                    with pytest.raises(aiomysql.OperationalError) as error:
                        await cur.execute("UPDATE ai_wallets SET balance=0")
                    assert error.value.args[0] == 1792
            finally:
                await conn.rollback()
        await sql("UPDATE ai_data_freshness SET as_of = UTC_TIMESTAMP() - INTERVAL 1 DAY")
        with pytest.raises(BusinessDataError, match='BUSINESS_DATA_STALE'):
            await source.read('get_customer_shipments', {}, l1)
        await sql("DELETE FROM ai_data_freshness")
        with pytest.raises(BusinessDataError, match='BUSINESS_DATA_STALE'):
            await source.read('get_customer_shipments', {}, l1)
    finally:
        await source.close()
