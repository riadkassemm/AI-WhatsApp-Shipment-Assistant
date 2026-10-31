"""Carry a removed WhatsApp number's history over when that phone is linked again.

Removing a number archives its live conversations (status CLOSED) and leaves
its history on the retired line ID, read-only. When the same phone is paired
again under a new line ID, the retired history moves onto the new line and the
conversations the removal archived return to the state they had before it.
Assignment fields are never rewritten, so ownership and its audit stay intact.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.support_models import ACTIVE_TICKET_STATUSES
from app.whatsapp_registry_removal import removed_number

REMOVAL_REASON = "whatsapp_line_removed"
RELINK_REASON = "whatsapp_line_relinked"
CLOSE_EVENTS = ("TICKET_CLOSED", "CHAT_CLOSED")
RESTORE_EVENTS = ("TICKET_RESTORED", "CHAT_RESTORED")
# The latest of these decides whether an archived conversation is still archived.
_LIFECYCLE = ("TICKET_RESOLVED", "TICKET_REOPENED", "CHAT_STARTED", "CHAT_REOPENED", *CLOSE_EVENTS, *RESTORE_EVENTS)


def phone_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _details(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def removal_details(ticket: dict[str, Any], line_id: str) -> dict[str, Any]:
    """What a removal must record so a later re-link can undo it exactly."""
    return {
        "reason": REMOVAL_REASON,
        "phone_number_id": line_id,
        "previous_status": ticket.get("status"),
        "previous_resolved_by_staff_id": ticket.get("resolved_by_staff_id"),
    }


def restored_status(ticket: dict[str, Any], closing: dict[str, Any], last_status_change: str | None) -> str:
    assigned = bool(ticket.get("current_assignee_id"))
    previous = closing.get("previous_status")
    if previous not in ACTIVE_TICKET_STATUSES:
        # Removals before previous_status was recorded: rebuild from the timeline.
        previous = last_status_change if last_status_change in ACTIVE_TICKET_STATUSES else None
    if previous and (previous == "NEW") != assigned:
        return previous
    return "ASSIGNED" if assigned else "NEW"


async def retired_lines_for_phone(cur: Any, line_id: str, phone: str) -> list[str]:
    await cur.execute(
        """SELECT DISTINCT business_phone_number_id AS line FROM support_tickets
           WHERE business_phone_number_id <> %s AND business_number_retired_at IS NOT NULL
             AND REGEXP_REPLACE(COALESCE(business_display_phone_number, ''), '[^0-9]', '') = %s""",
        (line_id, phone),
    )
    lines = []
    for row in await cur.fetchall():
        old = str(row["line"] or "")
        if not old or not await removed_number(cur, old):
            continue
        # A retired line's history is adopted once; what stayed behind then stays behind.
        await cur.execute(
            """SELECT 1 FROM whatsapp_integration_audit WHERE action='LINKED_DEVICE_HISTORY_RESTORED'
               AND JSON_VALID(details_json) AND JSON_CONTAINS(details_json, JSON_QUOTE(%s), '$.from_phone_number_ids')
               LIMIT 1""",
            (old,),
        )
        if await cur.fetchone():
            continue
        await cur.execute("SELECT 1 FROM whatsapp_phone_numbers WHERE phone_number_id=%s", (old,))
        if not await cur.fetchone():
            lines.append(old)
    return sorted(lines)


async def adopt_retired_history(
    cur: Any, *, line_id: str, phone: str, actor_id: int, actor_name: str, apply: bool = True,
    archived_only: bool = False,
) -> dict[str, Any]:
    """Move retired history for ``phone`` onto ``line_id`` inside the caller's transaction.

    With ``archived_only`` only the conversations the removal archived move (and
    are restored); the rest of the history stays read-only on the retired line.
    With ``apply=False`` nothing is written; the result describes what would change.
    """
    phone = phone_digits(phone)
    summary: dict[str, Any] = {"from_lines": [], "moved": 0, "restored": [], "conflicts": []}
    if not re.fullmatch(r"[0-9]{5,15}", phone):
        return summary
    summary["from_lines"] = await retired_lines_for_phone(cur, line_id, phone)
    for old in summary["from_lines"]:
        await cur.execute(
            """SELECT id, ticket_reference, whatsapp_phone, status, active_key, conversation_origin,
                      current_assignee_id, current_assignee_name
               FROM support_tickets WHERE business_phone_number_id=%s ORDER BY id FOR UPDATE""",
            (old,),
        )
        tickets = list(await cur.fetchall())
        restorable = []
        for ticket in tickets:
            if ticket["status"] != "CLOSED" or ticket["active_key"] is not None:
                continue
            await cur.execute(
                f"""SELECT id, event_type, details_json FROM support_ticket_events
                    WHERE ticket_id=%s AND event_type IN ({','.join(['%s'] * len(_LIFECYCLE))})
                    ORDER BY id DESC LIMIT 1""",
                (ticket["id"], *_LIFECYCLE),
            )
            last = await cur.fetchone()
            closing = _details(last and last["details_json"])
            if not last or last["event_type"] not in CLOSE_EVENTS or closing.get("reason") != REMOVAL_REASON \
                    or str(closing.get("phone_number_id") or "") != old:
                continue
            # Claims and reopens set the status themselves, so only later changes count.
            await cur.execute(
                """SELECT JSON_UNQUOTE(JSON_EXTRACT(details_json, '$.to')) AS status FROM support_ticket_events
                   WHERE ticket_id=%s AND event_type='STATUS_CHANGED' AND id<%s AND JSON_VALID(details_json)
                     AND id > COALESCE((SELECT MAX(r.id) FROM support_ticket_events r WHERE r.ticket_id=%s
                         AND r.event_type IN ('TICKET_CLAIMED', 'TICKET_REOPENED') AND r.id<%s), 0)
                   ORDER BY id DESC LIMIT 1""",
                (ticket["id"], last["id"], ticket["id"], last["id"]),
            )
            changed = await cur.fetchone()
            restorable.append((ticket, closing, restored_status(ticket, closing, changed and changed["status"])))
        if not archived_only:
            summary["moved"] += sum(1 for ticket in tickets if ticket["active_key"] is None)
        if apply and not archived_only:
            # Removal leaves no live rows behind; a live row here keeps its key and line.
            await cur.execute(
                """UPDATE support_tickets SET business_phone_number_id=%s, business_number_retired_at=NULL
                   WHERE business_phone_number_id=%s AND active_key IS NULL""",
                (line_id, old),
            )
        for ticket, closing, status in restorable:
            active_key = f"{line_id}:{ticket['whatsapp_phone']}"
            await cur.execute(
                "SELECT ticket_reference FROM support_tickets WHERE active_key=%s AND id<>%s LIMIT 1",
                (active_key, ticket["id"]),
            )
            current = await cur.fetchone()
            if current:
                # The customer already wrote again on the new line; that ticket is the live one.
                summary["conflicts"].append({"id": ticket["id"], "ticket_reference": ticket["ticket_reference"],
                                             "active_ticket_reference": current["ticket_reference"]})
                continue
            summary["restored"].append({"id": ticket["id"], "ticket_reference": ticket["ticket_reference"],
                                        "status": status, "whatsapp_phone": ticket["whatsapp_phone"],
                                        "conversation_origin": ticket["conversation_origin"]})
            if archived_only:
                summary["moved"] += 1
            if not apply:
                continue
            await cur.execute(
                """UPDATE support_tickets SET business_phone_number_id=%s, business_number_retired_at=NULL,
                       status=%s, active_key=%s, closed_at=NULL, resolved_by_staff_id=%s,
                       updated_at=UTC_TIMESTAMP(6), last_activity_at=UTC_TIMESTAMP(6)
                   WHERE id=%s AND status='CLOSED' AND active_key IS NULL""",
                (line_id, status, active_key, closing.get("previous_resolved_by_staff_id"), ticket["id"]),
            )
            await cur.execute(
                """INSERT INTO support_ticket_events (ticket_id, event_type, actor_type, actor_id, actor_name, details_json, created_at)
                   VALUES (%s, %s, 'AGENT', %s, %s, %s, UTC_TIMESTAMP(6))""",
                (ticket["id"], "CHAT_RESTORED" if ticket["conversation_origin"] == "STAFF" else "TICKET_RESTORED",
                 str(actor_id), actor_name, json.dumps({
                     "reason": RELINK_REASON, "from_phone_number_id": old, "phone_number_id": line_id,
                     "restored_status": status, "current_assignee_id": ticket["current_assignee_id"],
                 })),
            )
    return summary
