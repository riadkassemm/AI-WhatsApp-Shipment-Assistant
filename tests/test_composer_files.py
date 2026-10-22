"""Exercise actual browser drop/clipboard events and private PDF quick-reply UI."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from app.support_ui import _ASSETS, _TEMPLATE, _configure


def test_drop_paste_previews_and_pdf_quick_replies(tmp_path):
    browser=shutil.which(os.environ.get('SUPPORT_TEST_BROWSER','chromium')) or shutil.which('google-chrome')
    if not browser:
        if os.environ.get('SUPPORT_TEST_BROWSER'): pytest.fail('Configured browser unavailable')
        pytest.skip('Chrome/Chromium unavailable')
    state_helpers=_ASSETS['state.js'].split('function createIcon(',1)[1].split('function cookie(',1)[0]
    quick=_ASSETS['actions.js'].split('const QUICK_REPLIES_LIST_SUBTITLE = ',1)[1].split('const AI_REASON_LABELS = ',1)[0]
    setup='''
const $=id=>document.getElementById(id);
const state={authenticated:true,agent:{id:'7'},ticketId:42,composerEnabled:true,busy:false,
quickReplies:[],quickRepliesLoaded:true,quickRepliesBusy:false,quickRepliesRequest:0,quickReplyEditing:null};
const errors=[],calls=[];
const showError=message=>errors.push(message);
const text=(element,value)=>{element.textContent=value};
const setAppStatus=()=>{};const notify=()=>{};
const formatBytes=value=>String(value)+' bytes';
const emptyMarkup=title=>{const outer=document.createElement('div');outer.textContent=title;return {outer}};
const setButtonBusy=()=>{};const editPendingImage=()=>{};
function openDialog(id){$(id).classList.remove('hidden');state.activeModal=id;}
function closeDialog(id){$(id).classList.add('hidden');state.activeModal=null;}
function check(ok,why){if(!ok)throw Error(why);}
async function api(path,options){calls.push([path,options]);return {id:9,title:options.body.get('title'),body:options.body.get('body'),attachment_filename:options.body.get('file').name,attachment_size:50};}
let pendingFetch;
window.fetch=()=>new Promise(resolve=>{pendingFetch=resolve});
const response=()=>({ok:true,blob:async()=>new Blob(['%PDF-1.4\\n%%EOF'],{type:'application/pdf'})});
'''
    assertions='''
(async()=>{
installComposerFileInput();
const zone=$('composerDropZone'),message=$('message');
const png=new File([new Uint8Array([137,80,78,71])],'Clipboard.png',{type:'image/png'});
const pdf=new File(['%PDF-1.4\\n%%EOF'],'Shipping guide.pdf',{type:'application/pdf'});
function transfer(files){const value=new DataTransfer();for(const file of files)value.items.add(file);return value;}
function drop(files){const event=new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer(files)});zone.dispatchEvent(event);return event;}
const textPaste=new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:new DataTransfer()});
message.dispatchEvent(textPaste);check(!textPaste.defaultPrevented,'plain text paste stays native');
let event=drop([pdf]);check(event.defaultPrevented,'file drop cannot navigate');
check(state.pendingMedia.kind==='document'&&state.pendingMedia.file.name===pdf.name,'PDF drop stages draft');
check(!$('mediaDraft').classList.contains('hidden')&&$('mediaDraft').textContent.includes(pdf.name),'real preview is visible');
check(!calls.length,'drop never auto-sends');
drop([png]);check(state.pendingMedia.file.name===pdf.name,'existing attachment is not overwritten');
clearMediaDraft();
const paste=new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:transfer([png])});message.dispatchEvent(paste);
check(paste.defaultPrevented&&state.pendingMedia.kind==='image','image clipboard creates preview');
check($('mediaDraft').querySelector('img'),'image preview rendered');
clearMediaDraft();state.composerEnabled=false;drop([pdf]);check(!state.pendingMedia,'read-only ticket cannot accept files');
state.composerEnabled=true;state.editTarget={};drop([pdf]);check(!state.pendingMedia,'editing does not attach');
state.editTarget=null;state.mediaRecorder={};drop([pdf]);check(!state.pendingMedia,'recording does not attach');state.mediaRecorder=null;
drop([png,pdf]);check(!state.pendingMedia,'multiple files rejected without discarding any');
drop([new File(['x'],'script.exe',{type:'application/x-msdownload'})]);check(!state.pendingMedia,'unsupported type rejected');
check(!attachComposerFile(new File([],'empty.pdf',{type:'application/pdf'})),'empty file rejected');
check(!attachComposerFile(new File([new Uint8Array(6*1024*1024)],'large.png',{type:'image/png'})),'size limit enforced before upload');
const outside=new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer([pdf])});document.body.dispatchEvent(outside);check(outside.defaultPrevented&&!state.pendingMedia,'missed drop never leaves page');
const dragText=new DataTransfer();dragText.setData('text/plain','hello');const plainDrop=new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dragText});zone.dispatchEvent(plainDrop);check(!plainDrop.defaultPrevented,'text drag preserved');
const saved={id:4,title:'Shipping guide',body:'Please see our shipping guide.',attachment_filename:pdf.name,attachment_size:50};
state.quickReplies=[saved];
message.value='Existing draft';await attachQuickReplyPdf(saved);check(!pendingFetch,'saved PDF cannot overwrite text draft');message.value='';
let task=attachQuickReplyPdf(saved);state.ticketId=43;pendingFetch(response());await task;check(!state.pendingMedia,'late PDF cannot go to another customer');
state.ticketId=42;openDialog('quickRepliesModal');task=attachQuickReplyPdf(saved);pendingFetch(response());await task;
check(state.pendingMedia.kind==='document'&&message.value===saved.body,'saved PDF and caption populate composer');
check(!state.activeModal&&!calls.length,'PDF is previewed, not sent automatically');
clearMediaDraft();message.value='';
showQuickReplyForm(null);$('quickReplyPdf').files=transfer([pdf]).files;updateQuickReplyCount();
check($('quickReplyBody').maxLength===1024,'PDF caption limit');
$('quickReplyTitle').value='Shipping guide';$('quickReplyBody').value='';await saveQuickReply({preventDefault(){}});
check(calls.length===1&&calls[0][0]==='/api/support/quick-replies/pdf','PDF save uses upload API');
check(calls[0][1].body instanceof FormData&&calls[0][1].body.get('file').name===pdf.name,'saved PDF carries file');
state.quickReplies=[saved,{id:5,title:'Opening hours',body:'Our office is open Monday to Friday.'}];renderQuickReplies();
check($('quickRepliesList').textContent.includes('Attach PDF'),'PDF reuse action visible');
$('quickRepliesSearch').value='Shipping guide.pdf';renderQuickReplies();check($('quickRepliesList').children.length===1,'search matches filename');
$('quickRepliesSearch').value='';renderQuickReplies();openDialog('quickRepliesModal');
$('sessionLoading').classList.add('hidden');
document.body.dataset.attachmentsTest='passed';
})().catch(error=>{document.body.dataset.attachmentsTest='FAILED: '+error.message});
'''
    script=setup+'function createIcon('+state_helpers+_ASSETS['media.js']+'const QUICK_REPLIES_LIST_SUBTITLE = '+quick+assertions
    html=_configure(_TEMPLATE.replace('__SUPPORT_STYLE__','<style>'+_ASSETS['support.css']+'</style>').replace('__SUPPORT_THEME__','').replace('__SUPPORT_SCRIPTS__','<script>'+script+'</script>'))
    path=tmp_path/'attachments.html';path.write_text(html)
    command=[browser,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage','--no-first-run','--disable-background-networking','--user-data-dir='+str(tmp_path/'profile'),'--window-size=1440,1000','--virtual-time-budget=2000','--dump-dom']
    if os.environ.get('SUPPORT_ATTACHMENT_SCREENSHOT'):command.append('--screenshot='+os.environ['SUPPORT_ATTACHMENT_SCREENSHOT'])
    result=subprocess.run([*command,path.as_uri()],capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stderr
    assert 'data-attachments-test="passed"' in result.stdout, result.stdout[result.stdout.find('<body'):result.stdout.find('<body')+400] + result.stderr[-1500:]
