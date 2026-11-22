"""AI tickets in the shipped dashboard (Step 3c), with synthetic data and no providers."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from urllib.parse import urlsplit


SETUP = r'''<script>
window.browserErrors=[];
window.addEventListener('error',event=>browserErrors.push(event.message));
window.addEventListener('unhandledrejection',event=>browserErrors.push(String(event.reason)));
class TestEventSource { constructor(){ window.testSource=this; } close() {} }
window.EventSource=TestEventSource;
const base={ticket_reference:'T-AI',whatsapp_phone:'15550009101',customer_name:'Test Customer',
  conversation_origin:'CUSTOMER',business_phone_number_id:'90005',business_display_phone_number:'15550000000',
  reason:'AI conversation',ai_summary:'',created_at:'2026-11-22T08:00:00',last_activity_at:'2026-11-22T08:05:00'};
const aiTicket={...base,id:501,status:'NEW',active_key:'90005:15550009101',handling_mode:'AI',
  ai_last_reply_at:'2026-11-22T08:05:00',current_assignee_id:null,current_assignee_name:null};
const escalated={...base,id:502,ticket_reference:'T-ESC',status:'NEW',active_key:'90005:15550009102',
  handling_mode:'HUMAN',ai_escalation_reason:'IDENTITY_VERIFICATION_REQUIRED',ai_escalated_at:'2026-11-22T08:06:00',
  reason:'IDENTITY_VERIFICATION_REQUIRED',ai_summary:'Customer asks for the wallet balance.',
  current_assignee_id:null,current_assignee_name:null};
const messages=[
  {id:1,ticket_id:501,sender_type:'CUSTOMER',direction:'INBOUND',message_type:'text',body:'Where is my parcel?',
   created_at:'2026-11-22T08:04:00',send_status:'RECEIVED'},
  {id:2,ticket_id:501,sender_type:'AI',sender_name:'AI assistant',direction:'OUTBOUND',message_type:'text',
   body:'Your parcel is in Beirut.',created_at:'2026-11-22T08:05:00',send_status:'SENT'},
  {id:3,ticket_id:501,sender_type:'CUSTOMER',direction:'INBOUND',message_type:'audio',body:'Voice note',
   ai_media_kind:'transcript',ai_media_text:'Where is my parcel?',created_at:'2026-11-22T08:06:00',send_status:'RECEIVED'},
  {id:4,ticket_id:501,sender_type:'CUSTOMER',direction:'INBOUND',message_type:'image',body:'Parcel photo',
   ai_media_kind:'image_notes',ai_media_text:'A sealed parcel with a shipping label.',created_at:'2026-11-22T08:07:00',send_status:'RECEIVED'}];
const permissions={can_operate:true,can_reply:false,can_forward:true,can_transfer:false,can_claim:false,
  can_take_over:false,can_reopen:false,can_delete_history:false,can_delete_chat:false,is_read_only_observer:false,is_supervisor:true};
const detail=(ticket,extra)=>({ticket,context:{},shipment:null,messages:ticket.id===501?messages:[],messages_has_more:false,
  events:extra.events||[],customer_identity:{source:'guest',name:ticket.customer_name},previous_ticket_history:[],
  previous_ticket_history_has_more:false,ai_usage:extra.ai_usage||null,permissions:{...permissions,...extra.permissions}});
window.takeOvers=[];window.verifications=[];
const nativeFetch=window.fetch.bind(window);
window.fetch=async(url,options={})=>{
  const parsed=new URL(url,location.origin),path=parsed.pathname;
  const reply=data=>Promise.resolve(new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}}));
  if(path==='/api/support/tickets'){
    const view=parsed.searchParams.get('view');
    const items=view==='ai'?[aiTicket]:view==='unassigned'?[escalated]:[];
    return reply({items,total:items.length,page:1,page_size:30,filter_options:{support_numbers:[],staff:[]}});
  }
  if(path==='/api/support/queue-counts') return reply({counts:{ai:1,unassigned:1,mine:0,chats:0}});
  if(path==='/api/support/tickets/501') return reply(detail(aiTicket,{permissions:{can_take_over:true},
    ai_usage:{estimated:true,requests:3,pending:0,cost_usd:0.0042}}));
  if(path==='/api/support/tickets/502') return reply(detail(escalated,{permissions:{can_claim:true},
    events:[{id:9,event_type:'AI_ESCALATED',actor_type:'AI',actor_name:'AI assistant',created_at:'2026-11-22T08:06:00',
      details_json:'{"reason_code":"IDENTITY_VERIFICATION_REQUIRED"}'}]}));
  if(path==='/api/support/tickets/501/take-over'&&options.method==='POST'){takeOvers.push(path);return reply({changed:true});}
  if(path==='/api/support/tickets/501/ai-identity') return reply({level:'L1',reason:'PHONE_MATCH',customer_userid:'cust-501',
    can_verify:true,can_revoke:false,verification_days:30,verification:null});
  if(path==='/api/support/tickets/501/ai-identity/verify'&&options.method==='POST'){
    verifications.push(JSON.parse(options.body));
    return reply({level:'L2',reason:'STAFF_VERIFIED',customer_userid:'cust-501',can_verify:true,can_revoke:true,verification_days:30,
      verification:{id:1,verified_at:'2026-11-22T09:00:00+00:00',expires_at:'2026-12-22T09:00:00+00:00',verified_by_staff_name:'Demo Admin'}});
  }
  if(/^\/api\/support\/tickets\/\d+\/ai-identity$/.test(path)) return reply({level:'L0',reason:'NO_MATCH',can_verify:false,can_revoke:false});
  return nativeFetch(url,options);
};
</script>'''

CHECKS = r'''<script>
(async()=>{try{
  await bootstrap();
  const check=(condition,message)=>{if(!condition)throw Error(message)};
  const tab=$('tabs').querySelector('[data-view="ai"]');
  check(tab && tab.textContent.includes('AI tickets'),'AI tickets tab missing');
  setQueueView('ai'); await loadQueue();
  check($('queue').textContent.includes('AI answering'),'AI chip missing from the AI queue');

  await openTicket(501);
  check($('conversation').textContent.includes('AI transcript: Where is my parcel?'),'Transcript missing');
  check($('conversation').textContent.includes('AI image notes: A sealed parcel'),'Image notes missing');
  const probe=document.createElement('div');
  addMessage(probe,{...messages[3],ai_media_text:'<img src=x onerror=alert(1)>'});
  check(probe.textContent.includes('<img src=x'),'Media notes were not escaped');
  check(!probe.querySelector('img[src="x"]'),'Media notes executed as HTML');
  const deletedProbe=document.createElement('div');
  addMessage(deletedProbe,{...messages[3],deleted_for_everyone_at:'2026-11-22T08:08:00'});
  check(!deletedProbe.textContent.includes('A sealed parcel'),'Deleted media notes exposed');
  const banner=$('readOnlyBanner');
  check(!banner.classList.contains('hidden') && banner.textContent.includes('AI assistant is answering'),'AI banner missing');
  const actions=$('actions').textContent;
  check(actions.includes('Take over'),'Take over button missing');
  check(!actions.includes('Claim ticket') && !actions.includes('Resolve') && !actions.includes('Change status'),
    'Staff actions shown while the AI handles the ticket: '+actions);
  check($('message').disabled,'Composer enabled on an AI ticket');
  const context=document.body.textContent;
  check(context.includes('The AI assistant is answering this conversation'),'AI assistant card missing');
  check(context.includes('AI cost (estimate)') && context.includes('$0.0042'),'Per-ticket AI cost missing');
  check($('conversation').textContent.includes('Your parcel is in Beirut.'),'AI message missing from the timeline');
  const waitFor=async(test,message)=>{for(let i=0;i<100&&!test();i++) await new Promise(r=>setTimeout(r,20)); check(test(),message);};
  await waitFor(()=>$('aiIdentitySlot').textContent.includes('Phone matched'),'AI identity card missing');
  const verifyButton=[...$('aiIdentitySlot').querySelectorAll('button')].find(b=>b.textContent.includes('Verify customer for AI'));
  check(verifyButton,'Verify button missing');
  verifyButton.click();
  await waitFor(()=>!$('confirmModal').classList.contains('hidden') && $('confirmInput').value==='cust-501','Verify dialog missing');
  $('confirmAcceptBtn').click();
  await waitFor(()=>verifications.length===1,'Verification not sent');
  check(verifications[0].userid==='cust-501' && verifications[0].confirmed===true,'Wrong verification request');
  await waitFor(()=>$('aiIdentitySlot').textContent.includes('Verified by staff'),'Card did not show L2');
  check($('aiIdentitySlot').textContent.includes('Remove verification'),'Remove action missing');
  if(location.search.includes('preview=1')) return;

  // AI conversations never alert staff; an escalation does.
  const alerts=[]; notifyNewCustomerActivity=text=>alerts.push(text);
  testSource.onmessage({data:JSON.stringify({type:'customer_message',authorized:true,handling_mode:'AI',ticket_id:503,customer_name:'A'})});
  testSource.onmessage({data:JSON.stringify({type:'ticket_created',authorized:true,handling_mode:'AI',ticket_id:504,customer_name:'B'})});
  check(alerts.length===0,'AI conversation raised a staff alert: '+alerts.join('; '));
  testSource.onmessage({data:JSON.stringify({type:'ticket_created',authorized:true,escalated_from_ai:true,ticket_id:505,customer_name:'C'})});
  check(alerts.length===1 && alerts[0].includes('AI assistant handed a conversation with C'),'Escalation alert wrong: '+alerts.join('; '));

  const takeOver=[...$('actions').querySelectorAll('button')].find(button=>button.textContent.includes('Take over'));
  takeOver.click();
  for(let i=0;i<50&&!takeOvers.length;i++) await new Promise(resolve=>setTimeout(resolve,20));
  check(takeOvers.length===1,'Take over request not sent');
  for(let i=0;i<50&&state.view!=='mine';i++) await new Promise(resolve=>setTimeout(resolve,20));
  check(state.view==='mine','Taking over did not move to My tickets');

  setQueueView('unassigned'); await loadQueue();
  check($('queue').textContent.includes('From AI: Customer identity must be verified'),'Escalation chip missing');
  await openTicket(502);
  check($('actions').textContent.includes('Claim ticket'),'Escalated ticket is not claimable');
  check(document.body.textContent.includes('Handed over by AI') && document.body.textContent.includes('Customer identity must be verified'),
    'Escalation card missing');
  check($('conversation').textContent.includes('Handed to staff by AI: Customer identity must be verified'),'Escalation event label missing');
  check($('readOnlyBanner').classList.contains('hidden'),'AI banner shown on a human ticket');
  check(browserErrors.length===0,browserErrors.join('; '));
  await nativeFetch('/test-result',{method:'POST',body:JSON.stringify({passed:true})});
}catch(error){await nativeFetch('/test-result',{method:'POST',body:JSON.stringify({error:String(error),stack:error.stack})})}})();
</script>'''


def test_ai_tickets_in_the_dashboard(tmp_path: Path, monkeypatch):
    from app import support_ui
    monkeypatch.setattr(support_ui.settings, 'support_ui_enabled', True)
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured Chrome/Chromium browser'
    page = asyncio.run(support_ui.support_dashboard()).body.decode()
    page = page.replace('</head>', SETUP + '</head>').replace('</body>', CHECKS + '</body>')
    finished, results = Event(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            mime, status = 'application/json', 200
            if path == '/support':
                content, mime = page.encode(), 'text/html'
            elif path.startswith('/support/assets/'):
                response = asyncio.run(support_ui.support_asset(path.rsplit('/', 1)[-1]))
                content, mime, status = response.body, response.headers['content-type'], response.status_code
            elif path == '/api/support/me':
                content = b'{"id":"8","name":"Demo Admin","role":"admin"}'
            else:
                content = b'{}'
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')
            if self.path == '/test-result':
                results.append(json.loads(body))
                finished.set()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    flags = [browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
             '--disable-background-networking', '--no-first-run', '--window-size=1440,1200']
    url = f'http://127.0.0.1:{server.server_port}/support'
    process = subprocess.Popen([*flags, '--user-data-dir=' + str(tmp_path / 'profile'), url],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert finished.wait(30), 'AI tickets browser test did not complete'
        assert results == [{'passed': True}], results
        screenshot = os.environ.get('SUPPORT_AI_TICKETS_SCREENSHOT')
        if screenshot:
            subprocess.run([*flags, '--user-data-dir=' + str(tmp_path / 'preview'), '--virtual-time-budget=4000',
                            '--screenshot=' + screenshot, url + '?preview=1'], check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        process.terminate()
        process.wait(5)
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_escalation_labels_fall_back_to_readable_text():
    source = Path('app/static/support/state.js').read_text()
    helper = source[source.index('const AI_ESCALATION_LABELS'):source.index('const MESSAGE_ROLE_LABELS')]
    result = subprocess.run(['node', '-e', helper + """
const assert=require('node:assert/strict');
assert.equal(aiEscalationLabel('IDENTITY_VERIFICATION_REQUIRED'),'Customer identity must be verified');
assert.equal(aiEscalationLabel('SOMETHING_NEW_HAPPENED'),'Something new happened');
assert.equal(aiEscalationLabel(null),'');
"""], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
