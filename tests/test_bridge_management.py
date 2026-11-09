"""Focused read-only/RBAC regressions using isolated data and no real bridge."""
import asyncio
import json
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI

from app import bridge_management_api as api_module
from app import bridge_management_service as service_module
from app.linked_device_api import router as legacy_router
from app.support_auth import SupportAgent, get_current_agent, require_support_csrf


class Repository:
    def __init__(self):
        self.calls = []

    async def numbers(self):
        self.calls.append('numbers')
        return [{'phone_number_id': '90001', 'display_name': 'Office', 'is_enabled': 0}]

    async def retired_label(self, line):
        return {'display_name': 'Retired office', 'display_phone_number': '96170000000'}

    async def history(self, line):
        self.calls.append('history')
        return [{'id': 1, 'action': 'LINKED_DEVICE_LINE_REMOVED', 'actor_staff_id': 9,
                 'created_at': datetime(2026, 11, 8), 'details_json': 'PRIVATE'}]


class Client:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def get_management_status(self):
        self.calls.append('status')
        if self.fail:
            raise RuntimeError('PRIVATE')
        return {'lines': [
            {'line_id': '90001', 'status': 'error', 'lifecycle': 'fenced_unresolved',
             'recovery_category': 'storage_failure', 'is_paired': True,
             'qr': 'PRIVATE', 'qr_data_url': 'PRIVATE', 'error': 'PRIVATE',
             'pending_callbacks': 3, 'uncertain_sends': 2, 'retry_scheduled': False,
             'diagnostics': {'transport': {'remote_ip': '2001:db8::1', 'remote_port': 443, 'headers': 'PRIVATE'},
                             'last_category': 'temporary_transport', 'heartbeat_success_at': 1000},
             'isolation': {'safe': False, 'blockers': ['WORK_NOT_SETTLED', 'PRIVATE']}},
            {'line_id': '90002', 'status': 'decommissioned', 'lifecycle': 'decommissioned',
             'local_removal': {'unlinked': False}},
            {'line_id': '99999', 'status': 'connected', 'display_phone_number': 'PRIVATE'},
        ], 'storage': {'level': 'critical', 'issues': [
            {'line_id': '90001', 'code': 'STORE_UNAVAILABLE', 'kind': 'auth', 'level': 'critical', 'path': 'PRIVATE'}]}}

    async def get_auth_recovery(self, line):
        self.calls.append('assessment')
        return {'status': 'BLOCKED', 'blockers': ['OFFLINE_JOURNAL_VALIDATION_REQUIRED'],
                'recovery_allowed': True, 'creds': 'PRIVATE', 'operations': [{'source': 'get',
                'provider_outcome': 'rejected', 'wrapper_outcome': 'rejected', 'credentials': 'PRIVATE'}]}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(service_module, 'configured_linked_device_lines', lambda: {'90001': ''})
    monkeypatch.setattr(service_module, 'quarantined_linked_device_line_ids', lambda: {'90002'})
    service = service_module.BridgeManagementService(Repository(), Client())
    monkeypatch.setattr(api_module, 'bridge_management_service', service)
    return service


async def request(path, role='admin', method='GET', payload=None):
    app = FastAPI()
    app.include_router(api_module.router)
    app.include_router(legacy_router)

    async def agent():
        return SupportAgent(id='1', name='Operator', role=role)

    app.dependency_overrides[get_current_agent] = agent
    app.dependency_overrides[require_support_csrf] = agent
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        return await client.request(method, '/api/support/whatsapp-linked-device' + path, json=payload)


def test_inventory_is_sanitized_cached_and_includes_quarantine(service):
    async def run():
        first, second = await asyncio.gather(service.management(), service.management())
        assert first == second
        assert service.client.calls == ['status'] and service.repository.calls == ['numbers']
        assert [line['line_id'] for line in first['lines']] == ['90001', '90002']
        assert 'PRIVATE' not in json.dumps(first)
        line, retired = first['lines']
        assert line['is_enabled'] is False and line['transport']['family'] == 'IPv6'
        assert line['authentication']['state'] == 'review_required'
        assert line['delivery']['uncertain_sends'] == 2
        assert 'WORK_NOT_SETTLED' in line['blockers']
        assert all(action['enabled'] is False for action in line['actions'])
        assert retired['authentication']['state'] == 'abandoned'
        assert retired['authentication']['remote_unlinked'] is False
        first['lines'].clear()
        assert len((await service.management())['lines']) == 2
    asyncio.run(run())


def test_bridge_failure_retains_inventory_without_claiming_fresh_health(service):
    async def run():
        await service.management()
        service.at = -100
        service.client.fail = True
        payload = await service.management()
        assert 'BRIDGE_UNAVAILABLE' in payload['issues'] and not payload['bridge_available']
        line = payload['lines'][0]
        assert line['connection']['state'] == 'unknown'
        assert line['observed_at'] is None and line['last_observed_at']
        assert line['delivery']['uncertain_sends'] is None
        assert line['storage']['level'] == 'unknown'
        assert len(payload['lines']) == 2
    asyncio.run(run())


@pytest.mark.parametrize('role', ['agent', 'driver'])
@pytest.mark.parametrize('path', ['/management', '/lines/90001/diagnostics', '/lines/90001/history', '/lines/90001/auth-recovery'])
def test_staff_cannot_read_management(service, role, path):
    response = asyncio.run(request(path, role))
    assert response.status_code == 403
    assert service.client.calls == service.repository.calls == []


@pytest.mark.parametrize('role', ['admin', 'supervisor'])
def test_admin_and_super_admin_have_identical_read_and_assessment_access(service, role):
    async def run():
        assert (await request('/management', role)).status_code == 200
        assert (await request('/lines/90001/diagnostics', role)).status_code == 200
        history = await request('/lines/90002/history', role)
        assert history.status_code == 200 and 'PRIVATE' not in history.text
        assert history.headers['cache-control'] == 'no-store'
        response = await request('/lines/90001/auth-recovery', role)
        assert response.status_code == 200 and response.json()['recovery_allowed'] is False
        assert 'PRIVATE' not in response.text
        assert (await request('/lines/99999/diagnostics', role)).status_code == 404
        assert (await request('/lines/99999/auth-recovery', role)).status_code == 404
        assert service.client.calls.count('assessment') == 1
    asyncio.run(run())


@pytest.mark.parametrize('role', ['agent', 'driver'])
@pytest.mark.parametrize('method,path,payload', [
    ('POST', '/lines', {'request_id': '00000000-0000-4000-8000-000000000001'}),
    ('POST', '/lines/90001/connect', None), ('POST', '/lines/90001/disconnect', None),
    ('DELETE', '/lines/90001', {'close_active_tickets': True}),
    ('DELETE', '/lines/90001', {'close_active_tickets': True, 'mode': 'decommission'}),
])
def test_legacy_mutation_routes_deny_staff_before_bridge_access(service, role, method, path, payload):
    assert asyncio.run(request(path, role, method, payload)).status_code == 403
    assert service.client.calls == service.repository.calls == []


def test_quarantined_assessment_never_contacts_auth_owner(service):
    async def run():
        assessment = await service.assessment('90002')
        assert assessment['blockers'] == ['IDENTITY_DECOMMISSIONED']
        assert 'assessment' not in service.client.calls
    asyncio.run(run())


def test_registry_mutations_deny_staff_before_persistence():
    from app.whatsapp_registry import WhatsAppRegistryForbiddenError, WhatsAppRegistryService
    service = WhatsAppRegistryService(repository=object())
    async def run():
        for role in ('agent', 'driver'):
            agent = SupportAgent(id='1', name='Operator', role=role)
            for operation in (lambda: service.set_number_enabled(agent, '90001', True),
                              lambda: service.set_number_enabled(agent, '90001', False),
                              lambda: service.set_number_display_name(agent, '90001', 'Office'),
                              lambda: service.remove_saved_number(agent, '90001'),
                              lambda: service.sync_waba(agent, '90001')):
                with pytest.raises(WhatsAppRegistryForbiddenError):
                    await operation()
    asyncio.run(run())
