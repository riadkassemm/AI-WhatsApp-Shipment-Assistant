// Focused DOM identity and stale-session regression; no browser/provider needed.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function harness() {
  class Element {
    constructor() { this.children = []; this.dataset = {}; this.textContent = ''; this.attributes = {}; this.classList = {add() {}, remove() {}, toggle() {}}; }
    append(...nodes) { this.children.push(...nodes); nodes.forEach(node => {node.parent = this;}); }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener() {}
    remove() { this.parent.children = this.parent.children.filter(node => node !== this); }
    get lastElementChild() { return this.children.at(-1); }
    get childElementCount() { return this.children.length; }
  }
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const context = vm.createContext({console, AbortSignal, Date, Map, Set,
    $: get, document: {createElement: () => new Element()},
    state: {activeModal: 'bridgeManagementModal', agent: {can_assess_whatsapp_authentication: false}},
    canViewBridgeManagement: () => true, canManageWhatsApp: () => false,
    dashboardRefresh: {request() {}}, openDialog() {}, closeDialog() {},
    api: async () => { throw new Error('Unexpected request'); }});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/support/bridge-management.js'), 'utf8'), context);
  return {context, get, run: source => vm.runInContext(source, context)};
}

const line = id => ({line_id: id, display_name: '<Office>', phone: '96170000000', is_enabled: false,
  lifecycle: 'active', explanation: 'Temporarily unavailable.', blockers: [], connection: {state: 'disconnected'},
  authentication: {}, storage: {level: 'unknown', issues: []}, transport: {}, heartbeat: {}, delivery: {},
  actions: [{label: 'Safe reconnect', enabled: false, reason: 'Read only', blockers: []}]});
const snapshot = {checked_at: '2026-11-08T12:00:00Z', issues: [], lines: [line('90001'), line('90002')],
  recent_incidents: [], incident_notice: 'Observed here only.', incident_window_started_at: '2026-11-08T12:00:00Z'};

test('refresh preserves identity buttons, selected number and diagnostic nodes', () => {
  const h = harness(); h.context.payload = snapshot;
  h.run('bridgeManagement.snapshot = payload; renderBridgeManagement(payload); selectBridgeIdentity("90002", false); renderBridgeDetail();');
  const button = h.get('bridgeIdentities').children[1];
  const facts = h.get('bridgeFacts').children[0];
  h.run('renderBridgeManagement(payload)');
  assert.equal(h.get('bridgeIdentities').children[1], button);
  assert.equal(button.attributes['aria-pressed'], 'true');
  assert.equal(h.get('bridgeFacts').children[0], facts);
  assert.equal(h.get('bridgeIdentityTitle').textContent, '<Office> · 96170000000');
  assert.equal(h.get('bridgeFutureActions').children[0].children[0].disabled, true);
});

test('response arriving after session reset cannot repopulate diagnostics', async () => {
  const h = harness(); let resolve;
  h.context.api = () => new Promise(done => { resolve = done; });
  const pending = h.run('loadBridgeManagement()');
  h.run('resetBridgeManagement()');
  resolve(snapshot); await pending;
  assert.equal(h.get('bridgeIdentities').children.length, 0);
  assert.equal(h.run('bridgeManagement.snapshot'), null);
});

test('failed refresh explicitly labels retained observations stale', async () => {
  const h = harness(); h.context.payload = snapshot;
  h.run('bridgeManagement.snapshot = payload; renderBridgeManagement(payload)');
  const button = h.get('bridgeIdentities').children[0];
  await h.run('loadBridgeManagement()');
  assert.match(h.get('bridgeTelemetryWarning').textContent, /stale/);
  assert.equal(h.get('bridgeIdentities').children[0], button);
});

function recoveryHarness() {
  const h=harness(), saved=new Map();
  h.context.sessionStorage={getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value),removeItem:key=>saved.delete(key)};
  h.context.crypto={randomUUID:()=> '00000000-0000-4000-8000-000000000001'};
  h.context.showConfirmDialog=async()=>true;
  h.context.state.agent={id:'7',role:'admin',can_recover_whatsapp:true};
  h.context.payload=structuredClone(snapshot);
  const selected=h.context.payload.lines[0];
  selected.administration={epoch:'00000000-0000-4000-8000-000000000002',version:0,generation:3,paused:false};
  selected.actions=[{action:'pause',label:'Pause',enabled:true,reason:'Ready',blockers:[]}];
  h.run('bridgeManagement.snapshot=payload; bridgeManagement.freshAt=Date.now(); renderBridgeManagement(payload)');
  return h;
}

test('timed-out POST is tracked by its original ID across reload and never resubmitted',async()=>{
  const h=recoveryHarness(), calls=[];
  let complete=false;
  h.context.api=async(path,options={})=>{
    calls.push([options.method||'GET',path,options]);
    if(options.method==='POST') throw new Error('Lost response');
    if(path.includes('/operations/')) return {id:'00000000-0000-4000-8000-000000000001',line_id:'90001',
      terminal:complete,message:complete?'Paused':'Still processing'};
    if(path.endsWith('/operations')) return {operations:[]};
    throw new Error('Unexpected call');
  };
  await h.run('requestBridgeRecovery("pause")');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.filter(c=>c[0]==='POST').length,1);
  const request=JSON.parse(calls[0][2].body);
  assert.equal(request.operation_id,calls[0][2].headers['Idempotency-Key']);
  assert.equal(request.expected_generation,3);
  assert.ok(h.run('pendingBridgeOperation()'));
  await h.run('requestBridgeRecovery("pause")');
  assert.equal(calls.filter(c=>c[0]==='POST').length,1);
  h.run('resetBridgeManagement(); bridgeManagement.snapshot=payload; bridgeManagement.freshAt=Date.now(); renderBridgeManagement(payload)');
  complete=true; await h.run('loadBridgeOperations()');
  assert.equal(h.run('pendingBridgeOperation()'),null);
  assert.match(h.get('bridgeOperationNotice').textContent,/Paused/);
  assert.equal(calls.filter(c=>c[0]==='POST').length,1);
});

test('stale diagnostics and staff capability block recovery even when previous snapshot allowed it',async()=>{
  const h=recoveryHarness();
  h.run('bridgeManagement.freshAt=0; renderBridgeDetail()');
  assert.equal(h.get('bridgeFutureActions').children[0].children[0].disabled,true);
  assert.match(h.get('bridgeFutureActions').children[0].children[1].textContent,/stale/);
  await h.run('requestBridgeRecovery("pause")');
  h.run('bridgeManagement.freshAt=Date.now(); state.agent.can_recover_whatsapp=false; renderBridgeDetail()');
  assert.equal(h.get('bridgeFutureActions').children[0].children[0].disabled,true);
  await h.run('requestBridgeRecovery("pause")');
  assert.equal(h.run('pendingBridgeOperation()'),null);
});

test('an unsaved request is released only after a persistent 404 well past submission, without resubmitting',async()=>{
  const h=recoveryHarness(), calls=[];
  h.context.api=async(path,options={})=>{
    calls.push([options.method||'GET',path]);
    if(options.method==='POST') throw Object.assign(new Error('Server unavailable'),{status:503});
    if(path.includes('/operations/')) throw Object.assign(new Error('This operation was not found.'),{status:404});
    if(path.endsWith('/operations')) return {operations:[]};
    throw new Error('Unexpected call');
  };
  await h.run('requestBridgeRecovery("pause")');
  await new Promise(resolve=>setImmediate(resolve));
  const pending=h.run('pendingBridgeOperation()');
  assert.ok(pending && Number.isFinite(pending.at));
  // Within the grace period a 404 may only mean the save is still in flight.
  await h.run('loadBridgeOperations()');
  assert.ok(h.run('pendingBridgeOperation()'));
  assert.match(h.get('bridgeOperationNotice').textContent,/not yet confirmed/);
  h.run('sessionStorage.setItem(bridgeOperationKey(), JSON.stringify({...pendingBridgeOperation(), at: Date.now()-bridgeOperationSaveGraceMs-1}))');
  await h.run('loadBridgeOperations()');
  assert.equal(h.run('pendingBridgeOperation()'),null);
  assert.match(h.get('bridgeOperationNotice').textContent,/was not saved/);
  assert.equal(calls.filter(c=>c[0]==='POST').length,1);
});
