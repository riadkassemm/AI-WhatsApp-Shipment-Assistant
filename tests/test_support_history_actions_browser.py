"""Exercise historical message actions through the shipped dashboard and DOM.

As in test_support_delete_browser, only the loopback HTTP API and event transport
are replaced. No WhatsApp, database, Redis or customer messages are used.
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

from tests.test_support_delete_browser import SETUP


CHECKS = r'''<script>
(async()=>{try{
  const origin=TEST_ORIGIN;
  const check=(condition,message)=>{if(!condition)throw Error(message)};
  const waitFor=async(condition,message)=>{
    const deadline=Date.now()+8000;
    while(!condition()){
      if(Date.now()>deadline)throw Error('Timed out: '+message);
      await new Promise(resolve=>setTimeout(resolve,10));
    }
  };
  const row=id=>$('conversation').querySelector('[data-message-id="'+id+'"]');
  const action=(id,name)=>row(id)?.querySelector('[data-action="'+name+'"]');
  await bootstrap();
  stopDashboardRefresh();
  await waitFor(()=>!dashboardRefresh.running,'background refresh finished');
  state.view=origin==='STAFF'?'chats':'mine';
  await openTicket(42,{conversationOrigin:origin});
  check(state.composerEnabled,'Claimed conversation is not replyable');
  const selectionEpoch=state.historySelectionEpoch;
  const textarea=$('message');
  const draft='Keep this draft while acting on previous tickets';
  const unchanged=()=>{
    check(String(state.ticketId)==='42','Selected conversation changed');
    check(state.historySelectionEpoch===selectionEpoch,'Conversation was reselected');
    check(state.currentConversationOrigin===origin,'Conversation origin changed');
    check($('message')===textarea&&textarea.value===draft,'Message draft changed');
    check(state.replyTarget?.id===901&&state.replyTarget?.ticketId===40,'Historical reply target changed');
    check(state.composerEnabled&&!textarea.disabled&&!$('sendBtn').disabled,'Composer was left disabled');
  };

  for(const name of ['delete','forward','react','reply','select']){
    check(action(901,name),'Historical message missing '+name);
  }
  check(row(901).dataset.ticketId==='40','Historical message lost its source ticket');
  check(action(890,'delete')&&action(890,'forward')&&action(890,'select'),
    'Other-line history lost non-provider actions');
  check(!action(890,'reply')&&!action(890,'react'),'Other-line history offered incompatible provider actions');
  check([...$('conversation').querySelectorAll('.context-msg')].every(item=>
    !item.querySelector('[data-action="delete"],[data-action="reply"],[data-action="react"],[data-action="forward"],[data-action="select"]')),
    'AI context exposed persisted message actions');
  action(901,'reply').click();
  textarea.value=draft;
  textarea.dispatchEvent(new Event('input',{bubbles:true}));

  // Select one historical and one current message, then use the real picker.
  action(901,'select').click();
  action(1001,'select').click();
  check(state.selectedMessages.size===2,'Mixed history selection failed');
  check(state.selectedMessages.get('901').ticket_id===40&&state.selectedMessages.get('1001').ticket_id===42,
    'Selection lost the original ticket IDs');
  $('forwardSelectedBtn').click();
  await waitFor(()=>state.activeModal==='forwardModal'&&state.forwardConversations.some(item=>Number(item.id)===43),
    'forward destinations loaded');
  await waitFor(()=>document.activeElement===$('forwardSearch'),'forward dialog focus');
  const destination=[...$('forwardConversationsList').querySelectorAll('.contact-picker-row')]
    .find(item=>item.textContent.includes('Synthetic destination'));
  check(destination,'Owned forwarding destination was not rendered');
  destination.click();
  $('confirmForwardBtn').click();
  await waitFor(()=>!state.forwardBusy&&state.activeModal===null,'mixed forwarding completed');
  check(state.selectedMessages.size===0&&!state.selectingMessages,'Forwarded selection was not cleared');
  unchanged();

  // Both setting and removing a reaction must update the historical row.
  for(const expected of ['👍','']){
    action(901,'react').click();
    row(901).querySelector('.reaction-option').click();
    check(state.busy,'Reaction did not enter its request state');
    await waitFor(()=>!state.busy,'history reaction completed');
    const reaction=state.previousTicketHistory.find(thread=>Number(thread.ticket.id)===40)
      .messages.find(message=>Number(message.id)===901).reactions
      .find(item=>item.reactor_type==='AGENT');
    check(reaction?.emoji===expected,'Historical reaction was not refreshed');
    check(Boolean(row(901).querySelector('.message-reactions'))===Boolean(expected),
      'Historical reaction display is stale');
    unchanged();
  }

  // Delete an owned historical message for everyone and another for this user.
  // The quote target is a third historical message and must survive both.
  for(const [id,scope] of [[902,'everyone'],[903,'me']]){
    action(id,'select').click();
    textarea.focus();
    textarea.setSelectionRange(5,16,'backward');
    action(id,'delete').click();
    await waitFor(()=>document.activeElement===$('deleteMeBtn'),'delete dialog focus');
    const confirm=$(scope==='everyone'?'deleteEveryoneBtn':'deleteMeBtn');
    check(!confirm.disabled&&!confirm.classList.contains('hidden'),'Historical delete option unavailable');
    confirm.click();
    check(state.busy&&textarea.disabled,'Historical deletion did not disable the composer during its request');
    await waitFor(()=>!state.busy&&state.activeModal===null,'historical deletion completed');
    unchanged();
    check(textarea.selectionStart===5&&textarea.selectionEnd===16&&textarea.selectionDirection==='backward',
      'Historical deletion changed the draft selection');
    check(!state.selectedMessages.has(String(id)),'Deleted history remained selected');
    check(scope==='me'?!row(id):row(id)?.textContent.includes('This message was deleted'),
      'Historical deletion was not rendered');
    check(!document.body.classList.contains('modal-open')&&!document.querySelector('.modal-backdrop:not(.hidden)'),
      'Delete dialog left a blocking overlay');
  }
  clearMessageSelection();
  check($('queue').querySelector('.ticket-row[aria-selected="true"]'),'Sidebar selection disappeared');
  for(const name of ['reply','forward'])check(!action(901,name).disabled,name+' remained disabled');
  check(action(901,'react').getAttribute('aria-disabled')==='false','React remained disabled');
  textarea.scrollIntoView({block:'nearest'});
  const bounds=textarea.getBoundingClientRect();
  check(document.elementFromPoint(bounds.left+bounds.width/2,bounds.top+bounds.height/2)===textarea,
    'Composer is covered after historical deletion');
  textarea.focus();
  check(document.activeElement===textarea,'Composer cannot receive focus');

  // The outgoing reply belongs to the current ticket and quotes the old message.
  $('sendBtn').click();
  await supportSendQueue.waitForGroup(supportSendGroup(state.ticketId));
  check(row(1003)?.textContent.includes(draft),'Reply to historical message was not rendered');
  check(textarea.value===''&&state.replyTarget===null,'Successful reply did not clear the draft');
  check(String(state.ticketId)==='42'&&state.historySelectionEpoch===selectionEpoch,'Reply changed the selected conversation');
  check(!textarea.disabled&&!$('imageBtn').disabled,'Composer stopped working after the reply');
  check(!dashboardRefresh.started&&dashboardRefresh.timer===null,'Background polling masked the result');
  check(browserErrors.length===0,browserErrors.join('; '));
  await fetch('/test-result',{method:'POST',body:JSON.stringify({passed:true})});
}catch(error){await fetch('/test-result',{method:'POST',body:JSON.stringify({error:String(error),stack:error.stack,browserErrors})})}})();
</script>'''


@pytest.mark.parametrize('origin', ['CUSTOMER', 'STAFF'])
def test_history_actions_keep_original_ticket_ids_and_active_composer_in_browser(
    tmp_path: Path, monkeypatch, origin: str,
):
    from app import support_ui
    monkeypatch.setattr(support_ui.settings, 'support_ui_enabled', True)
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured Chrome/Chromium browser'
    page = asyncio.run(support_ui.support_dashboard()).body.decode()
    checks = CHECKS.replace('TEST_ORIGIN', json.dumps(origin))
    page = page.replace('</head>', SETUP + '</head>').replace('</body>', checks + '</body>')
    ticket = {
        'id': 42, 'ticket_reference': 'T-42', 'customer_name': 'Synthetic customer',
        'whatsapp_phone': '0000000', 'status': 'ASSIGNED', 'conversation_origin': origin,
        'active_key': 'synthetic-42', 'current_assignee_id': '7', 'current_assignee_name': 'Test staff',
        'business_phone_number_id': '90005', 'business_display_phone_number': '+000', 'display_name': 'Test line',
        'created_at': '2026-10-30T09:00:00', 'last_activity_at': '2026-10-30T10:01:00',
        'unread_count': 0, 'dashboard_unread_count': 0,
    }
    destination = {**ticket, 'id': 43, 'ticket_reference': 'T-43',
                   'customer_name': 'Synthetic destination', 'whatsapp_phone': '0000001', 'active_key': 'synthetic-43'}
    incoming = {
        'id': 1001, 'ticket_id': 42, 'direction': 'INBOUND', 'sender_type': 'CUSTOMER',
        'message_type': 'text', 'body': 'Synthetic current message', 'whatsapp_message_id': 'ld:90005:CURRENT',
        'created_at': '2026-10-30T10:00:00', 'reactions': [],
    }
    historical = {**incoming, 'id': 901, 'ticket_id': 40, 'body': 'Synthetic historical reply target',
                  'whatsapp_message_id': 'ld:90005:HISTORICAL', 'created_at': '2026-10-29T10:00:00'}
    owned = {
        **historical, 'id': 902, 'direction': 'OUTBOUND', 'sender_type': 'AGENT', 'sender_id': '7',
        'sender_name': 'Test staff', 'body': 'Synthetic historical message to delete for everyone',
        'whatsapp_message_id': 'ld:90005:OWNED', 'created_at': '2026-10-29T10:01:00',
        'send_status': 'SENT', 'can_delete_for_everyone': True, 'can_edit': True, 'version': 1,
    }
    other = {**historical, 'id': 903, 'body': 'Synthetic historical message to hide',
             'whatsapp_message_id': 'ld:90005:OTHER', 'created_at': '2026-10-29T10:02:00'}
    source = {**ticket, 'id': 40, 'ticket_reference': 'T-40', 'status': 'CLOSED', 'active_key': None,
              'current_assignee_id': '8', 'conversation_origin': 'STAFF' if origin == 'CUSTOMER' else 'CUSTOMER'}
    cross_line = {**source, 'id': 39, 'ticket_reference': 'T-39', 'business_phone_number_id': '90006'}
    threads = [
        {'ticket': cross_line, 'context': {}, 'messages_has_more': False,
         'messages': [{**historical, 'id': 890, 'ticket_id': 39, 'whatsapp_message_id': 'ld:90006:CROSS',
                       'body': 'Synthetic message from a different business line'}]},
        {'ticket': source, 'context': {'recent_messages': [{'role': 'assistant', 'content': 'Synthetic AI context'}]},
         'messages_has_more': False, 'messages': [historical, owned, other]},
    ]
    detail = {
        'ticket': ticket, 'context': {}, 'shipment': None, 'events': [], 'messages': [incoming],
        'messages_has_more': False, 'previous_ticket_history': threads, 'previous_ticket_history_has_more': False,
        'customer_identity': {'source': 'guest', 'registered_user': False, 'saved_contact': False},
        'permissions': {'can_reply': True, 'can_operate': True, 'can_transfer': True, 'can_forward': True,
                        'can_claim': False, 'can_reopen': False, 'is_read_only_observer': False,
                        'can_delete_history': False, 'can_delete_chat': False, 'is_supervisor': False},
    }
    lock, finished = Lock(), Event()
    results, action_requests, sent_messages = [], [], []

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
                    content = {'items': [ticket, destination], 'total': 2, 'page': 1, 'page_size': 50}
                elif path in {'/api/support/tickets/42', '/api/support/chats/42'}:
                    content = detail
                elif path == '/api/support/queue-counts':
                    content = {'counts': {'unassigned': 0, 'mine': 2, 'chats': 2}}
                else:
                    content = {}
                payload = json.dumps(content).encode()
            self._send(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            path = urlsplit(self.path).path
            content = {}
            with lock:
                if path.endswith(('/forward', '/reaction', '/delete')):
                    action_requests.append((path, body))
                if path in {'/api/support/tickets/40/messages/901/forward', '/api/support/tickets/42/messages/1001/forward'}:
                    content = {'message': {'id': 2000 + len(action_requests), 'ticket_id': 43, 'send_status': 'SENT'}}
                elif path == '/api/support/tickets/40/messages/901/reaction':
                    reaction = {'id': 1, 'ticket_id': 40, 'message_id': 901, 'emoji': body.get('emoji'),
                                'reactor_type': 'AGENT', 'reactor_id': '7', 'reactor_name': 'Test staff'}
                    historical['reactions'] = [reaction]
                    content = {'reaction': reaction}
                elif path == '/api/support/tickets/40/messages/902/delete':
                    deleted = {**owned, 'body': None, 'deleted_for_everyone_at': '2026-10-30T10:02:00',
                               'can_edit': False, 'can_delete_for_everyone': False, 'version': 2}
                    threads[1]['messages'] = [deleted if item['id'] == 902 else item for item in threads[1]['messages']]
                    content = {'message': deleted}
                elif path == '/api/support/tickets/40/messages/903/delete':
                    threads[1]['messages'] = [item for item in threads[1]['messages'] if item['id'] != 903]
                    content = {'message': {'id': 903, 'deleted_for_me': True}}
                elif path == '/api/support/tickets/42/messages':
                    sent_messages.append(body)
                    sent = {**owned, 'id': 1003, 'ticket_id': 42, 'body': body.get('body'),
                            'client_message_id': body.get('client_message_id'), 'whatsapp_message_id': 'ld:90005:NEXT',
                            'created_at': '2026-10-30T10:03:00', 'reply_to': historical}
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
        assert finished.wait(35), 'Dashboard did not complete the historical message action regression'
        assert results == [{'passed': True}], results
        assert [path for path, _ in action_requests] == [
            '/api/support/tickets/40/messages/901/forward',
            '/api/support/tickets/42/messages/1001/forward',
            '/api/support/tickets/40/messages/901/reaction',
            '/api/support/tickets/40/messages/901/reaction',
            '/api/support/tickets/40/messages/902/delete',
            '/api/support/tickets/40/messages/903/delete',
        ]
        forwarded = [body for path, body in action_requests if path.endswith('/forward')]
        assert all(body['destination_ticket_id'] == 43 and body['client_message_id'] for body in forwarded)
        assert forwarded[0]['client_message_id'] != forwarded[1]['client_message_id']
        reactions = [body for path, body in action_requests if path.endswith('/reaction')]
        assert [body['emoji'] for body in reactions] == ['👍', '']
        assert all(body['context_ticket_id'] == 42 and body['client_reaction_id'] for body in reactions)
        assert [body for path, body in action_requests if path.endswith('/delete')] == [{'scope': 'everyone'}, {'scope': 'me'}]
        assert len(sent_messages) == 1, sent_messages
        assert sent_messages[0]['body'] == 'Keep this draft while acting on previous tickets'
        assert sent_messages[0]['reply_to_message_id'] == 901
        assert sent_messages[0]['client_message_id']
    finally:
        process.terminate()
        process.wait(5)
        server.shutdown()
        server.server_close()
        thread.join(5)
