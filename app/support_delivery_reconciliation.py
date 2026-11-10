"""Bounded periodic delivery evidence and reply-read reconciliation."""
import asyncio
import logging


FULL_PASS_ACTIONS = ('reconcile_uncertain_deliveries', 'reconcile_delivery_receipts', 'sync_reply_reads',
                     'resend_held_replies')


async def reconcile_deliveries_forever(service, interval=30, held_interval=5):
    """Resend held replies every few seconds; run the full pass every interval.

    A reply refused while its number was reconnecting goes out within seconds
    of the number coming back instead of waiting for the next full pass.
    """
    cursor = 0
    ticks_per_pass = max(1, round(interval / held_interval))
    tick = 0
    while True:
        await asyncio.sleep(held_interval)
        tick += 1
        actions = FULL_PASS_ACTIONS if tick % ticks_per_pass == 0 else ('resend_held_replies',)
        for action in actions:
            try:
                if action == 'reconcile_uncertain_deliveries':
                    cursor = await service.reconcile_uncertain_deliveries(cursor)
                else:
                    await getattr(service, action)()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger('shipment-bot').warning('Support reconciliation unavailable: %s', action)
