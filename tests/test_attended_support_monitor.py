from scripts.attended_support_monitor import collect


def test_missing_metrics_cannot_be_reported_as_healthy():
    report = collect(lambda _: [])
    assert not report['healthy']
    assert 'runtime_collector_fresh' in report['failed_checks']
    assert 'all_eight_lines_connected' in report['failed_checks']


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
