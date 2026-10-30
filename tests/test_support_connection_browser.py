"""Live-update recovery and outage behavior in Chrome with the native EventSource.

The complete dashboard runs as shipped against a local fake server that can
answer 502, drop the event stream, simulate an outage, expire the session and
revoke ticket access. No real services, accounts or customer data are involved.
"""
import asyncio
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from urllib.parse import urlsplit


QUEUE_ITEMS = [{
    'id': 100 + index, 'ticket_reference': f'T-{100 + index}', 'customer_name': f'Customer {index}',
    'whatsapp_phone': '0000000', 'status': 'ASSIGNED', 'conversation_origin': 'CUSTOMER',
    'active_key': f'k{index}', 'current_assignee_id': '7', 'current_assignee_name': 'Test staff',
    'business_phone_number_id': '90005', 'business_display_phone_number': '+000', 'display_name': 'Line',
    'last_activity_at': '2026-10-30T10:00:00', 'created_at': '2026-10-30T09:00:00',
    'ai_summary': f'Synthetic summary {index}', 'unread_count': 0, 'dashboard_unread_count': 0,
} for index in range(30)]
TICKET = {**QUEUE_ITEMS[0], 'id': 42, 'ticket_reference': 'T-42', 'customer_name': 'Synthetic customer'}
DETAIL = {
    'ticket': TICKET, 'context': {}, 'shipment': None, 'events': [],
    'messages': [{'id': 1001, 'ticket_id': 42, 'direction': 'INBOUND', 'sender_type': 'CUSTOMER',
                  'message_type': 'text', 'body': 'Protected synthetic text', 'created_at': '2026-10-30T09:59:00',
                  'reactions': []}],
    'messages_has_more': False, 'previous_ticket_history': [], 'previous_ticket_history_has_more': False,
    'customer_identity': {'source': 'guest', 'name': None, 'registered_user': False, 'saved_contact': False},
    'permissions': {'can_operate': True, 'can_reply': True, 'can_reopen': False, 'can_forward': True,
                    'can_transfer': False, 'is_read_only_observer': False, 'can_claim': False,
                    'can_delete_history': False, 'can_delete_chat': False, 'is_supervisor': False},
}

SETUP = r'''<script>
window.browserErrors=[];
window.addEventListener('error',event=>browserErrors.push(event.message));
window.addEventListener('unhandledrejection',event=>browserErrors.push(String(event.reason)));
// Deterministic jitter: the lower bound (50%) of each backoff step.
Math.random=()=>0;
const NativeEventSource=window.EventSource;
window.eventSources=[];
window.EventSource=class extends NativeEventSource{constructor(...args){super(...args);eventSources.push(this);}};
</script>'''

CHECKS = r'''<script>
(async()=>{try{
  const check=(condition,message)=>{if(!condition)throw Error(message)};
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const waitFor=async(condition,message,timeout=8000)=>{
    const end=Date.now()+timeout;
    while(!condition()){if(Date.now()>end)throw Error('Timed out: '+message);await pause(25);}
  };
  const control=body=>fetch('/test-control',{method:'POST',body:JSON.stringify(body)});
  const log=async()=>(await fetch('/test-log')).json();
  const count=(entries,path,status)=>entries.filter(([p,s])=>p===path&&(status===undefined||s===status)).length;
  const live=()=>eventSources.filter(source=>source.readyState!==EventSource.CLOSED);
  const banner=()=>$('connectionBanner');

  await bootstrap();
  await openTicket(42,{conversationOrigin:'CUSTOMER'});
  check($('conversation').textContent.includes('Protected synthetic text'),'Conversation did not render');
  $('message').value='Unsent synthetic draft';
  const queueBox=$('queue');queueBox.style.height='120px';queueBox.scrollTop=200;
  const scrollTop=queueBox.scrollTop;

  // 1. Two 502 responses, then recovery: one controlled lifecycle, no native retry.
  await waitFor(()=>state.supportEventSource?.readyState===EventSource.OPEN,'live updates did not connect');
  let entries=(await log()).requests;
  check(count(entries,'/api/support/events',502)===2,'expected two 502 responses: '+JSON.stringify(entries));
  check(count(entries,'/api/support/events',200)===1,'expected one successful stream');
  check(eventSources.length===3&&live().length===1,'EventSource instances: '+eventSources.length);
  check(state.supportEventsRetryTimer===null&&state.supportEventsAttempt===0,'retry state not reset');
  await pause(4000); // longer than the server's 3 s native retry hint
  entries=(await log()).requests;
  check(count(entries,'/api/support/events')===3,'native EventSource retried in parallel');
  check(banner().classList.contains('hidden'),'notice visible while connected');

  // 2. Short outage: the stream drops and every read answers 502.
  const before=(await log()).requests.length;
  await control({api_fail:true,drop_streams:true});
  await waitFor(()=>state.supportEventSource===null,'dropped stream was not closed');
  requestDashboardRefresh('queue','conversation','counts');
  await waitFor(()=>!banner().classList.contains('hidden'),'outage notice not shown');
  check($('connectionBannerText').textContent.startsWith('Connection problem'),'notice: '+$('connectionBannerText').textContent);
  check(state.queueItems.length===30&&queueBox.querySelectorAll('.ticket-row').length===30,'queue rows were not kept');
  check(!queueBox.textContent.includes('Could not load this queue'),'queue was replaced by an error');
  check(queueBox.scrollTop===scrollTop,'queue scroll position changed');
  check($('conversation').textContent.includes('Protected synthetic text'),'conversation was cleared');
  check(String(state.ticketId)==='42','selected conversation changed');
  check($('message').value==='Unsent synthetic draft','draft was lost');
  check(!$('error').classList.contains('visible'),'temporary failure shown as an error banner');
  check(live().length===0,'a stream was opened while the session check failed');
  // Escalation for an extended outage (time-shifted; the timer path is unit tested).
  connectionHealth.failingScopes.set('queue',Date.now()-50000);renderConnectionStatus();
  check(banner().classList.contains('extended'),'extended outage was not escalated');
  check($('connectionBannerText').textContent.startsWith('Connection lost for 1 minute'),'extended text');
  await control({api_fail:false});
  await waitFor(()=>state.supportEventSource?.readyState===EventSource.OPEN,'did not reconnect after the outage',15000);
  await waitFor(()=>banner().classList.contains('hidden'),'notice did not clear after recovery');
  check(live().length===1,'more than one live stream after recovery');
  entries=(await log()).requests.slice(before);
  const reopened=entries.findIndex(([p,s])=>p==='/api/support/events'&&s===200);
  const afterOpen=entries.slice(reopened+1).map(([p])=>p);
  check(afterOpen.includes('/api/support/tickets')&&afterOpen.includes('/api/support/queue-counts')&&afterOpen.includes('/api/support/tickets/42'),
    'no authoritative reconciliation after reconnect: '+JSON.stringify(afterOpen));
  check(count(entries,'/api/support/events',200)===1,'duplicate streams after recovery');
  check($('message').value==='Unsent synthetic draft','draft lost after recovery');

  // One event after recovery produces exactly one notification.
  let notifications=0;window.notifyNewCustomerActivity=()=>{notifications++};
  await control({event:{type:'customer_message',ticket_id:77,message_id:5,authorized:true,conversation_origin:'CUSTOMER',customer_name:'Synthetic'}});
  await waitFor(()=>notifications>0,'event not delivered');
  await pause(400);
  check(notifications===1,'duplicate notifications: '+notifications);

  // 3. Revoked ticket access removes protected content instead of keeping it as stale.
  await control({ticket_status:403});
  requestDashboardRefresh('conversation');
  await waitFor(()=>state.ticketId===null,'revoked conversation stayed selected');
  check(!$('conversation').textContent.includes('Protected synthetic text'),'protected content remained');
  check($('conversation').textContent.includes('Conversation access restricted'),'no access message');
  check(banner().classList.contains('hidden'),'permission change shown as a connection problem');

  // 4. Expired session: the reconnect check follows the existing sign-in flow.
  await control({me_status:401,drop_streams:true});
  await waitFor(()=>!$('loginPage').classList.contains('hidden'),'sign-in form not shown',15000);
  check(state.authenticated===false&&state.supportEventSource===null,'session state not cleared');
  check(state.supportEventsRetryTimer===null&&!dashboardRefresh.started,'timers left running after sign-out');
  check(banner().classList.contains('hidden'),'notice visible on the sign-in page');
  const signedOut=(await log()).requests.length;
  await pause(3000);
  entries=(await log()).requests.slice(signedOut);
  check(count(entries,'/api/support/events')===0&&count(entries,'/api/support/me')===0,'requests continued after sign-out: '+JSON.stringify(entries));
  check(browserErrors.length===0,browserErrors.join('; '));
  await fetch('/test-result',{method:'POST',body:JSON.stringify({passed:true})});
}catch(error){await fetch('/test-result',{method:'POST',body:JSON.stringify({error:String(error),stack:error.stack,browserErrors})})}})();
</script>'''


def test_live_updates_recover_and_dashboard_survives_outages_in_browser(tmp_path: Path, monkeypatch):
    from app import support_ui
    monkeypatch.setattr(support_ui.settings, 'support_ui_enabled', True)
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured Chrome/Chromium browser'
    page = asyncio.run(support_ui.support_dashboard()).body.decode()
    page = page.replace('</head>', SETUP + '</head>').replace('</body>', CHECKS + '</body>')

    lock = Lock()
    control = {'events_fail': 2, 'api_fail': False, 'me_status': 200, 'ticket_status': 200}
    requests, streams, results, finished, stopping = [], [], [], Event(), Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, status, content, mime='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.end_headers()
            self.wfile.write(content)

        def _stream(self):
            commands = queue.Queue()
            with lock:
                streams.append(commands)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(b'retry: 3000\n\n')
                while not stopping.is_set():
                    try:
                        command = commands.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if command is None:
                        return
                    self.wfile.write(b'data: ' + json.dumps(command).encode() + b'\n\n')
            except OSError:
                pass
            finally:
                with lock:
                    if commands in streams:
                        streams.remove(commands)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in {'/support'} or path.startswith('/support/assets/'):
                if path == '/support':
                    return self._send(200, page.encode(), 'text/html')
                response = asyncio.run(support_ui.support_asset(path.rsplit('/', 1)[-1]))
                return self._send(response.status_code, response.body, response.headers['content-type'])
            if path == '/test-log':
                with lock:
                    return self._send(200, json.dumps({'requests': requests}).encode())
            with lock:
                if path == '/api/support/events' and control['events_fail'] > 0:
                    control['events_fail'] -= 1
                    status = 502
                elif path.startswith('/api/support/') and control['api_fail']:
                    status = 502
                elif path == '/api/support/me':
                    status = control['me_status']
                elif path == '/api/support/tickets/42':
                    status = control['ticket_status']
                else:
                    status = 200
                requests.append([path, status])
            if status == 502:
                return self._send(502, b'<html>502 Bad Gateway</html>', 'text/html')
            if status != 200:
                return self._send(status, json.dumps({'detail': 'synthetic'}).encode())
            if path == '/api/support/events':
                return self._stream()
            if path == '/api/support/me':
                content = {'id': '7', 'name': 'Test staff', 'role': 'agent'}
            elif path == '/api/support/tickets':
                content = {'items': QUEUE_ITEMS, 'total': 30, 'page': 1, 'page_size': 30}
            elif path == '/api/support/tickets/42':
                content = DETAIL
            elif path == '/api/support/queue-counts':
                content = {'counts': {'unassigned': 0, 'mine': 30, 'chats': 0}}
            else:
                content = {}
            self._send(200, json.dumps(content).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            path = urlsplit(self.path).path
            if path == '/test-control':
                with lock:
                    for key in ('api_fail', 'me_status', 'ticket_status'):
                        if key in body:
                            control[key] = body[key]
                    for commands in list(streams):
                        if body.get('drop_streams'):
                            commands.put(None)
                        elif 'event' in body:
                            commands.put(body['event'])
            elif path == '/test-result':
                results.append(body)
                finished.set()
            self._send(200, b'{}')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = subprocess.Popen([browser, '--headless', '--no-sandbox', '--disable-gpu',
        '--disable-dev-shm-usage', '--disable-background-networking', '--no-first-run',
        '--user-data-dir=' + str(tmp_path / 'profile'), f'http://127.0.0.1:{server.server_port}/support'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert finished.wait(90), 'Browser checks did not finish'
        assert results == [{'passed': True}], results
    finally:
        stopping.set()
        process.terminate()
        process.wait(5)
        server.shutdown()
        server.server_close()
        thread.join(5)
