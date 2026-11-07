"""Loopback runtime diagnostics. Export only counts, timings and hashed identities."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import urllib.parse

try:
    from scripts.check_inbound_drain import inspect
    from scripts.bridge_tcp_diagnostics import tcp_samples
except ModuleNotFoundError:
    from check_inbound_drain import inspect
    from bridge_tcp_diagnostics import tcp_samples

CATEGORIES = ('temporary_transport', 'restart_required', 'logged_out', 'authentication_failure',
              'session_conflict', 'protocol_failure', 'storage_failure')


def api_probe():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8000/health', timeout=3) as response:
        return response.status == 200 and json.loads(response.read(4096)).get('status') == 'ok'


def bridge_probe():
    try:
        from scripts.check_bridge_storage import NoRedirect
    except ModuleNotFoundError:
        from check_bridge_storage import NoRedirect
    origin = urllib.parse.urlsplit(os.environ.get('WHATSAPP_LINKED_DEVICE_URL', 'http://127.0.0.1:3001'))
    secret = os.environ.get('WHATSAPP_LINKED_DEVICE_SECRET', '')
    if (origin.scheme not in {'http', 'https'} or origin.hostname not in {'127.0.0.1', '::1', 'localhost'}
            or origin.username or origin.password or origin.path not in {'', '/'}
            or origin.query or origin.fragment or len(secret) < 32):
        raise ValueError('INVALID_PRIVATE_BRIDGE_ORIGIN')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def read(endpoint):
        request = urllib.request.Request(urllib.parse.urlunsplit((origin.scheme, origin.netloc, endpoint, '', '')),
                                         headers={'Authorization': 'Bearer ' + secret})
        with opener.open(request, timeout=3) as response:
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError('INVALID_BRIDGE_HEALTH')
            return json.loads(body)

    snapshot = read('/v1/status')
    try:
        read('/v1/health/ready')
        snapshot['ready'] = True
    except urllib.error.HTTPError as error:
        if error.code != 503:
            raise
        snapshot['ready'] = False
    return snapshot


def database_probe():
    """Indexed, read-only delivery counts. No message content leaves MariaDB."""
    import pymysql
    host = os.environ.get('DB_HOST', '127.0.0.1')
    if host not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('EXPECTED_LOCAL_SUPPORT_DATABASE')
    connection = pymysql.connect(host=host, port=int(os.environ.get('DB_PORT', '3306')),
        user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'], database=os.environ['DB_NAME'],
        connect_timeout=2, read_timeout=3, write_timeout=2, autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT send_status, COUNT(*) FROM support_ticket_messages
                WHERE send_status IN ('UNCERTAIN','DISPATCHING','PENDING') AND direction='OUTBOUND'
                GROUP BY send_status""")
            return dict(cursor.fetchall())
    finally:
        connection.close()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('INVALID_DIAGNOSTIC_VALUE')
    return value


def bridge_metrics(snapshot, tcp=None):
    tcp = tcp or {}
    lines = snapshot['lines']
    if not isinstance(lines, list) or not 0 <= len(lines) <= 128:
        raise ValueError('INVALID_BRIDGE_LINES')
    values = {'support_bridge_ready': int(snapshot['ready']), 'support_bridge_lines': len(lines),
        'support_bridge_connected_lines': sum(line.get('status') == 'connected' for line in lines),
        'support_bridge_paired_lines': sum(bool(line.get('is_paired')) for line in lines),
        'support_bridge_qr_codes': sum(bool(line.get('qr')) for line in lines)}
    eligible = [line for line in lines if line.get('eligible', True) is True]
    values.update(support_bridge_eligible_lines=len(eligible),
        support_bridge_eligible_paired_lines=sum(bool(line.get('is_paired')) for line in eligible),
        support_bridge_fenced_lines=sum(str(line.get('lifecycle', '')).startswith('fenced_') for line in lines),
        support_bridge_fenced_unresolved_lines=sum(line.get('lifecycle') == 'fenced_unresolved' for line in lines),
        support_bridge_decommissioned_lines=sum(line.get('lifecycle') == 'decommissioned' for line in lines),
        support_bridge_eligible_storage_critical=int(snapshot.get('runtime', {}).get('eligible_storage_critical') is not False))
    diagnostics_ok = bool(lines)
    identities = set()
    for line in lines:
        raw_id = line.get('line_id')
        if not isinstance(raw_id, str) or not raw_id.isdigit() or not 5 <= len(raw_id) <= 32 or raw_id in identities:
            raise ValueError('INVALID_BRIDGE_IDENTITY')
        identities.add(raw_id)
        identity = hashlib.sha256(raw_id.encode()).hexdigest()[:16]
        labels = 'identity="' + identity + '"'
        values['support_bridge_identity_connected{' + labels + '}'] = int(line.get('status') == 'connected')
        retained = line.get('lifecycle') in {'fenced_retained', 'decommissioned'} and line.get('isolation', {}).get('safe') is True
        values['support_bridge_identity_decommissioned{' + labels + '}'] = int(line.get('lifecycle') == 'decommissioned')
        values['support_bridge_identity_fenced{' + labels + '}'] = int(str(line.get('lifecycle', '')).startswith('fenced_'))
        values['support_bridge_identity_retained{' + labels + '}'] = int(retained)
        # Fenced identities still expose callback and uncertainty backlogs even
        # though starting a transport to obtain heartbeat metrics is forbidden.
        for metric, field in [('callback_pending', 'pending_callbacks'), ('callback_oldest_age_seconds', 'oldest_callback_age_seconds'),
                ('callback_waiting_history', 'waiting_history_reactions'), ('callback_dead_letters', 'failed_callbacks'),
                ('uncertain_sends', 'uncertain_sends')]:
            values['support_bridge_' + metric + '{' + labels + '}'] = number(line[field])
        diagnostic = line.get('diagnostics') or {}
        supported = diagnostic.get('supported') is True and diagnostic.get('version') == 2
        diagnostics_ok = diagnostics_ok and (supported or retained)
        values['support_bridge_identity_diagnostics_up{' + labels + '}'] = int(supported)
        if not supported:
            continue
        fields = {
            'disconnects_total': diagnostic['disconnects_total'],
            'reconnect_attempts_total': diagnostic['reconnect_attempts'],
            'recovery_seconds_sum': diagnostic['recovery_seconds_sum'],
            'recovery_count': diagnostic['recovery_count'],
            'recovery_seconds_last': diagnostic['recovery_seconds_last'],
            'generation': diagnostic['generation'],
            'generation_start_timestamp_seconds': diagnostic['generation_started_at'] / 1000,
            'heartbeat_requests_total': diagnostic['heartbeat_requests'],
            'heartbeat_successes_total': diagnostic['heartbeat_successes'],
            'heartbeat_query_timeouts_total': diagnostic['heartbeat_query_timeouts'],
            'heartbeat_request_timestamp_seconds': (diagnostic['heartbeat_request_at'] or 0) / 1000,
            'heartbeat_query_completed_timestamp_seconds': (diagnostic['heartbeat_query_completed_at'] or 0) / 1000,
            'heartbeat_success_timestamp_seconds': (diagnostic['heartbeat_success_at'] or 0) / 1000,
            'raw_frames_total': diagnostic['raw_frames'],
            'decoded_frames_total': diagnostic['decoded_frames'],
            'raw_frame_timestamp_seconds': (diagnostic['last_raw_frame_at'] or 0) / 1000,
            'decoded_frame_timestamp_seconds': (diagnostic['last_frame_at'] or 0) / 1000,
            'heartbeat_errors_total': diagnostic['heartbeat_failures'],
            'heartbeat_timeouts_total': diagnostic['heartbeat_timeouts'],
            'heartbeat_frame_age_seconds': diagnostic['heartbeat_frame_age_ms'] / 1000,
            'heartbeat_timer_delay_seconds': diagnostic['heartbeat_timer_delay_ms'] / 1000,
            'heartbeat_sample_timestamp_seconds': (diagnostic['heartbeat_observed_at'] or 0) / 1000,
            'callback_pending': line['pending_callbacks'],
            'callback_oldest_age_seconds': line['oldest_callback_age_seconds'],
            'callback_waiting_history': line['waiting_history_reactions'],
            'callback_dead_letters': line['failed_callbacks'],
            'uncertain_sends': line['uncertain_sends'],
        }
        transport = diagnostic.get('transport') or {}
        family = transport.get('family')
        if family in {'IPv4', 'IPv6', 'unknown'}:
            values['support_bridge_transport_family{' + labels + ',family="' + family + '"}'] = 1
        configured_family = diagnostic.get('configured_ip_family')
        if type(configured_family) is int and configured_family in (0, 4):
            fields['configured_ip_family'] = configured_family
        if type(transport.get('tls_authorized')) is bool:
            fields['transport_tls_authorized'] = int(transport['tls_authorized'])
        sample = tcp.get(raw_id)
        fields['tcp_sample_up'] = int(sample is not None)
        if sample is not None:
            # These are per-socket gauges: use the generation metric when
            # comparing samples. A replacement connection resets TCP counters.
            for name in ('receive_queue_bytes', 'send_queue_bytes', 'retransmitted_segments',
                         'retransmitted_bytes', 'rtt_ms', 'rto_ms', 'bytes_received', 'bytes_acked',
                         'bytes_sent', 'segments_in', 'data_segments_in', 'unacked_segments',
                         'last_receive_ms', 'last_ack_receive_ms', 'send_window_bytes',
                         'sample_started_timestamp_seconds', 'sample_timestamp_seconds'):
                if name in sample:
                    fields['tcp_' + name] = sample[name]
        if diagnostic.get('heartbeat_query_elapsed_ms') is not None:
            fields['heartbeat_query_elapsed_seconds'] = diagnostic['heartbeat_query_elapsed_ms'] / 1000
        if diagnostic.get('heartbeat_rtt_ms') is not None:
            fields['heartbeat_rtt_seconds'] = diagnostic['heartbeat_rtt_ms'] / 1000
        for name, value in fields.items():
            values['support_bridge_' + name + '{' + labels + '}'] = number(value)
        for category in CATEGORIES:
            values['support_bridge_disconnect_reason_total{' + labels + ',reason="' + category + '"}'] = number(
                diagnostic['disconnects'].get(category, 0))
    loop = snapshot.get('event_loop') or {}
    if loop.get('sampled_at') is None:
        diagnostics_ok = False
    else:
        values['support_bridge_event_loop_sample_timestamp_seconds'] = number(loop['sampled_at']) / 1000
        values['support_bridge_event_loop_lag_p99_seconds'] = number(loop['p99_ms']) / 1000
        values['support_bridge_event_loop_lag_max_seconds'] = number(loop['max_ms']) / 1000
        values['support_bridge_event_loop_lag_spikes_total'] = number(loop['lag_spikes_total'])
    values['support_bridge_diagnostics_up'] = int(diagnostics_ok)
    return values


def collect(client, *, probe=api_probe, bridge=bridge_probe, database=database_probe, now=time.time, tcp=tcp_samples):
    values = {'support_runtime_check_timestamp_seconds': now()}
    try:
        expected = int(os.environ.get('WHATSAPP_EXPECTED_IDENTITIES', '8'))
        if not 1 <= expected <= 128:
            raise ValueError('INVALID_EXPECTED_IDENTITIES')
    except ValueError:
        expected = 0  # Invalid configuration must alarm, not match an empty bridge.
    values['support_bridge_expected_lines'] = expected
    try:
        values['support_api_up'] = int(probe())
    except Exception:
        values['support_api_up'] = 0
    try:
        snapshot = bridge()
        # Retain operational health even if optional diagnostics are malformed.
        lines = snapshot['lines']
        values.update(support_bridge_ready=int(snapshot['ready']), support_bridge_lines=len(lines),
            support_bridge_connected_lines=sum(line.get('status') == 'connected' for line in lines),
            support_bridge_paired_lines=sum(bool(line.get('is_paired')) for line in lines),
            support_bridge_qr_codes=sum(bool(line.get('qr')) for line in lines))
        try:
            try:
                samples = tcp(lines)
            except Exception:
                samples = {}
            values.update(bridge_metrics(snapshot, samples))
        except Exception:
            values['support_bridge_diagnostics_up'] = 0
    except Exception:
        values.update(support_bridge_ready=0, support_bridge_diagnostics_up=0)
    try:
        counts = database()
        values['support_delivery_database_check_up'] = 1
        for status in ('UNCERTAIN', 'DISPATCHING', 'PENDING'):
            values['support_database_' + status.lower() + '_deliveries'] = number(counts.get(status, 0))
    except Exception:
        values['support_delivery_database_check_up'] = 0
    try:
        counts = inspect(client)
        values['support_redis_up'] = 1
        for key, value in counts.items():
            values[f'support_inbound_{key}'] = value
    except Exception:
        values['support_redis_up'] = 0
    return ''.join(f'{key} {value}\n' for key, value in values.items())


def main():
    import redis
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--socket', default='/run/redis/redis-server.sock')
    args = parser.parse_args()
    client = redis.Redis(unix_socket_path=args.socket, socket_timeout=5)
    try:
        content = collect(client)
        temporary = args.output.with_suffix('.pending')
        with temporary.open('w') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.output)
    finally:
        client.close()


if __name__ == '__main__':
    main()
