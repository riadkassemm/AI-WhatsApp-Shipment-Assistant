"""Actual shipped markup/assets in Chromium; no API, credentials or network."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.bridge_management_service import describe_line, iso
from app.support_auth import SupportAgent
from app.support_ui import _ASSETS, _SCRIPT_NAMES, _TEMPLATE, _configure


@pytest.mark.parametrize('role', ['agent', 'admin', 'supervisor'])
@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('recovery', [False, True])
def test_management_roles_layout_and_read_only_interactions(tmp_path, role, width, recovery):
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    if not browser:
        pytest.skip('Select Chromium with SUPPORT_TEST_BROWSER')
    agent = SupportAgent(id='7', name='Mock operator', role=role)
    capabilities = {'can_view_whatsapp_management': agent.can_view_whatsapp_management,
                    'can_manage_whatsapp': agent.can_manage_whatsapp_connections,
                    'can_link_whatsapp': agent.can_manage_whatsapp_connections,
                    'can_assess_whatsapp_authentication': agent.can_assess_whatsapp_authentication,
                    'can_recover_whatsapp': agent.can_recover_whatsapp}
    now = time.time()
    lines = []
    for index, state in enumerate(('connected', 'disconnected', 'error', 'decommissioned'), 1):
        raw = {'status': state, 'is_paired': True, 'lifecycle': 'fenced_unresolved' if index == 3 else 'active',
               'retry_scheduled': index == 2, 'recovery_category': 'temporary_transport' if index == 2 else None,
               'pending_callbacks': index, 'uncertain_sends': 2 if index == 3 else 0,
               'diagnostics': {'supported': True, 'configured_ip_family': 4,
                               'transport': {'remote_ip': '203.0.113.1' if index == 1 else '2001:db8::1',
                                             'remote_port': 443, 'tls_authorized': True},
                               'heartbeat_success_at': now * 1000, 'heartbeat_observed_at': now * 1000}}
        lines.append(describe_line(str(90000 + index), {'display_name': ['Beirut Office', 'Tyre Office',
            'Saida Office', 'Retired Office'][index - 1], 'is_enabled': index < 3,
            'display_phone_number': '+9617000000' + str(index)}, raw,
            {'level': 'ok', 'issues': [], 'checked_at': now * 1000}, index == 4, iso(now), now))
    payload = {'lines': lines, 'checked_at': iso(now), 'issues': [], 'recent_incidents': [],
               'incident_notice': 'Observed in this API worker only; gaps may omit incidents.',
               'incident_window_started_at': iso(now)}
    if recovery:
        lines[0]['administration'] = {'epoch':'00000000-0000-4000-8000-000000000001',
            'generation':1,'version':0,'paused':False,'active_operation_id':None}
        lines[0]['actions'] = [{'action':'pause','label':'Pause','enabled':True,'blockers':[],
                               'reason':'Ready for an authorized request.'}]
    before = """window.testErrors=[]; addEventListener('error', event => testErrors.push(event.message));
window.fetch = async () => {throw new Error('Unexpected network request');};
"""
    scripts = []
    for name in _SCRIPT_NAMES:
        source = _ASSETS[name]
        if name == 'actions.js':
            assert source.count('\nbootstrap();\n') == 1
            source = source.replace('\nbootstrap();\n', '\n')
        scripts.append('<script>' + _configure(source) + '</script>')
    assertions = r"""
(async () => {
  const check = (value, message) => {if (!value) throw new Error(message);};
  const calls = [], role = ROLE, snapshot = SNAPSHOT, recovery = RECOVERY;
  let operation = null;
  let unavailable = false;
  api = async (path, options = {}) => {
    calls.push([options.method || 'GET', path]);
    if (options.method === 'POST') {
      check(recovery && role !== 'agent' && path.endsWith('/operations'), 'Unauthorized mutation');
      const body=JSON.parse(options.body);
      check(body.action === 'pause' && body.operation_id === options.headers['Idempotency-Key'] && body.confirm_line_id === '90001', 'Operation identity');
      operation={id:body.operation_id,line_id:'90001',action:'pause',operator_id:'7',state:'succeeded',
        reason_code:'ADMIN_PAUSED',message:'This number is paused by an administrator.',terminal:true,
        can_cancel:false,created_at:snapshot.checked_at,completed_at:snapshot.checked_at,audit:[]};
      snapshot.lines[0].administration.paused=true;
      snapshot.lines[0].administration.version=1;
      snapshot.lines[0].actions=[{action:'resume',label:'Resume',enabled:true,blockers:[],reason:'Ready for an authorized request.'}];
      return operation;
    }
    if (unavailable) throw new Error('Mock outage');
    if (path.endsWith('/management')) return structuredClone(snapshot);
    if (path.endsWith('/history')) return {events: []};
    if (path.endsWith('/operations')) return {operations: operation ? [operation] : []};
    if (path.includes('/operations/')) return operation;
    if (path.endsWith('/auth-recovery')) return {status: 'BLOCKED', recovery_allowed: false,
      notice: 'Observation only. No fence was changed.', blockers: ['OWNER_NOT_RETIRED']};
    throw new Error('Unexpected path');
  };
  // Capabilities come from the real backend policy (support_auth.SupportAgent).
  state.agent = {id:'7', name:'Mock operator', role, ...CAPABILITIES};
  const manager = role !== 'agent';
  check(['can_view_whatsapp_management','can_manage_whatsapp','can_link_whatsapp','can_assess_whatsapp_authentication',
    'can_recover_whatsapp'].every(flag => state.agent[flag] === manager), 'Admin and Super Admin share WhatsApp capabilities');
  setAgentIdentity(state.agent); configureStaffAccess();
  state.authenticated = true;
  $('sessionLoading').classList.add('hidden'); $('dashboard').classList.remove('hidden');
  check($('manageWhatsAppBtn').classList.contains('hidden') === (role === 'agent'), 'Management role visibility');
  openBridgeManagement();
  if (role === 'agent') {
    check(state.activeModal !== 'bridgeManagementModal' && calls.length === 0, 'Staff management access');
  } else {
    await loadBridgeManagement();
    check($('bridgeIdentities').children.length === 4, 'Complete inventory');
    check(dashboardRefresh.interval() === 5000, 'Five-second refresh');
    check(!$('bridgeLegacyWorkflowsBtn').classList.contains('hidden'), 'Legacy pairing/removal permission');
    check(!$('bridgeAssessmentBtn').classList.contains('hidden'), 'Assessment permission');
    if (recovery) {
      check(!$('bridgeFutureActions').querySelector('button').disabled, 'Pause enabled');
      const pending=requestBridgeRecovery('pause');
      await Promise.resolve();
      check(state.activeModal === 'confirmModal', 'Explicit confirmation');
      $('confirmAcceptBtn').click(); await pending; await Promise.resolve();
      check(operation?.state === 'succeeded', 'Saved operation result');
      check($('bridgeOperationNotice').textContent.includes('paused'), 'Progress/result message');
    }
    selectBridgeIdentity('90003'); await Promise.resolve();
    const row = $('bridgeIdentities').children[2], facts = $('bridgeFacts').firstElementChild;
    row.focus(); await loadBridgeManagement();
    check(document.activeElement === row && $('bridgeFacts').firstElementChild === facts, 'Focus/DOM continuity');
    check($('bridgeExplanation').textContent.includes('authentication review'), 'Fenced explanation');
    check([...$('bridgeFutureActions').querySelectorAll('button')].every(button => button.disabled), 'Recovery disabled');
    await loadBridgeAssessment(); check($('bridgeAssessment').textContent.includes('OWNER_NOT_RETIRED'), 'Sanitized assessment');
    unavailable = true; await loadBridgeManagement();
    check($('bridgeTelemetryWarning').textContent.includes('stale'), 'Telemetry outage');
    unavailable = false; await loadBridgeManagement();
    const panel = $('bridgeManagementModal').firstElementChild.getBoundingClientRect();
    check(panel.left >= 0 && panel.right <= innerWidth + 1, 'Modal horizontal overflow');
    check(calls.filter(([method]) => method === 'POST').length === (recovery ? 1 : 0), 'No repeated mutations');
    if (recovery) { selectBridgeIdentity('90001'); await loadBridgeOperations(); }
  }
  check(testErrors.length === 0, testErrors.join('; '));
  document.body.dataset.bridgeTest = 'passed';
})().catch(error => {document.body.dataset.bridgeTest = 'FAILED: ' + error.stack;});
""".replace('CAPABILITIES', json.dumps(capabilities)).replace('ROLE', json.dumps(role)).replace('SNAPSHOT', json.dumps(payload)).replace('RECOVERY', json.dumps(recovery))
    html = _configure(_TEMPLATE.replace('<script src="/support/image-editor.js"></script>', '')
        .replace('__SUPPORT_THEME__', '<script>' + before + '</script>')
        .replace('__SUPPORT_STYLE__', '<style>' + _ASSETS['support.css'] + '</style>')
        .replace('__SUPPORT_SCRIPTS__', '\n'.join(scripts) + '<script>' + assertions + '</script>'))
    page = tmp_path / 'management.html'
    page.write_text(html)
    command = [browser, '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
               '--disable-background-networking', '--host-resolver-rules=MAP * ~NOTFOUND',
               '--user-data-dir=' + str(tmp_path / 'profile'), f'--window-size={width},1000',
               '--virtual-time-budget=3000', '--dump-dom']
    artifact_dir = os.environ.get('SUPPORT_MANAGEMENT_SCREENSHOTS')
    if artifact_dir and role == 'supervisor':
        folder = Path(artifact_dir)
        folder.mkdir(parents=True, exist_ok=True)
        command.append('--screenshot=' + str(folder / f'bridge-management-{width}-recovery-{recovery}.png'))
    result = subprocess.run([*command, page.as_uri()], capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr[-1000:]
    start = result.stdout.find('<body')
    assert 'data-bridge-test="passed"' in result.stdout, result.stdout[start:start+1000] + result.stderr[-500:]
