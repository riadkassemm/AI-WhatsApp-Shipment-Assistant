"""Restore a removed WhatsApp number's history onto the line it was linked again as.

Enabling a re-linked number does this automatically. Use this script for numbers
re-linked before that existed, or when the automatic step failed. It reports
what would change and writes nothing unless --apply is given.

Run with the app environment from the project root:

    python scripts/restore_relinked_history.py --line 90026 --actor-staff-id 6 [--archived-only] [--apply]

A retired line's history is adopted once. With --archived-only, only the
conversations its removal archived come back; the rest stays read-only.

Output lists ticket references and states only; customer details are never printed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.support_auth import SupportAgent
from app.whatsapp_registry import whatsapp_registry_repository, whatsapp_registry_service
from app.whatsapp_relink import phone_digits


async def run(line_id: str, actor_staff_id: int, apply: bool, archived_only: bool) -> int:
    line = await whatsapp_registry_repository.fetch_one(
        """SELECT phone_number_id, source, is_enabled, display_phone_number FROM whatsapp_phone_numbers
           WHERE phone_number_id=%s AND removed_at IS NULL""", (line_id,),
    )
    if not line or line.get("source") != "LINKED_DEVICE" or not line.get("is_enabled"):
        print("The line must be an enabled linked-device number.", file=sys.stderr)
        return 2
    phone = phone_digits(line.get("display_phone_number"))
    if not phone:
        print("The line has no paired phone number yet.", file=sys.stderr)
        return 2
    staff = await whatsapp_registry_repository.fetch_one(
        "SELECT id, name, role FROM staff WHERE id=%s AND is_active=1 AND deleted_at IS NULL", (actor_staff_id,),
    )
    if not staff or staff.get("role") not in {"admin", "supervisor"}:
        print("The actor must be an active Admin or Super Admin.", file=sys.stderr)
        return 2
    agent = SupportAgent(id=str(staff["id"]), name=str(staff["name"]), role=str(staff["role"]))
    summary = await whatsapp_registry_service.restore_relinked_history(
        agent, line_id, phone, apply=apply, archived_only=archived_only,
    )
    print(("Applied" if apply else "Dry run (nothing written)") + f" for line {line_id}")
    print("Removed lines with this phone: " + (", ".join(summary["from_lines"]) or "none"))
    print(f"Conversations moved onto this line: {summary['moved']}")
    print(f"Archived conversations restored: {len(summary['restored'])}")
    for item in summary["restored"]:
        print(f"  {item['ticket_reference']} -> {item['status']}")
    print(f"Left archived (customer already has a live conversation): {len(summary['conflicts'])}")
    for item in summary["conflicts"]:
        print(f"  {item['ticket_reference']} (live: {item['active_ticket_reference']})")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--line", required=True, help="Line ID the number was linked again as")
    parser.add_argument("--actor-staff-id", required=True, type=int, help="Admin recorded as performing the restore")
    parser.add_argument("--archived-only", action="store_true",
                        help="Restore only the conversations the removal archived; leave the rest")
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run)")
    args = parser.parse_args()

    async def run_and_close() -> int:
        try:
            return await run(args.line, args.actor_staff_id, args.apply, args.archived_only)
        finally:
            pool = whatsapp_registry_repository._pool
            if pool is not None:
                pool.close()
                await pool.wait_closed()

    sys.exit(asyncio.run(run_and_close()))


if __name__ == "__main__":
    main()
