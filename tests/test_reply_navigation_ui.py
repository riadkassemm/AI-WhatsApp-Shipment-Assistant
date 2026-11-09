"""Browser regressions for universal quotes, pagination, and stale requests."""
import os
import shutil
import subprocess

from app.support_ui import _ASSETS, _TEMPLATE, _configure


def test_reply_navigation_browser(tmp_path):
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install Chromium for UI regressions'
    rendering = _ASSETS['rendering.js']
    navigation = rendering[rendering.index('// Quote navigation leaves'):rendering.index('function applyMediaZoom(')]
    merge = rendering[rendering.index('function mergeTimelineMessages('):rendering.index('function changeCachedMessage(')]
    add_message = rendering[rendering.index('function addMessage('):rendering.index('// Compact message toolbar:')]
    selection = rendering[rendering.index('function selectedConversationText('):rendering.index('function beginConversationSelection(')]
    history = rendering[rendering.index('async function loadEarlierConversations('):rendering.index('async function loadOlderConversationMessages(')]
    script = r'''
const $=id=>document.getElementById(id);
const state={authenticated:true,ticketId:42,historySelectionEpoch:1,previousTicketHistory:[],
 cachedCurrentMessages:[],currentTicketData:{ticket:{id:42},permissions:{can_reply:false,can_forward:false}},
 selectedMessages:new Map(),previousHistoryExpanded:false,conversationSelectionPointer:null};
const notices=[],calls=[];
const notify=t=>notices.push(t),updateScrollLatestButton=()=>{},updateSharedHistoryShortcut=()=>{};
let investigationTicket=60,investigationRequest=1,investigationMessageBefore=850;
const investigationStillAllowed=request=>request===investigationRequest;
const MESSAGE_ROLE_LABELS={CUSTOMER:'Customer'},messagePreview=m=>m.body||m.message_type||'Message';
const messageClass=()=>'customer',messageActor=()=>'Customer',initials=()=>'CU';
const canChangeWhatsAppMessage=()=>false,messageReactionBar=()=>null;
const messageTextForCopy=m=>m.deleted_for_everyone_at?'':m.body||'',appendMessageTextWithLinks=(e,t)=>e.textContent=t;
const fmt=()=>'12:00',messageOccurredAt=()=>'2026-10-27T12:00:00Z',messageArrivedLate=()=>false;
const labelMessageAction=()=>{},createIcon=()=>document.createElement('span');
const createVoiceNotePlayer=()=>{const player=document.createElement('div'),audio=document.createElement('audio');player.append(audio);return {player,audio};};
const historyDetailBase=()=>'/api/support/tickets/'+state.ticketId,timelineSignature=()=>'';
const beginHistoryLoad=()=>({}),historyLoadIsCurrent=()=>true,finishHistoryLoad=()=>{},showError=t=>notices.push(t);
function check(ok,why){if(!ok)throw Error(why);}
let responder=()=>{throw Error('Unexpected API call');};
async function api(path){calls.push(path);return responder(path);}
function renderConversation(data){
 const c=$('conversation');c.replaceChildren();
 for(const thread of data.previous_ticket_history||[])for(const m of thread.messages||[])addMessage(c,m,'previous-ticket-msg');
 for(const m of data.messages||[])addMessage(c,m);
}
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
''' + merge + selection + navigation + add_message + history + r'''
(async()=>{
const c=$('conversation');
// Exercise the actual shared renderer for each supported original AND reply type.
for(const kind of ['text','image','audio','document','video','sticker','location','contacts']){
 c.replaceChildren();
 addMessage(c,{id:1,message_type:kind,body:'Original '+kind,media_url:kind==='document'?'data:application/pdf,':null});
 const spacer=document.createElement('div');spacer.style.height='1800px';c.append(spacer);
 addMessage(c,{id:2,message_type:kind,reply_to:{id:1,source_message_id:2,body:'Original '+kind}});
 c.scrollTop=c.scrollHeight;
 const quote=c.querySelector('.message-quote');
 check(quote.tagName==='BUTTON'&&quote.type==='button','quote is keyboard-accessible');
 quote.click();
 check(c.querySelector('[data-message-id="1"]').classList.contains('reply-target'),'highlight original '+kind);
 check(calls.length===0,'loaded target needs no API');
 // Existing text selection must not block a keyboard/programmatic activation.
 const range=document.createRange();range.selectNodeContents(c.querySelector('.message-body'));
 window.getSelection().removeAllRanges();window.getSelection().addRange(range);
 quote.click();check(!selectedConversationText(),'navigation clears an unrelated text selection');
 // Dragging within a quote keeps copy available and does not jump.
 c.querySelector('[data-message-id="1"]').classList.remove('reply-target');
 range.selectNodeContents(quote.querySelector('span'));window.getSelection().addRange(range);
 quote.dispatchEvent(new MouseEvent('click',{bubbles:true,detail:1}));
 check(selectedConversationText()&&!c.querySelector('.reply-target'),'drag-selected quote remains copyable');
 window.getSelection().removeAllRanges();

}
// Off-page original, including fallback quote with no local target ID.
state.cachedCurrentMessages=[{id:300,body:'Latest',reply_to:{source_message_id:300,body:'Earlier message'}}];
state.currentTicketData.messages=state.cachedCurrentMessages;renderConversation(state.currentTicketData);
$('draft').value='Unsent draft';const draftFile={name:'pending.pdf'};state.pendingMediaItems=[draftFile];
responder=path=>path.endsWith('/reply')?{ticket:{id:42},message_id:1,messages:[{id:200,body:'Between'},...state.cachedCurrentMessages],messages_has_more:true}
 :{messages:[{id:1,body:'Old original'},{id:100,body:'Intervening'}],messages_has_more:false};
await navigateToReply({source_message_id:300},c.querySelector('.message-quote'));
check(state.cachedCurrentMessages.map(m=>m.id).join(',')==='1,100,200,300','load intervening history without gaps');
check(calls[1].includes('before_message_id=200'),'keyset pagination');
check(c.querySelector('[data-message-id="1"]').classList.contains('reply-target'),'loaded original highlighted');
check($('draft').value==='Unsent draft'&&state.pendingMediaItems[0]===draftFile,'drafts preserved');
check(state.ticketId===42&&state.currentTicketData.permissions.can_reply===false,'navigation cannot enable composer');
// Cross-ticket navigation retains current assignment and the history cursor.
state.previousTicketHistory=[{ticket:{id:30},messages:[]}];state.previousTicketHistoryHasMore=true;
responder=()=>({ticket:{id:10},message_id:7,messages:[{id:7,body:'Historical original'}],messages_has_more:false});
await navigateToReply({source_message_id:300},c.querySelector('.message-quote'));
check(state.ticketId===42&&state.replyHistoryBoundary===30,'history lookup does not switch tickets or skip intermediate conversations');
responder=()=>({previous_ticket_history:[{ticket:{id:20},messages:[{id:8}]}],previous_ticket_history_has_more:true});
await loadEarlierConversations();
check(calls.at(-1).includes('before_ticket_id=30'),'history pagination resumes original boundary');
check(state.previousTicketHistory.map(t=>t.ticket.id).join(',')==='10,20,30'&&state.replyHistoryBoundary===20,'history remains ordered');
// No-progress/missing target must terminate rather than loop indefinitely.
responder=path=>path.endsWith('/reply')?{ticket:{id:42},message_id:3,messages:[{id:200}],messages_has_more:true}:{messages:[],messages_has_more:true};
await navigateToReply({source_message_id:300},document.createElement('button'));
check(notices.pop()==='Original message is unavailable.','unavailable target explained');
// Deterministic request barriers: switching tickets and newer clicks fence responses.
let gate=deferred();responder=()=>gate.promise;
const pending=navigateToReply({source_message_id:300},document.createElement('button'));
state.ticketId=99;state.historySelectionEpoch++;
gate.resolve({ticket:{id:42},message_id:999,messages:[{id:999}],messages_has_more:false});await pending;
check(!state.cachedCurrentMessages.some(m=>m.id===999),'switch cancels stale response');
state.ticketId=42;gate=deferred();responder=()=>gate.promise;
const stale=navigateToReply({source_message_id:300},document.createElement('button'));
await navigateToReply({id:7},document.createElement('button'));
gate.resolve({ticket:{id:42},message_id:999,messages:[{id:999}],messages_has_more:false});await stale;
check(!state.cachedCurrentMessages.some(m=>m.id===999),'new click cancels old lookup');
responder=()=>{throw Error('Connection interrupted');};
const errorButton=document.createElement('button');await navigateToReply({source_message_id:300},errorButton);
check(notices.pop()==='Connection interrupted'&&!errorButton.hasAttribute('aria-busy'),'errors clear loading and allow retry');
// Manager review navigation uses its own scroll container and leaves live chat alone.
const review=document.createElement('div');review.id='investigationMessages';document.body.append(review);
const olderButton=document.createElement('button');olderButton.id='investigationOlderMessages';document.body.append(olderButton);
addMessage(review,{id:900,body:'Review reply',reply_to:{source_message_id:900,body:'Earlier message'}},'investigation');
const cachedBefore=JSON.stringify(state.cachedCurrentMessages), ticketBefore=state.ticketId;
responder=()=>({ticket:{id:60},message_id:800,messages:[{id:800,body:'Review original'},{id:900}],messages_has_more:false});
await navigateToReply({source_message_id:900},review.querySelector('.message-quote'));
check(calls.at(-1).includes('/tickets/60/'),'review resolves its own ticket');
check(review.querySelector('[data-message-id="800"]').classList.contains('reply-target'),'review target highlighted in review pane');
check(!c.querySelector('[data-message-id="800"]')&&state.ticketId===ticketBefore&&JSON.stringify(state.cachedCurrentMessages)===cachedBefore,'review leaves operational conversation untouched');
check(!review.firstChild.querySelector('.message-actions'),'loaded review messages stay read-only');
check(investigationMessageBefore===null&&olderButton.classList.contains('hidden'),'review pagination does not duplicate loaded pages');
review.remove();
// Reduced-motion users get immediate scrolling with a visible temporary outline.
window.matchMedia=()=>({matches:true});let scrollOptions;const scroll=c.scrollTo.bind(c);c.scrollTo=options=>{scrollOptions=options;scroll(options);};
highlightReplyMessage(7);check(scrollOptions.behavior==='auto','reduced motion respected');
check(c.querySelectorAll('.reply-target').length===1,'only one highlighted destination');
// End highlight deterministically by advancing the registered timer callback.
const originalTimeout=window.setTimeout;let finish;
window.setTimeout=fn=>{finish=fn;return 0;};highlightReplyMessage(7);finish();window.setTimeout=originalTimeout;
check(!c.querySelector('.reply-target'),'highlight is temporary');
document.body.dataset.result='passed';
if(document.documentElement.dataset.capture){
 $('dashboard').classList.remove('hidden');$('sessionLoading').classList.add('hidden');
 $('ticketTitle').textContent='Test conversation';$('ticketSubtitle').textContent='Reply navigation · Synthetic example';
 c.replaceChildren();
 addMessage(c,{id:1,body:'The delivery is scheduled for tomorrow morning.'});
 addMessage(c,{id:2,body:'Thank you. Please call before arriving.',reply_to:{id:1,source_message_id:2,body:'The delivery is scheduled for tomorrow morning.',sender_name:'Customer'}});
 c.querySelector('[data-message-id="1"]').classList.add('reply-target');
}

})().catch(error=>{document.body.dataset.result='FAILED: '+error.stack;});
'''
    page = tmp_path/'reply-navigation.html'
    html = ('<html><head><style>'+_ASSETS['support.css']+
            '</style></head><body><input id="draft"><div id="conversation" style="height:300px;overflow:auto"></div><script>'+script+'</script></body></html>')
    screenshot = os.environ.get('SUPPORT_REPLY_SCREENSHOT')
    if screenshot:
        html = _configure(_TEMPLATE.replace('__SUPPORT_THEME__', '')
                          .replace('__SUPPORT_STYLE__', '<style>'+_ASSETS['support.css']+'</style>')
                          .replace('__SUPPORT_SCRIPTS__', '<input id="draft" hidden><script>'+script+'</script>'))
        html = html.replace('<html ', '<html data-capture="true" ', 1)
    page.write_text(html)
    command = [browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
               '--disable-background-networking', '--user-data-dir='+str(tmp_path/'profile'),
               '--virtual-time-budget=3000', '--dump-dom', page.as_uri()]
    if screenshot:
        command[1:1] = ['--force-prefers-reduced-motion', '--window-size=1440,1000', '--screenshot='+screenshot]
    result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    start = result.stdout.find('<body')
    assert 'data-result="passed"' in result.stdout, result.stdout[start:start+1200]+result.stderr[-500:]
