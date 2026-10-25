"""Run the shipped quick-reply UI and location message action in Chromium."""
import os
from pathlib import Path
import shutil
import subprocess

from app.support_ui import _ASSETS, _TEMPLATE, _configure


def test_tabs_location_action_preview_and_manager_assignment(tmp_path):
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured test browser'
    quick = _ASSETS['actions.js'].split('const QUICK_REPLIES_LIST_SUBTITLE = ', 1)[1].split('const AI_REASON_LABELS = ', 1)[0]
    icons = _ASSETS['state.js'].split('function createIcon(', 1)[1].split('function cookie(', 1)[0]
    rendering = _ASSETS['rendering.js']
    message = rendering[rendering.index('function addMessage('):rendering.index('// Compact message toolbar:')]
    links = rendering[rendering.index('function appendMessageTextWithLinks('):rendering.index('function createVoiceNotePlayer(')]
    copying = rendering[rendering.index('function messageTextForCopy('):rendering.index('async function copyMessageText(')]
    bindings = _ASSETS['actions.js'].split("$('quickReplyAudience').addEventListener('change', updateQuickReplyAudience);", 1)[1].split("$('quickReplyBody').addEventListener", 1)[0]
    setup = r'''
const $=id=>document.getElementById(id);
const state={authenticated:true,agent:{id:'7',role:'agent'},ticketId:42,composerEnabled:true,busy:false,
  quickReplies:[],quickRepliesLoaded:true,quickRepliesBusy:false,quickRepliesRequest:0,quickReplyEditing:null,quickReplyTab:'text'};
const calls=[],notifications=[];
const text=(element,value)=>{element.textContent=value};
const notify=value=>notifications.push(value), setButtonBusy=()=>{}, focusMessageComposer=()=>{};
const staffRoleLabel=role=>({supervisor:'Super Admin',admin:'Admin',agent:'Agent'})[role];
const copyMessageText=async value=>{calls.push(['copy',value]);return true};
const emptyMarkup=(title,body)=>{const outer=document.createElement('div');outer.textContent=title+' '+body;return {outer}};
const canAttachToComposer=()=>state.composerEnabled&&!state.busy;
function openDialog(id){$(id).classList.remove('hidden');state.activeModal=id;}
function closeDialog(id){$(id).classList.add('hidden');state.activeModal=null;}
function openLocationModal(){openDialog('locationModal');$('locationForm').reset();}
function check(ok,why){if(!ok)throw Error(why);}
const messageClass=()=>'customer', messageActor=()=>'Customer', initials=()=>'EX';
const renderReplyQuote=()=>{}, canChangeWhatsAppMessage=()=>false, messageReactionBar=()=>null;
const fmt=()=>'12:00',messageOccurredAt=()=>'2026-10-24T12:00:00Z';
function labelMessageAction(button,action,label){button.dataset.action=action;button.setAttribute('aria-label',label);}
let replies=[{id:1,title:'Hours',body:'Open daily'}, {id:2,title:'Guide',body:'',attachment_filename:'Guide.pdf'},
  {id:3,title:'Main office',body:'',location:{latitude:33.89,longitude:35.5,name:'Main office',address:'Reception desk'}}];
let rejectSave=false, skippedCount=0;
async function api(path,options){
  calls.push([path,options]);
  if(path.endsWith('/recipients'))return {items:[{id:7,name:'Staff One',role:'agent'},{id:8,name:'<img src=x> Manager',role:'supervisor'}]};
  if(!options)return {items:replies};
  if(rejectSave)throw Error('Save rejected');
  const fields=options.body instanceof FormData?JSON.parse(options.body.get('assignment')):JSON.parse(options.body);
  if(path.includes('/assign'))return {saved_count:(fields.all_staff?2:fields.recipient_ids.length)-skippedCount,skipped_count:skippedCount,item:null};
  const saved={id:4,...fields};replies.push(saved);return saved;
}
'''
    assertions = r'''
(async()=>{
await openQuickRepliesModal();
check($('quickRepliesList').children.length===1&&$('quickRepliesList').textContent.includes('Hours'),'text tab filters');
$('quickReplyTabPdf').click();check($('quickRepliesList').textContent.includes('Attach PDF')&&!$('quickRepliesList').textContent.includes('Hours'),'PDF tab filters');
$('quickReplyTabPdf').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));check($('quickRepliesList').textContent.includes('Main office'),'keyboard switches to location tab');
check($('quickReplyTabLocation').getAttribute('aria-selected')==='true'&&$('quickReplyTabText').tabIndex===-1,'tab accessibility');
$('quickRepliesSearch').value='reception';renderQuickReplies();check($('quickRepliesList').children.length===1,'location address search');
let count=calls.length;$('quickRepliesList').querySelector('.quick-reply-copy').click();
check(state.activeModal==='locationModal'&&$('locationLatitude').value==='33.89'&&$('locationName').value==='Main office','saved location stages existing preview');
check(calls.length===count,'preview never sends');closeDialog('locationModal');
state.composerEnabled=false;await openQuickRepliesModal();previewQuickReplyLocation(replies[2]);
check(state.activeModal==='quickRepliesModal'&&$('quickRepliesError').textContent.includes('reply to'),'read-only cannot send a location');
closeDialog('quickRepliesModal');
const m={id:55,sender_type:'CUSTOMER',direction:'INBOUND',message_type:'location',location_latitude:0,location_longitude:0,location_name:'Shared office',location_address:'Lobby'};
const history=document.createElement('div');document.body.append(history);addMessage(history,m,'history');
const action=history.querySelector('[data-action="save-location"]');check(action,'history location has save action');
await action.onclick();
check(state.quickReplyEditing==='new'&&$('quickReplyTitle').value==='Shared office'&&$('quickReplyLatitude').value==='0','message action copies coordinates and opens name editor');
check($('quickReplyAssignment').classList.contains('hidden'),'regular staff do not see assignment');
$('quickReplyTitle').value='';count=calls.length;await saveQuickReply({preventDefault(){}});check(calls.length===count,'location name required');
$('quickReplyTitle').value='Saved office';await saveQuickReply({preventDefault(){}});
const savedCall=calls.find(c=>c[0]==='/api/support/quick-replies'&&c[1]?.method==='POST');
check(JSON.parse(savedCall[1].body).location.latitude===0,'zero latitude preserved when saving');
check(!calls.some(c=>String(c[0]).includes('/tickets/')),'saving from read-only history never sends');
history.replaceChildren();addMessage(history,{...m,deleted_for_everyone_at:'now'},'history');
check(!history.querySelector('[data-action="save-location"]'),'deleted location cannot be saved');history.remove();
state.agent={id:'8',role:'supervisor'};state.composerEnabled=true;
showQuickReplyForm(null);check(!$('quickReplyAssignment').classList.contains('hidden'),'Super Admin can choose recipients');
$('quickReplyAudience').value='selected';await updateQuickReplyAudience();
check($('quickReplyRecipients').querySelectorAll('input').length===2&&!$('quickReplyRecipients').querySelector('img'),'recipient names safely rendered');
$('quickReplyTitle').value='Branch office';$('quickReplyMapsUrl').value='https://www.google.com/maps?q=33.9,35.6';
count=calls.length;await saveQuickReply({preventDefault(){}});check(calls.length===count,'selected audience requires selections');
$('quickReplyRecipients').querySelector('input').checked=true;await saveQuickReply({preventDefault(){}});
let assigned=calls.filter(c=>c[0]==='/api/support/quick-replies/assign').at(-1);
check(JSON.parse(assigned[1].body).recipient_ids.join()==='7'&&!JSON.parse(assigned[1].body).all_staff,'only selected recipients submitted');
selectQuickReplyTab('text');showQuickReplyForm(null);$('quickReplyBody').value='Team text';$('quickReplyAudience').value='all';await saveQuickReply({preventDefault(){}});
assigned=calls.filter(c=>c[0]==='/api/support/quick-replies/assign').at(-1);check(JSON.parse(assigned[1].body).all_staff,'all-staff option submitted');
selectQuickReplyTab('pdf');showQuickReplyForm(null);const transfer=new DataTransfer();transfer.items.add(new File(['%PDF-1.4'],'Team.pdf',{type:'application/pdf'}));$('quickReplyPdf').files=transfer.files;
$('quickReplyAudience').value='all';await saveQuickReply({preventDefault(){}});
assigned=calls.find(c=>c[0]==='/api/support/quick-replies/assign/pdf');check(assigned[1].body.get('file').name==='Team.pdf'&&JSON.parse(assigned[1].body.get('assignment')).all_staff,'PDF distribution carries file and audience');
selectQuickReplyTab('location');showQuickReplyForm(null);$('quickReplyTitle').value='Try again';$('quickReplyLatitude').value='1';$('quickReplyLongitude').value='2';rejectSave=true;
await saveQuickReply({preventDefault(){}});check(state.quickReplyEditing==='new'&&$('quickReplyTitle').value==='Try again'&&$('quickRepliesError').textContent==='Save rejected','failed save preserves draft');rejectSave=false;
state.quickReplies=replies;showQuickReplyList();selectQuickReplyTab('location');
state.agent.role='agent';renderQuickReplies();
check(!$('quickRepliesList').querySelector('[aria-label^="Add to staff:"]'),'regular staff cannot share saved replies');
state.agent.role='admin';renderQuickReplies();
check($('quickRepliesList').querySelector('[aria-label^="Add to staff:"]'),'Admin has share icon beside existing actions');
const before=JSON.stringify(state.quickReplies);
await showQuickReplyAssignment(replies[1]);
check($('quickReplyContentFields').classList.contains('hidden')&&$('quickReplyShareSummary').textContent.includes('PDF'),'saved PDF assignment requires no upload');
check($('quickReplyRecipients').querySelectorAll('input').length===1&&$('quickReplyRecipients').querySelector('input').value==='7','owner excluded from share targets');
count=calls.length;await saveQuickReply({preventDefault(){}});check(calls.length===count,'saved reply assignment needs recipients');
$('quickReplyRecipients').querySelector('input').checked=true;rejectSave=true;await saveQuickReply({preventDefault(){}});
check(state.quickReplySharing===2&&$('quickReplyRecipients').querySelector('input').checked,'failed share keeps selection');rejectSave=false;skippedCount=1;
await saveQuickReply({preventDefault(){}});
check(notifications.at(-1)==='Quick reply added for 0 staff members. 1 already had it and were skipped.','duplicate sharing reports skipped rather than added');skippedCount=0;
assigned=calls.filter(c=>c[0]==='/api/support/quick-replies/2/assign').at(-1);
check(JSON.stringify(JSON.parse(assigned[1].body))==='{"recipient_ids":[7],"all_staff":false}','saved share submits only source ID and audience, not file or content');
check(JSON.stringify(state.quickReplies)===before&&state.quickReplySharing===null,'sharing does not duplicate or edit owner replies');
showQuickReplyForm(replies[1]);check(!$('quickReplyContentFields').classList.contains('hidden')&&state.quickReplySharing===null,'normal edit works after sharing');
showQuickReplyList();selectQuickReplyTab('location');
document.body.dataset.result='passed';$('sessionLoading').classList.add('hidden');
__SCREENSHOT_FORM__
})().catch(error=>{document.body.dataset.result='FAILED: '+error.message});
'''
    form = ''
    if os.environ.get('SUPPORT_QUICK_REPLY_SCREENSHOT_FORM'):
        form = "showQuickReplyForm(null);$('quickReplyTitle').value='Main office';$('quickReplyLatitude').value='33.89';$('quickReplyLongitude').value='35.5';$('quickReplyAddress').value='Reception desk';$('quickReplyAudience').value='selected';await updateQuickReplyAudience();$('quickReplyRecipients').querySelector('input').checked=true;$('quickReplyRecipients').querySelectorAll('span')[1].textContent='Office manager · Super Admin';"
    if os.environ.get('SUPPORT_QUICK_REPLY_SCREENSHOT_SHARE'):
        form = "await showQuickReplyAssignment(replies[2]);$('quickReplyRecipients').querySelector('input').checked=true;"
    script = setup + 'function createIcon(' + icons + 'const QUICK_REPLIES_LIST_SUBTITLE = ' + quick + bindings + message + links + copying + assertions.replace('__SCREENSHOT_FORM__', form)
    html = _configure(_TEMPLATE.replace('__SUPPORT_STYLE__', '<style>'+_ASSETS['support.css']+'</style>').replace('__SUPPORT_THEME__', '').replace('__SUPPORT_SCRIPTS__', '<script>'+script+'</script>'))
    path = tmp_path/'quick-replies.html'; path.write_text(html)
    command = [browser,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--no-first-run',
               '--disable-background-networking','--user-data-dir='+str(tmp_path/'profile'),'--window-size=1100,1100','--virtual-time-budget=2000','--dump-dom']
    if os.environ.get('SUPPORT_QUICK_REPLY_SCREENSHOT'):
        command.append('--screenshot='+os.environ['SUPPORT_QUICK_REPLY_SCREENSHOT'])
    result = subprocess.run([*command,path.as_uri()], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'data-result="passed"' in result.stdout, result.stdout[result.stdout.find('<body'):result.stdout.find('<body')+500] + result.stderr[-1200:]
