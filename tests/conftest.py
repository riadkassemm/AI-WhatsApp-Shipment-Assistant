"""Unit helpers. Integration tests use their own real service fixture."""
import pytest


@pytest.fixture
def outbound_unit(monkeypatch, tmp_path):
    from app.support_repository import support_repository
    from app.support_media import support_media_store

    async def claim(*args, **kwargs):
        return 'unit-claim'

    async def begin(*args, **kwargs):
        return True

    def install(repository):
        async def finish_for_repository(message_id, token, *, status, provider_id=None, error=None):
            await repository.update_message_send_state(message_id, send_status=status,
                                                       whatsapp_message_id=provider_id, error_message=error)
        monkeypatch.setattr(repository, 'claim_delivery', claim)
        monkeypatch.setattr(repository, 'begin_delivery', begin)
        monkeypatch.setattr(repository, 'finish_delivery', finish_for_repository)
    install(support_repository)
    monkeypatch.setattr(support_media_store, '_root', tmp_path)
    return install
