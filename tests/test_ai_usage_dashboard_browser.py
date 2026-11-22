"""Exercise the shipped Usage & Costs UI with synthetic data and no providers."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from urllib.parse import urlsplit


def test_ai_usage_dashboard_in_browser(tmp_path: Path, monkeypatch):
    from app import support_ui
    monkeypatch.setattr(support_ui.settings, 'support_ui_enabled', True)
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    assert browser, 'Install the configured Chrome/Chromium browser'
    page = asyncio.run(support_ui.support_dashboard()).body.decode()
    setup = r'''<script>
window.browserErrors=[];
window.addEventListener('error',event=>browserErrors.push(event.message));
window.addEventListener('unhandledrejection',event=>browserErrors.push(String(event.reason)));
class TestEventSource { close() {} }
window.EventSource=TestEventSource;
const fixtureBudget={monthly_limit_usd:300,daily_limit_usd:20,warning_thresholds:[50,80,95],
  enforcement_enabled:true,override_until:'2026-11-23T16:30:00Z',budget_timezone:'Asia/Beirut',version:1};
const fixtureRow={name:'gpt-example',requests:184,input_tokens:42000,cached_input_tokens:12000,
  output_tokens:18000,reasoning_tokens:2000,cost_usd:12.5,failures:0};
const fixtureReport={estimated:true,filters:{from:'2026-11-20',to:'2026-11-22',line:null,model:null},
  budget:fixtureBudget,status:{within_budget:true,reason:null},
  note:'Estimated by the application from API usage. The OpenAI invoice is final and may differ.',
  summary:{today_cost_usd:12.5,month_cost_usd:42.75,monthly_budget_usd:300,budget_remaining_usd:257.25,
    budget_used_percent:14.25,range_cost_usd:30.5,offline_cost_usd:4,api_requests:420,
    ai_tickets_handled:null,average_cost_per_ticket_usd:null,escalation_rate:null,resolution_rate:null},
  daily:[{day:'2026-11-20',requests:110,cost_usd:8,pending_usd:0},
    {day:'2026-11-21',requests:126,cost_usd:10,pending_usd:1},
    {day:'2026-11-22',requests:184,cost_usd:12.5,pending_usd:.5}],
  by_model:[fixtureRow,{...fixtureRow,name:'gpt-other',cost_usd:18}],
  by_purpose:[{...fixtureRow,name:'turn'},{...fixtureRow,name:'eval',cost_usd:4}],
  by_tier:[{...fixtureRow,name:'primary'},{...fixtureRow,name:'strong',cost_usd:18}]};
const fixturePrices={prices:[{id:1,model_prefix:'gpt-example',input_per_million:'1',cached_input_per_million:'.1',
  output_per_million:'5',audio_per_minute:null,batch_factor:'.5',verified:false,retired:false,
  source:'Synthetic fixture',effective_from:'2026-11-22T00:00:00Z'}],blocked_models:[]};
const nativeFetch=window.fetch.bind(window);
window.budgetWrites=[];
window.fetch=async(url,options={})=>{
  const parsed=new URL(url,location.origin),path=parsed.pathname;
  const reply=data=>Promise.resolve(new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}}));
  if(path==='/api/support/ai-usage') {
    const report=structuredClone(fixtureReport);
    for(const key of ['from','to','line','model']) if(parsed.searchParams.has(key)) report.filters[key]=parsed.searchParams.get(key);
    if(report.filters.model) report.by_model=report.by_model.filter(row=>row.name===report.filters.model);
    return reply(report);
  }
  if(path==='/api/support/ai-prices') return reply(fixturePrices);
  if(path==='/api/support/ai-budget' && options.method==='PUT') {
    budgetWrites.push(JSON.parse(options.body)); return reply(fixtureBudget);
  }
  if(path==='/api/support/ai-settings') return reply({mode:'MANUAL',manual_ai_enabled:false,effective_enabled:false,
    effective_reason:'MANUAL_OFF',version:1,schedule_timezone:'Asia/Beirut'});
  if(path==='/api/support/whatsapp-numbers') return reply({items:[]});
  return nativeFetch(url,options);
};
</script>'''
    checks = r'''<script>
(async()=>{try{
  await bootstrap();
  const check=(condition,message)=>{if(!condition)throw Error(message)};
  await openAiSettingsModal();
  selectAiTab('usage');
  await loadAiUsage();
  check(!$('aiTabUsage').classList.contains('hidden'),'Manager tab hidden');
  check($('aiChartSpend').querySelectorAll('path.bar').length===3,'Spend chart missing bars');
  check($('aiUsageCards').textContent.includes('Offline cost'),'Offline totals absent');
  check($('aiUsageTablesBody').textContent.includes('By model tier'),'Tier breakdown absent');
  check($('aiBudgetOverride').value==='2026-11-23T16:30','UTC override shifted');
  check(state.aiUsageTimer!==null,'Usage polling missing');
  if(location.search.includes('preview=1')) return;
  $('aiUsageModel').value='gpt-example';
  await loadAiUsage();
  check([...$('aiUsageModel').options].some(option=>option.value==='gpt-other'),'Filter discarded other models');
  $('aiBudgetMonthly').value='275';
  $('aiBudgetMonthly').dispatchEvent(new Event('input',{bubbles:true}));
  await loadAiUsage();
  check($('aiBudgetMonthly').value==='275','Polling overwrote unsaved budget');
  selectAiTab('numbers');
  check(state.aiUsageTimer===null,'Polling continued outside usage tab');
  selectAiTab('usage'); await loadAiUsage();
  check($('aiBudgetMonthly').value==='275','Switching tabs discarded unsaved budget');
  await saveAiBudget({preventDefault(){}});
  check(budgetWrites[0].monthly_limit_usd==='275','Wrong budget saved');
  check(budgetWrites[0].override_until==='2026-11-23T16:30:00.000Z','Budget override changed timezone on save');
  const revised={...fixturePrices.prices[0],model_prefix:'<img src=x onerror=alert(1)>'};
  renderAiPrices({prices:[revised]});
  check(!$('aiPricesTable').querySelector('img'),'Price model injected markup');
  $('aiPricesTable').querySelector('button').click();
  check($('aiPriceModel').value===revised.model_prefix,'Revise did not prefill form');
  const originalApi=api;
  let release;
  api=path=>path.startsWith('/api/support/ai-usage') ? new Promise(resolve=>{release=resolve}) : originalApi(path);
  const pending=loadAiUsage();
  closeAiSettingsModal();
  const oldReport=state.aiUsage;
  release({...fixtureReport,summary:{...fixtureReport.summary,month_cost_usd:999}});
  await pending;
  check(state.aiUsage===oldReport,'Closed tab accepted a stale response');
  api=originalApi;
  await openAiSettingsModal(); selectAiTab('usage'); await loadAiUsage();
  showLogin();
  check(state.aiUsageTimer===null && state.aiBudget===null,'Logout left spending state or timer');
  state.agent={id:'7',name:'Agent',role:'agent'};state.authenticated=true;
  await openAiSettingsModal();
  check($('aiTabUsage').classList.contains('hidden'),'Agent sees Usage & Costs');
  check(browserErrors.length===0,browserErrors.join('; '));
  await nativeFetch('/test-result',{method:'POST',body:JSON.stringify({passed:true})});
}catch(error){await nativeFetch('/test-result',{method:'POST',body:JSON.stringify({error:String(error),stack:error.stack})})}})();
</script>'''
    page = page.replace('</head>', setup + '</head>').replace('</body>', checks + '</body>')
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
            elif path == '/api/support/tickets':
                content = b'{"items":[],"total":0,"page":1,"page_size":30}'
            elif path == '/api/support/queue-counts':
                content = b'{"counts":{"unassigned":0,"mine":0,"chats":0}}'
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
        assert finished.wait(30), 'Usage dashboard browser test did not complete'
        assert results == [{'passed': True}], results
        screenshot = os.environ.get('SUPPORT_AI_USAGE_SCREENSHOT')
        if screenshot:
            subprocess.run([*flags, '--user-data-dir=' + str(tmp_path / 'preview'), '--virtual-time-budget=3000',
                            '--screenshot=' + screenshot, url + '?preview=1'], check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        process.terminate()
        process.wait(5)
        server.shutdown()
        server.server_close()
        thread.join(5)
