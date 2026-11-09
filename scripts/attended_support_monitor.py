"""Attended Customer Support health console. Silence is never health; no customer data displayed."""
import argparse
import json
import time
import urllib.parse
import urllib.request

CHECKS = {
    'monitoring_activated': 'support_monitoring_expected == 1',
    'runtime_collector_fresh': 'time() - support_runtime_check_timestamp_seconds < 120',
    'storage_collector_fresh': 'time() - migration_storage_scan_timestamp_seconds < 180',
    'watchdog_fresh': 'time() - bridge_watchdog_timestamp_seconds < 180',
    'api_up': 'support_api_up == 1',
    'redis_up': 'support_redis_up == 1',
    'bridge_ready': 'support_bridge_ready == 1',
    'bridge_diagnostics_present': 'support_bridge_diagnostics_up == 1',
    'delivery_monitoring_present': 'support_delivery_database_check_up == 1',
    # Instant console checks must cover the 10s observer + 30s collector +
    # 30s scrape, plus timer/probe jitter. A live sample can legitimately be
    # 60–75s old here. Sustained-staleness Prometheus alerts remain unchanged.
    'event_loop_observer_fresh': 'time() - support_bridge_event_loop_sample_timestamp_seconds < 90',
    # Expected identities come from the registry each collection (no static
    # count). Unreadable registry/manifest data is a failure, not health.
    'identity_registry_available': 'support_bridge_registry_up == 1',
    'identity_manifest_available': 'support_bridge_manifest_up == 1',
    'all_expected_identities_healthy': 'support_bridge_expected_unhealthy_lines == 0 and on(job, instance) (support_bridge_expected_healthy_lines == on(job, instance) support_bridge_expected_lines)',
    'no_removed_identity_reactivated': 'support_bridge_removed_reactivated_lines == 0',
    'fenced_retirement_proven': 'support_bridge_fenced_unresolved_lines == 0',
    'no_pairing_required': 'support_bridge_qr_codes == 0',
    'storage_safe': 'support_bridge_eligible_storage_critical == 0',
    'checkpoint_healthy': 'bridge_storage_checkpoint_failed == 0',
    'node_exporter_up': 'up{job="node"} == 1',
    'no_inbound_corruption': 'support_inbound_invalid == 0',
    'disk_reserve': 'node_filesystem_avail_bytes{mountpoint="/"} > 10737418240',
}


def query(expression):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = 'http://127.0.0.1:9090/api/v1/query?' + urllib.parse.urlencode({'query': expression})
    with opener.open(url, timeout=3) as response:
        body = json.loads(response.read(1024 * 1024))
    if body.get('status') != 'success':
        raise ValueError('PROMETHEUS_UNAVAILABLE')
    return body['data']['result']


def collect(read=query):
    checks = {}
    for name, expression in CHECKS.items():
        try:
            checks[name] = bool(read(expression))
        except Exception:
            checks[name] = False
    try:
        checks['no_firing_critical_alerts'] = not bool(read('ALERTS{alertstate="firing",severity="critical"}'))
    except Exception:
        checks['no_firing_critical_alerts'] = False
    # Surface actionable warnings even while current connectivity is healthy.
    attention = []
    try:
        for alert in read('ALERTS{alertstate="firing",severity="warning"}'):
            name = alert.get('metric', {}).get('alertname', '')
            if name in {'BridgeRepeatedDisconnects', 'BridgeHeartbeatDelayed', 'BridgeEventLoopStalls',
                        'BridgeCallbackBacklog', 'BridgeCallbackDeadLetters', 'SupportDeliveryUncertaintyIncreased', 'BridgeIdentityFenced', 'BridgeIdentityDecommissioned',
                        'BridgeRegistryMismatch', 'BridgePairingStalled', 'BridgeRetainedIdentityStorageIssue'}:
                attention.append(name)
    except Exception:
        checks['warning_alerts_readable'] = False
    return {'checked_at': time.time(), 'healthy': all(checks.values()),
            'failed_checks': [name for name, passed in checks.items() if not passed],
            'attention': sorted(set(attention))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    while True:
        report = collect()
        print(json.dumps(report), flush=True)
        if args.once:
            raise SystemExit(0 if report['healthy'] else 1)
        time.sleep(15)
