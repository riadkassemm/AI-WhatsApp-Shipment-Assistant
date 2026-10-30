const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const refresh = fs.readFileSync('app/static/support/refresh.js', 'utf8');
const apiSource = fs.readFileSync('app/static/support/api.js', 'utf8');
const rendering = fs.readFileSync('app/static/support/rendering.js', 'utf8');
const gate = () => {let resolve; return {promise: new Promise(r => {resolve = r}), resolve: () => resolve()};};
const microtasks = async () => {for (let i = 0; i < 30; i++) await Promise.resolve();};
function clock() {
  let now = 0, id = 0; const timers = new Map();
  return {now: () => now, timers,
    later: (fn, delay) => {timers.set(++id, {at: now + delay, fn}); return id;},
    cancel: id => timers.delete(id),
    advance: async ms => {
      const end = now + ms;
      for (;;) {
        const next = [...timers.entries()].sort((a,b) => a[1].at-b[1].at)[0];
        if (!next || next[1].at > end) break;
        now = next[1].at; timers.delete(next[0]); next[1].fn(); await microtasks();
      }
      now = end; await microtasks();
    }
  };
}
function coordinator(options) {
  const context = vm.createContext({setTimeout,clearTimeout,Date});
  vm.runInContext(refresh.slice(0,refresh.indexOf('const dashboardRefresh =')) + '\nglobalThis.Coordinator=DashboardRefreshCoordinator;',context);
  return new context.Coordinator(options);
}
test('hidden tabs stop polls, restoration coalesces events, no overlap, logout cleans timers', async () => {
  const time=clock(), blocked=gate(), entered=gate(); let visible=true, enabled=true;
  const calls=[];
  const scheduler=coordinator({...time, visible:()=>visible,enabled:()=>enabled,interval:()=>30000,
    run:async scopes=>{calls.push([...scopes]);if(calls.length===1){entered.resolve();await blocked.promise;}}});
  scheduler.start(); await time.advance(30000); await entered.promise;
  scheduler.request('counts');scheduler.request('counts','queue');
  await time.advance(120000);assert.equal(calls.length,1);
  visible=false;scheduler.visibilityChanged();blocked.resolve();await microtasks();
  await time.advance(900000);assert.equal(calls.length,1);assert.equal(time.timers.size,0);
  visible=true;scheduler.visibilityChanged();scheduler.request('queue','conversation');
  await time.advance(100);assert.equal(calls.length,2);
  assert.deepEqual(new Set(calls[1]),new Set(['counts','queue','conversation']));
  enabled=false;scheduler.stop();await time.advance(900000);assert.equal(calls.length,2);assert.equal(time.timers.size,0);
});
test('account change while refresh is running does not lose the new timer',async()=>{
  const time=clock(),release=gate();let calls=0;
  const scheduler=coordinator({...time,visible:()=>true,enabled:()=>true,interval:()=>30000,
    run:async()=>{calls++;if(calls===1)await release.promise;}});
  scheduler.start();await time.advance(30000);scheduler.stop();scheduler.start();
  await time.advance(30000);assert.equal(calls,1);release.resolve();await microtasks();
  await time.advance(30000);assert.equal(calls,2);scheduler.stop();
});
function apiHarness() {
  const pending=[],counts=[];
  const context=vm.createContext({FormData,DOMException,Date,Map,JSON,
    state:{authenticated:true,agent:{id:7,role:'agent'}}, document:{hidden:false},
    dashboardRefresh:{request:()=>{}}, csrf:()=> 'test', SUPPORT_UI_VERSION:'test',
    updateQueueCount:(view,total)=>counts.push([view,total]),
    fetch:(path)=>new Promise(resolve=>pending.push({path,resolve})),
    showLogin:()=>{}, noticeDashboardUpdate:()=>{}});
  vm.runInContext(apiSource.slice(0,apiSource.indexOf('// After a deploy')) + '\n' +
    rendering.slice(rendering.indexOf('async function refreshAllQueueCounts'),rendering.indexOf('function updateQueueSummary')),context);
  const finish=(index,data={})=>pending[index].resolve({ok:true,status:200,json:async()=>data});
  return {context,pending,counts,finish};
}
test('GET singleflight scopes parameters and account; stale session responses rejected', async()=>{
  const {context:c,pending,finish}=apiHarness();
  const a=c.api('/api/support/tickets?view=mine'),b=c.api('/api/support/tickets?view=mine');
  assert.equal(pending.length,1);finish(0,{total:2});assert.deepEqual(await a,await b);
  const old=c.api('/api/support/tickets/42'); const rejected=assert.rejects(old,{name:'AbortError'});
  c.resetDashboardRequests();c.state.agent.id=8;
  const next=c.api('/api/support/tickets/42');assert.equal(pending.length,3);
  finish(1,{secret:'old session'});finish(2,{id:42});await rejected;assert.equal((await next).id,42);
  const filtered=c.api('/api/support/tickets?view=mine&support_number=90002');finish(3);await filtered;
});
test('counts share one flight, cache briefly, invalidate on mutations, never cross roles',async()=>{
  const {context:c,pending,finish,counts}=apiHarness();
  const a=c.refreshAllQueueCounts(),b=c.refreshAllQueueCounts();assert.equal(pending.length,1);
  finish(0,{counts:{mine:3}});await Promise.all([a,b]);assert.deepEqual(counts,[['mine',3]]);
  await c.refreshAllQueueCounts();assert.equal(pending.length,1);
  const mutation=c.api('/api/support/tickets/42/resolve',{method:'POST'});finish(1);await mutation;
  const fresh=c.refreshAllQueueCounts();finish(2,{counts:{mine:2}});await fresh;
  c.state.agent.role='supervisor';const role=c.refreshAllQueueCounts();finish(3,{counts:{active:9}});await role;
  c.document.hidden=true;await c.refreshAllQueueCounts();assert.equal(pending.length,4);
});
test('15 minute deterministic workload records reduced visible/hidden request counts',async()=>{
  const time=clock();let visible=true;const counts={queue:0,conversation:0,sidebar:0};
  const scheduler=coordinator({...time,visible:()=>visible,enabled:()=>true,interval:()=>30000,
    run:async()=>{counts.queue++;counts.conversation++;counts.sidebar++;}});
  scheduler.start();await time.advance(900000);
  assert.deepEqual(counts,{queue:30,conversation:30,sidebar:30});
  visible=false;scheduler.visibilityChanged();await time.advance(900000);
  assert.deepEqual(counts,{queue:30,conversation:30,sidebar:30});
  visible=true;scheduler.visibilityChanged();await time.advance(100);
  assert.deepEqual(counts,{queue:31,conversation:31,sidebar:31});scheduler.stop();
  // Baseline idle active conversation: 3s conversation + 2s unassigned + 5s counts.
  console.log(JSON.stringify({benchmark:'dashboard_15min_no_events',before:{conversation:300,queue:450,sidebar:180,total:930},
    after_visible:{conversation:30,queue:30,sidebar:30,total:90},after_hidden:{total:0},visible_return:{total:3}}));
});

test('continuous events cannot postpone the bounded refresh batch indefinitely',async()=>{
  const time=clock();let calls=0;
  const scheduler=coordinator({...time,visible:()=>true,enabled:()=>true,interval:()=>30000,run:async()=>{calls++}});
  scheduler.start();
  for(let i=0;i<8;i++){scheduler.request('queue','counts');await time.advance(50);}
  assert.equal(calls,4);scheduler.stop();
});
