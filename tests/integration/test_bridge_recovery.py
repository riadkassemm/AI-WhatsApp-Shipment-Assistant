"""Disposable SQL tests for durable coordination and retained deliveries."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.bridge_recovery_repository import bridge_recovery_repository as operations, RecoveryConflict
from app.bridge_recovery_service import BridgeRecoveryService
from app.bridge_paused_replies import paused_replies
from app.config import settings
from app.support_auth import SupportAgent
from app.support_delivery import deliver
from app.whatsapp_registry import whatsapp_registry_repository
from app.whatsapp_client import WhatsAppSendError

ADMIN=SupportAgent(id='7',name='Synthetic admin',role='admin')
SUPER=SupportAgent(id='8',name='Synthetic super',role='supervisor')


def command(action='pause'):
    return {'id':str(uuid4()),'action':action,'expected_version':0,'expected_generation':1,'expected_epoch':str(uuid4())}


def configure(repo,monkeypatch):
    monkeypatch.setattr(whatsapp_registry_repository,'_pool',repo._pool)
    monkeypatch.setattr(settings,'whatsapp_admin_recovery_enabled',True)


@pytest.mark.asyncio
async def test_durable_idempotency_cancel_audit_and_action_limits(infrastructure,monkeypatch):
    repo,sql,_=infrastructure; configure(repo,monkeypatch)
    await repo._validate_schema(repo._pool)
    request=command()
    first=await operations.create('90005',ADMIN,request)
    assert (await operations.create('90005',ADMIN,request))['id']==first['id']
    assert await operations.paused('90005')
    with pytest.raises(RecoveryConflict,match='IDEMPOTENCY_CONFLICT'):
        await operations.create('90005',ADMIN,{**request,'action':'resume'})
    with pytest.raises(RecoveryConflict,match='MANAGEMENT_OPERATION_ACTIVE'):
        await operations.create('90005',SUPER,command())
    cancelled=await operations.cancel(first['id'],SUPER)
    assert cancelled['state']=='cancelled' and not await operations.paused('90005')
    assert (await operations.cancel(first['id'],SUPER))['state']=='cancelled'
    audit=await operations.audit(first['id'])
    assert [(r['actor_id'],r['state']) for r in audit]==[('7','queued'),('8','cancelled')]
    for _ in range(2):
        row=await operations.create('90005',ADMIN,command()); await operations.cancel(row['id'],ADMIN)
    with pytest.raises(RecoveryConflict,match='ACTION_RATE_LIMITED'):
        await operations.create('90005',ADMIN,command())
    assert len(await sql('SELECT id FROM whatsapp_bridge_operations'))==3


@pytest.mark.asyncio
async def test_cross_worker_lock_and_restart_do_not_reissue_unknown_command(infrastructure,monkeypatch):
    repo,sql,_=infrastructure; configure(repo,monkeypatch)
    row=await operations.create('90005',ADMIN,command())
    calls=[]
    payloads=[]; reject=False
    async def request(method,path,**kwargs):
        calls.append(method)
        if method=='POST': payloads.append(kwargs['payload'])
        exc=WhatsAppSendError('PRIVATE',delivery_uncertain=method=='POST' and not reject)
        exc.http_status=404 if method=='GET' else 409 if reject else 504
        exc.bridge_code='OPERATION_NOT_FOUND' if method=='GET' else 'STALE_OBSERVATION'; raise exc
    client=SimpleNamespace(_json_request=request)
    async with operations.coordinated('operation:90005') as conn:
        assert conn is not None
        await BridgeRecoveryService(operations,client).process(row)
        assert not calls
    await BridgeRecoveryService(operations,client).process(row)
    await BridgeRecoveryService(operations,client).process(await operations.get(row['id']))
    restored=await operations.get(row['id'])
    # Unknown outcome: reserved, uncancellable and resubmitted only unchanged.
    assert restored['state']=='running' and calls==['GET','POST','GET','POST']
    assert payloads[0]==payloads[1] and payloads[0]['id']==row['id']
    assert (await operations.controls())[0]['active_operation_id']==row['id']
    with pytest.raises(RecoveryConflict,match='CANCELLATION_UNSAFE'):
        await operations.cancel(row['id'],ADMIN)
    assert await operations.paused('90005')
    # A restarted bridge (new epoch) definitively rejects the stale command
    # before admission: it never paused, so API admission is restored.
    reject=True
    await BridgeRecoveryService(operations,client).process(await operations.get(row['id']))
    final=await operations.get(row['id'])
    assert final['state']=='failed' and final['reason_code']=='STALE_OBSERVATION'
    assert not await operations.paused('90005') and (await operations.controls())[0]['active_operation_id'] is None


async def message(repo,key):
    row, _ = await repo.add_ticket_message(ticket_id=42,sender_type='AGENT',sender_id='7',direction='OUTBOUND',
        body='Synthetic retained reply',client_message_id=key,send_status='PENDING')
    return row


@pytest.mark.asyncio
async def test_staff_pause_preserves_work_expiry_order_and_uncertainty(infrastructure,monkeypatch):
    repo,sql,_=infrastructure; configure(repo,monkeypatch)
    first=await message(repo,'first'); second=await message(repo,'second')
    pause=await operations.create('90005',ADMIN,command())
    calls=[]
    async def send(**kwargs): calls.append(kwargs); return {'messages':[{'id':'ld:90005:first'}]}
    transport=SimpleNamespace(send_text=send)
    ticket=await repo.get_ticket(42)
    held=await deliver(repo,transport,None,ticket,first)
    assert held['admin_hold']==1 and held['send_status']=='PENDING' and not calls
    assert await repo.list_held_replies()==[]
    await operations.cancel(pause['id'],ADMIN)
    assert [m['id'] for m in await repo.list_held_replies()]==[first['id'],second['id']]
    await deliver(repo,transport,None,ticket,second)
    assert not calls
    sent=await deliver(repo,transport,None,ticket,first)
    assert sent['send_status']=='SENT' and sent['admin_hold']==0 and len(calls)==1
    # Uncertain work is never resent, so it must never block later replies
    # (nothing would ever release them).
    await sql("UPDATE support_ticket_messages SET send_status='UNCERTAIN',admin_hold=1 WHERE id=%s",(first['id'],))
    assert not await repo.administrative_delivery_blocked('90005',second['id'])
    await sql("UPDATE support_ticket_messages SET send_status='FAILED',delivery_retry_safe=1,held_until=UTC_TIMESTAMP(6)-INTERVAL 1 DAY WHERE id=%s",(first['id'],))
    await repo.expire_held_replies()
    assert (await repo.get_message(first['id']))['held_until'] is not None
    assert (await repo.list_held_replies())[0]['id']==first['id']
    # Retained proven non-sends keep order inside their conversation only.
    assert await repo.administrative_delivery_blocked('90005',second['id'])
    other,_=await repo.add_ticket_message(ticket_id=43,sender_type='AGENT',sender_id='8',direction='OUTBOUND',
        body='Synthetic other customer',client_message_id='other-customer',send_status='PENDING')
    assert not await repo.administrative_delivery_blocked('90005',other['id'])


@pytest.mark.asyncio
async def test_retained_reply_outcomes_and_unclaimable_holds_release_the_conversation(infrastructure,monkeypatch):
    repo,sql,_=infrastructure; configure(repo,monkeypatch)
    pause=await operations.create('90005',ADMIN,command())
    first=await message(repo,'definitive'); second=await message(repo,'after-definitive')
    ticket=await repo.get_ticket(42)
    async def forbidden(**kwargs): raise AssertionError('Provider called while paused')
    for row in (first,second):
        assert (await deliver(repo,SimpleNamespace(send_text=forbidden),None,ticket,row))['admin_hold']==1
    await operations.cancel(pause['id'],ADMIN)
    async def rejected(**kwargs):
        exc=WhatsAppSendError('PRIVATE',delivery_uncertain=False); exc.bridge_code='INVALID_RECIPIENT'; raise exc
    failed=await deliver(repo,SimpleNamespace(send_text=rejected),None,ticket,await repo.get_message(first['id']),retry=True)
    # A definitive, non-transient rejection becomes an ordinary failure (staff
    # may resend manually): it is not retried forever and blocks nothing.
    assert failed['send_status']=='FAILED' and failed['admin_hold']==0 and failed['held_until'] is None
    assert not await repo.administrative_delivery_blocked('90005',second['id'])
    assert [m['id'] for m in await repo.list_held_replies()]==[second['id']]
    # Transferred while retained: the original sender can no longer send it.
    await sql("UPDATE support_tickets SET current_assignee_id='8' WHERE id=42")
    assert await repo.release_unclaimable_admin_holds()==[42]
    released=await repo.get_message(second['id'])
    assert released['send_status']=='FAILED' and released['delivery_retry_safe'] and released['admin_hold']==0
    assert released['error_message']=='Not sent; retry is available.' and await repo.list_held_replies()==[]
    assert await repo.release_unclaimable_admin_holds()==[]
    # A still-claimable retained reply is untouched.
    await sql("UPDATE support_tickets SET current_assignee_id='7' WHERE id=42")
    kept=await message(repo,'claimable')
    await sql("UPDATE support_ticket_messages SET admin_hold=1 WHERE id=%s",(kept['id'],))
    assert await repo.release_unclaimable_admin_holds()==[]
    assert (await repo.get_message(kept['id']))['admin_hold']==1


@pytest.mark.asyncio
async def test_bot_queue_keeps_exact_reply_and_never_retries_uncertain_dispatch(infrastructure,monkeypatch):
    repo,sql,_=infrastructure; configure(repo,monkeypatch)
    pause=await operations.create('90005',ADMIN,command())
    calls=[]; mirrors=[]
    async def forbidden(): raise AssertionError('Provider called while paused')
    for source,body in [('inbound-1','Exact first reply'),('inbound-2','Exact second reply')]:
        result=await paused_replies.send_or_queue(forbidden,line_id='90005',source_id=source,recipient='155500042',body=body)
        assert result['queued']
    await paused_replies.send_or_queue(forbidden,line_id='90005',source_id='inbound-1',recipient='155500042',body='Exact first reply')
    assert len(await sql('SELECT id FROM whatsapp_paused_bot_replies'))==2
    with pytest.raises(RuntimeError,match='DEFERRED_REPLY_CONFLICT'):
        await paused_replies.enqueue('90005','inbound-1','155500042','Different generated reply')
    await operations.cancel(pause['id'],ADMIN)
    import app.bridge_paused_replies as module
    from app.conversation_store import conversation_store
    from app.support_service import support_service
    async def revoked(_): return False
    async def mirror(incoming): mirrors.append(incoming); return True
    async def send(*args,**kwargs):
        calls.append((args,kwargs))
        if len(calls)==1: raise WhatsAppSendError('Unknown provider outcome',delivery_uncertain=True)
        return {'messages':[{'id':'ld:90005:second'}]}
    outcome={'status':'unknown'}; lookups=[]
    async def evidence(**kwargs): lookups.append(kwargs); return outcome
    monkeypatch.setattr(conversation_store,'is_inbound_revoked',revoked)
    monkeypatch.setattr(support_service,'sync_linked_device_outbound',mirror)
    monkeypatch.setattr(module,'linked_device_client',SimpleNamespace(send_text=send,get_delivery_status=evidence))
    await paused_replies.process(); await paused_replies.process()
    rows=await sql('SELECT * FROM whatsapp_paused_bot_replies ORDER BY id')
    # The uncertain first reply is never resent and does not hold the next one.
    assert len(calls)==2 and [r['state'] for r in rows]==['uncertain','sent']
    assert calls[0][0][1]=='Exact first reply' and calls[1][0][1]=='Exact second reply'
    assert calls[0][1]['client_message_id']==rows[0]['transport_id']
    assert not await paused_replies.confirm({'client_message_id':rows[0]['transport_id'],'line_id':'90006',
        'sender_phone':'155500042','message_id':'ld:90006:wrong'})
    outcome.update(status='confirmed',message_id='ld:90005:confirmed')
    await paused_replies.process()
    assert not lookups  # Rechecks are rate-limited per uncertain reply.
    await sql("UPDATE whatsapp_paused_bot_replies SET updated_at=UTC_TIMESTAMP(6)-INTERVAL 2 MINUTE WHERE id=%s",(rows[0]['id'],))
    await paused_replies.process()
    assert len(calls)==2 and len(lookups)==1
    assert [r['state'] for r in await sql('SELECT state FROM whatsapp_paused_bot_replies ORDER BY id')]==['sent','sent']
    # Bot replies are never mirrored into support conversations / new tickets.
    assert mirrors==[] and len(await sql('SELECT id FROM support_tickets'))==3
    history=await BridgeRecoveryService().history('90005')
    assert 'Exact' not in json.dumps(history)


@pytest.mark.asyncio
async def test_bot_queue_retries_only_transient_non_sends_and_keeps_customer_order(infrastructure,monkeypatch):
    repo,sql,_=infrastructure; configure(repo,monkeypatch)
    import app.bridge_paused_replies as module
    from app.conversation_store import conversation_store
    for source,recipient in [('a1','155500041'),('a2','155500041'),('b1','155500043')]:
        await paused_replies.enqueue('90005',source,recipient,'Synthetic '+source)
    async def revoked(_): return False
    monkeypatch.setattr(conversation_store,'is_inbound_revoked',revoked)
    calls=[]; mode={'code':'NOT_CONNECTED'}
    async def send(recipient,body,**kwargs):
        calls.append(body)
        if mode['code']:
            exc=WhatsAppSendError('PRIVATE',delivery_uncertain=False); exc.bridge_code=mode['code']; raise exc
        return {'messages':[{'id':'ld:90005:'+kwargs['client_message_id'][-8:]}]}
    async def evidence(**kwargs): return {'status':'unknown'}
    monkeypatch.setattr(module,'linked_device_client',SimpleNamespace(send_text=send,get_delivery_status=evidence))
    await paused_replies.process(); await paused_replies.process()
    # Offline is line-wide: one attempt, then a cooldown before the next one.
    assert calls==['Synthetic a1']
    assert [r['state'] for r in await sql('SELECT state FROM whatsapp_paused_bot_replies ORDER BY id')]==['queued']*3
    await sql("UPDATE whatsapp_paused_bot_replies SET updated_at=UTC_TIMESTAMP(6)-INTERVAL 1 MINUTE")
    mode['code']='INVALID_RECIPIENT'
    await paused_replies.process()
    # A reply-specific rejection is final for that reply only.
    states=[r['state'] for r in await sql('SELECT state FROM whatsapp_paused_bot_replies ORDER BY id')]
    assert states==['failed','failed','failed'] and calls.count('Synthetic a1')==2
    await paused_replies.process()
    assert len(calls)==4
    # A retained staff reply for customer 41 goes before that customer's bot reply.
    await sql("UPDATE support_tickets SET current_assignee_id='7',status='ASSIGNED' WHERE id=41")
    staff,_=await repo.add_ticket_message(ticket_id=41,sender_type='AGENT',sender_id='7',direction='OUTBOUND',
        body='Synthetic staff first',client_message_id='staff-first',send_status='PENDING')
    await sql("UPDATE support_ticket_messages SET admin_hold=1 WHERE id=%s",(staff['id'],))
    mode['code']=None
    async def forbidden(): raise AssertionError('Sent ahead of the retained staff reply')
    queued=await paused_replies.send_or_queue(forbidden,line_id='90005',source_id='a3',recipient='+1 555-000-41',body='Synthetic a3')
    assert queued['queued']  # Same customer despite a differently formatted number.
    await paused_replies.enqueue('90005','b2','155500043','Synthetic b2')
    await paused_replies.process()
    assert calls[-1]=='Synthetic b2' and 'Synthetic a3' not in calls
    # Another customer's reply is not held by customer 41's retained work.
    assert not await paused_replies.waiting('90005','155500043')

@pytest.mark.asyncio
async def test_pause_during_dispatch_finishes_original_and_retains_later_reply(infrastructure,monkeypatch):
    repo,sql,_=infrastructure; configure(repo,monkeypatch)
    entered,release=asyncio.Event(),asyncio.Event(); calls=[]
    async def send(**kwargs):
        calls.append(kwargs); entered.set(); await release.wait()
        return {'messages':[{'id':'ld:90005:inflight'}]}
    first=await message(repo,'inflight'); ticket=await repo.get_ticket(42)
    task=asyncio.create_task(deliver(repo,SimpleNamespace(send_text=send),None,ticket,first))
    await asyncio.wait_for(entered.wait(),3)
    pause=await operations.create('90005',ADMIN,command())
    second=await message(repo,'afterpause')
    held=await deliver(repo,SimpleNamespace(send_text=send),None,ticket,second)
    assert held['admin_hold']==1 and len(calls)==1
    release.set(); assert (await task)['send_status']=='SENT'
    assert await operations.paused('90005')
    await operations.cancel(pause['id'],ADMIN)
    assert not await repo.administrative_delivery_blocked('90005',second['id'])


@pytest.mark.asyncio
async def test_pause_after_media_preparation_prevents_provider_dispatch(infrastructure,monkeypatch,tmp_path):
    repo,sql,_=infrastructure; configure(repo,monkeypatch)
    row,_=await repo.add_ticket_message(ticket_id=42,sender_type='AGENT',sender_id='7',direction='OUTBOUND',
        body='Document',message_type='document',client_message_id='media',send_status='PENDING')
    asset=tmp_path/'synthetic.pdf'; asset.write_bytes(b'%PDF synthetic')
    store=SimpleNamespace(get=lambda *_:SimpleNamespace(path=asset,mime_type='application/pdf',filename='synthetic.pdf'))
    async def upload(**kwargs):
        await operations.create('90005',ADMIN,command()); return 'ldmedia:90005:synthetic'
    async def send(**kwargs): raise AssertionError('Media dispatched after administrative pause')
    result=await deliver(repo,SimpleNamespace(upload_media=upload,send_media=send),store,await repo.get_ticket(42),row)
    assert result['send_status']=='FAILED' and result['delivery_retry_safe'] and result['admin_hold']
    assert asset.read_bytes()==b'%PDF synthetic'
