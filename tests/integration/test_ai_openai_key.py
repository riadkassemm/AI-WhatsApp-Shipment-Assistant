"""Dashboard-managed OpenAI key on real MariaDB. No request ever reaches OpenAI."""
import json

import pytest
from cryptography.fernet import Fernet

from app import ai_provider
from app.ai_provider import AIProviderCredentialRepository, AIProviderService
from app.support_auth import SupportAgent

ADMIN = SupportAgent(id='8', name='Manager', role='supervisor')
KEY_A = 'sk-proj-' + 'A' * 40 + 'wxyz'
KEY_B = 'sk-proj-' + 'B' * 40 + '9876'


class FakeClient:
    def __init__(self, api_key):
        self.api_key = api_key

        class Models:
            async def retrieve(self, model):
                return {'id': model}

        self.models = Models()


async def service_for(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    monkeypatch.setattr(ai_provider.settings, 'ai_secrets_encryption_key', Fernet.generate_key().decode())
    monkeypatch.setattr(ai_provider.settings, 'openai_api_key', '')
    storage = AIProviderCredentialRepository()
    storage._pool = repo._pool
    service = AIProviderService(storage)
    monkeypatch.setattr(service, '_build_client', lambda key, **_kw: FakeClient(key))
    return service, sql


@pytest.mark.asyncio
async def test_key_is_encrypted_audited_by_fingerprint_and_rotates(infrastructure, monkeypatch):
    service, sql = await service_for(infrastructure, monkeypatch)
    assert await service.credential_state() == (False, 'API_KEY_MISSING')

    await service.save_key(ADMIN, KEY_A)
    row = (await sql("SELECT * FROM ai_provider_credentials WHERE provider='openai'"))[0]
    assert KEY_A not in row['api_key_encrypted']
    assert (row['key_last4'], row['status'], row['version']) == ('wxyz', 'VALID', 1)
    assert row['updated_by_staff_id'] == 8 and row['last_tested_at'] is not None
    assert (await service.get_client()).api_key == KEY_A

    await service.save_key(ADMIN, KEY_B, test_first=False)
    assert (await service.get_client()).api_key == KEY_B
    row = (await sql("SELECT version, status, last_tested_at FROM ai_provider_credentials"))[0]
    assert (row['version'], row['status'], row['last_tested_at']) == (2, 'UNTESTED', None)

    audit = await sql("SELECT action, old_configuration_json, new_configuration_json FROM ai_control_settings_audit "
                      "WHERE action LIKE %s ORDER BY id", ('OPENAI_KEY_%',))
    assert [entry['action'] for entry in audit] == ['OPENAI_KEY_SAVED', 'OPENAI_KEY_SAVED']
    assert json.loads(audit[1]['new_configuration_json'])['key_last4'] == '9876'
    for entry in audit:
        assert KEY_A not in entry['old_configuration_json'] + entry['new_configuration_json']
        assert KEY_B not in entry['old_configuration_json'] + entry['new_configuration_json']


@pytest.mark.asyncio
async def test_status_updates_only_apply_to_the_current_key_and_removal_clears_it(infrastructure, monkeypatch):
    service, sql = await service_for(infrastructure, monkeypatch)
    await service.save_key(ADMIN, KEY_A)
    await service.repository.set_status(version=99, status='INVALID', error_code='invalid_key')
    assert (await sql("SELECT status FROM ai_provider_credentials"))[0]['status'] == 'VALID'
    await service.repository.set_status(version=1, status='INVALID', error_code='invalid_key')
    assert await service.credential_state() == (False, 'API_KEY_INVALID')

    await service.delete_key(ADMIN)
    row = (await sql("SELECT api_key_encrypted, key_last4, key_fingerprint, status, version FROM ai_provider_credentials"))[0]
    assert row == {'api_key_encrypted': None, 'key_last4': None, 'key_fingerprint': None,
                   'status': 'NOT_CONFIGURED', 'version': 2}
    assert await service.credential_state() == (False, 'API_KEY_MISSING')
