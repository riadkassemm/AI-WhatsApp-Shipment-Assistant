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
