from scripts.export_runtime_metrics import bridge_metrics, collect


class EmptyRedis:
    def scan_iter(self, **_kwargs):
        return iter(())


def snapshot():
    return {'ready': True, 'lines': [{'line_id': str(90000 + index), 'status': 'connected', 'is_paired': True,
        'diagnostics': {'supported': True, 'disconnects_total': 3, 'disconnects': {'temporary_transport': 3},
            'reconnect_attempts': 3, 'recovery_count': 3, 'recovery_seconds_sum': 9, 'recovery_seconds_last': 3,
            'heartbeat_failures': 0, 'heartbeat_timeouts': 3, 'heartbeat_frame_age_ms': 100,
            'heartbeat_timer_delay_ms': 25, 'heartbeat_observed_at': 123000, 'heartbeat_rtt_ms': 500},
        'pending_callbacks': 1, 'oldest_callback_age_seconds': 12, 'waiting_history_reactions': 0,
        'failed_callbacks': 0, 'uncertain_sends': 1, 'display_phone_number': 'private-number', 'qr': None,
    } for index in range(8)], 'event_loop': {'sampled_at': 123000, 'p99_ms': 30, 'max_ms': 50, 'lag_spikes_total': 0}}


def test_runtime_checks_emit_health_and_pseudonymous_diagnostics():
    output = collect(EmptyRedis(), probe=lambda: True, bridge=snapshot, database=lambda: {'UNCERTAIN': 1}, now=lambda: 123)
    assert 'support_api_up 1' in output
    assert 'support_bridge_connected_lines 8' in output
    assert 'support_inbound_inflight 0' in output
    assert 'support_runtime_check_timestamp_seconds 123' in output
    assert 'support_bridge_diagnostics_up 1' in output
    assert 'support_database_uncertain_deliveries 1' in output
    assert 'private-number' not in output and '90000' not in output
    assert 'support_bridge_disconnects_total{identity="' in output


def test_unreachable_services_report_failure_without_exception_details():
    def fail():
        raise RuntimeError('private credential or payload')
    output = collect(EmptyRedis(), probe=fail, bridge=fail, database=fail)
    assert 'support_api_up 0' in output and 'support_bridge_ready 0' in output
    assert 'support_bridge_diagnostics_up 0' in output and 'support_delivery_database_check_up 0' in output
    assert 'private' not in output


def test_missing_or_nonfinite_diagnostics_do_not_masquerade_as_health():
    for bad in [None, float('nan')]:
        data = snapshot()
        data['lines'][0]['diagnostics']['heartbeat_timer_delay_ms'] = bad
        output = collect(EmptyRedis(), probe=lambda: True, bridge=lambda: data, database=lambda: {})
        assert 'support_bridge_connected_lines 8' in output
        assert 'support_bridge_diagnostics_up 0' in output
        assert 'nan' not in output


def test_unknown_reason_text_is_never_a_metric_label():
    data = snapshot()
    data['lines'][0]['diagnostics']['disconnects']['private provider payload'] = 999
    output = str(bridge_metrics(data))
    assert 'private provider payload' not in output
