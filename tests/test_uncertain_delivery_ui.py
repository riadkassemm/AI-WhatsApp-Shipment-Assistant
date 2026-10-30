"""Exercise the shipped DOM renderer/actions in a real browser with synthetic data."""
import os
from pathlib import Path
import shutil
import subprocess


def test_uncertain_delivery_actions_in_browser(tmp_path):
    from app.support_ui import _HTML
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured test browser'
    rendering = _HTML[_HTML.index('function addMessage('):_HTML.index('// Compact message toolbar:')]
    actions = _HTML[_HTML.index('function canCopyUncertainMessage('):_HTML.index('function canResendMessage(')]
    links = _HTML[_HTML.index('function appendMessageTextWithLinks('):_HTML.index('function createVoiceNotePlayer(')]
    copy = _HTML[_HTML.index('function messageTextForCopy('):_HTML.index('async function copyMessageText(')]
    script = r'''
const state = {ticketId:42, agent:{id:'7'}, currentTicketData:{permissions:{can_reply:true}}};
const messageClass = () => 'agent', messageActor = () => 'Staff';
const initials = () => 'EX', renderReplyQuote = () => {}, canChangeWhatsAppMessage = () => false;
const fmt = () => '12:00', messageOccurredAt = () => '2026-10-22T12:00:00Z',messageArrivedLate=()=>false;
const labelMessageAction = () => {}, messageReactionBar = () => null;
const copyMessageText = () => {}, createIcon = () => document.createElement('span');
const setButtonBusy = (button,busy) => {button.disabled=busy};
const requests=[], updates=[], alerts=[];
let accept=false, confirmation, fail=false;
const confirmDialog = async options => {confirmation=options;return accept};
const alertDialog = async options => {alerts.push(options)};
const notify = message => {alerts.push({message})};
const loadQueue = async () => {};
const upsertTimelineMessage = (m,ticket) => {updates.push({m,ticket})};
const original = {id:10,ticket_id:42,sender_type:'AGENT',sender_id:'7',direction:'OUTBOUND',message_type:'text',body:'Example message',send_status:'UNCERTAIN'};
const api = async (path,options) => {
  requests.push({path,options});
  if(fail) throw Error('Status temporarily unavailable');
  return path.endsWith('new-copy') ? {message:{...original,id:11,send_status:'SENT',replacement_for_message_id:10}} : {message:original,notice:'No confirmation is available. Nothing was resent.'};
};
''' + rendering + links + copy + actions + r'''
(async () => {try {
  const c=document.getElementById('messages');
  addMessage(c,original);
  const check=[...c.querySelectorAll('button')].find(b=>b.textContent==='Check status');
  const send=[...c.querySelectorAll('button')].find(b=>b.textContent==='Send a new copy');
  if(!check || !send || c.textContent.includes('Request review')) throw Error('Incorrect actions');
  await check.onclick();
  if(requests.length!==1 || !requests[0].path.endsWith('/42/messages/10/check-delivery')) throw Error('Status action dispatched incorrectly');
  await send.onclick();
  if(requests.length!==1 || !confirmation.message.includes('twice')) throw Error('Cancellation sent a copy or warning missing');
  accept=true;
  await send.onclick();
  if(requests.length!==2 || !JSON.parse(requests[1].options.body).accept_duplicate_risk) throw Error('Missing explicit confirmation');
  if(updates.at(-1).m.id!==11 || updates.at(-1).ticket!==42) throw Error('Copy not shown in original conversation');
  addMessage(c,updates.at(-1).m);
  if(!c.textContent.includes('New copy of message #10')) throw Error('Copy trace missing');
  state.currentTicketData.permissions.can_reply=false;
  if(canCopyUncertainMessage(original)) throw Error('History granted send rights');
  state.currentTicketData.permissions.can_reply=true;
  state.ticketId=43;
  if(canCopyUncertainMessage(original)) throw Error('Another ticket granted send rights');
  fail=true;
  await check.onclick();
  if(check.disabled || !alerts.at(-1).message.includes('unavailable')) throw Error('Failed check left action disabled');
  document.body.dataset.result='passed';
} catch(error) {document.body.dataset.result='failed: '+error.message}})();
'''
    css = (Path(__file__).resolve().parents[1] / 'app/static/support/support.css').read_text()
    page = tmp_path / 'uncertain-delivery.html'
    page.write_text('<html><head><style>'+css+'body{padding:32px;overflow:auto}#messages{max-width:850px}</style></head><body><div id="messages"></div><script>'+script+'</script></body></html>')
    result = subprocess.run([browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
        '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--user-data-dir='+str(tmp_path/'profile'),
        '--virtual-time-budget=1000', '--screenshot='+str(tmp_path/'uncertain-delivery.png'), '--window-size=1100,750', '--dump-dom', page.as_uri()],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'data-result="passed"' in result.stdout, result.stdout[-3000:]
