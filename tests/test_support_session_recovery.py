"""Session bootstrap uses fake timers and synthetic requests; no real services."""
from pathlib import Path
import subprocess


BOOTSTRAP = Path('app/static/support/rendering.js').read_text()
BOOTSTRAP = BOOTSTRAP[BOOTSTRAP.index('async function bootstrap('):BOOTSTRAP.index('async function logout(')]
HARNESS = r'''
const assert = require('node:assert/strict');
const timers = new Map(); let timerId = 0;
const setTimeout = (callback, delay) => {timers.set(++timerId, {callback, delay}); return timerId};
const clearTimeout = id => timers.delete(id);
const settle = async () => {for (let n=0;n<30;n++) await Promise.resolve()};
const fire = async delay => {
  const entry = [...timers].find(([,timer])=>timer.delay===delay);
  assert(entry, 'missing timer: '+delay);
  timers.delete(entry[0]); entry[1].callback(); await settle();
};
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {textContent:'', hidden:false,
    classList:{add(){elements.get(id).hidden=true},remove(){elements.get(id).hidden=false}}});
  return elements.get(id);
};
const text = (element,value) => element.textContent=value;
const state = {pageLeaving:false,authenticated:false};
let requests=0, dashboards=0, restores=0, signins=0;
const syncThemeControls=()=>{},setAgentIdentity=()=>{},renderAiSettingsUnavailable=()=>{};
const loadAiSettings=async()=>{},refreshAllQueueCounts=async()=>{};
const restoreSupportWorkspace=async()=>{restores++};
const showDashboard=()=>{dashboards++;state.authenticated=true};
const showError=message=>{throw Error(message)};
let respond;
const api=(path,options)=>{assert.equal(path,'/api/support/me');requests++;return respond(options)};
const fail=code=>Object.assign(Error('synthetic failure'),{status:code});
'''


def run(checks):
    script = HARNESS + BOOTSTRAP + '\n(async()=>{' + checks + r'''
    })().catch(error=>{console.error(error);process.exitCode=1});'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_transient_bootstrap_recovery_is_bounded_and_singleflight():
    run(r'''
respond=async()=>{if(requests===1)throw new TypeError('network');if(requests===2)throw fail(503);return {id:'7',role:'agent'}};
const first=bootstrap(),duplicate=bootstrap(); await settle();
assert.equal(requests,1);assert.equal(dashboards,0);assert($('sessionRetryBtn').hidden);
await fire(1000);assert.equal(requests,2);assert.equal(dashboards,0);
await fire(2000);await Promise.all([first,duplicate]);
assert.equal(requests,3);assert.equal(dashboards,1);assert.equal(restores,1);
assert.equal(timers.size,0);assert.equal(state.sessionCheckController,null);assert.equal(state.bootstrapTask,null);
// A persistent outage stops after three attempts and keeps the workspace intact.
state.authenticated=false;state.savedSelection=42;requests=0;respond=async()=>{throw fail(502)};
const outage=bootstrap();await settle();await fire(1000);await fire(2000);await outage;
assert.equal(requests,3);assert.equal(state.authenticated,false);assert.equal(state.savedSelection,42);
assert(!$('sessionRetryBtn').hidden);assert.match($('sessionLoadingStatus').textContent,/Unable to check/);
assert.equal(timers.size,0);assert.equal(dashboards,1);
respond=async()=>({id:'7'});await bootstrap();assert.equal(dashboards,2);assert.equal(restores,2);
''')


def test_invalid_sessions_and_programming_errors_are_never_retried():
    run(r'''
for(const status of [401,403,400,404]){
  requests=0;respond=async()=>{if(status===401)signins++;throw fail(status)};
  await bootstrap();assert.equal(requests,1);assert.equal(dashboards,0);assert.equal(timers.size,0);
  if(status===403) assert.match($('sessionLoadingStatus').textContent,/does not have access/);
}
assert.equal(signins,1);respond=async()=>{throw Error('not a network error')};requests=0;
await bootstrap();assert.equal(requests,1);assert.equal(dashboards,0);
''')


def test_retryable_http_statuses_recover_without_sign_in():
    run(r'''
for(const status of [408,429,500,502,503,504]){
  requests=0;state.authenticated=false;
  respond=async()=>{if(requests===1)throw fail(status);return {id:'7'}};
  const task=bootstrap();await settle();await fire(1000);await task;
  assert.equal(requests,2);assert.equal(state.authenticated,true);assert.equal(timers.size,0);
}
assert.equal(signins,0);
''')


def test_timeout_and_navigation_cancel_requests_and_retry_timers():
    run(r'''
let aborted=0;
respond=({signal})=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>{
  aborted++;reject(new DOMException('cancelled','AbortError'));
},{once:true}));
const hanging=bootstrap();await settle();await fire(10000);
assert.equal(aborted,1);assert.equal(requests,1);assert.equal(dashboards,0);
respond=async()=>({id:'7'});await fire(1000);await hanging;assert.equal(dashboards,1);assert.equal(timers.size,0);
state.authenticated=false;requests=0;respond=async()=>{throw new TypeError('network')};
const waiting=bootstrap();await settle();
state.pageLeaving=true;state.sessionCheckController.abort();await waiting;
assert.equal(timers.size,0);assert.equal(requests,1);assert.equal(dashboards,1);
// A late response from a cancelled page cannot authenticate the workspace.
state.pageLeaving=false;let finish;
respond=()=>new Promise(resolve=>{finish=resolve});
const late=bootstrap();await settle();state.pageLeaving=true;state.sessionCheckController.abort();
finish({id:'8'});await late;assert.equal(dashboards,1);assert.equal(state.agent.id,'7');assert.equal(timers.size,0);
''')


def test_cancelled_body_response_cannot_sign_out_or_restore_stale_identity():
    # Exercise the real API adapter, including cancellation during JSON parsing.
    source = Path('app/static/support/api.js').read_text()
    source = source[:source.index('// After a deploy,')]
    script = r'''
const assert=require('node:assert/strict');
const state={agent:{id:'7'},pageLeaving:false};let logins=0,finish;
const SUPPORT_UI_VERSION='test',noticeDashboardUpdate=()=>{};
const showLogin=()=>{logins++};
const controller=new AbortController();
const fetch=async()=>({ok:false,status:401,headers:{get:()=>null},json:()=>new Promise(resolve=>{finish=resolve})});
''' + source + r'''
(async()=>{
const pending=api('/api/support/me',{signal:controller.signal});
for(let n=0;n<10;n++)await Promise.resolve();
controller.abort();finish({});
await assert.rejects(pending,{name:'AbortError'});assert.equal(logins,0);assert.equal(dashboardRequests.size,0);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_browser_back_navigation_restarts_cancelled_initial_session_check():
    actions = Path('app/static/support/actions.js').read_text()
    handler = actions[actions.index("window.addEventListener('pageshow'"):actions.index('restoreLayoutPreferences();')]
    run(r'''
let pageshow;
const window={addEventListener:(event,callback)=>{assert.equal(event,'pageshow');pageshow=callback}};
const openTicket=()=>{throw Error('Unexpected ticket refresh')},loadQueue=openTicket;
$('loginPage').classList.contains=()=>true;
''' + handler + r'''
let finish;
respond=()=>new Promise(resolve=>{finish=resolve});
const leaving=bootstrap();await settle();state.pageLeaving=true;state.sessionCheckController.abort();
respond=async()=>({id:'8'});
pageshow({persisted:true});assert.equal(requests,1,'restoration waits for cancellation');
finish({id:'7'});await leaving;await settle();
assert.equal(requests,2);assert.equal(dashboards,1);assert.equal(state.agent.id,'8');assert.equal(timers.size,0);
''')


def test_cached_read_view_audits_do_not_render_in_timeline():
    source = Path('app/static/support/rendering.js').read_text()
    source = source[source.index('function addEvent('):source.index('function dayKey(')]
    script = r'''
const assert=require('node:assert/strict');
const document={createElement:()=>({})},formatStatus=x=>x,fmt=x=>x;
const entries=[],container={append:node=>entries.push(node)};
''' + source + r'''
for(const event_type of ['CONVERSATION_VIEWED','INCOMING_MARKED_READ','TICKET_CLAIMED'])addEvent(container,{event_type});
assert.equal(entries.length,1);assert.match(entries[0].textContent,/TICKET CLAIMED/);
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
