"""Bounded periodic delivery evidence and reply-read reconciliation."""
import asyncio
import logging


async def reconcile_deliveries_forever(service, interval=30):
    cursor = 0
    while True:
        await asyncio.sleep(interval)
        for action in ('reconcile_uncertain_deliveries', 'reconcile_delivery_receipts', 'sync_reply_reads',
                       'resend_held_replies'):
            try:
                if action == 'reconcile_uncertain_deliveries':
                    cursor = await service.reconcile_uncertain_deliveries(cursor)
                else:
                    await getattr(service, action)()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger('shipment-bot').warning('Support reconciliation unavailable: %s', action)
