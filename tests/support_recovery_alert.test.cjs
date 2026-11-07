// Shipped dashboard renderers with synthetic health data; no services or sessions.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/support/actions.js', 'utf8');
const helpers = source.slice(source.indexOf('const LINE_HEALTH_INTERVAL_MS'), source.indexOf('function linkedDeviceAdditionStorageKey('));
const modal = source.slice(source.indexOf('function renderLinkedDeviceStatus('), source.indexOf('async function loadLinkedDeviceStatus('));

function harness() {
  const element = () => ({dataset: {}, textContent: '', children: [], classList: {hidden: true,
    toggle(_, hidden) { this.hidden = hidden; }},
    append(...nodes) { this.children.push(...nodes); }, replaceChildren() { this.children = []; }});
  const elements = new Map(), notices = [];
  const $ = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const context = vm.createContext({$, text: (node, value) => { node.textContent = value; },
    state: {lineAlertsSeen: new Set(), currentTicketData: {ticket: {business_phone_number_id: '90011'}, permissions: {can_reply: true}}},
    document: {createElement: element}, canLinkWhatsApp: () => true, canManageWhatsApp: () => false,
    notifyNewCustomerActivity: value => notices.push(value), pendingLinkedDeviceAddition: () => false,
    createSupportNumberLabel: () => element()});
  vm.runInContext(helpers + '\n' + modal, context);
  return {context, $, notices};
}
const issue = (code, kind = 'auth', line_id = '90011', level = 'critical') =>
  ({code, kind, line_id, level, name: line_id === '90011' ? 'Beirut Office' : null});
const payload = issues => ({lines: [{line_id: '90011', name: 'Beirut Office', status: 'offline',
  connection_state: 'storage_recovery_required', alert: true}], storage: {level: 'critical', issues}});

test('auth fence explains session recovery in banner, notification and composer without blaming disk space', () => {
  const h = harness();
  h.context.renderLineHealth(payload([issue('STORE_UNAVAILABLE')]));
  assert.match(h.$('lineAlertText').textContent, /session recovery required for Beirut Office/);
  assert.match(h.$('composerHelp').textContent, /session recovery required.*New sends are paused/);
  for (const value of [h.$('lineAlertText').textContent, h.$('composerHelp').textContent, ...h.notices]) {
    assert.doesNotMatch(value, /disk space|capacity is low|relink/i);
  }
  assert.equal(h.$('lineAlertActionBtn').classList.hidden, true);
  const count = h.notices.length;
  h.context.renderLineHealth(payload([issue('STORE_UNAVAILABLE')]));
  assert.equal(h.notices.length, count, 'same incident must not alert repeatedly');
});

test('only capacity codes recommend disk checks, including inode exhaustion', () => {
  for (const code of ['DISK_SPACE_LOW', 'INODES_LOW']) {
    const h = harness();
    h.context.renderLineHealth(payload([issue(code, null, null)]));
    assert.match(h.$('lineAlertText').textContent, /storage capacity is low.*disk space and file capacity/);
    assert.match(h.$('composerHelp').textContent, /storage capacity is low.*New sends are paused/);
  }
});

test('mixed capacity and auth failures explain both rather than hide either', () => {
  const h = harness();
  h.context.renderLineHealth(payload([issue('DISK_SPACE_LOW', null, null), issue('STORE_UNAVAILABLE')]));
  assert.match(h.$('lineAlertText').textContent, /administrator attention/);
  assert.match(h.$('lineAlertText').textContent, /disk space/);
  assert.match(h.$('lineAlertText').textContent, /session recovery/);
});

test('store kind does not confuse callback state, maintenance, or unknown failures with auth or disk', () => {
  const h = harness();
  for (const kind of ['outbox', 'state', undefined]) {
    const notice = h.context.whatsAppRecoveryNotice({issues: [{code: 'STORE_UNAVAILABLE', kind}]});
    assert.match(notice.title, /saved-state recovery/);
    assert.doesNotMatch(notice.detail, /disk space|session recovery/);
  }
  const maintenance = h.context.whatsAppRecoveryNotice({issues: [issue('COMPACTION_FAILED')]});
  assert.match(maintenance.title, /automatic maintenance/);
  assert.doesNotMatch(maintenance.detail, /disk space/);
  assert.match(h.context.whatsAppRecoveryNotice({issues: []}).title, /recovery needs review/);
});

test('an affected line does not change a healthy line composer or report connected lines offline', () => {
  const h = harness();
  h.context.state.currentTicketData.ticket.business_phone_number_id = '90012';
  h.$('composerHelp').textContent = 'Healthy composer';
  h.context.renderLineHealth(payload([issue('STORE_UNAVAILABLE')]));
  assert.equal(h.$('composerHelp').textContent, 'Healthy composer');
  const online = payload([issue('STORE_UNAVAILABLE')]);
  online.lines[0].status = 'connected';
  h.notices.length = 0;
  h.context.state.lineAlertsSeen.clear();
  h.context.renderLineHealth(online);
  assert.doesNotMatch(h.$('lineAlertText').textContent, /also offline/);
  assert.ok(h.notices.every(value => !value.includes('offline')));
});

test('missing collectors describe unavailable monitoring without a storage diagnosis', () => {
  const h = harness();
  h.context.renderLineHealth({lines: [], storage: {level: 'critical', issues: [
    issue('BRIDGE_UNAVAILABLE', null, null), issue('MONITOR_UNAVAILABLE', null, null)]}});
  assert.match(h.$('lineAlertText').textContent, /connection status is unavailable/);
  assert.doesNotMatch(h.notices[0], /disk space|session recovery required/);
});

test('connection modal replaces misleading raw error and does not offer pairing for a fenced session', () => {
  const h = harness();
  h.context.renderLinkedDeviceStatus({configured: true, lines: [{line_id: '90011', status: 'error', is_paired: false,
    recovery_category: 'storage_failure', storage_issues: [issue('STORE_UNAVAILABLE')],
    error: 'Could not safely load or save the WhatsApp session. Check bridge storage before reconnecting.'}]});
  const row = h.$('linkedDeviceLines').children[0];
  assert.equal(row.children.length, 2, 'no pairing button while recovery is unresolved');
  assert.match(row.children[1].textContent, /session recovery required.*Saved sessions are retained/);
  assert.doesNotMatch(row.children[1].textContent, /Check bridge storage|disk space/);
});

test('genuine logout retains the existing pairing action and normal connections keep their label', () => {
  const h = harness();
  h.context.renderLinkedDeviceStatus({configured: true, lines: [
    {line_id: '90011', status: 'logged_out', logged_out_at: 1000, recovery_category: 'logged_out'},
    {line_id: '90012', status: 'connected', is_paired: true}]});
  const [loggedOut, connected] = h.$('linkedDeviceLines').children;
  assert.match(loggedOut.children[1].textContent, /Logged out by WhatsApp/);
  assert.equal(loggedOut.children[2].textContent, 'Re-pair (keeps all history)');
  assert.equal(connected.children[1].textContent, 'Connected');
});
