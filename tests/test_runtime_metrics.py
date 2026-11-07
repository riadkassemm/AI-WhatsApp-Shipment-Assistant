from scripts.export_runtime_metrics import bridge_metrics, collect


class EmptyRedis:
    def scan_iter(self, **_kwargs):
        return iter(())


def snapshot():
    return {'ready': True, 'lines': [{'line_id': str(90000 + index), 'status': 'connected', 'is_paired': True,
        'diagnostics': {'supported': True, 'version': 2, 'generation': 1, 'generation_started_at': 100000,
            'heartbeat_requests': 10, 'heartbeat_successes': 7, 'heartbeat_query_timeouts': 3,
            'heartbeat_request_at': 119500, 'heartbeat_query_completed_at': 120000, 'heartbeat_success_at': 120000, 'raw_frames': 20, 'decoded_frames': 20,
            'last_raw_frame_at': 120000, 'last_frame_at': 120000, 'disconnects_total': 3, 'disconnects': {'temporary_transport': 3},
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


def test_expected_identity_count_is_independent_of_live_connections(monkeypatch):
    monkeypatch.setenv('WHATSAPP_EXPECTED_IDENTITIES', '6')
    data = snapshot()
    data['lines'] = data['lines'][:5]
    output = collect(EmptyRedis(), probe=lambda: True, bridge=lambda: data, database=lambda: {})
    assert 'support_bridge_expected_lines 6\n' in output
    assert 'support_bridge_connected_lines 5\n' in output
    for invalid in ('0', '-1', '129', 'private-invalid-value'):
        monkeypatch.setenv('WHATSAPP_EXPECTED_IDENTITIES', invalid)
        output = collect(EmptyRedis(), probe=lambda: True, bridge=lambda: data, database=lambda: {})
        assert 'support_bridge_expected_lines 0\n' in output
        assert 'private-invalid-value' not in output
    monkeypatch.delenv('WHATSAPP_EXPECTED_IDENTITIES')
    output = collect(EmptyRedis(), probe=lambda: True, bridge=lambda: data, database=lambda: {})
    assert 'support_bridge_expected_lines 8\n' in output


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


def test_old_diagnostics_and_timeouts_do_not_claim_successful_heartbeat():
    data = snapshot()
    d = data['lines'][0]['diagnostics']
    d.update(heartbeat_success_at=None, heartbeat_rtt_ms=None, heartbeat_successes=0,
             heartbeat_query_elapsed_ms=20000)
    metrics = bridge_metrics(data)
    assert not any(key.startswith('support_bridge_heartbeat_rtt_seconds') and value == 20
                   for key, value in metrics.items())
    assert any(key.startswith('support_bridge_heartbeat_success_timestamp_seconds') and value == 0
               for key, value in metrics.items())
    d['version'] = 1
    assert bridge_metrics(data)['support_bridge_diagnostics_up'] == 0


def test_tcp_metric_labels_never_include_endpoints_or_untrusted_fields():
    data = snapshot()
    data['lines'][0]['diagnostics']['transport'] = {'remote_ip': '2001:db8::1', 'family': 'IPv6'}
    metrics = bridge_metrics(data, {'90000': {'retransmitted_segments': 3, 'private_payload': 99}})
    assert any(key.startswith('support_bridge_tcp_retransmitted_segments') and value == 3 for key, value in metrics.items())
    assert '2001:db8' not in str(metrics) and 'private_payload' not in str(metrics)


def test_ip_policy_tls_and_kernel_progress_are_exported_without_endpoint_or_error_labels():
    data = snapshot()
    data['lines'][0]['diagnostics'].update(configured_ip_family=4, socket_error_code='private-error',
        transport={'remote_ip': '192.0.2.1', 'family': 'IPv4', 'tls_authorized': True})
    metrics = bridge_metrics(data, {'90000': {'bytes_received': 1234, 'bytes_acked': 456,
        'segments_in': 10, 'unacked_segments': 1, 'sample_started_timestamp_seconds': 100,
        'sample_timestamp_seconds': 100.2, 'private_payload': 99}})
    for name, expected in {'configured_ip_family': 4, 'transport_tls_authorized': 1,
                           'tcp_bytes_received': 1234, 'tcp_bytes_acked': 456,
                           'tcp_segments_in': 10, 'tcp_unacked_segments': 1,
                           'tcp_sample_timestamp_seconds': 100.2}.items():
        assert any(k.startswith('support_bridge_' + name + '{') and v == expected for k, v in metrics.items())
    assert '192.0.2' not in str(metrics) and 'private' not in str(metrics)
    assert not any(k.startswith('support_bridge_tcp_send_window_bytes') for k in metrics)
