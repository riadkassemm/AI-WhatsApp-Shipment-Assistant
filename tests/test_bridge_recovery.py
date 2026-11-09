"""Recovery boundary and HTTP uncertainty; no provider or real credentials."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app import bridge_recovery_api as api, support_auth
from app.bridge_recovery_service import BridgeRecoveryService, recovery_actions
from app.config import settings
from app.whatsapp_client import WhatsAppSendError


def error(status=None, uncertain=False, code='BRIDGE_UNAVAILABLE'):
    result = WhatsAppSendError('PRIVATE_PROVIDER_ERROR', delivery_uncertain=uncertain)
    result.http_status = status; result.bridge_code = code
    return result


@pytest.mark.asyncio
async def test_real_cookie_csrf_role_boundary_and_input_validation(monkeypatch):
    calls = []
    async def session(token):
        for role in ('agent','admin','supervisor'):
            if token == hashlib.sha256(role.encode()).hexdigest():
                return {'session_id':7,'staff_id':7,'name':'Synthetic','role':role,
                        'csrf_hash':hashlib.sha256(b'test-csrf').hexdigest()}
    async def touch(_): pass
    async def create(line, agent, request):
        calls.append((line,agent.role,request)); return {'id':request['id'],'state':'queued'}
    async def get(_): return {'state':'running'}
    async def history(_): return {'operations':[]}
    monkeypatch.setattr(support_auth.staff_repository,'get_active_session',session)
    monkeypatch.setattr(support_auth.staff_repository,'touch_session',touch)
    monkeypatch.setattr(api,'bridge_recovery_service',SimpleNamespace(create=create,get=get,history=history))
    monkeypatch.setattr(settings,'whatsapp_admin_recovery_enabled',True)
    app=FastAPI(); app.include_router(api.router)
    prefix='/api/support/whatsapp-linked-device'
    op=str(uuid4()); payload={'operation_id':op,'action':'pause','expected_version':0,'expected_generation':1,
                            'expected_epoch':str(uuid4()),'confirm_line_id':'90001'}
    async def request(role, method, suffix, csrf=True, body=None):
        headers={'Idempotency-Key':op}
        if csrf: headers['X-CSRF-Token']='test-csrf'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                cookies={settings.support_cookie_name:role} if role else {}) as client:
            return await client.request(method,prefix+suffix,headers=headers,json=body)
    for role, expected in [(None,401),('agent',403),('admin',202),('supervisor',202)]:
        response=await request(role,'POST','/lines/90001/operations',body=payload)
        assert response.status_code==expected
        for path in ['/operations/'+op,'/lines/90001/operations']:
            response=await request(role,'GET',path)
            assert response.status_code==(200 if expected==202 else expected)
        if expected!=202:
            assert (await request(role,'POST','/operations/'+op+'/cancel')).status_code==expected
    assert len(calls)==2
    for role in ('admin','supervisor'):
        assert (await request(role,'POST','/lines/90001/operations',csrf=False,body=payload)).status_code==403
        assert (await request(role,'POST','/operations/'+op+'/cancel',csrf=False)).status_code==403
    for change in ({'confirm_line_id':'90002'},{'operation_id':str(uuid4())},{'force':True},{'action':'clear-auth'}, {'expected_version':True}):
        assert (await request('admin','POST','/lines/90001/operations',body={**payload,**change})).status_code==422
    monkeypatch.setattr(settings,'whatsapp_admin_recovery_enabled',False)
    assert (await request('admin','POST','/lines/90001/operations',body=payload)).status_code==409
    assert len(calls)==2


class MemoryOperations:
    def __init__(self):
        self.row={'id':str(uuid4()),'line_id':'90001','state':'queued',
                  'parameters_json':json.dumps({'id':'synthetic','action':'pause','expected_version':3})}
        self.transitions=[]; self.options=[]
    @asynccontextmanager
    async def coordinated(self,key): yield self
    async def get(self,_): return dict(self.row)
    async def update(self,conn,row,state,reason,**kwargs):
        if kwargs.get('expected_state') and self.row['state']!=kwargs['expected_state']: return False
        self.row.update(state=state,reason_code=reason); self.transitions.append((state,reason)); self.options.append(kwargs); return True


@pytest.mark.asyncio
async def test_timeout_and_api_restart_resubmit_only_the_identical_saved_command():
    repo=MemoryOperations(); calls=[]; posted=[]; outcome=None
    async def request(method,path,**kwargs):
        calls.append(method)
        if method=='GET':
            assert path.endswith('/'+repo.row['id'])
            if outcome: return {'id':repo.row['id'],'state':outcome,'reason_code':'ADMIN_PAUSED'}
            raise error(404,code='OPERATION_NOT_FOUND')
        assert repo.row['state']=='running'  # Durable intent precedes network.
        posted.append((path,kwargs['payload'])); raise error(504,True)
    client=SimpleNamespace(_json_request=request)
    await BridgeRecoveryService(repo,client).process(repo.row)
    assert repo.row['state']=='running' and calls==['GET','POST']
    # API restart / lost response: only the bridge's own "no record of this ID"
    # permits resubmission, and it is byte-identical under the same ID.
    await BridgeRecoveryService(repo,client).process(repo.row)
    assert repo.row['state']=='running' and calls==['GET','POST','GET','POST']
    assert posted[0]==posted[1] and posted[0][1]==json.loads(repo.row['parameters_json'])
    outcome='succeeded'
    await BridgeRecoveryService(repo,client).process(repo.row)
    assert repo.row['state']=='succeeded' and calls.count('POST')==2
    assert all(not option.get('restore_pause') for option in repo.options)


@pytest.mark.asyncio
async def test_unknown_line_or_unavailable_bridge_never_submits():
    for failure in (error(404,code='UNKNOWN_LINE'), error(503,False,'BRIDGE_INITIALIZING'), error(None,False,'BRIDGE_UNAVAILABLE')):
        repo=MemoryOperations(); calls=[]
        async def request(method,path,**kwargs): calls.append(method); raise failure
        await BridgeRecoveryService(repo,SimpleNamespace(_json_request=request)).process(repo.row)
        assert calls==['GET'] and repo.row['state']=='queued' and repo.row['reason_code']=='BRIDGE_UNAVAILABLE'


@pytest.mark.asyncio
async def test_offline_observation_does_not_claim_a_command_was_dispatched():
    repo=MemoryOperations(); calls=[]
    async def request(method,path,**kwargs): calls.append(method); raise error()
    await BridgeRecoveryService(repo,SimpleNamespace(_json_request=request)).process(repo.row)
    assert repo.row['state']=='queued' and calls==['GET']


@pytest.mark.asyncio
async def test_known_bridge_rejection_is_sanitized_and_not_retried():
    repo=MemoryOperations()
    async def request(method,path,**kwargs):
        raise error(404,code='OPERATION_NOT_FOUND') if method=='GET' else error(409,False,'STALE_OBSERVATION')
    await BridgeRecoveryService(repo,SimpleNamespace(_json_request=request)).process(repo.row)
    assert repo.transitions[-1]==('failed','STALE_OBSERVATION')
    # Rejected before bridge admission: the bridge never paused, so the API
    # admission state returns to what it was before this request.
    assert repo.options[-1]=={'restore_pause':True}
    assert 'PRIVATE' not in json.dumps(repo.transitions)


def test_actions_fail_closed_on_missing_state_and_sanitize_private_values():
    raw={'supported':True,'epoch':str(uuid4()),'version':1,'generation':2,'paused':False,
         'actions':{a:{'enabled':True,'blockers':[]} for a in ('pause','resume','reconnect')}}
    view=recovery_actions(raw,None)
    assert [a['enabled'] for a in view['actions']]==[True,True,False]
    assert not any(a['enabled'] for a in recovery_actions(raw,None,available=False)['actions'])
    assert not any(a['enabled'] for a in recovery_actions(raw,None,enabled_in_app=False)['actions'])
    private={**raw,'epoch':'/private/auth.json','active_operation_id':'PRIVATE','version':'PRIVATE'}
    assert 'PRIVATE' not in json.dumps(recovery_actions(private,None))
    assert not any(a['enabled'] for a in recovery_actions(private,None)['actions'])
    paused=recovery_actions(raw,{'paused':1})
    assert [a['enabled'] for a in paused['actions']]==[False,False,True]

@pytest.mark.asyncio
async def test_management_refresh_reads_controls_once_and_never_submits(monkeypatch):
    from app import bridge_management_service as management
    from app.bridge_recovery_repository import bridge_recovery_repository
    calls=[]
    async def controls(): calls.append('SELECT controls'); return []
    async def numbers(): calls.append('SELECT inventory'); return [{'phone_number_id':'90001','is_enabled':True}]
    async def status():
        calls.append('GET status')
        return {'lines':[{'line_id':'90001','status':'connected','administration':{
            'supported':True,'epoch':str(uuid4()),'generation':1,'version':0,'paused':False,
            'actions':{'pause':{'enabled':True,'blockers':[]}},'secret':'PRIVATE'}}]}
    monkeypatch.setattr(settings,'whatsapp_admin_recovery_enabled',True)
    monkeypatch.setattr(management,'configured_linked_device_lines',lambda:{'90001':''})
    monkeypatch.setattr(management,'quarantined_linked_device_line_ids',lambda:set())
    monkeypatch.setattr(bridge_recovery_repository,'controls',controls)
    service=management.BridgeManagementService(SimpleNamespace(numbers=numbers),SimpleNamespace(get_management_status=status))
    views=await asyncio.gather(*(service.management() for _ in range(5)))
    assert calls==['SELECT inventory','GET status','SELECT controls']
    assert 'PRIVATE' not in json.dumps(views)
    assert next(a for a in views[0]['lines'][0]['actions'] if a['action']=='pause')['enabled']
    async def unavailable(): raise RuntimeError('PRIVATE')
    monkeypatch.setattr(bridge_recovery_repository,'controls',unavailable)
    service.at=-100
    result=await service.management()
    assert 'RECOVERY_STATE_UNAVAILABLE' in result['issues']
    assert not any(a['enabled'] for a in result['lines'][0]['actions'])

@pytest.mark.asyncio
async def test_primary_bot_reply_keeps_redis_guard_when_durably_queued_on_default_line(monkeypatch,tmp_path):
    from app import main
    from app.bridge_paused_replies import paused_replies
    from app.conversation_store import InMemoryConversationStore
    monkeypatch.setattr(settings,'whatsapp_admin_recovery_enabled',True)
    monkeypatch.setattr(settings,'whatsapp_transport','linked_device')
    monkeypatch.setattr(settings,'whatsapp_linked_device_default_line_id','90001')
    monkeypatch.setattr(settings,'whatsapp_linked_device_lines',{'90001':''})
    monkeypatch.setattr(settings,'whatsapp_linked_device_secret','synthetic-secret-'*4)
    monkeypatch.setattr(settings,'whatsapp_linked_device_data_directory',str(tmp_path))
    monkeypatch.setattr(main,'conversation_store',InMemoryConversationStore())
    calls=[]
    async def queue(provider_send,**kwargs): calls.append(kwargs); return {'queued':True}
    monkeypatch.setattr(paused_replies,'send_or_queue',queue)
    assert (await main._send_text_once(to='155500001',body='Saved reply',source_message_id='source'))['queued']
    assert (await main._send_text_once(to='155500001',body='Saved reply',source_message_id='source'))['deduplicated']
    assert len(calls)==1 and calls[0]['line_id']=='90001' and calls[0]['body']=='Saved reply'


@pytest.mark.asyncio
async def test_bot_pause_race_retains_only_proven_non_send(monkeypatch):
    from app.bridge_paused_replies import PausedReplies
    queue=PausedReplies(); saved=[]
    async def waiting(line_id,recipient): assert (line_id,recipient)==('90001','155500001'); return False
    async def enqueue(*args): saved.append(args); return {'queued':True}
    monkeypatch.setattr(queue,'waiting',waiting); monkeypatch.setattr(queue,'enqueue',enqueue)
    uncertain=False
    async def send(): raise error(409,uncertain,'ADMIN_PAUSED')
    params=dict(line_id='90001',source_id='source',recipient='155500001',body='Original reply')
    assert (await queue.send_or_queue(send,**params))['queued']
    assert saved[0][-1]=='Original reply'
    uncertain=True
    with pytest.raises(WhatsAppSendError): await queue.send_or_queue(send,**params)
    assert len(saved)==1


@pytest.mark.asyncio
async def test_unreadable_pause_state_refuses_before_provider_and_releases_guard(monkeypatch):
    from app.bridge_paused_replies import PausedReplies
    queue=PausedReplies(); calls=[]
    async def waiting(*_): raise RuntimeError('PRIVATE database detail')
    async def send(): calls.append('provider'); return {}
    monkeypatch.setattr(queue,'waiting',waiting)
    with pytest.raises(WhatsAppSendError) as raised:
        await queue.send_or_queue(send,line_id='90001',source_id='source',recipient='155500001',body='Reply')
    assert not raised.value.delivery_uncertain and raised.value.bridge_code=='RECOVERY_STATE_UNAVAILABLE'
    assert calls==[] and 'PRIVATE' not in str(raised.value)
