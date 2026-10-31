import asyncio
from types import SimpleNamespace
import pytest

from app.support_delivery_reconciliation import reconcile_deliveries_forever


@pytest.mark.asyncio
async def test_worker_keeps_cursor_after_failure_and_propagates_cancellation(monkeypatch):
    cursors = []
    pauses = []
    read_checks = []
    held_passes = []
    async def receipts():
        pass
    async def reads():
        read_checks.append(True)
    async def held():
        held_passes.append(True)
    async def pause(interval):
        pauses.append(interval)
    async def reconcile(cursor):
        cursors.append(cursor)
        if len(cursors) == 1:
            return 20
        if len(cursors) == 2:
            raise RuntimeError('temporary failure')
        raise asyncio.CancelledError
    monkeypatch.setattr('app.support_delivery_reconciliation.asyncio.sleep', pause)
    with pytest.raises(asyncio.CancelledError):
        await reconcile_deliveries_forever(SimpleNamespace(reconcile_uncertain_deliveries=reconcile, reconcile_delivery_receipts=receipts, sync_reply_reads=reads,
                                                         resend_held_replies=held))
    assert cursors == [0, 20, 20]
    assert pauses == [30, 30, 30]

    assert read_checks == [True, True], "read sync continues even if delivery lookup fails"
    assert held_passes == [True, True], "held replies are retried on every completed pass"
