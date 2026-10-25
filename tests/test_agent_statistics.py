from datetime import date, timedelta
from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException
from app.support_statistics import date_bounds


def test_date_range_respects_daylight_saving_and_inclusive_end():
    lower, upper = date_bounds(date(2026, 3, 8), date(2026, 3, 8), 'America/New_York')
    assert upper - lower == timedelta(hours=23)
    lower, upper = date_bounds(date(2026, 11, 1), date(2026, 11, 1), 'America/New_York')
    assert upper - lower == timedelta(hours=25)
    lower, upper = date_bounds(date(2026, 10, 23), date(2026, 10, 23), 'Asia/Beirut')
    assert lower.isoformat() == '2026-10-22T21:00:00'
    assert upper.isoformat() == '2026-10-23T21:00:00'


@pytest.mark.parametrize('start,end,zone', [
    (date(2026, 1, 2),date(2026, 1, 1),'UTC'),
    (date(2025, 1, 1),date(2026, 1, 2),'UTC'),
    (date(2026, 1, 1),date(2026, 1, 1),'../../etc/passwd'),
    (date(9999, 12, 31),date(9999, 12, 31),'UTC'),
])
def test_invalid_report_ranges(start,end,zone):
    with pytest.raises(HTTPException) as error:
        date_bounds(start,end,zone)
    assert error.value.status_code == 422


def test_activity_frontend_idle_visibility_resume_and_sequence():
    source = Path('app/static/support/statistics.js').read_text()
    factory = source[source.index('function createSupportActivityTracker('):source.index('const supportActivity =')]
    script = "const assert=require('node:assert/strict');\n" + factory + '''
let now=0, visible=true; const sent=[];
const tracker=createSupportActivityTracker(value=>sent.push(value),()=>now,()=>visible);
tracker.start('tab'); assert.equal(sent.length,1); assert.equal(sent[0].reset,true);
now=15000; tracker.tick(); assert.equal(sent.at(-1).active,true); assert.equal(sent.at(-1).reset,false);
now=60000; tracker.tick(); assert.equal(sent.at(-1).active,false);
const idleCount=sent.length; now=75000; tracker.tick(); assert.equal(sent.length,idleCount);
now=80000; tracker.input(); assert.equal(sent.at(-1).reset,true);
visible=false; now=85000; tracker.visibility(); assert.equal(sent.at(-1).active,false);
now=90000; tracker.tick(); assert.equal(sent.length,idleCount+2);
visible=true; now=95000; tracker.visibility(); assert.equal(sent.at(-1).reset,true);
now=200000; tracker.input(); assert.equal(sent.at(-1).reset,true,'input after throttled idle creates a new baseline');
assert.deepEqual(sent.map(x=>x.sequence),sent.map((_,i)=>i+1));
tracker.stop(); const count=sent.length; tracker.tick(); tracker.input(); assert.equal(sent.length,count);
tracker.start('new-tab'); assert.equal(sent.at(-1).sequence,1);
'''
    result = subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0,result.stderr


def test_statistics_browser_roles_filters_sorting_and_safe_rendering(tmp_path):
    import os
    import shutil
    from app.support_ui import _TEMPLATE, _ASSETS
    requested = os.environ.get('SUPPORT_TEST_BROWSER')
    browser = shutil.which(requested or 'chromium') or shutil.which('google-chrome')
    if not browser:
        if requested: pytest.fail(f'Browser unavailable: {requested}')
        pytest.skip('Chrome/Chromium unavailable')
    setup = '''
const $=id=>document.getElementById(id);
const state={authenticated:true,agent:{id:'8',role:'supervisor'},activeModal:null};
function openDialog(id){$(id).classList.remove('hidden');state.activeModal=id;}
function closeDialog(id){$(id).classList.add('hidden');state.activeModal=null;}
const calls=[];
const downloads=[];const exports=[];
URL.createObjectURL=()=> 'blob:test'; URL.revokeObjectURL=()=>{};
HTMLAnchorElement.prototype.click=function(){downloads.push(this.download)};
async function fetch(path){exports.push(path);return {ok:true,blob:async()=>new Blob(['%PDF-test'])};}

const report={agents:[{id:7,name:'Alex',role:'agent',is_active:1},{id:8,name:'Morgan',role:'supervisor',is_active:1}],
rows:[{id:7,name:'Alex',role:'agent',is_active:1,rank:1,active_seconds:7215,tickets_claimed:12,tickets_resolved:10,messages_sent:48,tickets_transferred:5,tickets_received_by_transfer:3},
{id:8,name:'Morgan',role:'supervisor',is_active:1,rank:2,active_seconds:3600,tickets_claimed:8,tickets_resolved:6,messages_sent:25,tickets_transferred:2,tickets_received_by_transfer:4}],
totals:{active_seconds:10815,tickets_claimed:20,tickets_resolved:16,messages_sent:73,tickets_transferred:7,tickets_received_by_transfer:7},
rank_by:'tickets_resolved',direction:'desc',timezone:'Asia/Beirut',activity_tracking_started_at:'2026-10-23T00:00:00Z',
transfer_history_since:'2026-09-01T00:00:00Z',ticket_history_since:'2026-09-01T00:00:00Z',message_history_since:'2026-09-01T00:00:00Z'};
for(const row of report.rows)Object.assign(row,{average_handling_seconds:600,median_handling_seconds:480,average_resolution_seconds:900,median_resolution_seconds:720,handling_samples:12,resolution_samples:8});
report.timing_totals={average_handling_seconds:600,median_handling_seconds:480,average_resolution_seconds:900,median_resolution_seconds:720};
let resolvePending=null;
const renderedInvestigation=[];
function addMessage(container,message,mode){renderedInvestigation.push(mode);const p=document.createElement('p');p.textContent=message.body;container.append(p);}
const investigationDetail={ticket:{ticket_reference:'TEST-42'},periods:[{agent_id:'7',agent_name:'Alex',started_at:'2026-10-23T10:00:00Z',ended_at:'2026-10-23T10:05:00Z',end_reason:'transferred',valid:true,seconds:300}],
 original_claimant_id:'7',original_claimant_name:'Alex',issues:[],messages:[{id:1,body:'Synthetic customer message',sender_type:'CUSTOMER',direction:'INBOUND',send_status:'RECEIVED'}],events:[{id:1,event_type:'TICKET_TRANSFERRED',actor_name:'Alex',created_at:'2026-10-23T10:05:00Z',details_json:'{}'}],next_message_before:1,next_event_before:1};
async function api(path) {
 calls.push(path);
 if(resolvePending==='wait') return new Promise(resolve=>{resolvePending=resolve});
 if(path.startsWith('/api/support/statistics/tickets/'))return investigationDetail;
 if(path.startsWith('/api/support/statistics/tickets?'))return {items:[{id:42,ticket_reference:'TEST-42',customer_name:'Example',status:'RESOLVED',flags:[],issues:[]}],next_before:42};
 const query=new URL('http://test'+path).searchParams;
 return {...report,rank_by:query.get('rank_by'),direction:query.get('direction')};
}
function check(condition,message){if(!condition)throw Error(message);}
'''
    setup += _ASSETS['state.js'][_ASSETS['state.js'].index('function staffRoleLabel(role)'):]
    assertions = '''
(async()=>{
for(const role of ['agent','unexpected']){state.agent.role=role;check(!canViewAgentStatistics(),'agent cannot open report');}
state.agent.role='admin';check(canViewAgentStatistics(),'admin access');
state.agent.role='supervisor';check(canViewAgentStatistics(),'supervisor access');
$('agentStatisticsBtn').classList.remove('hidden');$('agentStatisticsBtn').click();
await Promise.resolve();await Promise.resolve();
check($('statisticsRows').children.length===2,'report renders staff');
check($('statisticsRows').textContent.includes('Super Admin'),'role uses new display label');
check(!$('statisticsPdfBtn').disabled,'export enabled after report loads');
const appliedStart=$('statisticsStart').value;
$('statisticsStart').value='2020-01-01';
await downloadAgentStatisticsPdf();
check(exports[0].includes('start='+appliedStart),'PDF uses applied filters, not an unsaved draft');
check(downloads[0].endsWith('.pdf'),'PDF attachment downloads');
$('statisticsStart').value=appliedStart;

check($('statisticsRanking').textContent.includes('Tickets resolved'),'ranking criterion explicit');
check($('statisticsTotals').textContent.includes('73'),'team totals');
$('statisticsAgent').value='7';$('statisticsAgent').dispatchEvent(new Event('change'));
await Promise.resolve();await Promise.resolve();check(calls.at(-1).includes('agent_id=7'),'agent filter sent');
document.querySelector('button[data-metric="messages_sent"]').click();
await Promise.resolve();await Promise.resolve();check(calls.at(-1).includes('rank_by=messages_sent'),'metric sorting sent');
check($('statisticsRanking').textContent.includes('Outbound messages sent'),'selected criterion changed');
document.querySelector('button[data-metric="messages_sent"]').click();
await Promise.resolve();await Promise.resolve();check(calls.at(-1).includes('direction=asc'),'reverse sort');
for (const metric of ['tickets_transferred','tickets_received_by_transfer']) {
 document.querySelector('button[data-metric="'+metric+'"]').click();
 await Promise.resolve();await Promise.resolve();
 check(calls.at(-1).includes('rank_by='+metric),'transfer ranking sent');
 check($('statisticsRanking').textContent.includes(statisticsMetrics[metric]),'transfer ranking label');
}
check($('statisticsRows').firstElementChild.children.length===12,'transfer and timing columns render');
check($('statisticsTotals').children.length===10,'transfer and timing totals render');
for(const metric of ['average_handling_seconds','median_resolution_seconds']){
 $('statisticsMetric').value=metric;await loadAgentStatistics();
 check($('statisticsRanking').textContent.includes(statisticsMetrics[metric]),'timing ranking uses selected criterion');
}
state.ticketId=99;
await browseStatisticsTickets();check(calls.at(-1).includes('agent_id=7'),'investigation uses applied staff filter');
await investigateStatisticsTicket(42);
check(state.ticketId===99&&renderedInvestigation[0]==='investigation','investigation never opens operational ticket');
check($('investigationEvents').textContent.includes('TICKET_TRANSFERRED')&&$('investigationPeriods').textContent.includes('Alex'),'audit and responsibility shown');
await investigateStatisticsTicket(42,'messages');check(calls.at(-1).includes('before_message=1'),'message pagination');
await investigateStatisticsTicket(42,'events');check(calls.at(-1).includes('before_event=1'),'audit pagination');
check(!calls.some(path=>path.includes('/view')||path.includes('/read')||path.startsWith('/api/support/tickets/')),'investigation makes no operational calls');
$('statisticsFlaggedOnly').checked=true;await browseStatisticsTickets();check(calls.at(-1).includes('flagged_only=true'),'review indicator filter');
report.rows[0].name='<img src=x onerror="alert(1)">';renderAgentStatistics(report);
check(!$('statisticsRows').querySelector('img'),'staff names render as text');
resolvePending='wait';const pending=loadAgentStatistics();
state.agent.role='agent';clearAgentStatistics();resolvePending(report);await pending;
check($('statisticsRows').children.length===0,'late response after permission loss cannot disclose data');
check($('agentStatisticsModal').classList.contains('hidden'),'report closes on permission loss');
check($('statisticsPdfBtn').disabled,'export disabled on permission loss');
await downloadAgentStatisticsPdf();check(exports.length===1,'agent cannot export');

state.agent.role='supervisor';report.rows[0].name='Alex';renderAgentStatistics(report);openDialog('agentStatisticsModal');
$('statisticsAgent').value='';$('statisticsMetric').value='tickets_resolved';$('statisticsDirection').value='desc';
resolvePending=null;await loadAgentStatistics();
$('statisticsError').textContent='';
document.body.dataset.statisticsTest='passed';
if('__INVESTIGATION_SCREENSHOT__'==='yes'){await browseStatisticsTickets();await investigateStatisticsTicket(42);$('statisticsInvestigation').scrollIntoView();}
})().catch(error=>{document.body.dataset.statisticsTest='FAILED: '+error.message;});
'''
    assertions = assertions.replace('__INVESTIGATION_SCREENSHOT__', 'yes' if os.environ.get('SUPPORT_INVESTIGATION_SCREENSHOT') else 'no')
    html = (_TEMPLATE.replace('__SUPPORT_STYLE__','<style>'+_ASSETS['support.css']+'</style>')
            .replace('__SUPPORT_THEME__','').replace('__SUPPORT_SCRIPTS__','<script>'+setup+_ASSETS['statistics.js']+assertions+'</script>'))
    path=tmp_path/'statistics.html';path.write_text(html)
    command=[browser,'--headless','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
             '--no-first-run','--no-default-browser-check','--disable-background-networking',
             '--user-data-dir='+str(tmp_path/'profile'),'--window-size=1440,1100',
             '--virtual-time-budget=2000','--dump-dom']
    if os.environ.get('SUPPORT_STATISTICS_SCREENSHOT'):
        command.append('--screenshot='+os.environ['SUPPORT_STATISTICS_SCREENSHOT'])
    result=subprocess.run([*command,path.as_uri()],capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stderr
    assert 'data-statistics-test="passed"' in result.stdout,result.stdout[-5000:]


def test_pdf_export_paginates_includes_filters_and_escapes_staff_names():
    from io import BytesIO
    from pypdf import PdfReader
    from app.support_statistics_pdf import statistics_pdf, display_text
    row = dict(id='8', name='Morgan <Admin> & Team', role='supervisor', is_active=True,
               rank=1, active_seconds=3661, tickets_claimed=12, tickets_resolved=10, messages_sent=30,tickets_transferred=5,tickets_received_by_transfer=3)
    report = dict(start='2026-10-01', end='2026-10-23', timezone='Asia/Beirut', agent_id=8,
                  agents=[row], rows=[{**row, 'rank': i} for i in range(1, 101)], totals=row,
                  rank_by='messages_sent', direction='desc')
    content = statistics_pdf(report)
    reader = PdfReader(BytesIO(content))
    text = '\n'.join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) > 1
    for expected in ['Agent Statistics', 'Morgan <Admin> & Team', 'Super Admin',
                     'Outbound messages sent', 'Highest first', '2026-10-01', '2026-10-23',
                     'Asia/Beirut', '1h 1m 1s', 'Deleted staff are excluded', 'Tickets transferred', 'Tickets received by transfer']:
        assert expected in ' '.join(text.split())
    assert all('Rank' in page.extract_text() or 'Average handling time' in ' '.join(page.extract_text().split()) for page in reader.pages[:-1])
    assert '&lt;' in display_text('<Admin>')
    assert display_text('محمد') != 'محمد'  # Arabic shaping and visual direction
    assert statistics_pdf({**report, 'rows': [{**row, 'name': 'محمد أحمد'}]}).startswith(b'%PDF-')
