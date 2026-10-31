from __future__ import annotations

import json
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path

# Keep imports lightweight in artifact validation environments.
fake_aiomysql = sys.modules.get("aiomysql") or types.ModuleType("aiomysql")
fake_aiomysql.Pool = getattr(fake_aiomysql, "Pool", object)
fake_aiomysql.DictCursor = getattr(fake_aiomysql, "DictCursor", object)
fake_aiomysql.IntegrityError = getattr(
    fake_aiomysql, "IntegrityError", type("IntegrityError", (Exception,), {})
)
sys.modules["aiomysql"] = fake_aiomysql

from app.support_timing import responsibility_periods
from app.whatsapp_relink import phone_digits, removal_details, restored_status


def test_restored_status_prefers_recorded_state_and_keeps_assignment_consistent():
    assigned, unassigned = {"current_assignee_id": "7"}, {"current_assignee_id": None}
    assert restored_status(assigned, {"previous_status": "WAITING_CUSTOMER"}, None) == "WAITING_CUSTOMER"
    assert restored_status(unassigned, {"previous_status": "NEW"}, None) == "NEW"
    # Older removals did not record a status: the last change after a claim decides.
    assert restored_status(assigned, {}, "IN_PROGRESS") == "IN_PROGRESS"
    assert restored_status(assigned, {}, None) == "ASSIGNED"
    assert restored_status(unassigned, {}, None) == "NEW"
    # Never produce an assigned NEW ticket or an unassigned working one.
    assert restored_status(assigned, {"previous_status": "NEW"}, None) == "ASSIGNED"
    assert restored_status(unassigned, {"previous_status": "IN_PROGRESS"}, None) == "NEW"
    assert restored_status(assigned, {"previous_status": "RESOLVED"}, "CLOSED") == "ASSIGNED"


def test_removal_records_what_a_relink_restores():
    ticket = {"status": "IN_PROGRESS", "resolved_by_staff_id": 6, "whatsapp_phone": "15550000042"}
    assert removal_details(ticket, "90020") == {
        "reason": "whatsapp_line_removed", "phone_number_id": "90020",
        "previous_status": "IN_PROGRESS", "previous_resolved_by_staff_id": 6,
    }
    assert phone_digits("+961 76 063-972") == "96176063972"


def _event(event_id, kind, actor, details=None, minute=0):
    return {"id": event_id, "event_type": kind, "actor_type": "AGENT", "actor_id": actor,
            "details_json": json.dumps(details or {}), "created_at": datetime(2026, 10, 31, 8, minute)}


def test_restored_chat_returns_to_its_assignee_not_the_manager():
    events = [
        _event(1, "CHAT_STARTED", "7"),
        _event(2, "CHAT_CLOSED", "6", {"reason": "whatsapp_line_removed"}, 30),
        _event(3, "CHAT_RESTORED", "6", {"current_assignee_id": "7"}, 40),
    ]
    timing = responsibility_periods(events, datetime(2026, 10, 31, 9))
    assert [(p["agent_id"], p["end_reason"]) for p in timing["periods"]] == [("7", "closed"), ("7", "ongoing")]
    assert not timing["issues"]


def test_restoring_an_unassigned_ticket_starts_no_period():
    events = [_event(1, "TICKET_RESTORED", "6", {"current_assignee_id": None})]
    timing = responsibility_periods(events, datetime(2026, 10, 31, 9))
    assert timing["periods"] == [] and not timing["issues"]


def test_timeline_explains_archive_and_restore():
    source = Path("app/static/support/rendering.js").read_text()
    helper = source[source.index("function addEvent("):source.index("function dayKey(")]
    state = Path("app/static/support/state.js").read_text()
    labels = state[state.index("const STATUS_LABELS"):state.index("const MESSAGE_ROLE_LABELS")]
    formatter = state[state.index("function formatStatus("):state.index("function statusClass(")]
    script = labels + formatter + r"""
const assert=require('node:assert/strict');
const document={createElement:()=>({})},fmt=x=>x;
const entries=[],container={append:node=>entries.push(node)};
""" + helper + r"""
const removed=JSON.stringify({reason:'whatsapp_line_removed',phone_number_id:'90020'});
addEvent(container,{event_type:'TICKET_CLOSED',actor_name:'Riad',created_at:'t',details_json:removed});
addEvent(container,{event_type:'CHAT_CLOSED',actor_name:'Riad',created_at:'t',details_json:removed});
addEvent(container,{event_type:'TICKET_RESTORED',actor_name:'Riad',created_at:'t',details_json:JSON.stringify({restored_status:'IN_PROGRESS'})});
addEvent(container,{event_type:'TICKET_TRANSFERRED',actor_name:'Souad',created_at:'t',details_json:JSON.stringify({from_agent_name:'Souad Assaf',to_agent_name:'Ghina Hallak'})});
addEvent(container,{event_type:'TICKET_CLAIMED',actor_name:'Souad',created_at:'t'});
assert.deepEqual(entries.map(e=>e.textContent),[
 'Archived: WhatsApp number removed · Riad · t','Archived: WhatsApp number removed · Riad · t',
 'Restored: WhatsApp number linked again (In progress) · Riad · t',
 'Transferred: Souad Assaf → Ghina Hallak · Souad · t','Ticket claimed · Souad · t']);
assert.equal(formatStatus('CLOSED'),'Archived');assert.equal(formatStatus('RESOLVED'),'Resolved');
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
