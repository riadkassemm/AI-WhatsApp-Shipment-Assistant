"""Transfer-aware projections of retained ticket audit; never invent assignments."""
import json
from statistics import mean, median

TIMING_METRICS = ('average_handling_seconds', 'median_handling_seconds',
                  'average_resolution_seconds', 'median_resolution_seconds')
LIFECYCLE_EVENTS = ('TICKET_CLAIMED', 'TICKET_TRANSFERRED', 'TICKET_RESOLVED',
                    'TICKET_REOPENED', 'CHAT_STARTED', 'CHAT_REOPENED', 'CHAT_CLOSED', 'STATUS_CHANGED')


def details(event):
    try:
        value = json.loads(event.get('details_json') or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def responsibility_periods(events, as_of):
    periods, issues = [], []
    active = None
    cycle_start = None
    cycle = 0
    original_claimant = None

    def finish(at, reason, resolver=None, valid=True):
        nonlocal active, cycle_start
        if active:
            active.update(ended_at=at, end_reason=reason, resolver_id=resolver,
                          seconds=max(0, (at-active['started_at']).total_seconds()), valid=valid)
            active['resolution_seconds'] = active['seconds'] if valid and reason == 'resolved' and resolver == active['agent_id'] else None
            active['cycle_resolution_seconds'] = (at-cycle_start).total_seconds() if valid and cycle_start and reason == 'resolved' else None
            periods.append(active)
            active = None
        if reason in {'resolved', 'closed'}:
            cycle_start = None

    for event in sorted(events, key=lambda e: (e['created_at'], e['id'])):
        at, kind, data = event['created_at'], event['event_type'], details(event)
        if at > as_of or kind not in LIFECYCLE_EVENTS:
            continue
        actor = str(event.get('actor_id') or '') if event.get('actor_type') == 'AGENT' else ''
        if kind in {'TICKET_CLAIMED', 'CHAT_STARTED', 'CHAT_REOPENED', 'TICKET_REOPENED'}:
            owner = str(data.get('current_assignee_id') or '') if kind == 'TICKET_REOPENED' else actor
            if kind == 'TICKET_CLAIMED' and original_claimant is None:
                original_claimant = actor or None
            if active and active['agent_id'] == owner and kind == 'TICKET_CLAIMED':
                continue  # Historical duplicate claim; not a second period.
            if active:
                finish(at, 'incomplete', valid=False)
                issues.append('An assignment started before the preceding period was closed.')
            cycle += 1
            cycle_start = at if owner else None
            if owner:
                active = dict(agent_id=owner, started_at=at, start_event=kind, cycle=cycle)
            else:
                issues.append('An assignment/reopen event has no authoritative owner.')
        elif kind == 'TICKET_TRANSFERRED':
            previous, target = str(data.get('from_agent_id') or ''), str(data.get('to_agent_id') or '')
            valid = bool(active and previous == active['agent_id'] and target)
            if not valid:
                issues.append('A transfer has missing or inconsistent prior assignment history.')
                cycle_start = None
            finish(at, 'transferred', valid=valid)
            if target:
                active = dict(agent_id=target, started_at=at, start_event=kind, cycle=cycle)
        elif kind in {'TICKET_RESOLVED', 'CHAT_CLOSED'} or (kind == 'STATUS_CHANGED' and data.get('to') in {'RESOLVED', 'CLOSED'}):
            if not active and kind != 'STATUS_CHANGED':
                issues.append('A resolution/closure has no recorded responsibility start.')
            valid = not data.get('last_assignee_id') or bool(active and str(data['last_assignee_id']) == active['agent_id'])
            if not valid:
                issues.append('The resolution owner disagrees with the assignment history.')
            # Staff chat closure is not counted as a customer-ticket resolution.
            reason = 'resolved' if kind == 'TICKET_RESOLVED' else 'closed'
            finish(at, reason, actor or None, valid=valid)
    if active:
        finish(as_of, 'ongoing')
        periods[-1]['ended_at'] = None
    return dict(periods=periods, issues=list(dict.fromkeys(issues)), original_claimant_id=original_claimant)


def period_in_range(period, start, end):
    return period['valid'] and period['ended_at'] is not None and start <= period['ended_at'] < end


def timing_summary(handling, resolution):
    return dict(zip(TIMING_METRICS, [round(fn(values), 3) if values else None
                                   for values in (handling, resolution) for fn in (mean, median)]),
                handling_samples=len(handling), resolution_samples=len(resolution))


def review_indicators(periods, events, messages, start, end, staff_id=None):
    """Descriptive prompts only. Transfers do not imply failure to resolve/respond."""
    flags = []
    for p in periods:
        owner = p['agent_id']
        if not p['valid'] or (staff_id is not None and owner != str(staff_id)):
            continue
        until = min(p['ended_at'] or end, end)
        own_messages = [m for m in messages if str(m.get('sender_id') or '') == owner
                        and p['started_at'] <= m['sent_at'] < until]
        confirmed = any(m['send_status'] == 'SENT' and m.get('whatsapp_message_id') for m in own_messages)
        attempts = bool(own_messages)
        unconfirmed = any(m['send_status'] in {'PENDING','DISPATCHING','UNCERTAIN'} for m in own_messages)
        closed = period_in_range(p, start, end)
        def flag(code, explanation):
            flags.append(dict(agent_id=owner, code=code, explanation=explanation, period_start=p['started_at']))
        if closed and p['end_reason'] == 'resolved' and p['resolver_id'] == owner:
            if p['seconds'] < 60:
                flag('fast_resolution', 'Resolved within 60 seconds of this responsibility period starting.')
            if not confirmed and not unconfirmed:
                flag('resolved_without_response', 'No confirmed response attributable to this owner during the resolved period; inspect any failed sends.')
        if closed and p['seconds'] < 30:
            flag('short_handling', 'Responsibility period lasted less than 30 seconds.')
        owned_events = [e for e in events if str(e.get('actor_id') or '') == owner and e.get('actor_type') == 'AGENT'
                        and max(start, p['started_at']) <= e['created_at'] < until]
        # Do not flag a legitimate transfer as an unanswered/resolution failure.
        if p['end_reason'] != 'transferred' and not attempts and (until-p['started_at']).total_seconds() >= 300:
            views = sum(e['event_type'] == 'CONVERSATION_VIEWED' for e in owned_events)
            if views >= 3:
                flag('repeated_views_without_response', f'{views} recorded opens with no response attempt in this period.')
        reads = [e for e in owned_events if e['event_type'] == 'INCOMING_MARKED_READ' and details(e).get('mode') != 'reply_sync']
        if reads and not confirmed and p['end_reason'] != 'transferred':
            flag('read_without_response', f'{len(reads)} attributed read actions without a confirmed response in this period.')
        if len(reads) >= 3 and not attempts and p['end_reason'] != 'transferred':
            flag('repeated_reads_without_response', 'Repeated attributed read actions without a response attempt.')
    transfers = {}
    for p in periods:
        if period_in_range(p, start, end) and p['end_reason'] == 'transferred':
            transfers[p['agent_id']] = transfers.get(p['agent_id'], 0) + 1
    for owner, count in transfers.items():
        if count >= 3 and (staff_id is None or str(staff_id) == owner):
            flags.append(dict(agent_id=owner, code='frequent_transfers', explanation=f'{count} outgoing responsibility transfers in this ticket during the selected dates.'))
    return flags


def validate_current_projection(result, ticket):
    """Current row may reveal missing audit; it cannot supply a historical start/end."""
    ongoing = result['periods'][-1] if result['periods'] and result['periods'][-1]['ended_at'] is None else None
    if ongoing and (ticket.get('active_key') is None or str(ticket.get('current_assignee_id') or '') != ongoing['agent_id']):
        ongoing.update(valid=False, seconds=None, end_reason='incomplete')
        result['issues'].append('The current assignment/status disagrees with the retained audit; ongoing duration is unknown.')
    if not ongoing and ticket.get('active_key') is not None and ticket.get('current_assignee_id'):
        result['issues'].append('The current assignment has no recorded active responsibility start.')
    return result
