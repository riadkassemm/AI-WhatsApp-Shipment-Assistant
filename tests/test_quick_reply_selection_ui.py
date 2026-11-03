"""Exercise the shipped multi-select quick-reply flow with synthetic sends."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from app.support_ui import _ASSETS, _TEMPLATE, _configure


SETUP = r'''
const $=id=>document.getElementById(id);
const state={authenticated:true,agent:{id:'7',role:'agent'},ticketId:42,composerEnabled:true,busy:false,
  activeModal:null,quickReplies:[],quickRepliesLoaded:true,quickRepliesBusy:false,quickRepliesRequest:0,
  quickReplySelecting:false,quickReplySelection:new Map(),quickReplyBatch:null,quickReplyEditing:null,
  quickReplyTab:'text',localMessages:new Map(),textSendChain:Promise.resolve()};
const calls=[],downloads=[],timeline=[],notices=[],copies=[];
const animationFrames=[];
window.requestAnimationFrame=callback=>{animationFrames.push(callback);return animationFrames.length};
let sendBehavior=null,downloadBehavior=null,renderFailure=false,nextMessageId=100,queueRefreshes=0;
const text=(element,value)=>{element.textContent=value};
const notify=value=>notices.push(value);
const staffRoleLabel=role=>role;
const emptyMarkup=(title,body)=>{const outer=document.createElement('div');outer.textContent=title+' '+body;return {outer}};
const canAttachToComposer=()=>state.authenticated&&state.ticketId&&state.composerEnabled&&!state.busy;
const copyMessageText=async value=>{copies.push(value);return true};
function setButtonBusy(button,busy){button.disabled=busy;}
function setWorkspaceBusy(busy){$('message').disabled=busy;$('sendBtn').disabled=busy;}
function openDialog(id){$(id).classList.remove('hidden');$(id).setAttribute('aria-hidden','false');state.activeModal=id;}
function closeDialog(id){$(id).classList.add('hidden');$(id).setAttribute('aria-hidden','true');state.activeModal=null;}
function showLogin(){state.authenticated=false;state.activeModal=null;resetQuickReplies();}
function upsertTimelineMessage(message,ticketId){if(renderFailure)throw Error('Synthetic render failure');timeline.push({message,ticketId});}
async function refreshAllQueueCounts(){queueRefreshes++;}
function check(condition,message){if(!condition)throw Error(message);}
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done});return {promise,resolve};}
function seedReplies(){return [
  {id:1,title:'Welcome',body:'Welcome! How can we help?'},
  {id:2,title:'Shipping guide',body:'Please review this guide.',attachment_filename:'Guide.pdf',attachment_mime_type:'application/pdf'},
  {id:3,title:'Office photo',body:'Our office entrance.',attachment_filename:'Office.png',attachment_mime_type:'image/png'},
  {id:4,title:'Main office',body:'',location:{latitude:0,longitude:35.5,name:'Main office',address:'Reception desk'}},
  {id:5,title:'Tracking details',body:'Your tracking link is ready.'}
];}
let replies=seedReplies();
async function api(path,options){
  if(!options)return {items:replies};
  const body=options.body;
  const payload=body instanceof FormData?Object.fromEntries(body.entries()):JSON.parse(body);
  const call={path,payload,body};calls.push(call);
  return sendBehavior?await sendBehavior(call):{message:{id:nextMessageId++,ticket_id:42,send_status:'SENT'}};
}
window.fetch=async(path,options)=>{
  downloads.push({path,options});
  if(downloadBehavior)return await downloadBehavior(path,options);
  return {ok:true,status:200,blob:async()=>new Blob(['saved attachment bytes'],{type:path.endsWith('/pdf')?'application/pdf':'image/png'})};
};
const draftMedia={file:new File(['draft'],'Draft.png',{type:'image/png'}),caption:'Draft caption'};
const draftReply={id:901,ticketId:40,preview:'Historical quote'};
function setDraft(){
  $('message').value='Keep this existing draft';$('message').setSelectionRange(4,12,'backward');
  state.pendingMedia=draftMedia;state.replyTarget=draftReply;
}
async function checkDraft(){
  for(const callback of animationFrames.splice(0))callback(performance.now());
  await Promise.resolve();
  check($('message').value==='Keep this existing draft','Composer text was changed');
  check($('message').selectionStart===4&&$('message').selectionEnd===12,'Composer text selection was changed');
  check(state.pendingMedia===draftMedia&&state.replyTarget===draftReply,'Composer attachment or reply target changed');
}
async function prepare(ids=[]){
  state.busy=false;state.quickRepliesBusy=false;state.authenticated=true;state.agent={id:'7',role:'agent'};
  state.ticketId=42;state.composerEnabled=true;state.pageLeaving=false;state.activeModal=null;
  state.mediaRecorder=null;state.voiceRecording=null;state.editTarget=null;state.imageEditor=null;
  state.textSendChain=Promise.resolve();state.localMessages.clear();
  resetQuickReplies();replies=seedReplies();state.quickRepliesLoaded=true;
  calls.length=0;downloads.length=0;timeline.length=0;notices.length=0;
  sendBehavior=null;downloadBehavior=null;renderFailure=false;setDraft();setWorkspaceBusy(false);
  await openQuickRepliesModal();startQuickReplySelection();
  for(const id of ids)toggleQuickReplySelection(replies.find(reply=>reply.id===id));
}
'''


CHECKS = r'''
(async()=>{try{
  // Real row/checkbox and tab/search listeners preserve selection order.
  await prepare();
  check($('sendSelectedQuickRepliesBtn').disabled,'Empty selection can be sent');
  check($('selectQuickRepliesBtn').getAttribute('aria-pressed')==='true','Selection mode not announced');
  check(document.activeElement.classList.contains('quick-reply-select'),'Selection did not focus a checkbox');
  $('quickReplyTabLocation').click();
  $('quickRepliesList').querySelector('.quick-reply-select').click();
  $('quickReplyTabText').click();
  $('quickRepliesSearch').value='welcome';$('quickRepliesSearch').dispatchEvent(new Event('input'));
  $('quickRepliesList').querySelector('.quick-reply-copy').click();
  check($('quickRepliesList').querySelector('.quick-reply-row.selected .quick-reply-select').checked,'Row selection styling and checkbox disagree');
  $('quickReplyTabImage').click();$('quickRepliesList').querySelector('.quick-reply-select').click();
  $('quickReplyTabPdf').click();$('quickRepliesList').querySelector('.quick-reply-copy').click();
  check([...state.quickReplySelection.keys()].join()==='4,1,3,2','Tabs/search changed chosen send order');
  check($('quickReplySelectionCount').textContent==='4 selected','Selection count omitted hidden tabs');
  check(!$('sendSelectedQuickRepliesBtn').disabled&&$('addQuickReplyBtn').disabled,'Selection controls are inconsistent');
  check(calls.length===0&&downloads.length===0,'Selecting sent or downloaded content');
  $('cancelQuickReplySelectionBtn').click();
  check(!state.quickReplySelecting&&state.quickReplySelection.size===0,'Cancel did not clear selection');
  check($('quickReplySelectionBar').classList.contains('hidden'),'Cancel left selection toolbar visible');
  check(calls.length===0,'Cancel sent a message');await checkDraft();

  // Finish queued composer sends first, then await each selected send in order.
  await prepare([4,1,3,2]);
  const queued=deferred(),entered=deferred(),first=deferred();state.textSendChain=queued.promise;
  sendBehavior=async()=>{if(calls.length===1){entered.resolve();await first.promise;}return {message:{id:nextMessageId++,ticket_id:42,send_status:'SENT'}}};
  const pending=sendSelectedQuickReplies();
  check(state.busy&&state.quickRepliesBusy&&$('message').disabled,'Batch did not lock workspace');
  closeQuickRepliesModal();cancelQuickReplySelection();
  check(state.activeModal==='quickRepliesModal'&&state.quickReplySelection.size===4,'Pending batch could be closed or cancelled');
  await Promise.resolve();check(calls.length===0,'Batch overtook queued composer text');
  queued.resolve();await entered.promise;
  check(calls.length===1&&calls[0].path.endsWith('/location'),'First request ignored selection order');
  await sendSelectedQuickReplies();check(calls.length===1,'Double-click started another batch');
  first.resolve();await pending;
  check(calls.map(call=>call.path).join()==='/api/support/tickets/42/location,/api/support/tickets/42/messages,/api/support/tickets/42/media,/api/support/tickets/42/media','Mixed batch routes/order incorrect');
  check(calls[0].payload.latitude===0&&calls[0].payload.address==='Reception desk','Location payload changed');
  check(calls[1].payload.body==='Welcome! How can we help?','Text payload changed');
  check(calls[2].payload.media_type==='image'&&calls[2].payload.file.name==='Office.png','Image payload incorrect');
  check(calls[3].payload.media_type==='document'&&calls[3].payload.caption==='Please review this guide.','PDF payload incorrect');
  check(new Set(calls.map(call=>call.payload.client_message_id)).size===4,'Different selected replies share an attempt key');
  check(calls.every(call=>!('reply_to_message_id' in call.payload)),'Batch consumed composer quote target');
  check(!state.busy&&!state.quickRepliesBusy&&!$('message').disabled&&state.activeModal===null,'Successful batch left workspace locked');
  check(state.quickReplySelection.size===0&&!state.quickReplySelecting&&timeline.length===4,'Successful batch not rendered/cleared');await checkDraft();

  // Ambiguous media response keeps exact file bytes, caption and request key.
  await prepare([1,2,4]);
  const originalCaption=replies[1].body,originalLocation={...replies[3].location};
  let rejectPdf=true;
  sendBehavior=async call=>{if(rejectPdf&&call.path.endsWith('/media'))throw Error('Connection lost after upload');return {message:{id:nextMessageId++,ticket_id:42,send_status:'SENT'}}};
  await sendSelectedQuickReplies();
  check([...state.quickReplySelection.keys()].join()==='2,4'&&calls.length===2,'Ambiguous failure resent completed row or sent remainder');
  check($('quickRepliesError').textContent==='Connection lost after upload','Ambiguous failure was not explained');
  const failed=calls[1],savedFile=state.quickReplySelection.get('2').file;
  check(savedFile&&await savedFile.text()==='saved attachment bytes','Failed attachment bytes not retained');
  toggleQuickReplySelection(replies[0]);check(state.quickReplySelection.size===2,'Started batch accepted a new item');
  closeQuickRepliesModal();
  $('message').value='';state.pendingMedia=null;state.replyTarget=null;
  check(!dashboardIsIdle(),'Deploy reload could discard an ambiguous retained batch');
  replies[1].body='Changed saved caption';replies[1].attachment_filename='Changed.pdf';
  replies[3].location.latitude=99;replies[3].location.address='Changed saved address';
  rejectPdf=false;await openQuickRepliesModal();await sendSelectedQuickReplies();
  const retried=calls[2];
  check(retried.payload.client_message_id===failed.payload.client_message_id,'Retry used another attempt key');
  check(retried.payload.caption===originalCaption&&retried.payload.file.name==='Guide.pdf','Retry used changed saved metadata');
  check(await retried.payload.file.text()===await failed.payload.file.text()&&downloads.length===1,'Retry refetched or changed attachment bytes');
  check(calls[3].payload.latitude===originalLocation.latitude&&calls[3].payload.address===originalLocation.address,'Location selection was not a snapshot');
  check(calls.length===4&&dashboardIsIdle(),'Retry did not finish remaining batch cleanly');
  await prepare([1]);sendBehavior=async()=>{throw Error('Ambiguous request')};await sendSelectedQuickReplies();
  closeQuickRepliesModal();$('message').value='';state.pendingMedia=null;state.replyTarget=null;
  check(!dashboardIsIdle(),'Retained text selection did not prevent idle reload');
  cancelQuickReplySelection();check(dashboardIsIdle(),'Cancelled selection still blocked idle reload');

  // A persisted non-success is removed and never automatically sent again.
  for(const status of ['UNCERTAIN','FAILED','PENDING','DISPATCHING']){
    await prepare([1,5]);
    sendBehavior=async()=>({message:{id:nextMessageId++,ticket_id:42,send_status:status}});
    await sendSelectedQuickReplies();
    check(calls.length===1&&[...state.quickReplySelection.keys()].join()==='5','Non-success '+status+' did not stop remainder');
    check(timeline[0].message.send_status===status&&$('quickRepliesError').textContent.includes('delivery status'),'Non-success not visible for review');
    sendBehavior=null;await sendSelectedQuickReplies();
    check(calls.length===2&&calls[1].payload.body==='Your tracking link is ready.','Resuming resent non-success item');
    await checkDraft();
  }

  // Context changes never redirect a batch to a different conversation/user.
  for(const boundary of ['ticket','agent','permission','leaving','modal']){
    await prepare([1,5]);
    if(boundary==='ticket')state.ticketId=43;
    if(boundary==='agent')state.agent={id:'8',role:'agent'};
    if(boundary==='permission')state.composerEnabled=false;
    if(boundary==='leaving')state.pageLeaving=true;
    if(boundary==='modal')closeQuickRepliesModal();
    await sendSelectedQuickReplies();
    check(calls.length===0&&state.quickReplySelection.size===2,'Unsafe '+boundary+' context sent a message');
  }
  for(const boundary of ['ticket','permission']){
    await prepare([1,5]);
    sendBehavior=async()=>{if(boundary==='ticket')state.ticketId=43;else state.composerEnabled=false;return {message:{id:nextMessageId++,ticket_id:42,send_status:'SENT'}}};
    await sendSelectedQuickReplies();
    check(calls.length===1&&state.quickReplySelection.has('5'),'Mid-batch '+boundary+' change did not stop');
    check(calls[0].path==='/api/support/tickets/42/messages','Batch request lost pinned destination');
    check(!state.busy&&!state.quickRepliesBusy,'Paused context left controls busy');
    if(boundary==='ticket')check(timeline.length===0,'Old-ticket message inserted into newly selected chat');
  }
  await prepare([2]);
  const downloadEntered=deferred(),downloadRelease=deferred();
  downloadBehavior=async()=>{downloadEntered.resolve();await downloadRelease.promise;return {ok:true,status:200,blob:async()=>new Blob(['private attachment'])}};
  const downloading=sendSelectedQuickReplies();await downloadEntered.promise;state.composerEnabled=false;downloadRelease.resolve();await downloading;
  check(calls.length===0&&state.quickReplySelection.size===1,'Attachment sent after permission disappeared during download');

  // Logout invalidates pending requests and clears their workspace busy state.
  await prepare([1,5]);
  const sendEntered=deferred(),sendRelease=deferred();
  sendBehavior=async()=>{sendEntered.resolve();await sendRelease.promise;return {message:{id:nextMessageId++,ticket_id:42,send_status:'SENT'}}};
  const loggingOut=sendSelectedQuickReplies();await sendEntered.promise;showLogin();
  check(!state.busy&&!state.quickRepliesBusy&&state.quickReplyBatch===null,'Logout retained batch busy state');
  state.authenticated=true;state.agent={id:'8',role:'agent'};
  sendRelease.resolve();await loggingOut;
  check(calls.length===1&&state.quickReplySelection.size===0&&timeline.length===0,'Late response leaked into next staff session');
  await prepare([2]);downloadBehavior=async()=>({ok:false,status:401});await sendSelectedQuickReplies();
  check(!state.authenticated&&!state.busy&&!state.quickRepliesBusy&&calls.length===0,'Attachment expiry did not clear batch safely');

  // Display failure cannot cause a delivered item to be sent again.
  await prepare([1,5]);renderFailure=true;await sendSelectedQuickReplies();
  check(calls.length===2&&state.quickReplySelection.size===0,'Rendering failure affected delivery progress');await checkDraft();
  await prepare();cancelQuickReplySelection();state.composerEnabled=false;renderQuickReplies();
  check($('selectQuickRepliesBtn').disabled,'Read-only conversation offers selection send');startQuickReplySelection();
  check(!state.quickReplySelecting,'Read-only conversation entered selection mode');

  // Leave real styled UI showing synthetic content for the optional PR image.
  await prepare([1,5,2,4]);selectQuickReplyTab('text');$('sessionLoading').classList.add('hidden');
  const bounds=$('quickRepliesModal').querySelector('.modal-card').getBoundingClientRect();
  check(bounds.left>=-1&&bounds.right<=innerWidth+1,'Quick-reply modal overflows narrow viewport');
  const sendBounds=$('sendSelectedQuickRepliesBtn').getBoundingClientRect();
  check(sendBounds.left>=0&&sendBounds.right<=innerWidth,'Send selection control is outside viewport');
  document.body.dataset.result='passed';
}catch(error){document.body.dataset.result='FAILED: '+error.message;document.body.dataset.stack=error.stack}})();
'''


@pytest.mark.parametrize('width', [430, 1100])
def test_quick_reply_selection_send_order_retries_and_context_guards(tmp_path: Path, width: int):
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured Chrome/Chromium browser'
    actions = _ASSETS['actions.js']
    quick = actions.split('const QUICK_REPLIES_LIST_SUBTITLE = ', 1)[1].split('const AI_REASON_LABELS = ', 1)[0]
    icons = _ASSETS['state.js'].split('function createIcon(', 1)[1].split('function cookie(', 1)[0]
    bindings = actions.split("$('closeQuickRepliesBtn').addEventListener", 1)[1].split("$('quickReplyBody').addEventListener", 1)[0]
    idle = _ASSETS['api.js'].split('function dashboardIsIdle() {', 1)[1].split('function noticeDashboardUpdate(', 1)[0]
    focus = _ASSETS['media.js'].split('function focusMessageComposer() {', 1)[1].split('function updateComposerControls()', 1)[0]
    script = (SETUP + 'function createIcon(' + icons + 'function focusMessageComposer() {' + focus
              + 'const QUICK_REPLIES_LIST_SUBTITLE = ' + quick
              + "$('closeQuickRepliesBtn').addEventListener" + bindings
              + 'function dashboardIsIdle() {' + idle + CHECKS)
    html = _configure(_TEMPLATE.replace('__SUPPORT_STYLE__', '<style>' + _ASSETS['support.css'] + '</style>')
                      .replace('__SUPPORT_THEME__', '').replace('__SUPPORT_SCRIPTS__', '<script>' + script + '</script>'))
    path = tmp_path / 'quick-reply-selection.html'
    path.write_text(html)
    command = [browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
               '--no-first-run', '--disable-background-networking', '--user-data-dir=' + str(tmp_path / 'profile'),
               f'--window-size={width},1100', '--virtual-time-budget=6000', '--dump-dom']
    if width == 1100 and os.environ.get('SUPPORT_QUICK_REPLY_SELECTION_SCREENSHOT'):
        command.append('--screenshot=' + os.environ['SUPPORT_QUICK_REPLY_SELECTION_SCREENSHOT'])
    result = subprocess.run([*command, path.as_uri()], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    body_start = result.stdout.find('<body')
    assert 'data-result="passed"' in result.stdout, result.stdout[body_start:body_start + 2000] + result.stderr[-1200:]
