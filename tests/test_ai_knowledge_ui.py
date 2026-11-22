"""AI knowledge base review tab (Step 4c) in the shipped dashboard, synthetic data only."""
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
class TestEventSource { close() {} }
window.EventSource=TestEventSource;
const draft={id:11,intent_key:'refund',version:1,status:'DRAFT',title:'Refunds',approved_policy:'Refunds above <AMOUNT_1> need a manager.',
  data_needed:'Order ID',escalate_when:'Always',examples:[{style:'en',customer:'refund?',reply:'Let me check.'}],
  observed_practice:'Agents asked for a photo.',source:'AI_DRAFT',source_ticket_count:42,row_version:1};
window.knowledgeWrites=[];
const nativeFetch=window.fetch.bind(window);
window.fetch=async(url,options={})=>{
  const parsed=new URL(url,location.origin),path=parsed.pathname;
  const reply=data=>Promise.resolve(new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}}));
  if(path==='/api/support/ai-knowledge') return reply({articles:[draft],approved_topics:0,prompt_tokens_estimate:0,prompt_token_budget:15000,over_budget:false});
  if(path==='/api/support/ai-knowledge/11'&&options.method==='PUT'){const body=JSON.parse(options.body);knowledgeWrites.push(['save',body]);
    Object.assign(draft,body,{row_version:2});return reply(draft);}
  if(path==='/api/support/ai-knowledge/11') return reply(draft);
  if(path==='/api/support/ai-knowledge/11/approve'){knowledgeWrites.push(['approve',JSON.parse(options.body)]);
    Object.assign(draft,{status:'APPROVED',approved_by_staff_name:'Demo Admin',approved_at:'2026-11-22T10:00:00Z'});return reply(draft);}
  if(path==='/api/support/ai-settings') return reply({mode:'MANUAL',manual_ai_enabled:false,effective_enabled:false,
    effective_reason:'MANUAL_OFF',version:1,schedule_timezone:'Asia/Beirut'});
  if(path==='/api/support/whatsapp-numbers') return reply({items:[]});
  return nativeFetch(url,options);
};
</script>'''

CHECKS = r'''<script>
(async()=>{try{
  await bootstrap();
  const check=(condition,message)=>{if(!condition)throw Error(message)};
  const waitFor=async(test,message)=>{for(let i=0;i<100&&!test();i++) await new Promise(r=>setTimeout(r,20)); check(test(),message);};
  await openAiSettingsModal();
  check(!$('aiTabKnowledge').classList.contains('hidden'),'Knowledge tab hidden from an Admin');
  selectAiTab('knowledge');
  await waitFor(()=>$('aiKnowledgeList').textContent.includes('Refunds'),'Draft not listed');
  check($('aiKnowledgeSummary').textContent.includes('0 approved topics'),'Summary missing');
  $('aiKnowledgeList').querySelector('button').click();
  await waitFor(()=>$('aiKnowledgePolicy').value.includes('<AMOUNT_1>'),'Draft not opened');
  check($('aiKnowledgeIntent').disabled,'Topic key editable');
  check($('aiKnowledgeMeta').textContent.includes('drafted from 42 de-identified tickets'),'Source missing');
  check($('aiKnowledgeExamples').value==='en | refund? | Let me check.','Examples not shown');
  $('aiKnowledgePolicy').value='Refunds above 100 USD need a manager.';
  $('aiKnowledgeSaveBtn').click();
  await waitFor(()=>knowledgeWrites.length===1,'Draft not saved');
  check(knowledgeWrites[0][1].row_version===1 && knowledgeWrites[0][1].examples[0].reply==='Let me check.','Wrong save payload');
  await waitFor(()=>aiKnowledgeArticle && aiKnowledgeArticle.row_version===2,'Saved draft not reloaded');
  $('aiKnowledgeApproveBtn').click();
  await waitFor(()=>!$('confirmModal').classList.contains('hidden'),'Approve confirmation missing');
  $('confirmAcceptBtn').click();
  await waitFor(()=>knowledgeWrites.length===2,'Approve not sent');
  check(knowledgeWrites[1][1].row_version===2,'Approve used a stale version');
  await waitFor(()=>!$('aiKnowledgeReviseBtn').classList.contains('hidden'),'Approved article not read-only');
  check($('aiKnowledgePolicy').readOnly && $('aiKnowledgeSaveBtn').classList.contains('hidden'),'Approved article still editable');
  state.agent={id:'7',name:'Agent',role:'agent'};
  await openAiSettingsModal();
  check($('aiTabKnowledge').classList.contains('hidden'),'Agent sees the Knowledge tab');
  check(browserErrors.length===0,browserErrors.join('; '));
  await nativeFetch('/test-result',{method:'POST',body:JSON.stringify({passed:true})});
}catch(error){await nativeFetch('/test-result',{method:'POST',body:JSON.stringify({error:String(error),stack:error.stack})})}})();
</script>'''


def test_knowledge_tab_in_the_dashboard(tmp_path: Path, monkeypatch):
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
        assert finished.wait(30), 'Knowledge browser test did not complete'
        assert results == [{'passed': True}], results
        screenshot = os.environ.get('SUPPORT_AI_KNOWLEDGE_SCREENSHOT')
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
