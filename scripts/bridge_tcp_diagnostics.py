"""Passive, bounded TCP_INFO snapshots for the bridge's existing sockets only."""
from __future__ import annotations

import ipaddress
import re
import subprocess
import time
from typing import Any, Callable


def endpoint(value: str) -> tuple[str, int]:
    host, port = value.rsplit(':', 1)
    return str(ipaddress.ip_address(host.strip('[]'))), int(port)


def tcp_samples(lines: list[dict[str, Any]], run: Callable[..., Any] = subprocess.run,
                now: Callable[[], float] = time.time) -> dict[str, dict[str, float]]:
    """No probes, packet capture, process listing, payloads or socket changes.

    Missing permissions/ss/connections return no evidence, never zero loss.
    Endpoint tuples are used privately for matching, never metric labels.
    """
    targets = {}
    for line in lines:
        transport = (line.get('diagnostics') or {}).get('transport') or {}
        try:
            local = transport['local_port']
            remote = transport['remote_port']
            if type(local) is not int or type(remote) is not int or not 1 <= local <= 65535 or not 1 <= remote <= 65535:
                continue
            address = str(ipaddress.ip_address(transport['remote_ip']))
            targets[(local, address, remote)] = line['line_id']
        except (KeyError, ValueError, TypeError):
            continue
    if not targets or len(targets) > 128:
        return {}
    ports = sorted({item[0] for item in targets})
    command = ['ss', '-tinHO', 'state', 'established', '(',
               *' or '.join(f'sport = :{port}' for port in ports).split(), ')']
    try:
        started_at = now()
        result = run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     text=True, timeout=2, check=True)
        sampled_at = now()
        if len(result.stdout) > 262144:
            return {}
    except (OSError, subprocess.SubprocessError):
        return {}
    samples = {}
    for row in result.stdout.splitlines():
        try:
            # With an explicit state filter, ss omits the State column.
            fields = row.split()
            receive_queue, send_queue = int(fields[0]), int(fields[1])
            _, local_port = endpoint(fields[2])
            remote_ip, remote_port = endpoint(fields[3])
            identity = targets.get((local_port, remote_ip, remote_port))
            if not identity or min(receive_queue, send_queue) < 0:
                continue
            info = ' '.join(fields[4:])
            sample = {'receive_queue_bytes': receive_queue, 'send_queue_bytes': send_queue,
                      'sample_started_timestamp_seconds': started_at, 'sample_timestamp_seconds': sampled_at}
            patterns = {'retransmitted_segments': r'\bretrans:\d+/(\d+)\b',
                        'retransmitted_bytes': r'\bbytes_retrans:(\d+)\b',
                        'bytes_received': r'\bbytes_received:(\d+)\b',
                        'bytes_acked': r'\bbytes_acked:(\d+)\b',
                        'bytes_sent': r'\bbytes_sent:(\d+)\b',
                        'segments_in': r'\bsegs_in:(\d+)\b',
                        'data_segments_in': r'\bdata_segs_in:(\d+)\b',
                        'unacked_segments': r'\bunacked:(\d+)\b',
                        'last_receive_ms': r'\blastrcv:(\d+)\b',
                        'last_ack_receive_ms': r'\blastack:(\d+)\b',
                        'send_window_bytes': r'\bsnd_wnd:(\d+)\b',
                        'rtt_ms': r'\brtt:(\d+(?:\.\d+)?)/', 'rto_ms': r'\brto:(\d+(?:\.\d+)?)\b'}
            for name, pattern in patterns.items():
                match = re.search(pattern, info)
                if match:
                    sample[name] = float(match[1])
            samples[identity] = sample
        except (ValueError, IndexError):
            continue
    return samples
