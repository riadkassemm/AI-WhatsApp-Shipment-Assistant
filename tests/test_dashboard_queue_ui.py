"""Exercise shipped dashboard functions with deterministic DOM/event doubles."""
from pathlib import Path
import subprocess


def run(script):
    result = subprocess.run(['node', '-e', script], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_current_assignee_chat_toasts_ignore_duplicates_and_outgoing():
    source = Path('app/static/support/events.js').read_text()
    helper = source[source.index('function handleAssignedChatNotification('):source.index('let dashboardReadTimer')]
    run("""
const assert=require('node:assert/strict');
const state={agent:{id:7},chatNotificationIds:new Set()};
const saved=new Map(); const sessionStorage={getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)};
const toasts=[],sounds=[],opens=[];
const notify=(...args)=>toasts.push(args),notifyNewCustomerActivity=x=>sounds.push(x);
const setQueueView=x=>opens.push(x),openTicket=async(id,options)=>opens.push([id,options]);
""" + helper + """
const event={authorized:true,chat_notification:true,current_assignee_id:'7',ticket_id:42,message_id:10,message:{direction:'INBOUND'}};
handleAssignedChatNotification({...event,current_assignee_id:'8'});
handleAssignedChatNotification({...event,chat_notification:false});
handleAssignedChatNotification({...event,authorized:false});
handleAssignedChatNotification({...event,message:{direction:'OUTBOUND'}});
assert.equal(toasts.length,0);
handleAssignedChatNotification(event); handleAssignedChatNotification(event);
assert.equal(toasts.length,1); assert.equal(sounds.length,1);
state.chatNotificationIds.clear(); handleAssignedChatNotification(event);
assert.equal(toasts.length,1,'refresh cannot replay an alert');
toasts[0][2](); assert.deepEqual(opens,['chats',[42,{conversationOrigin:'STAFF'}]]);
state.agent.id=8;
handleAssignedChatNotification({...event,message_id:11});
assert.equal(toasts.length,1,'old owner metadata never notifies new logged-in user');
handleAssignedChatNotification({...event,message_id:11,current_assignee_id:'8'});
assert.equal(toasts.length,2);
""")


def test_read_only_visible_inbound_messages_with_confirmed_boundary():
    source = Path('app/static/support/events.js').read_text()
    helper = source[source.index('async function markVisibleDashboardMessagesRead('):]
    run("""
const assert=require('node:assert/strict');
const state={authenticated:true,agent:{id:7},ticketId:42,activeModal:null,historyJumpActive:false,dashboardReadBoundaries:new Map(),
currentTicketData:{ticket:{id:42},messages:[{id:1,direction:'INBOUND'},{id:2,direction:'INBOUND'},{id:3,direction:'OUTBOUND'}]}};
let visible=true,focused=true,calls=[],fail=false,refreshes=0;
const document={visibilityState:'visible',hasFocus:()=>focused};
const window={innerWidth:1440,innerHeight:1000};
const nodes=[1,2,3].map(id=>({dataset:{messageId:String(id)},getBoundingClientRect:()=>({top:id===2?200:50,bottom:id===2?220:70,height:20})}));
const $=id=>({getBoundingClientRect:()=>({top:0,bottom:100,width:visible?300:0,height:100}),querySelectorAll:()=>nodes});
const api=async(path,options)=>{calls.push([path,JSON.parse(options.body)]);if(fail)throw Error('offline');return {dashboard_unread_count:1}};
const requestDashboardRefresh=scope=>{assert.equal(scope,"queue");refreshes++};
"""+helper+"""
(async()=>{
focused=false; await markVisibleDashboardMessagesRead(); assert.equal(calls.length,0);
focused=true; state.activeModal='investigation'; await markVisibleDashboardMessagesRead(); assert.equal(calls.length,0);
state.activeModal=null; visible=false; await markVisibleDashboardMessagesRead(); assert.equal(calls.length,0);
visible=true; fail=true; await markVisibleDashboardMessagesRead(); assert.equal(state.dashboardReadBoundaries.size,0);
fail=false; await markVisibleDashboardMessagesRead();
assert.deepEqual(calls.at(-1),['/api/support/tickets/42/dashboard-read',{message_id:1}]);
assert.equal(state.dashboardReadBoundaries.get('7:42'),1); assert.equal(refreshes,1);
await markVisibleDashboardMessagesRead(); assert.equal(calls.length,2,'confirmed boundary does not repeat');
nodes[1].getBoundingClientRect=()=>({top:60,bottom:80,height:20}); await markVisibleDashboardMessagesRead();
assert.equal(calls.at(-1)[1].message_id,2,'only scrolling to the newer message advances the boundary');
})().catch(e=>{console.error(e);process.exitCode=1});
""")


def test_filter_paths_combination_and_pagination_reset():
    source = Path('app/static/support/rendering.js').read_text()
    helper = source[source.index('function queueSupportsUnreadFilter('):source.index('function uniqueQueueItems(')]
    run("""
const assert=require('node:assert/strict');
const state={view:'chats',query:'invoice',page:5,queueRequest:1,queueExhausted:true,queueFilters:{supportNumber:'90005',staffId:'7',status:'closed'}};
let next; const setTimeout=f=>{next=f;return 1},clearTimeout=()=>{},loadQueue=()=>{};
"""+helper+"""
let params=new URL(queuePath(3,30),'http://x').searchParams;
for(const [key,value] of Object.entries({page:'3',page_size:'30',q:'invoice',support_number:'90005',staff_id:'7',chat_status:'closed'})) assert.equal(params.get(key),value);
queueCriteriaChanged(); assert.equal(state.page,1);assert.equal(state.queueRequest,2);assert.equal(state.queueExhausted,false);
state.view='mine';params=new URL(queuePath(1,30),'http://x').searchParams;assert.equal(params.has('staff_id'),false);assert.equal(params.has('chat_status'),false);
assert.equal(params.has('unread'),false);
state.queueFilters.read='unread';params=new URL(queuePath(1,30),'http://x').searchParams;
assert.equal(params.get('unread'),'true');assert.equal(params.has('keep_ticket_id'),false);
state.ticketId=42;params=new URL(queuePath(1,30),'http://x').searchParams;assert.equal(params.get('keep_ticket_id'),'42');
state.view='chats';params=new URL(queuePath(1,30),'http://x').searchParams;assert.equal(params.get('unread'),'true');
state.view='history';params=new URL(queuePath(1,30),'http://x').searchParams;
assert.equal(params.has('unread'),false,'unread applies to My tickets and Chats only');assert.equal(params.has('keep_ticket_id'),false);
""")


def test_queue_controls_and_unread_dots_in_browser(tmp_path):
    import os
    import shutil
    from app.support_ui import _ASSETS, _TEMPLATE, _configure
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install Chromium for browser regressions'
    script = _configure(_ASSETS['state.js'] + '\n' + _ASSETS['rendering.js']) + r'''
const check=(ok,label)=>{if(!ok)throw Error(label)};
try {
state.authenticated=true;state.agent={id:'7',name:'Test Agent',role:'agent'};state.view='chats';
state.queueItems=[{id:42,customer_name:'Example customer',whatsapp_phone:'15550000001',conversation_origin:'STAFF',
 current_assignee_name:'Test Agent',current_assignee_id:'7',status:'ASSIGNED',active_key:'90005:15550000001',
 display_name:'Export Office',business_display_phone_number:'15550000002',dashboard_unread_count:2,last_activity_at:new Date().toISOString()}];
state.total=1;
renderQueueFilters({support_numbers:[{id:'90005',name:'Export Office'}],staff:[{id:'7',name:'Test Agent'}]});
renderQueue();
check(!$('queueStaffLabel').classList.contains('hidden')&&!$('queueStatusLabel').classList.contains('hidden'),'chat filters visible');
check($('queueSupportNumber').options[1].textContent==='Export Office','recognizable support line');
check($('queue').querySelector('.unread-dot[aria-label="Unread messages"][title="Unread messages"]'),'accessible unread dot');
check(state.dashboardReadBoundaries.size===0,'list render does not mark read');
state.queueFilters={supportNumber:'90005',staffId:'7',status:'open'};renderQueueFilters();
check($('queueSupportNumber').value==='90005'&&$('queueStaff').value==='7'&&$('queueStatus').value==='open','combined selections');
check($('queueSupportNumber').labels[0].querySelector('.sr-only'),'support filter retains accessible label');
check($('queueStaff').labels[0].querySelector('.sr-only'),'staff filter retains accessible label');
state.queueFilters.supportNumber='removed';state.queueFilters.staffId='deleted';
check(renderQueueFilters({support_numbers:[{id:'90005',name:'Export Office'}],staff:[{id:'7',name:'Test Agent'}]}),'stale selection requests reload');
check(state.queueFilters.supportNumber===''&&state.queueFilters.staffId==='','stale filters reset to All');
check($('queueSupportNumber').options.length===2&&$('queueStaff').options.length===2,'no phantom options');
check(!$('queueReadLabel').classList.contains('hidden'),'chats unread filter visible');
check($('queueRead').labels[0].querySelector('.sr-only'),'unread filter retains accessible label');
state.view='mine';renderQueueFilters();renderQueue();
check($('queueStaffLabel').classList.contains('hidden')&&$('queueStatusLabel').classList.contains('hidden'),'mine cannot choose another staff');
check(!$('queueReadLabel').classList.contains('hidden'),'mine unread filter visible');
state.queueFilters.read='unread';renderQueueFilters();check($('queueRead').value==='unread','unread selection');
const keptItems=state.queueItems;state.queueItems=[];state.total=0;renderQueue();
check($('queue').textContent.includes('No unread conversations'),'unread empty state');
state.queueItems=keptItems;state.total=1;state.queueFilters.read='all';renderQueueFilters();renderQueue();
check($('queue').querySelector('.unread-dot'),'mine unread dot');
state.queueItems[0].dashboard_unread_count=0;renderQueue();check(!$('queue').querySelector('.unread-dot'),'confirmed read clears dot');
state.view='history';renderQueueFilters();check(!$('queueStaffLabel').classList.contains('hidden'),'history staff filter');
check($('queueReadLabel').classList.contains('hidden'),'history has no unread filter');
state.view='chats';state.queueItems[0].dashboard_unread_count=2;renderQueueFilters();renderQueue();
$('sessionLoading').classList.add('hidden');$('dashboard').classList.remove('hidden');
$('agentName').textContent='Test Agent';$('ticketTitle').textContent='Select a conversation';
for(const button of $('tabs').querySelectorAll('[data-view]'))button.classList.toggle('active',button.dataset.view==='chats');
document.body.dataset.result='passed';
} catch(e){document.body.dataset.result='FAILED: '+e.stack}
'''
    html = _configure(_TEMPLATE.replace('__SUPPORT_THEME__', '')
                      .replace('__SUPPORT_STYLE__', '<style>'+_ASSETS['support.css']+'</style>')
                      .replace('__SUPPORT_SCRIPTS__', '<script>'+script+'</script>'))
    page = tmp_path/'queue-filters.html'; page.write_text(html)
    command = [browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
               '--disable-background-networking', '--user-data-dir='+str(tmp_path/'profile'),
               '--virtual-time-budget=500', '--dump-dom', page.as_uri()]
    if os.environ.get('SUPPORT_QUEUE_SCREENSHOT'):
        command[1:1] = ['--window-size=1440,1000', '--screenshot='+os.environ['SUPPORT_QUEUE_SCREENSHOT']]
    result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert 'data-result="passed"' in result.stdout, result.stdout[result.stdout.find('<body'):][:700]
