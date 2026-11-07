"""Explicit root-only registry archival after durable, verified local quarantine.

Uses existing transactional removal guards. Does not delete messages, media,
delivery claims, callbacks or authentication files; never sends WhatsApp traffic.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import urllib.request


async def archive(line_id):
    from app.config import quarantined_linked_device_line_ids, settings
    from app.whatsapp_registry import WhatsAppRegistryRepository
    if line_id not in quarantined_linked_device_line_ids():
        raise RuntimeError("DURABLE_QUARANTINE_REQUIRED")
    request = urllib.request.Request(settings.whatsapp_linked_device_url.rstrip("/") + "/v1/status",
        headers={"Authorization": "Bearer " + settings.whatsapp_linked_device_secret})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        snapshot = json.load(response)
    line = next((line for line in snapshot["lines"] if line.get("line_id") == line_id), None)
    if not line or line.get("lifecycle") != "decommissioned" or line.get("isolation", {}).get("safe") is not True:
        raise RuntimeError("RUNNING_QUARANTINE_UNVERIFIED")
    repository = WhatsAppRegistryRepository()
    try:
        await repository.prepare_linked_device_removal(line_id, actor_id=None, close_active_tickets=True)
        tickets = await repository.finish_linked_device_removal(line_id, actor_id=None,
            actor_name="Production operator — approved local decommission", close_active_tickets=True,
            remote_unlinked=line.get("local_removal", {}).get("unlinked") is True)
        return {"line_id": line_id, "archived_active_conversations": len(tickets), "local_removal": line["local_removal"]}
    finally:
        if repository._pool is not None:
            repository._pool.close()
            await repository._pool.wait_closed()


def main():
    from dotenv import dotenv_values
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("line_id")
    parser.add_argument("--confirm-permanent-local-removal", action="store_true", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.line_id.isdigit() or not 5 <= len(args.line_id) <= 32:
        raise SystemExit("ROOT_OPERATOR_AND_SELECTED_IDENTITY_REQUIRED")
    os.environ.update({k: v for k, v in dotenv_values("/etc/whatsapp-support/app.env").items() if v is not None})
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    print(json.dumps(asyncio.run(archive(args.line_id)), ensure_ascii=False))


if __name__ == "__main__":
    main()
