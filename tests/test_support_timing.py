from datetime import datetime, timedelta
import json

from app.support_timing import responsibility_periods, review_indicators, timing_summary

BASE = datetime(2026, 10, 1, 10)


def event(n, kind, actor='7', **data):
    return dict(id=n+1, created_at=BASE+timedelta(minutes=n), event_type=kind,
                actor_id=actor, actor_type='AGENT', details_json=json.dumps(data))


def test_transfer_splits_responsibility_and_resolver_time():
    result = responsibility_periods([
        event(0, 'TICKET_CLAIMED'), event(5, 'TICKET_TRANSFERRED', from_agent_id='7', to_agent_id='8'),
        event(15, 'TICKET_RESOLVED', '8', last_assignee_id='8')], BASE+timedelta(hours=1))
    first, second = result['periods']
    assert (first['agent_id'], first['seconds'], first['resolution_seconds']) == ('7',300,None)
    assert (second['agent_id'],second['seconds'],second['resolution_seconds'],second['cycle_resolution_seconds']) == ('8',600,600,900)
    assert not result['issues']


def test_repeated_transfers_and_reopen_exclude_resolved_gap():
    events = [event(0,'TICKET_CLAIMED'), event(5,'TICKET_TRANSFERRED',from_agent_id='7',to_agent_id='8'),
              event(7,'TICKET_TRANSFERRED','8',from_agent_id='8',to_agent_id='7'),
              event(10,'TICKET_RESOLVED',last_assignee_id='7'),
              event(60,'TICKET_REOPENED','8',current_assignee_id='8',last_assignee_id='7'),
              event(65,'TICKET_RESOLVED','8',last_assignee_id='8')]
    result = responsibility_periods(events, BASE+timedelta(hours=2))
    assert [p['seconds'] for p in result['periods']] == [300,120,180,300]
    assert result['periods'][-1]['cycle_resolution_seconds'] == 300
    assert result['original_claimant_id'] == '7'
    assert timing_summary([300,120,180,300], [180,300])['median_handling_seconds'] == 240


def test_missing_history_does_not_invent_duration_and_manager_resolution_not_credited_to_owner():
    result = responsibility_periods([event(5,'TICKET_TRANSFERRED',from_agent_id='7',to_agent_id='8'),
                                    event(15,'TICKET_RESOLVED','9',last_assignee_id='8')], BASE+timedelta(hours=1))
    assert len(result['periods']) == 1 and result['issues']
    assert result['periods'][0]['seconds'] == 600
    assert result['periods'][0]['resolution_seconds'] is None
    assert result['periods'][0]['cycle_resolution_seconds'] is None
    missing = responsibility_periods([event(5,'TICKET_RESOLVED')], BASE+timedelta(hours=1))
    assert missing['periods'] == [] and missing['issues']


def test_open_waiting_period_is_elapsed_but_not_completed_sample():
    events = [event(0,'TICKET_CLAIMED'), event(1,'STATUS_CHANGED',to='WAITING_CUSTOMER')]
    p = responsibility_periods(events, BASE+timedelta(minutes=20))['periods'][0]
    assert p['seconds'] == 1200 and p['ended_at'] is None and p['resolution_seconds'] is None


def test_review_indicators_do_not_blame_transferor_or_short_reply():
    end = BASE+timedelta(hours=1)
    events = [event(0,'TICKET_CLAIMED'), *[event(n,'CONVERSATION_VIEWED') for n in (1,2,3)],
              event(6,'TICKET_TRANSFERRED',from_agent_id='7',to_agent_id='8'), event(20,'TICKET_RESOLVED','8')]
    periods = responsibility_periods(events,end)['periods']
    messages = [dict(sender_id='8',sent_at=BASE+timedelta(minutes=10),send_status='SENT',whatsapp_message_id='x')]
    assert review_indicators(periods,events,messages,BASE,end) == []
    flags = review_indicators(periods,events,[],BASE,end)
    assert [f['agent_id'] for f in flags] == ['8']
    assert flags[0]['code'] == 'resolved_without_response'


def test_repeated_views_and_read_sync_are_distinct_and_filtered():
    end = BASE+timedelta(hours=1)
    events = [event(0,'TICKET_CLAIMED'), *[event(n,'CONVERSATION_VIEWED') for n in (1,2,3)],
              event(4,'INCOMING_MARKED_READ',mode='reply_sync')]
    periods = responsibility_periods(events,end)['periods']
    flags = review_indicators(periods,events,[],BASE,end,7)
    assert [f['code'] for f in flags] == ['repeated_views_without_response']
    assert review_indicators(periods,events,[],BASE,end,8) == []


def test_mismatched_current_assignment_is_unknown_and_not_flagged():
    from app.support_timing import validate_current_projection
    end=BASE+timedelta(hours=1)
    events=[event(0,'TICKET_CLAIMED'),*[event(n,'CONVERSATION_VIEWED') for n in (1,2,3)]]
    projection=validate_current_projection(responsibility_periods(events,end),{'active_key':None,'current_assignee_id':'7'})
    assert projection['periods'][0]['seconds'] is None and projection['issues']
    assert review_indicators(projection['periods'],events,[],BASE,end)==[]


def test_fast_resolutions_failures_unknown_delivery_and_frequent_transfers():
    end=BASE+timedelta(hours=1)
    events=[event(0,'TICKET_CLAIMED'),event(0,'TICKET_RESOLVED')]
    events[-1]['id']=2
    periods=responsibility_periods(events,end)['periods']
    assert {f['code'] for f in review_indicators(periods,events,[],BASE,end)}=={'fast_resolution','short_handling','resolved_without_response'}
    events=[event(0,'TICKET_CLAIMED'),event(10,'TICKET_RESOLVED')]
    periods=responsibility_periods(events,end)['periods']
    failed=[dict(sender_id='7',sent_at=BASE+timedelta(minutes=1),send_status='FAILED',whatsapp_message_id=None)]
    assert review_indicators(periods,events,failed,BASE,end)[0]['code']=='resolved_without_response'
    failed[0]['send_status']='UNCERTAIN'
    assert review_indicators(periods,events,failed,BASE,end)==[]
    events=[event(0,'TICKET_CLAIMED')]
    for i in range(1,7):
        sender,recipient=('7','8') if i%2 else ('8','7')
        events.append(event(i,'TICKET_TRANSFERRED',sender,from_agent_id=sender,to_agent_id=recipient))
    flags=review_indicators(responsibility_periods(events,end)['periods'],events,[],BASE,end,7)
    assert [f['code'] for f in flags]==['frequent_transfers']
