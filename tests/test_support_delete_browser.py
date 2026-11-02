"""Delete through the shipped dashboard without losing the active conversation.

Chrome runs the real assets and DOM against a loopback fake API. No WhatsApp,
database, Redis, staff accounts or customer messages are used.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from urllib.parse import urlsplit

import pytest


SETUP = r'''<script>
window.browserErrors=[];
window.addEventListener('error',event=>browserErrors.push(event.message));
window.addEventListener('unhandledrejection',event=>browserErrors.push(String(event.reason)));
// Only the event transport is replaced; rendering, controls and timers are native.
class TestEventSource { close() {} }
window.EventSource=TestEventSource;
</script>'''

CHECKS = r'''<script>
(async()=>{try{
  const origin=TEST_ORIGIN,scope=TEST_SCOPE;
  const check=(condition,message)=>{if(!condition)throw Error(message)};
  const waitFor=async(condition,message)=>{
    const deadline=Date.now()+8000;
    while(!condition()){
      if(Date.now()>deadline)throw Error('Timed out: '+message);
      await new Promise(resolve=>setTimeout(resolve,10));
    }
  };
  const row=id=>$('conversation').querySelector('[data-message-id="'+id+'"]');

  await bootstrap();
  // A later poll must not conceal controls left disabled by the delete refresh.
  stopDashboardRefresh();
  await waitFor(()=>!dashboardRefresh.running,'background refresh finished');
  state.view=origin==='STAFF'?'chats':'mine';
  await openTicket(42,{conversationOrigin:origin});
  check(state.composerEnabled,'Conversation was not replyable');
  check(row(1001)&&row(1002),'Initial messages did not render');
  const selectionEpoch=state.historySelectionEpoch;
  const textarea=$('message');
  const draft='Keep this unsent reply after deleting another message';
  row(1001).querySelector('[data-action="reply"]').click();
  textarea.value=draft;
  textarea.dispatchEvent(new Event('input',{bubbles:true}));
  textarea.focus();
  textarea.setSelectionRange(5,16,'backward');
  const reply=JSON.stringify(state.replyTarget);
  check(state.replyTarget?.id===1001,'Reply target was not selected');

  row(1002).querySelector('[data-action="delete"]').click();
  check(state.activeModal==='messageDeleteModal','Delete dialog did not open');
  // Wait for the real dialog focus timer before activating its submit button.
  await waitFor(()=>document.activeElement===$('deleteMeBtn'),'delete dialog focus');
  const confirm=$(scope==='everyone'?'deleteEveryoneBtn':'deleteMeBtn');
  check(!confirm.disabled&&!confirm.classList.contains('hidden'),'Delete option unavailable');
  confirm.click();
  check(state.busy,'Delete did not enter busy state');
  check(textarea.disabled&&$('sendBtn').disabled,'Composer stayed active during deletion');
  await waitFor(()=>!state.busy&&state.activeModal===null,'delete and silent refresh completed');

  check(String(state.ticketId)==='42','Selected conversation changed');
  check(state.currentConversationOrigin===origin,'Conversation origin changed');
  check(state.historySelectionEpoch===selectionEpoch,'Conversation was reselected');
  check($('queue').querySelector('.ticket-row[aria-selected="true"]'),'Sidebar selection disappeared');
  check($('message')===textarea,'Composer textarea was replaced');
  check(textarea.value===draft,'Unsent draft changed');
  check(textarea.selectionStart===5&&textarea.selectionEnd===16&&textarea.selectionDirection==='backward',
    'Composer selection changed');
  check(JSON.stringify(state.replyTarget)===reply&&!$('replyDraft').classList.contains('hidden'),
    'Reply to the other message was lost');
  check(state.composerEnabled,'Composer permission was lost');
  for(const id of ['message','sendBtn','imageBtn','voiceBtn','locationBtn']){
    check(!$(id).disabled,id+' remained disabled after deletion');
  }
  const headerButtons=[...$('actions').querySelectorAll('[data-ticket-action="true"]')];
  check(headerButtons.length>0&&headerButtons.every(button=>!button.disabled),'Header actions remained disabled');
  const inbound=row(1001);
  check(!inbound.querySelector('[data-action="reply"]').disabled,'Reply remained disabled');
  check(!inbound.querySelector('[data-action="forward"]').disabled,'Forward remained disabled');
  const reaction=inbound.querySelector('.message-reaction-menu');
  check(!reaction.classList.contains('disabled')&&reaction.querySelector('summary').getAttribute('aria-disabled')==='false',
    'Reactions remained disabled');
  check($('messageDeleteModal').classList.contains('hidden')&&$('messageDeleteModal').getAttribute('aria-hidden')==='true',
    'Delete dialog remained visible');
  check(!document.body.classList.contains('modal-open')&&!document.querySelector('.modal-backdrop:not(.hidden)'),
    'Modal overlay remained active');
  if(scope==='everyone'){
    check(row(1002)?.textContent.includes('This message was deleted'),'Deleted placeholder missing');
    check(!row(1002).textContent.includes('Synthetic message to delete'),'Deleted content remained');
  }else{
    check(!row(1002),'Delete for me left the message visible');
  }

  // Check hit-testing as well as focus: a leftover overlay can block clicks even
  // when programmatic focus works. Send the preserved draft through the real UI.
  textarea.scrollIntoView({block:'nearest'});
  const bounds=textarea.getBoundingClientRect();
  check(document.elementFromPoint(bounds.left+bounds.width/2,bounds.top+bounds.height/2)===textarea,
    'Composer is covered by another element');
  textarea.focus();
  check(document.activeElement===textarea,'Composer could not receive focus');
  $('sendBtn').click();
  await state.textSendChain;
  check(row(1003)?.textContent.includes(draft),'Next message was not delivered and rendered');
  check(textarea.value===''&&state.replyTarget===null,'Successful send did not clear the draft');
  check(String(state.ticketId)==='42'&&state.currentConversationOrigin===origin,'Send changed conversation');
  check(state.historySelectionEpoch===selectionEpoch,'Sending required reselecting the conversation');
  check(!textarea.disabled&&!$('imageBtn').disabled,'Composer stopped working after sending');
  check(!dashboardRefresh.started&&dashboardRefresh.timer===null,'Background polling masked the result');
  check(browserErrors.length===0,browserErrors.join('; '));
  await fetch('/test-result',{method:'POST',body:JSON.stringify({passed:true})});
}catch(error){await fetch('/test-result',{method:'POST',body:JSON.stringify({error:String(error),stack:error.stack,browserErrors})})}})();
</script>'''


@pytest.mark.parametrize('origin', ['CUSTOMER', 'STAFF'])
@pytest.mark.parametrize('scope', ['everyone', 'me'])
def test_deleting_message_keeps_conversation_and_composer_active_in_browser(
    tmp_path: Path, monkeypatch, origin: str, scope: str,
):
    from app import support_ui
    monkeypatch.setattr(support_ui.settings, 'support_ui_enabled', True)
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured Chrome/Chromium browser'
    page = asyncio.run(support_ui.support_dashboard()).body.decode()
    checks = CHECKS.replace('TEST_ORIGIN', json.dumps(origin)).replace('TEST_SCOPE', json.dumps(scope))
    page = page.replace('</head>', SETUP + '</head>').replace('</body>', checks + '</body>')
    ticket = {
        'id': 42, 'ticket_reference': 'T-42', 'customer_name': 'Synthetic customer',
        'whatsapp_phone': '0000000', 'status': 'ASSIGNED', 'conversation_origin': origin,
        'active_key': 'synthetic-42', 'current_assignee_id': '7', 'current_assignee_name': 'Test staff',
        'business_phone_number_id': '90005', 'business_display_phone_number': '+000', 'display_name': 'Test line',
        'created_at': '2026-10-30T09:00:00', 'last_activity_at': '2026-10-30T10:01:00',
        'unread_count': 0, 'dashboard_unread_count': 0,
    }
    incoming = {
        'id': 1001, 'ticket_id': 42, 'direction': 'INBOUND', 'sender_type': 'CUSTOMER',
        'message_type': 'text', 'body': 'Synthetic inbound reply target',
        'whatsapp_message_id': 'synthetic-inbound', 'created_at': '2026-10-30T10:00:00', 'reactions': [],
    }
    outgoing = {
        'id': 1002, 'ticket_id': 42, 'direction': 'OUTBOUND', 'sender_type': 'AGENT',
        'sender_id': '7', 'sender_name': 'Test staff', 'message_type': 'text',
        'body': 'Synthetic message to delete', 'whatsapp_message_id': 'synthetic-outbound',
        'created_at': '2026-10-30T10:01:00', 'send_status': 'SENT', 'reactions': [],
        'can_delete_for_everyone': True, 'can_edit': True, 'version': 1,
    }
    detail = {
        'ticket': ticket, 'context': {}, 'shipment': None, 'events': [], 'messages': [incoming, outgoing],
        'messages_has_more': False, 'previous_ticket_history': [], 'previous_ticket_history_has_more': False,
        'customer_identity': {'source': 'guest', 'registered_user': False, 'saved_contact': False},
        'permissions': {'can_reply': True, 'can_operate': True, 'can_transfer': True, 'can_forward': True,
                        'can_claim': False, 'can_reopen': False, 'is_read_only_observer': False,
                        'can_delete_history': False, 'can_delete_chat': False, 'is_supervisor': False},
    }
    lock, finished = Lock(), Event()
    results, detail_requests, delete_requests, sent_messages = [], [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, content, mime='application/json', status=200):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/support':
                return self._send(page.encode(), 'text/html')
            if path.startswith('/support/assets/'):
                response = asyncio.run(support_ui.support_asset(path.rsplit('/', 1)[-1]))
                return self._send(response.body, response.headers['content-type'], response.status_code)
            with lock:
                if path == '/api/support/me':
                    content = {'id': '7', 'name': 'Test staff', 'role': 'agent'}
                elif path in {'/api/support/tickets', '/api/support/chats'}:
                    content = {'items': [ticket], 'total': 1, 'page': 1, 'page_size': 50}
                elif path in {'/api/support/tickets/42', '/api/support/chats/42'}:
                    detail_requests.append(path)
                    content = detail
                elif path == '/api/support/queue-counts':
                    content = {'counts': {'unassigned': 0, 'mine': 1, 'chats': 1}}
                else:
                    content = {}
                payload = json.dumps(content).encode()
            self._send(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            path = urlsplit(self.path).path
            content = {}
            with lock:
                if path == '/api/support/tickets/42/messages/1002/delete':
                    delete_requests.append(body)
                    if body.get('scope') == 'me':
                        detail['messages'] = [incoming]
                    else:
                        deleted = {**outgoing, 'body': None, 'deleted_for_everyone_at': '2026-10-30T10:02:00',
                                   'can_edit': False, 'can_delete_for_everyone': False, 'version': 2}
                        detail['messages'] = [incoming, deleted]
                        content = {'message': deleted}
                elif path == '/api/support/tickets/42/messages':
                    sent_messages.append(body)
                    sent = {**outgoing, 'id': 1003, 'body': body.get('body'),
                            'client_message_id': body.get('client_message_id'),
                            'whatsapp_message_id': 'synthetic-next', 'created_at': '2026-10-30T10:03:00',
                            'reply_to': incoming}
                    detail['messages'].append(sent)
                    content = {'message': sent}
                elif path == '/test-result':
                    results.append(body)
                payload = json.dumps(content).encode()
            self._send(payload)
            if path == '/test-result':
                finished.set()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = subprocess.Popen([
        browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
        '--disable-background-networking', '--no-first-run', '--window-size=1440,1000',
        '--user-data-dir=' + str(tmp_path / 'profile'), f'http://127.0.0.1:{server.server_port}/support',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert finished.wait(25), 'Dashboard did not complete the delete-and-send regression'
        assert results == [{'passed': True}], results
        detail_path = '/api/support/' + ('chats' if origin == 'STAFF' else 'tickets') + '/42'
        assert detail_requests == [detail_path, detail_path], detail_requests
        assert delete_requests == [{'scope': scope}]
        assert len(sent_messages) == 1, sent_messages
        assert sent_messages[0]['body'] == 'Keep this unsent reply after deleting another message'
        assert sent_messages[0]['reply_to_message_id'] == 1001
        assert sent_messages[0]['client_message_id']
    finally:
        process.terminate()
        process.wait(5)
        server.shutdown()
        server.server_close()
        thread.join(5)
