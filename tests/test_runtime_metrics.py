from scripts.export_runtime_metrics import bridge_metrics, collect


def eight_enabled():
    return {str(90000 + index): True for index in range(8)}


def no_removals():
    return set()


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
    output = collect(EmptyRedis(), probe=lambda: True, bridge=snapshot, database=lambda: {'UNCERTAIN': 1}, now=lambda: 123,
                     registry=eight_enabled, manifest=no_removals)
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
    output = collect(EmptyRedis(), probe=fail, bridge=fail, database=fail, registry=fail, manifest=fail)
    assert 'support_api_up 0' in output and 'support_bridge_ready 0' in output
    assert 'support_bridge_diagnostics_up 0' in output and 'support_delivery_database_check_up 0' in output
    assert 'private' not in output


def metric(output, name):
    return [line for line in output.splitlines() if line.startswith(name + ' ')]


def run(data, registry, removed=frozenset()):
    return collect(EmptyRedis(), probe=lambda: True, bridge=lambda: data, database=lambda: {},
                   registry=lambda: dict(registry), manifest=lambda: set(removed))


def test_expected_identities_follow_the_registry_not_a_static_count_or_live_sockets(monkeypatch):
    monkeypatch.setenv('WHATSAPP_EXPECTED_IDENTITIES', '6')  # Ignored: no static count.
    data = snapshot(); data['lines'] = data['lines'][:4]
    output = run(data, {line['line_id']: True for line in data['lines']})
    assert metric(output, 'support_bridge_expected_lines') == ['support_bridge_expected_lines 4']
    assert metric(output, 'support_bridge_expected_healthy_lines') == ['support_bridge_expected_healthy_lines 4']
    assert metric(output, 'support_bridge_expected_unhealthy_lines') == ['support_bridge_expected_unhealthy_lines 0']
    assert 'support_bridge_registry_up 1' in output and 'support_bridge_manifest_up 1' in output
    # Five enabled and one disconnects: the expected set does not shrink.
    data = snapshot(); data['lines'] = data['lines'][:5]; data['lines'][2]['status'] = 'disconnected'
    output = run(data, {line['line_id']: True for line in data['lines']})
    assert 'support_bridge_expected_lines 5\n' in output and 'support_bridge_expected_unhealthy_lines 1\n' in output
    assert output.count('state="disconnected"} 1') == 1
    # The socket disappearing entirely is still an expected identity: "missing".
    del data['lines'][2]
    output = run(data, {**{line['line_id']: True for line in data['lines']}, '90002': True})
    assert 'support_bridge_expected_lines 5\n' in output and output.count('state="missing"} 1') == 1
    assert '90002' not in output  # Identities are pseudonymous.


def test_added_disabled_removed_and_retained_identities():
    data = snapshot(); data['lines'] = data['lines'][:5]
    registry = {line['line_id']: True for line in data['lines'][:4]}
    # Newly paired number: registered disabled and pairing/connected -> not expected.
    registry['90004'] = False
    data['lines'][4]['qr'] = 'private-qr'
    output = run(data, registry)
    assert 'support_bridge_expected_lines 4\n' in output and 'support_bridge_expected_unhealthy_lines 0\n' in output
    assert 'support_bridge_disabled_lines 1\n' in output and 'support_bridge_pairing_lines 1\n' in output
    assert 'private-qr' not in output
    # Super Admin enables it: tracked from the next collection.
    data['lines'][4]['qr'] = None
    output = run(data, {**registry, '90004': True})
    assert 'support_bridge_expected_lines 5\n' in output and 'support_bridge_expected_healthy_lines 5\n' in output
    # Durable removal: registry row deleted and tombstoned -> no longer expected.
    removed_data = snapshot(); removed_data['lines'] = removed_data['lines'][:3]
    output = run(removed_data, {line['line_id']: True for line in removed_data['lines']}, {'90003'})
    assert 'support_bridge_expected_lines 3\n' in output and 'support_bridge_removed_reactivated_lines 0\n' in output
    # A tombstoned ID reappearing (registry or live bridge) is reported.
    output = run(snapshot(), {line['line_id']: True for line in snapshot()['lines']}, {'90003'})
    assert 'support_bridge_removed_reactivated_lines 1\n' in output
    # Decommissioned identities stay visible but are neither expected nor live.
    retained = snapshot(); retained['lines'] = retained['lines'][:3]
    retained['lines'][2].update(lifecycle='decommissioned', eligible=False, status='decommissioned', is_paired=False,
                                isolation={'safe': True})
    output = run(retained, {'90000': True, '90001': True}, {'90002'})
    assert 'support_bridge_expected_lines 2\n' in output and 'support_bridge_expected_unhealthy_lines 0\n' in output
    assert 'support_bridge_removed_reactivated_lines 0\n' in output and 'support_bridge_decommissioned_lines 1\n' in output
    # Enabled in the app but retained on the bridge is a disagreement, never health.
    output = run(retained, {'90000': True, '90001': True, '90002': True})
    assert output.count('state="retained"} 1') == 1 and 'support_bridge_expected_unhealthy_lines 1\n' in output
    # A bridge identity with no registry row is a mismatch, not an expected identity.
    output = run(snapshot(), {'90000': True})
    assert 'support_bridge_unregistered_lines 7\n' in output and 'support_bridge_expected_lines 1\n' in output


def test_unavailable_registry_manifest_or_bridge_never_reports_health():
    def fail():
        raise RuntimeError('private database detail')
    registry = {line['line_id']: True for line in snapshot()['lines']}
    output = collect(EmptyRedis(), probe=lambda: True, bridge=snapshot, database=lambda: {}, registry=fail, manifest=no_removals)
    assert 'support_bridge_registry_up 0' in output and 'support_bridge_expected_' not in output
    output = collect(EmptyRedis(), probe=lambda: True, bridge=snapshot, database=lambda: {}, registry=lambda: registry, manifest=fail)
    assert 'support_bridge_manifest_up 0' in output and 'support_bridge_expected_' not in output
    output = collect(EmptyRedis(), probe=lambda: True, bridge=fail, database=lambda: {}, registry=lambda: registry, manifest=no_removals)
    assert 'support_bridge_ready 0' in output and 'support_bridge_expected_unhealthy_lines 8\n' in output
    assert output.count('state="unknown"} 1') == 8 and 'private' not in output
    output = collect(EmptyRedis(), probe=lambda: True, bridge=snapshot, database=lambda: {},
                     registry=lambda: {'not-an-id': True}, manifest=no_removals)
    assert 'support_bridge_registry_up 0' in output and 'support_bridge_expected_' not in output


def test_missing_or_nonfinite_diagnostics_do_not_masquerade_as_health():
    for bad in [None, float('nan')]:
        data = snapshot()
        data['lines'][0]['diagnostics']['heartbeat_timer_delay_ms'] = bad
        output = collect(EmptyRedis(), probe=lambda: True, bridge=lambda: data, database=lambda: {},
                         registry=eight_enabled, manifest=no_removals)
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


def test_fenced_identity_keeps_backlog_metrics_without_claiming_transport_health():
    data = snapshot()
    data['lines'] = data['lines'][:7]
    data['runtime'] = {'eligible_storage_critical': False}
    line = data['lines'][-1]
    line.update(status='error', lifecycle='fenced_retained', eligible=False,
                isolation={'safe': True}, diagnostics={}, uncertain_sends=36, pending_callbacks=2)
    metrics = bridge_metrics(data)
    assert metrics['support_bridge_lines'] == 7
    assert metrics['support_bridge_eligible_lines'] == 6
    assert metrics['support_bridge_eligible_paired_lines'] == 6
    assert metrics['support_bridge_fenced_lines'] == 1
    assert metrics['support_bridge_fenced_unresolved_lines'] == 0
    assert metrics['support_bridge_diagnostics_up'] == 1
    assert metrics['support_bridge_eligible_storage_critical'] == 0
    assert any(k.startswith('support_bridge_uncertain_sends{') and v == 36 for k, v in metrics.items())
    assert any(k.startswith('support_bridge_identity_diagnostics_up{') and v == 0 for k, v in metrics.items())
    line.update(lifecycle='fenced_unresolved', isolation={'safe': False})
    assert bridge_metrics(data)['support_bridge_diagnostics_up'] == 0
    assert bridge_metrics(data)['support_bridge_fenced_unresolved_lines'] == 1
    del data['runtime']
    assert bridge_metrics(data)['support_bridge_eligible_storage_critical'] == 1


def test_decommissioned_identity_keeps_uncertainty_visible_without_counting_as_eligible():
    data = snapshot()
    data['lines'] = data['lines'][:7]
    data['runtime'] = {'eligible_storage_critical': False}
    data['lines'][-1].update(status='decommissioned', lifecycle='decommissioned', eligible=False,
        is_paired=False, isolation={'safe': True}, diagnostics={}, uncertain_sends=41, pending_callbacks=2)
    metrics = bridge_metrics(data)
    assert metrics['support_bridge_eligible_lines'] == 6
    assert metrics['support_bridge_decommissioned_lines'] == 1
    assert metrics['support_bridge_fenced_unresolved_lines'] == 0
    assert metrics['support_bridge_diagnostics_up'] == 1
    assert any(k.startswith('support_bridge_uncertain_sends{') and v == 41 for k, v in metrics.items())
