"""Real MariaDB/Redis: the inbound health probe used before counting failures."""
import pytest

import app.main as main
from app.conversation_store import RedisConversationStore


@pytest.mark.asyncio
async def test_dependency_probe_is_healthy_with_real_services(infrastructure, monkeypatch):
    repository, _sql, _redis = infrastructure
    from app.config import settings
    store = RedisConversationStore(settings.redis_url, 3600)
    try:
        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main, "support_repository", repository)
        monkeypatch.setattr(main, "_dependency_health_cache", None)
        await repository.ping()
        assert await main._inbound_dependencies_healthy() is True
    finally:
        await store._client.aclose()


@pytest.mark.asyncio
async def test_dependency_probe_reports_unhealthy_database(infrastructure, monkeypatch):
    _repository, _sql, _redis = infrastructure
    from app.config import settings

    class BrokenRepository:
        async def ping(self):
            raise ConnectionError("database unavailable")

    store = RedisConversationStore(settings.redis_url, 3600)
    try:
        monkeypatch.setattr(main, "conversation_store", store)
        monkeypatch.setattr(main, "support_repository", BrokenRepository())
        monkeypatch.setattr(main, "_dependency_health_cache", None)
        assert await main._inbound_dependencies_healthy() is False
    finally:
        await store._client.aclose()
