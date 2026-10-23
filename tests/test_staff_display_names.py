import asyncio
import json
from pathlib import Path
import subprocess

from app.support_staff_names import attach_current_staff_names


def test_staff_name_lookup_is_batched_and_type_scoped():
    class Cursor:
        calls=[]
        async def execute(self,sql,params): self.calls.append((sql,params))
        async def fetchall(self): return [{'id':7,'name':'New name'}]
    rows=[{'current_assignee_id':'7','current_assignee_name':'Old'},
          {'sender_type':'AGENT','sender_id':'7','sender_name':'Old'},
          {'reactor_type':'AGENT','reactor_id':'7','reactor_name':'Old'},
          {'sender_type':'CUSTOMER','sender_id':'7','sender_name':'Customer'},
          {'sender_type':'AGENT','sender_id':'7junk','sender_name':'Provider'},
          {'sender_type':'AGENT','sender_id':'8','sender_name':'Deleted staff'},
          {'actor_type':'SYSTEM','actor_id':'7','actor_name':'System'}]
    cursor=Cursor()
    asyncio.run(attach_current_staff_names(cursor,rows))
    assert len(cursor.calls)==1 and cursor.calls[0][1]==('7','8')
    assert rows[0]['current_assignee_name']==rows[1]['sender_name']==rows[2]['reactor_name']=='New name'
    assert rows[3]['sender_name']=='Customer' and rows[4]['sender_name']=='Provider'
    assert rows[5]['sender_name']=='Deleted staff' and rows[6]['actor_name']=='System'


def test_realtime_staff_rename_updates_cached_metadata_without_touching_delivery_or_drafts():
    source=Path('app/static/support/events.js').read_text()
    script=r'''
const assert=require('node:assert/strict');
const message={id:1,sender_type:'AGENT',sender_id:'7',sender_name:'Old',send_status:'DISPATCHING',body:'unchanged',
 reactions:[{reactor_type:'AGENT',reactor_id:'7',reactor_name:'Old'}],
 reply_to:{sender_type:'AGENT',sender_id:'7',sender_name:'Old'}};
const customer={id:2,sender_type:'CUSTOMER',sender_id:'7',sender_name:'Customer'};
const data={ticket:{id:42,current_assignee_id:'7',current_assignee_name:'Old'},messages:[message,customer],permissions:{can_reply:true}};
const state={authenticated:true,agent:{id:'7',name:'Old'},ticketPrefetch:new Map([['old',{}]]),
 currentTicketData:data,queueItems:[data.ticket],previousTicketHistory:[],cachedCurrentMessages:[message],
 globalSearchResults:[],conversationSearchResults:[],forwardConversations:[],
 realtimeMessages:new Map([['42:1',message]]),localMessages:new Map(),staffItems:[{id:'7',name:'Old'}],
 transferTargets:[{id:'7',name:'Old',role:'agent'}],replyTarget:{id:1,actor:'Old'},activeModal:null};
const option={value:'7',textContent:'Old'};
const $=id=>{assert.equal(id,'transferTarget');return {options:[option]}};
let renders=0;
const setAgentIdentity=agent=>assert.equal(agent.name,'New');
const canManageStaff=()=>true,renderStaffAccounts=()=>{},renderQueue=()=>{},renderTicketHeader=()=>{},
 renderContext=()=>{},renderReadOnlyState=()=>{},renderReplyDraft=()=>{};
const renderConversation=(data,options)=>{assert.equal(options.preserveAudio,true);renders++};
const timelineSignature=()=> 'new-signature',messageActor=message=>message.sender_name,staffRoleLabel=()=> 'Agent';
let source;class EventSource{constructor(){source=this;}close(){}}
''' + source + r'''
connectSupportEvents();
source.onmessage({data:JSON.stringify({type:'staff_profile_updated',authorized:false,staff_id:'7',name:'Bad'})});
assert.equal(state.agent.name,'Old');
source.onmessage({data:JSON.stringify({type:'staff_profile_updated',authorized:true,staff_id:'7',name:'New'})});
assert.equal(state.agent.name,'New');assert.equal(state.ticketPrefetch.size,0);
assert.equal(data.ticket.current_assignee_name,'New');assert.equal(message.sender_name,'New');
assert.equal(message.reactions[0].reactor_name,'New');assert.equal(message.reply_to.sender_name,'New');
assert.equal(customer.sender_name,'Customer');assert.equal(message.send_status,'DISPATCHING');
assert.equal(message.body,'unchanged');assert.equal(data.ticket.current_assignee_id,'7');
assert.equal(data.permissions.can_reply,true);assert.equal(state.replyTarget.actor,'New');
assert.equal(option.textContent,'New · Agent');assert.equal(renders,1);
'''
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
