"""Release gates: real HTTP authorization, SELECT-only reads, synthetic state."""
import asyncio
import copy
import hashlib
import json
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI

from app import bridge_management_api, support_api, support_auth
from app.bridge_management_repository import BridgeManagementRepository
from app.bridge_management_service import BridgeManagementService, describe_line
from app.config import configured_linked_device_lines, settings
from app.linked_device_api import router as legacy_router
from app.linked_device_client import LinkedDeviceWhatsAppClient
from app.whatsapp_registry import whatsapp_registry_repository, whatsapp_registry_service

PREFIX = '/api/support/whatsapp-linked-device'
MUTATIONS = [
    ('POST', PREFIX + '/lines', {'request_id': '00000000-0000-4000-8000-000000000001'}),
    ('POST', PREFIX + '/lines/90001/connect', None),
    ('POST', PREFIX + '/lines/90001/disconnect', None),
    ('DELETE', PREFIX + '/lines/90001', {'close_active_tickets': True}),
    ('DELETE', PREFIX + '/lines/90001', {'close_active_tickets': True, 'mode': 'decommission'}),
    ('PUT', '/api/support/whatsapp-numbers/90001', {'enabled': True}),
    ('PUT', '/api/support/whatsapp-numbers/90001/name', {'display_name': 'Office'}),
    ('DELETE', '/api/support/whatsapp-numbers/90001', None),
    ('POST', '/api/support/whatsapp-accounts/sync', {'waba_id': '1234567890'}),
]
READS = ['/management', '/lines/90001/diagnostics', '/lines/90001/history', '/lines/90001/auth-recovery']


@pytest.fixture
def release(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'whatsapp_linked_device_data_directory', str(tmp_path))
    monkeypatch.setattr(settings, 'whatsapp_linked_device_lines', {})
    monkeypatch.setattr(settings, 'whatsapp_linked_device_secret', 'synthetic-secret-' * 4)
    monkeypatch.setattr(settings, 'whatsapp_phone_number_id', '')
    manifest = {'version': 1, 'lines': {'90001': '', '90002': ''}, 'quarantined_lines': {
        '90003': {'line_id': '90003', 'state': 'permanently_decommissioned',
                  'reuse_allowed': False, 'remote_unlinked': False}}}
    (tmp_path / 'lines.json').write_text(json.dumps(manifest))
    for name in ('auth.json', 'auth.json.journal', '.auth-failure', '.auth-active',
                 'outbox.json', 'state.json', 'customer-media'):
        (tmp_path / name).write_bytes(b'SYNTHETIC_PRIVATE_EVIDENCE')
    database = [{'phone_number_id': '90001', 'display_name': 'Office',
                 'display_phone_number': '+96170000000', 'is_enabled': True},
                {'phone_number_id': '90002', 'display_name': 'Disabled', 'is_enabled': False}]
    sql, bridge_calls, session_touches, attempted_writes = [], [], [], []

    async def forbidden(*args, **kwargs):
        attempted_writes.append((args, kwargs))
        raise AssertionError('Management attempted a mutation or registry seeding')

    async def fetch_all(query, params=()):
        sql.append(query)
        assert query.lstrip().startswith('SELECT ')
        assert '*' not in query and 'details_json' not in query
        if 'whatsapp_phone_numbers' in query:
            return copy.deepcopy(database)
        assert 'whatsapp_integration_audit' in query
        return [{'id': 1, 'action': 'LINKED_DEVICE_LINE_CREATED', 'actor_staff_id': 7,
                 'created_at': datetime(2026, 11, 8)}]

    async def fetch_one(query, params=()):
        sql.append(query)
        assert query.lstrip().startswith('SELECT ') and 'support_tickets' in query
        assert 'body' not in query and '*' not in query
        return {'display_name': 'Retired office', 'display_phone_number': '+96170000003'}

    async def bridge_request(self, method, path, **kwargs):
        bridge_calls.append((method, path))
        assert method == 'GET'
        if path == '/v1/status':
            return {'lines': [
                {'line_id': '90001', 'status': 'connected', 'is_paired': True, 'lifecycle': 'active',
                 'qr_data_url': 'SYNTHETIC_QR_SECRET', 'error': 'SYNTHETIC_PRIVATE_EVIDENCE',
                 'pending_callbacks': 2, 'uncertain_sends': 3},
                {'line_id': '90002', 'status': 'error', 'lifecycle': 'fenced_unresolved'},
                {'line_id': '90003', 'status': 'decommissioned', 'local_removal': {'unlinked': False}}],
                'storage': {'level': 'ok', 'issues': []}}
        assert path == '/v1/lines/90001/auth-recovery'
        return {'status': 'BLOCKED', 'blockers': ['FINAL_EVIDENCE_MISSING'], 'recovery_allowed': False,
                'credentials': 'SYNTHETIC_PRIVATE_EVIDENCE'}

    async def session(token_hash):
        for index, role in enumerate(('agent', 'admin', 'supervisor'), 1):
            if token_hash == hashlib.sha256(role.encode()).hexdigest():
                return {'session_id': index, 'staff_id': index, 'role': role, 'name': 'Mock operator',
                        'csrf_hash': hashlib.sha256(b'synthetic-csrf').hexdigest()}
        return None

    async def touch(session_id):
        session_touches.append(session_id)

    monkeypatch.setattr(whatsapp_registry_repository, 'fetch_all', fetch_all)
    monkeypatch.setattr(whatsapp_registry_repository, 'fetch_one', fetch_one)
    monkeypatch.setattr(whatsapp_registry_repository, 'execute', forbidden)
    monkeypatch.setattr(whatsapp_registry_service, 'list_payload', forbidden)
    monkeypatch.setattr(LinkedDeviceWhatsAppClient, '_json_request', bridge_request)
    monkeypatch.setattr(support_auth.staff_repository, 'get_active_session', session)
    monkeypatch.setattr(support_auth.staff_repository, 'touch_session', touch)
    monkeypatch.setattr(support_auth, '_session_touched_at', {})
    service = BridgeManagementService(BridgeManagementRepository(), LinkedDeviceWhatsAppClient())
    monkeypatch.setattr(bridge_management_api, 'bridge_management_service', service)
    app = FastAPI()
    for router in (bridge_management_api.router, legacy_router, support_api.router):
        app.include_router(router)

    async def request(method, path, role=None, payload=None, csrf=True):
        # No dependency overrides: production cookie and CSRF checks run.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://mock',
                                     cookies={settings.support_cookie_name: role} if role else {}) as client:
            return await client.request(method, path, json=payload,
                headers={'X-CSRF-Token': 'synthetic-csrf'} if csrf else {})

    def files():
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in tmp_path.iterdir()}

    return request, service, database, sql, bridge_calls, attempted_writes, files


def test_cookie_rbac_and_csrf_cover_new_and_legacy_routes_without_side_effects(release):
    request, service, database, sql, calls, writes, files = release
    async def run():
        for suffix in READS:
            assert (await request('GET', PREFIX + suffix)).status_code == 401
            assert (await request('GET', PREFIX + suffix, 'agent')).status_code == 403
        for method, path, body in MUTATIONS:
            assert (await request(method, path, payload=body)).status_code == 401
            assert (await request(method, path, 'agent', body)).status_code == 403
            # Admin and Super Admin share WhatsApp management; both still need CSRF.
            for role in ('admin', 'supervisor'):
                assert (await request(method, path, role, body, csrf=False)).status_code == 403
        assert not sql and not calls and not writes
        for role in ('agent', 'admin', 'supervisor'):
            response = await request('GET', PREFIX + '/status', role)
            assert response.status_code == 200
            assert ('SYNTHETIC_QR_SECRET' in response.text) == (role in ('admin', 'supervisor'))
            assert 'SYNTHETIC_PRIVATE_EVIDENCE' not in response.text
    asyncio.run(run())


def test_refresh_detail_history_assessment_preserve_all_synthetic_state(release, caplog):
    request, service, database, sql, calls, writes, files = release
    before_files, before_database = files(), copy.deepcopy(database)
    async def run():
        for _ in range(2):
            service.at = -100
            for role in ('admin', 'supervisor'):
                for suffix in READS:
                    response = await request('GET', PREFIX + suffix, role)
                    assert response.status_code == 200
                    assert response.headers['cache-control'] == 'no-store'
                    assert 'SYNTHETIC_' not in response.text
            snapshot = await service.management()
            assert [row['line_id'] for row in snapshot['lines']] == ['90001', '90002', '90003']
            assert snapshot['lines'][1]['is_enabled'] is False
            assert snapshot['lines'][2]['authentication']['remote_unlinked'] is False
            assert '90003' not in configured_linked_device_lines()
            assert all(not action['enabled'] for row in snapshot['lines'] for action in row['actions'])
        assert calls.count(('GET', '/v1/status')) == 2
        assert calls.count(('GET', '/v1/lines/90001/auth-recovery')) == 1
    asyncio.run(run())
    assert files() == before_files and database == before_database
    assert not writes and 'SYNTHETIC_' not in caplog.text


@pytest.mark.parametrize('storage', [None, {'level': 'critical'}, {'level': 'warning', 'issues': []},
    {'level': 'critical', 'issues': [{'code': 'FUTURE_CODE', 'level': 'critical'}]},
    {'level': [], 'issues': []}, {'level': 'ok', 'issues': [None]}])
def test_incomplete_storage_telemetry_never_becomes_healthy(storage):
    line = describe_line('90001', {}, {'status': 'connected'}, storage, False, 'now', 1000)
    assert line['connection']['state'] == 'connected'
    assert line['storage']['level'] == 'unknown'


def test_other_identity_storage_issue_does_not_make_healthy_identity_unavailable():
    line = describe_line('90001', {}, {'status': 'connected'}, {'level': 'critical', 'issues': [
        {'code': 'STORE_UNAVAILABLE', 'level': 'critical', 'line_id': '90002'}]}, False, 'now', 1000)
    assert line['storage']['level'] == 'ok'
