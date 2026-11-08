"""Actual shipped markup/assets in Chromium; no API, credentials or network."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.bridge_management_service import describe_line, iso
from app.support_ui import _ASSETS, _SCRIPT_NAMES, _TEMPLATE, _configure


@pytest.mark.parametrize('role', ['agent', 'admin', 'supervisor'])
@pytest.mark.parametrize('width', [1440, 390])
def test_management_roles_layout_and_read_only_interactions(tmp_path, role, width):
    browser = shutil.which(os.environ.get('SUPPORT_TEST_BROWSER', 'chromium'))
    if not browser:
        pytest.skip('Select Chromium with SUPPORT_TEST_BROWSER')
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
  const calls = [], role = ROLE, snapshot = SNAPSHOT;
  let unavailable = false;
  api = async (path, options = {}) => {
    calls.push([options.method || 'GET', path]);
    check(!options.method || options.method === 'GET', 'Mutation request');
    if (unavailable) throw new Error('Mock outage');
    if (path.endsWith('/management')) return structuredClone(snapshot);
    if (path.endsWith('/history')) return {events: []};
    if (path.endsWith('/auth-recovery')) return {status: 'BLOCKED', recovery_allowed: false,
      notice: 'Observation only. No fence was changed.', blockers: ['OWNER_NOT_RETIRED']};
    throw new Error('Unexpected path');
  };
  state.agent = {id:'7', name:'Mock operator', role, can_view_whatsapp_management:role !== 'agent',
    can_manage_whatsapp:role === 'supervisor', can_link_whatsapp:role === 'supervisor',
    can_assess_whatsapp_authentication:role === 'supervisor'};
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
    check($('bridgeLegacyWorkflowsBtn').classList.contains('hidden') === (role !== 'supervisor'), 'Legacy permission');
    check($('bridgeAssessmentBtn').classList.contains('hidden') === (role !== 'supervisor'), 'Assessment permission');
    selectBridgeIdentity('90003'); await Promise.resolve();
    const row = $('bridgeIdentities').children[2], facts = $('bridgeFacts').firstElementChild;
    row.focus(); await loadBridgeManagement();
    check(document.activeElement === row && $('bridgeFacts').firstElementChild === facts, 'Focus/DOM continuity');
    check($('bridgeExplanation').textContent.includes('authentication review'), 'Fenced explanation');
    check([...$('bridgeFutureActions').querySelectorAll('button')].every(button => button.disabled), 'Recovery disabled');
    if (role === 'supervisor') {await loadBridgeAssessment(); check($('bridgeAssessment').textContent.includes('OWNER_NOT_RETIRED'), 'Sanitized assessment');}
    unavailable = true; await loadBridgeManagement();
    check($('bridgeTelemetryWarning').textContent.includes('stale'), 'Telemetry outage');
    unavailable = false; await loadBridgeManagement();
    const panel = $('bridgeManagementModal').firstElementChild.getBoundingClientRect();
    check(panel.left >= 0 && panel.right <= innerWidth + 1, 'Modal horizontal overflow');
    check(calls.every(([method]) => method === 'GET'), 'Read-only requests');
  }
  check(testErrors.length === 0, testErrors.join('; '));
  document.body.dataset.bridgeTest = 'passed';
})().catch(error => {document.body.dataset.bridgeTest = 'FAILED: ' + error.stack;});
""".replace('ROLE', json.dumps(role)).replace('SNAPSHOT', json.dumps(payload))
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
        command.append('--screenshot=' + str(folder / f'bridge-management-{width}.png'))
    result = subprocess.run([*command, page.as_uri()], capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr[-1000:]
    start = result.stdout.find('<body')
    assert 'data-bridge-test="passed"' in result.stdout, result.stdout[start:start+1000] + result.stderr[-500:]
