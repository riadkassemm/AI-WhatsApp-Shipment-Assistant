"""Bounded periodic verification of saved evidence; never sends to customers."""
import asyncio
import logging


async def reconcile_deliveries_forever(service, interval=30):
    cursor = 0
    while True:
        await asyncio.sleep(interval)
        try:
            cursor = await service.reconcile_uncertain_deliveries(cursor)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger('shipment-bot').warning('Support delivery reconciliation unavailable')
