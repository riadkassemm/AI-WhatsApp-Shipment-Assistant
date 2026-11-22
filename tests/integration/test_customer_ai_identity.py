"""AI identity levels (Step 4a, plan B4) against real MariaDB with synthetic accounts."""
import pytest
import pytest_asyncio

from app.customer_ai_identity import AIIdentityError, CustomerAIIdentityRepository, CustomerAIIdentityService
from app.support_auth import SupportAgent
from app.support_repository import SupportNotFoundError
from app.support_service import SupportValidationError


AGENT = SupportAgent(id='7', name='Agent', role='agent')
OTHER = SupportAgent(id='9', name='Someone', role='agent')
ADMIN = SupportAgent(id='8', name='Other', role='supervisor')
PHONE = '15550004201'


@pytest_asyncio.fixture
async def identity(infrastructure, monkeypatch):
    import app.support_service as service
    repo, sql, _ = infrastructure
    await sql("""CREATE TABLE users (id INT PRIMARY KEY, userid VARCHAR(100) UNIQUE, name VARCHAR(255),
                 mobile VARCHAR(255), status VARCHAR(10))""")

    async def fetch_users(query, params):
        return list(await sql(query, params))

    storage = CustomerAIIdentityRepository()
    storage._pool = repo._pool
    identity_service = CustomerAIIdentityService(storage, fetch_users=fetch_users)
    monkeypatch.setattr(service, 'customer_ai_identity_service', identity_service)
    # Ticket 42 (fixture) is assigned to agent 7; give it the customer's number.
    await sql("UPDATE support_tickets SET whatsapp_phone=%s WHERE id=42", (PHONE,))

    async def user(user_id, userid, mobile, status='1'):
        await sql("INSERT INTO users VALUES (%s,%s,%s,%s,%s)", (user_id, userid, f'Name {user_id}', mobile, status))

    return identity_service, sql, user, service.support_service


@pytest.mark.asyncio
async def test_levels_l0_and_l1_are_computed_from_accounts(identity):
    svc, sql, user, _ = identity
    assert (await svc.resolve(PHONE)).reason == 'NO_MATCH'
    # A suffix-only match is never enough.
    await user(1, 'suffix', '04201')
    assert (await svc.resolve(PHONE)).level == 'L0'
    await user(2, 'exact', '+1 555 000 4201', status='0')
    assert (await svc.resolve(PHONE)).reason == 'INACTIVE_ACCOUNT'
    await sql("UPDATE users SET status='1' WHERE id=2")
    found = await svc.resolve(PHONE)
    assert (found.level, found.reason, found.customer_userid) == ('L1', 'PHONE_MATCH', 'exact')
    assert found.allows('tracking') and found.allows('orders') and not found.allows('wallet')
    assert not found.allows('something_new')
    await user(3, 'second', '0015550004201')
    assert (await svc.resolve(PHONE)).reason == 'SHARED_NUMBER'


@pytest.mark.asyncio
async def test_staff_verification_gives_l2_and_is_audited(identity):
    svc, sql, user, support = identity
    await user(5, 'cust5', PHONE)
    with pytest.raises(SupportValidationError, match='Confirm'):
        await support.verify_ai_identity(42, AGENT, userid='cust5', confirmed=False)
    # Staff who cannot see the conversation cannot learn it exists.
    with pytest.raises(SupportNotFoundError):
        await support.verify_ai_identity(42, OTHER, userid='cust5', confirmed=True)
    result = await support.verify_ai_identity(42, AGENT, userid='cust5', confirmed=True)
    assert result['level'] == 'L2' and result['reason'] == 'STAFF_VERIFIED'
    assert result['verification']['verified_by_staff_name'] == 'Agent' and result['can_revoke'] is True
    level = await svc.resolve(PHONE)
    assert level.allows('wallet') and level.allows('payments') and level.allows('account')
    event = (await sql("SELECT event_type, actor_id FROM support_ticket_events WHERE ticket_id=42 ORDER BY id DESC LIMIT 1"))[0]
    assert event == {'event_type': 'AI_IDENTITY_VERIFIED', 'actor_id': '7'}
    days = (await sql("SELECT DATEDIFF(expires_at, verified_at) AS d FROM customer_ai_verifications"))[0]['d']
    assert days == 30
    # Verifying again replaces the previous verification.
    await support.verify_ai_identity(42, ADMIN, userid='cust5', confirmed=True)
    actions = [row['action'] + ':' + str(row['reason']) for row in
               await sql("SELECT action, reason FROM customer_ai_verification_audit ORDER BY id")]
    assert actions == ['VERIFIED:None', 'REVOKED:REPLACED', 'VERIFIED:None']
    assert len(await sql("SELECT id FROM customer_ai_verifications WHERE active_key IS NOT NULL")) == 1

    revoked = await support.revoke_ai_identity(42, AGENT)
    assert revoked['level'] == 'L1'
    assert (await sql("SELECT revoked_reason FROM customer_ai_verifications ORDER BY id DESC LIMIT 1"))[0][
        'revoked_reason'] == 'REVOKED_BY_STAFF'


@pytest.mark.asyncio
@pytest.mark.parametrize(('change', 'reason'), [
    ("UPDATE users SET mobile='15550009999' WHERE id=5", 'MOBILE_CHANGED'),
    ("UPDATE users SET status='0' WHERE id=5", 'ACCOUNT_INACTIVE'),
    ("DELETE FROM users WHERE id=5", 'ACCOUNT_MISSING'),
    ("INSERT INTO users VALUES (6,'other','Other','15550004201','1')", 'NUMBER_SHARED'),
    ("UPDATE customer_ai_verifications SET expires_at=UTC_TIMESTAMP(6)-INTERVAL 1 MINUTE", 'EXPIRED'),
])
async def test_verification_ends_automatically(identity, change, reason):
    svc, sql, user, _ = identity
    await user(5, 'cust5', PHONE)
    await svc.verify(phone=PHONE, userid='cust5', staff_id=7, staff_name='Agent', ticket_id=42)
    await sql(change)
    level = await svc.resolve(PHONE)
    assert level.level != 'L2'
    row = (await sql("SELECT active_key, revoked_reason FROM customer_ai_verifications"))[0]
    assert row == {'active_key': None, 'revoked_reason': reason}
    audit = (await sql("SELECT action, actor_type FROM customer_ai_verification_audit ORDER BY id DESC LIMIT 1"))[0]
    assert audit == {'action': 'EXPIRED' if reason == 'EXPIRED' else 'REVOKED', 'actor_type': 'SYSTEM'}


@pytest.mark.asyncio
async def test_verification_refuses_unsafe_requests(identity):
    svc, _sql, user, _ = identity
    await user(5, 'cust5', '15550001111')
    await user(6, 'inactive', '15550002222', status='0')
    await user(7, 'owner', PHONE)
    for userid, message in (('nobody', 'No customer account'), ('inactive', 'not active'),
                            ('cust5', 'registered on another customer account')):
        with pytest.raises(AIIdentityError, match=message):
            await svc.verify(phone=PHONE, userid=userid, staff_id=7, staff_name='Agent', ticket_id=42)
    # A customer writing from a number no account uses can be verified; the audit notes the mismatch.
    verified = await svc.verify(phone='15550007777', userid='cust5', staff_id=7, staff_name='Agent', ticket_id=None)
    assert verified.level == 'L2'
    assert (await svc.resolve('15550007777')).level == 'L2'


@pytest.mark.asyncio
async def test_unreadable_accounts_count_as_unknown(identity, monkeypatch):
    svc, *_ = identity

    async def broken(*_args):
        raise RuntimeError('replica down')

    monkeypatch.setattr(svc, '_fetch_users', broken)
    unknown = await svc.resolve_or_unknown(PHONE)
    assert (unknown.level, unknown.reason) == ('L0', 'LOOKUP_UNAVAILABLE')


@pytest.mark.asyncio
async def test_unclaimed_ticket_viewers_cannot_verify(identity):
    from app.support_service import SupportForbiddenError
    _svc, sql, user, support = identity
    await sql("UPDATE support_tickets SET whatsapp_phone=%s WHERE id=41", (PHONE,))
    await user(5, 'cust5', PHONE)
    view = await support.ai_identity(41, AGENT)
    assert view['level'] == 'L1' and view['can_verify'] is False
    with pytest.raises(SupportForbiddenError):
        await support.verify_ai_identity(41, AGENT, userid='cust5', confirmed=True)
