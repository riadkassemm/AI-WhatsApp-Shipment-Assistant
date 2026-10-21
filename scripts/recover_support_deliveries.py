"""Quarantine abandoned dispatches without contacting WhatsApp."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.support_repository import support_repository


async def main():
    try:
        count = await support_repository.recover_abandoned_deliveries()
        print(f'Quarantined {count} abandoned dispatches. No messages were sent.')
    finally:
        await support_repository.close()


if __name__ == '__main__':
    asyncio.run(main())
