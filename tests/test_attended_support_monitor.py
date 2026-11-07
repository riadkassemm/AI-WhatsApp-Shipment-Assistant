from scripts.attended_support_monitor import collect


def test_missing_metrics_cannot_be_reported_as_healthy():
    report = collect(lambda _: [])
    assert not report['healthy']
    assert 'runtime_collector_fresh' in report['failed_checks']
    assert 'all_expected_lines_connected' in report['failed_checks']
    assert 'expected_identity_count_valid' in report['failed_checks']


def test_attended_monitor_fails_when_prometheus_unreachable():
    def unavailable(_):
        raise OSError('private details must not escape')
    report = collect(unavailable)
    assert not report['healthy']
    assert 'private' not in str(report)


def test_short_flap_warning_is_visible_even_after_connectivity_recovers():
    def healthy_with_warning(expression):
        if 'severity="critical"' in expression:
            return []
        if 'severity="warning"' in expression:
            return [{"metric": {"alertname": "BridgeRepeatedDisconnects"}},
                    {"metric": {"alertname": "private-unrecognized-label"}}]
        return [{"value": [0, "1"]}]

    report = collect(healthy_with_warning)
    assert report["healthy"]
    assert report["attention"] == ["BridgeRepeatedDisconnects"]
    assert "private" not in str(report)


def test_retained_identity_requires_visible_operator_attention():
    def read(expression):
        if 'severity="critical"' in expression:
            return []
        if 'severity="warning"' in expression:
            return [{'metric': {'alertname': 'BridgeIdentityFenced'}}]
        return [{'value': [0, '1']}]
    report = collect(read)
    assert report['healthy']
    assert report['attention'] == ['BridgeIdentityFenced']


def test_observer_freshness_accounts_for_pipeline_but_rejects_stopped_or_missing_observer():
    import re
    for age, expected in [(72, True), (91, False), (None, False)]:
        def read(expression):
            if 'ALERTS{' in expression:
                return []
            if 'support_bridge_event_loop_sample_timestamp_seconds' in expression:
                limit = int(re.search(r'< (\d+)$', expression)[1])
                return [{'value': [0, '1']}] if age is not None and age < limit else []
            return [{'value': [0, '1']}]
        result = collect(read)
        assert result['healthy'] is expected
        assert ('event_loop_observer_fresh' in result['failed_checks']) is (not expected)
