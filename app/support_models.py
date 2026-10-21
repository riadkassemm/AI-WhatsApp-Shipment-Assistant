from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


TicketStatus = Literal[
    "NEW",
    "ASSIGNED",
    "IN_PROGRESS",
    "WAITING_CUSTOMER",
    "WAITING_INTERNAL",
    "RESOLVED",
    "CLOSED",
]

ACTIVE_TICKET_STATUSES: tuple[str, ...] = (
    "NEW",
    "ASSIGNED",
    "IN_PROGRESS",
    "WAITING_CUSTOMER",
    "WAITING_INTERNAL",
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "NEW": {"ASSIGNED"},
    "ASSIGNED": {"IN_PROGRESS", "WAITING_CUSTOMER", "WAITING_INTERNAL"},
    "IN_PROGRESS": {"WAITING_CUSTOMER", "WAITING_INTERNAL"},
    "WAITING_CUSTOMER": {"IN_PROGRESS", "WAITING_INTERNAL"},
    "WAITING_INTERNAL": {"IN_PROGRESS", "WAITING_CUSTOMER"},
    "RESOLVED": {"CLOSED"},
    "CLOSED": set(),
}


def may_reopen_customer_ticket(ticket: dict[str, Any], staff_id: str, *, supervisor: bool) -> bool:
    """Permission only; lifecycle, line availability, and uniqueness are separate guards.

    Resolution preserves current_assignee_id as the last assignee. Missing
    legacy resolver/assignment IDs never confer regular-agent permission.
    """
    return str(ticket.get("conversation_origin") or "CUSTOMER") != "STAFF" and (
        supervisor or (
            str(ticket.get("current_assignee_id") or "") == str(staff_id)
            and str(ticket.get("resolved_by_staff_id") or "") == str(staff_id)
        )
    )


@dataclass(frozen=True)
class HandoffRequest:
    reason: str
    summary: str = ""
    tracking_number: str | None = None
    requested_action: str | None = None


@dataclass(frozen=True)
class AIReplyResult:
    reply_text: str
    handoff: HandoffRequest | None = None
    auth_required: bool = False
    # Tool names executed during this AI turn. This is application metadata used to
    # verify that a protected request was actually resumed after login; it is never
    # exposed to the customer.
    tool_names: tuple[str, ...] = ()
    # The first protected tool call blocked by missing authentication. The backend can
    # persist and replay this exact safe application action after login instead of
    # relying on the model to reconstruct the request from credential turns.
    auth_tool_name: str | None = None
    auth_tool_arguments: dict[str, Any] | None = None
