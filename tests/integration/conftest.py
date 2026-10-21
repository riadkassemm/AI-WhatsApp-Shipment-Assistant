"""Real dependencies only. Run separately from legacy unit modules that install stubs."""
import os
import uuid
from pathlib import Path

from pymysql.constants import CLIENT
import aiomysql
import pytest
import pytest_asyncio
import redis.asyncio as redis


@pytest_asyncio.fixture
async def infrastructure(monkeypatch):
    if not os.environ.get('SUPPORT_INTEGRATION'):
        pytest.skip('Set SUPPORT_INTEGRATION=1 and use disposable MariaDB/Redis')
    import httpx
    original_send = httpx.AsyncClient.send
    async def guarded_send(client, *args, **kwargs):
        assert isinstance(client._transport, httpx.MockTransport), 'External HTTP is disabled in integration tests'
        return await original_send(client, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, 'send', guarded_send)
    from app.support_repository import SupportRepository
    from app.staff_repository import StaffRepository
    from app.config import settings
    import app.support_service as service
    import app.support_auth as auth
    import app.support_api as api
    assert aiomysql.__file__ and redis.__file__, 'Integration requires installed dependencies'
    db = 'support_test_' + uuid.uuid4().hex
    monkeypatch.setattr(settings, 'db_name', db)
    options = dict(user=os.environ.get('TEST_DB_USER', 'root'), password=os.environ.get('TEST_DB_PASSWORD', ''),
                   autocommit=True, charset='utf8mb4', client_flag=CLIENT.MULTI_STATEMENTS)
    if os.environ.get('TEST_DB_SOCKET'):
        options['unix_socket'] = os.environ['TEST_DB_SOCKET']
    else:
        options.update(host=os.environ.get('TEST_DB_HOST', '127.0.0.1'), port=int(os.environ.get('TEST_DB_PORT', '3306')))
    admin = await aiomysql.connect(**options)
    pool = None
    redis_url = os.environ['TEST_REDIS_URL']
    monkeypatch.setattr(settings, 'redis_url', redis_url)
    client = redis.from_url(redis_url, decode_responses=True)
    try:
        async with admin.cursor() as cur:
            await cur.execute(f'CREATE DATABASE `{db}`')
        pool = await aiomysql.create_pool(db=db, minsize=1, maxsize=8, **options)
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for path in sorted(Path('migrations').glob('*.sql')):
                    await cur.execute(path.read_text())
                    while await cur.nextset():
                        pass
        repository = SupportRepository()
        repository._pool = pool
        await repository._validate_schema(pool)
        staff = StaffRepository()
        staff._pool = pool
        monkeypatch.setattr(service, 'support_repository', repository)
        monkeypatch.setattr(auth, 'staff_repository', staff)
        async def sql(statement, args=()):
            async with pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(statement, args)
                    return await cur.fetchall()
        await sql("""INSERT INTO staff (id,name,email,email_normalized,password_hash,role,is_active,created_at,updated_at)
                     VALUES (7,'Agent','a@example.test','a@example.test','unused','agent',1,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)),
                            (8,'Other','b@example.test','b@example.test','unused','supervisor',1,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""")
        for ticket_id, owner in [(41, None), (42, '7'), (43, '8')]:
            await sql("""INSERT INTO support_tickets (id,ticket_reference,active_key,whatsapp_phone,reason,ai_summary,status,
                      current_assignee_id,business_phone_number_id,created_at,updated_at,last_activity_at)
                      VALUES (%s,%s,%s,%s,'test','test',%s,%s,'90005',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))""",
                      (ticket_id, f'TEST-{ticket_id}', f'90005:{ticket_id}', f'1555000{ticket_id}', 'ASSIGNED' if owner else 'NEW', owner))
        await client.ping()
        yield repository, sql, client
    finally:
        await client.aclose()
        if pool:
            pool.close()
            await pool.wait_closed()
        async with admin.cursor() as cur:
            await cur.execute(f'DROP DATABASE IF EXISTS `{db}`')
        admin.close()
