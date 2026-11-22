"""Per-number AI switch on real MariaDB (migrations applied by the fixture)."""
import json

import pytest

from app.ai_availability import AIAvailabilityService, AIControlRepository
from app.support_auth import SupportAgent
from app.whatsapp_registry import WhatsAppRegistryRepository, WhatsAppRegistryService

ADMIN = SupportAgent(id='8', name='Manager', role='supervisor')


async def setup(infrastructure, monkeypatch):
    repo, sql, _ = infrastructure
    import app.whatsapp_registry as module
    monkeypatch.setattr(module, 'configured_linked_device_lines', lambda: {})
    monkeypatch.setattr(module, 'get_transport', lambda *_: 'cloud')
    monkeypatch.setattr(module.settings, 'whatsapp_phone_number_id', '')
    monkeypatch.setattr(module.whatsapp_outreach_guard, 'used_today', _no_usage)
    await sql("""INSERT INTO whatsapp_phone_numbers (phone_number_id, display_phone_number, source, is_enabled)
                 VALUES ('90052','96170000052','META_SYNC',1), ('90048','96170000048','META_SYNC',1),
                        ('90099','96170000099','META_SYNC',0)""")
    # The AI settings migration seeds the single row; switch global AI on.
    await sql("UPDATE ai_control_settings SET control_mode='MANUAL', manual_ai_enabled=1 WHERE id=1")
    storage = WhatsAppRegistryRepository()
    storage._pool = repo._pool
    control = AIControlRepository()
    control._pool = repo._pool
    async def key_ok():
        # Key and budget checks are covered by their own tests; never use the global
        # services here (they would connect to the configured database).
        return True, None

    return WhatsAppRegistryService(storage), AIAvailabilityService(control, credential_state=key_ok, budget_state=key_ok), sql


async def _no_usage(ids):
    return {}


@pytest.mark.asyncio
async def test_numbers_start_off_and_the_switch_gates_effective_ai(infrastructure, monkeypatch):
    registry, availability, sql = await setup(infrastructure, monkeypatch)
    rows = await sql("SELECT phone_number_id, ai_enabled FROM whatsapp_phone_numbers ORDER BY phone_number_id")
    assert {row['phone_number_id']: row['ai_enabled'] for row in rows} == {'90048': 0, '90052': 0, '90099': 0}

    off = await availability.get_effective_ai_availability(line_id='90052')
    assert (off.effective_ai_enabled, off.effective_reason) == (False, 'LINE_AI_DISABLED')

    payload = await registry.set_number_ai_enabled(ADMIN, '90052', True)
    assert {n['phone_number_id']: n['ai_enabled'] for n in payload['numbers']}['90052'] is True
    on = await availability.get_effective_ai_availability(line_id='90052')
    assert on.effective_ai_enabled is True
    assert (await availability.get_settings_payload())['ai_enabled_line_count'] == 1

    audit = await sql("SELECT action, actor_staff_id, details_json FROM whatsapp_integration_audit WHERE phone_number_id='90052'")
    assert audit[-1]['action'] == 'AI_ENABLED' and audit[-1]['actor_staff_id'] == 8
    assert json.loads(audit[-1]['details_json']) == {'before': False, 'after': True}
    stamp = (await sql("SELECT ai_updated_by_staff_id, ai_updated_at FROM whatsapp_phone_numbers WHERE phone_number_id='90052'"))[0]
    assert stamp['ai_updated_by_staff_id'] == 8 and stamp['ai_updated_at'] is not None


@pytest.mark.asyncio
async def test_enable_all_skips_inactive_numbers_and_disable_all_covers_every_number(infrastructure, monkeypatch):
    registry, availability, sql = await setup(infrastructure, monkeypatch)
    await registry.set_all_numbers_ai_enabled(ADMIN, True)
    rows = {r['phone_number_id']: r['ai_enabled'] for r in await sql("SELECT phone_number_id, ai_enabled FROM whatsapp_phone_numbers")}
    assert rows == {'90052': 1, '90048': 1, '90099': 0}

    await sql("UPDATE whatsapp_phone_numbers SET ai_enabled=1 WHERE phone_number_id='90099'")
    await registry.set_all_numbers_ai_enabled(ADMIN, False)
    rows = {r['phone_number_id']: r['ai_enabled'] for r in await sql("SELECT phone_number_id, ai_enabled FROM whatsapp_phone_numbers")}
    assert rows == {'90052': 0, '90048': 0, '90099': 0}
    actions = [r['action'] for r in await sql("SELECT action FROM whatsapp_integration_audit ORDER BY id")]
    assert actions.count('AI_BULK_ENABLED') == 2 and actions.count('AI_BULK_DISABLED') == 3
    assert (await availability.get_effective_ai_availability(line_id='90052')).effective_ai_enabled is False


@pytest.mark.asyncio
async def test_registry_schema_guard_accepts_the_migrated_schema(infrastructure, monkeypatch):
    registry, _availability, _sql = await setup(infrastructure, monkeypatch)
    await registry._ensure_schema()
