"""The outreach guard's Lua script and the limit columns on real Redis and MariaDB."""
import uuid
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.whatsapp_outreach import OutreachLimitError, WhatsAppOutreachGuard, resolve_limits, text_fingerprint


@pytest.mark.asyncio
async def test_real_redis_enforces_customers_per_day_and_same_text(infrastructure):
    _repository, sql, client = infrastructure
    line = "9" + uuid.uuid4().hex[:12]
    guard = WhatsAppOutreachGuard(redis_url=settings.redis_url, clock=lambda: datetime(2026, 11, 17, 9, tzinfo=timezone.utc))
    await sql("""INSERT INTO whatsapp_phone_numbers (phone_number_id, is_enabled, provider_present, source, created_at,
                 outreach_daily_limit, outreach_same_text_limit) VALUES (%s, 1, 1, 'LINKED_DEVICE', '2026-10-01', 3, 2)""", (line,))
    rows = await sql("""SELECT created_at, outreach_daily_limit, outreach_new_number_daily_limit, outreach_same_text_limit
                        FROM whatsapp_phone_numbers WHERE phone_number_id = %s""", (line,))
    limits = resolve_limits(rows[0], now=datetime(2026, 11, 17, 9, tzinfo=timezone.utc))
    assert (limits.limit_today, limits.same_text_limit) == (3, 2)
    announcement = text_fingerprint("Please collect your orders today")
    try:
        assert await guard.reserve(line_id=line, recipient_phone="1", fingerprint=announcement, limits=limits) == 1
        assert await guard.reserve(line_id=line, recipient_phone="2", fingerprint=announcement, limits=limits) == 2
        with pytest.raises(OutreachLimitError, match="already sent to 2 customers"):
            await guard.reserve(line_id=line, recipient_phone="3", fingerprint=announcement, limits=limits)
        assert await guard.reserve(line_id=line, recipient_phone="3", fingerprint="text:other", limits=limits) == 3
        assert await guard.reserve(line_id=line, recipient_phone="1", fingerprint=announcement, limits=limits) == 3
        with pytest.raises(OutreachLimitError, match="started its 3 new chats"):
            await guard.reserve(line_id=line, recipient_phone="4", fingerprint="text:fourth", limits=limits)
        assert await guard.used_today([line, "unused"]) == {line: 3, "unused": 0}
        keys = [key async for key in client.scan_iter(f"whatsapp:outreach:{line}:*")]
        assert len(keys) == 3  # the day's customers + two texts
        for key in keys:
            assert 0 < await client.ttl(key) <= 2 * 24 * 60 * 60
    finally:
        async for key in client.scan_iter(f"whatsapp:outreach:{line}:*"):
            await client.delete(key)
        await guard._get_redis().aclose()
