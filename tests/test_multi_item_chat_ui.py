"""Exercise browser queue/selection behavior with controlled per-item API outcomes."""
import os
from pathlib import Path
import shutil
import subprocess

from app.support_ui import _ASSETS, _TEMPLATE, _configure


def test_multi_item_queue_forwarding_and_image_quick_replies(tmp_path):
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install Chromium for UI regressions'
    media = _ASSETS['media.js']
    rendering = _ASSETS['rendering.js']
    message_render = rendering[rendering.index('function addMessage('):rendering.index('// Compact message toolbar:')]
    actions = _ASSETS['actions.js']
    selection = actions[actions.index('function showForwardError('):actions.index('function showTransferError(')]
    quick = actions[actions.index('const QUICK_REPLIES_LIST_SUBTITLE = '):actions.index('const AI_REASON_LABELS = ')]
    setup = r'''
const $=id=>document.getElementById(id);
const state={authenticated:true,agent:{id:'7',role:'agent'},ticketId:42,composerEnabled:true,busy:false,
 pendingMedia:null,pendingMediaItems:[],selectedMessages:new Map(),selectingMessages:false,
 quickReplies:[],quickRepliesLoaded:true,quickRepliesBusy:false,quickRepliesRequest:0};
const errors=[],calls=[],notifications=[];
const text=(e,t)=>e.textContent=t,showError=t=>errors.push(t),notify=t=>notifications.push(t),setAppStatus=()=>{};
const setIcon=()=>{},formatBytes=n=>n+' bytes',setButtonBusy=()=>{},setWorkspaceBusy=()=>{},updateReply=()=>{};
function createIcon(name){const icon=document.createElementNS('http://www.w3.org/2000/svg','svg');icon.classList.add('icon');const use=document.createElementNS('http://www.w3.org/2000/svg','use');use.setAttribute('href','#icon-'+name);icon.append(use);return icon;}
const editPendingImage=()=>{},clearReplyTarget=()=>{state.replyTarget=null;};
const openTicket=async()=>{},messagePreview=m=>m.body||'Attachment',refreshAllQueueCounts=async()=>{};
const loadForwardChoices=async()=>{};
const messageClass=()=>'customer',messageActor=()=>'Customer',initials=()=>'EX';
const renderReplyQuote=()=>{},canChangeWhatsAppMessage=()=>false,messageReactionBar=()=>null;
const messageTextForCopy=m=>m.deleted_for_everyone_at?'':m.body||'',appendMessageTextWithLinks=(e,t)=>e.textContent=t;
const fmt=()=>'12:00',messageOccurredAt=()=>'2026-10-27T12:00:00Z';
function labelMessageAction(button,action,label){button.dataset.action=action;button.setAttribute('aria-label',label);button.dataset.tooltip=label;}

function openDialog(id){state.activeModal=id;$(id).classList.remove('hidden');}
function closeDialog(id){state.activeModal=null;$(id).classList.add('hidden');}
function emptyMarkup(title){const outer=document.createElement('div');outer.textContent=title;return {outer};}
function check(ok,why){if(!ok)throw Error(why);}
let mode='ok', failAt=0, attempt=0;
async function api(path,options){
 calls.push({path,body:options.body});attempt++;
 if(mode==='network'&&attempt===failAt)throw Error('Connection interrupted');
 return {message:{id:100+attempt,send_status:mode==='uncertain'?'UNCERTAIN':'SENT'}};
}
'''
    assertions = r'''
(async()=>{
const canvas=document.createElement('canvas');canvas.width=240;canvas.height=140;
const context=canvas.getContext('2d');context.fillStyle='#128c87';context.fillRect(0,0,240,140);context.fillStyle='white';context.font='24px sans-serif';context.fillText('Office photo',40,80);
const png=new File([await new Promise(resolve=>canvas.toBlob(resolve,'image/png'))],'Image.png',{type:'image/png'});
const pdf=new File(['%PDF-1.4'],'File.pdf',{type:'application/pdf'});
const video=new File(['video'],'Video.mp4',{type:'video/mp4'});
installComposerFileInput();
const transfer=new DataTransfer();[png,pdf,video].forEach(file=>transfer.items.add(file));
$('message').dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:transfer}));
check(state.pendingMediaItems.length===3&&$('mediaDraft').children.length===3,'multi-file paste previews all supported items');
const second=state.pendingMediaItems[1].id;removeMediaDraft(second);
check(state.pendingMediaItems.length===2&&state.pendingMediaItems[1].kind==='video','remove only chosen preview');
clearMediaDraft();composerFilesReceived([png,pdf,video]);
$('message').value='Caption';mode='network';failAt=2;
await sendMediaMessage();
check(calls.length===2&&state.pendingMediaItems.length===2,'partial HTTP failure stops queue and removes only completed item');
const key=calls[1].body.get('client_message_id');
check(calls[0].body.get('caption')==='Caption'&&calls[1].body.get('caption')==='','caption goes to first item only');
mode='ok';await sendMediaMessage();
check(calls.length===4&&calls[2].body.get('client_message_id')===key,'retry preserves ambiguous request identity');
check(!state.pendingMedia&&state.pendingMediaItems.length===0,'completed queue clears');
composerFilesReceived([pdf,png]);mode='uncertain';let before=calls.length;
await sendMediaMessage();check(calls.length===before+1&&state.pendingMediaItems.length===1,'uncertain result remains in timeline and stops remaining sends');
clearMediaDraft();mode='ok';
attachComposerFile(png);state.pendingMedia.caption='Saved caption';$('message').value='Unsent text draft';
await sendMediaMessage();check($('message').value==='Unsent text draft','saved attachment caption never discards unrelated text');$('message').value='';
for(let i=0;i<21;i++)attachComposerFile(png);
check(state.pendingMediaItems.length===20,'batch cap retains existing drafts');clearMediaDraft();
$('conversation').replaceChildren();
addMessage($('conversation'),{id:3,body:'third',direction:'INBOUND'});
addMessage($('conversation'),{id:99,body:'deleted',deleted_for_everyone_at:'2026-10-27'});
$('conversation').querySelector('[data-action=select]').click();
check($('conversation').querySelector('.message-select').checked,'message action enables a selected checkbox');
check($('conversation').querySelectorAll('.message-select').length===1,'deleted messages cannot be selected');
const checkbox=$('conversation').querySelector('.message-select');checkbox.click();
check(state.selectedMessages.size===0,'checkbox unselects one item');checkbox.click();
selectChatMessage({id:1,body:'first'});selectChatMessage({id:2,body:'second'});
check($('messageSelectionCount').textContent==='3 selected'&&!$('messageSelectionBar').classList.contains('hidden'),'visible selection count');
await openForwardModal([...state.selectedMessages.values()]);state.forwardSelection={kind:'conversation',id:43};
mode='network';failAt=attempt+2;before=calls.length;
await confirmForward();
check(calls[before].path.includes('/messages/1/forward')&&calls[before+1].path.includes('/messages/2/forward'),'forward order follows conversation');
check(state.forwardTarget.length===2&&state.selectedMessages.size===2,'partial forwarding retains unsent selections');
const forwardKey=JSON.parse(calls[before+1].body).client_message_id;
mode='ok';await confirmForward();
check(JSON.parse(calls[before+2].body).client_message_id===forwardKey,'forward retry uses same per-message key');
check(!state.selectingMessages&&state.selectedMessages.size===0,'successful forwarding clears selection');
state.quickReplies=[{id:4,title:'Office photo',body:'Welcome',attachment_filename:'office.png',attachment_mime_type:'image/png'}];
selectQuickReplyTab('image');
check($('quickRepliesList').textContent.includes('Attach image')&&$('quickRepliesList').querySelector('img'),'Images tab includes thumbnails and insert action');
showQuickReplyForm(state.quickReplies[0]);check($('quickReplyPdf').accept==='image/jpeg,image/png','image editor accepts only supported images');
showQuickReplyList();
// Save a visual artifact using synthetic attachments, never customer data.
composerFilesReceived([png,pdf,new File([png],'Second image.png',{type:'image/png'})]);
$('dashboard').classList.remove('hidden');$('sessionLoading').classList.add('hidden');
selectChatMessage({id:3,body:'third'});
check($('conversation').querySelector('.message-bubble').getBoundingClientRect().width>80,'selection preserves message bubble width');
text($('ticketTitle'),'Test conversation');text($('ticketSubtitle'),'Multiple attachments ready to send');
if (document.documentElement.dataset.capture==='images') {
  openDialog('quickRepliesModal');selectQuickReplyTab('image');
  $('quickRepliesList').querySelector('img').src=URL.createObjectURL(png);
}
document.body.dataset.multiItemTest='passed';
})().catch(error=>{document.body.dataset.multiItemTest='FAILED: '+error.message});
'''
    html = _configure(_TEMPLATE.replace('__SUPPORT_THEME__', '').replace('__SUPPORT_STYLE__', '<style>'+_ASSETS['support.css']+'</style>')
                      .replace('__SUPPORT_SCRIPTS__', '<script>'+setup+media+quick+selection+message_render+assertions+'</script>'))
    if os.environ.get('SUPPORT_MULTI_SCREENSHOT_VIEW') == 'images': html=html.replace('<html ', '<html data-capture="images" ',1)
    page = tmp_path/'multi-items.html'; page.write_text(html)
    command = [browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--disable-background-networking',
               '--user-data-dir='+str(tmp_path/'profile'), '--window-size=1440,1000', '--virtual-time-budget=3000', '--dump-dom']
    if os.environ.get('SUPPORT_MULTI_SCREENSHOT'): command.append('--screenshot='+os.environ['SUPPORT_MULTI_SCREENSHOT'])
    result = subprocess.run([*command,page.as_uri()],capture_output=True,text=True,timeout=45)
    assert result.returncode == 0, result.stderr
    start=result.stdout.find('<body')
    assert 'data-multi-item-test="passed"' in result.stdout, result.stdout[start:start+500]+result.stderr[-1000:]
