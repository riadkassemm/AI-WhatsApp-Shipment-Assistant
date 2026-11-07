import subprocess
from types import SimpleNamespace
from scripts.bridge_tcp_diagnostics import tcp_samples


def lines():
    return [{'line_id': str(90001 + index), 'diagnostics': {'transport': {
        'local_port': 34000 + index, 'remote_port': 443, 'remote_ip': address}}}
        for index, address in enumerate(['2001:db8::1', '192.0.2.1'])]


def test_passive_tcp_matching_and_retransmissions_for_both_families():
    def run(command, **kwargs):
        assert command == ['ss', '-tinHO', 'state', 'established', '(', 'sport', '=', ':34000',
                           'or', 'sport', '=', ':34001', ')']
        assert kwargs['timeout'] == 2 and 'shell' not in kwargs
        return SimpleNamespace(stdout='0 7 [2001:db8::2]:34000 [2001:db8::1]:443 cubic rtt:5.2/1.1 rto:201 retrans:0/3 bytes_retrans:120\n'
                                     '0 0 192.0.2.2:34001 192.0.2.1:443 cubic rtt:8.1/2.2 rto:202\n'
                                     '0 0 192.0.2.2:34001 192.0.2.99:443 cubic retrans:0/99\n')
    samples = tcp_samples(lines(), run)
    assert samples['90001']['retransmitted_segments'] == 3
    assert samples['90001']['retransmitted_bytes'] == 120
    assert samples['90002']['rtt_ms'] == 8.1
    assert 'retransmitted_segments' not in samples['90002'], 'missing field is unknown, not zero'
    assert '192.0.2' not in str(samples)


def test_missing_ss_permissions_timeouts_and_invalid_endpoints_are_unknown():
    for error in [FileNotFoundError(), PermissionError(), subprocess.TimeoutExpired('ss', 2)]:
        def fail(*args, **kwargs):
            raise error
        assert tcp_samples(lines(), fail) == {}
    def never(*args, **kwargs):
        raise AssertionError('invalid targets must not execute ss')
    assert tcp_samples([{'line_id': '90001', 'diagnostics': {'transport': {'remote_ip': 'bad'}}}], never) == {}


def test_tcp_receive_ack_progress_and_capture_bounds_are_optional_and_passive():
    times = iter([100.0, 100.25])
    def run(*args, **kwargs):
        return SimpleNamespace(stdout='0 74 [2001:db8::2]:34000 [2001:db8::1]:443 cubic '
            'bytes_sent:1000 bytes_acked:900 bytes_received:2000 segs_in:18 data_segs_in:10 '
            'unacked:1 lastrcv:20000 lastack:21000 snd_wnd:65535\n'
            '0 0 192.0.2.2:34001 192.0.2.1:443 cubic rtt:8/2\n')
    samples = tcp_samples(lines(), run, now=lambda: next(times))
    observed = samples['90001']
    for key, value in {'bytes_sent': 1000, 'bytes_acked': 900, 'bytes_received': 2000,
                       'segments_in': 18, 'data_segments_in': 10, 'unacked_segments': 1,
                       'last_receive_ms': 20000, 'last_ack_receive_ms': 21000,
                       'send_window_bytes': 65535}.items():
        assert observed[key] == value
        assert key not in samples['90002'], 'absent kernel counters remain unknown'
    assert observed['sample_started_timestamp_seconds'] == 100
    assert observed['sample_timestamp_seconds'] == 100.25
