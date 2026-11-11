#!/usr/bin/env python3
"""Export aggregate storage health without reading customer records or credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import time


def allocated(root: Path) -> tuple[int, int, int]:
    total = journal = snapshot = 0
    seen = set()
    for directory, directories, files in os.walk(root, followlinks=False, onerror=lambda error: (_ for _ in ()).throw(error)):
        directories[:] = [name for name in directories if not (Path(directory) / name).is_symlink()]
        for name in files:
            info = (Path(directory) / name).lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            # Sizes are per name: a hard link listed first must not hide them.
            if name.endswith('.journal'):
                journal = max(journal, info.st_size)
            if name.endswith('.snapshot'):
                snapshot = max(snapshot, info.st_size)
            if (info.st_dev, info.st_ino) in seen:
                continue
            seen.add((info.st_dev, info.st_ino))
            total += info.st_blocks * 512
    return total, journal, snapshot


def collect(paths: dict[str, Path], watchdog: Path, receipt: Path) -> str:
    lines = [f'migration_storage_scan_timestamp_seconds {time.time()}']
    for label, root in paths.items():
        try:
            if not root.is_dir():
                raise FileNotFoundError()
            total, journal, snapshot = allocated(root)
            lines.extend([f'application_storage_bytes{{application="{label}"}} {total}',
                          f'application_storage_scan_ok{{application="{label}"}} 1'])
            if label == 'bridge':
                lines.extend([f'bridge_largest_journal_bytes {journal}',
                              f'bridge_largest_snapshot_bytes {snapshot}'])
        except OSError:
            lines.append(f'application_storage_scan_ok{{application="{label}"}} 0')
    try:
        health = json.loads(watchdog.read_text())
        level = {'ok': 0, 'warning': 1, 'critical': 2}[health['level']]
        lines.extend([f'bridge_storage_health_level {level}',
                      f'bridge_watchdog_timestamp_seconds {float(health["checked_at"])}',
                      f'bridge_storage_checkpoint_failed {int(any(item.get("code") == "COMPACTION_FAILED" for item in health["issues"]))}'])
    except (OSError, ValueError, KeyError, TypeError):
        lines.append('bridge_watchdog_timestamp_seconds 0')
    try:
        backup = json.loads(receipt.read_text())
        verified = float(backup['restored_at'])
        if verified <= 0 or verified > time.time() or not backup['verified']:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError):
        verified = 0
    lines.append(f'local_backup_last_restore_verified_timestamp_seconds {verified}')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--state', type=Path, default=Path('/var/lib/whatsapp-support'))
    parser.add_argument('--database', type=Path, default=Path('/var/lib/mysql'))
    parser.add_argument('--releases', type=Path, default=Path('/opt/whatsapp-support/releases'))
    parser.add_argument('--watchdog', type=Path, default=Path('/run/whatsapp-storage-watchdog/status.json'))
    parser.add_argument('--receipt', type=Path, required=True, help='Existing verified local restore receipt; no backup is created by this collector')
    args = parser.parse_args()
    content = collect({'bridge': args.state / 'linked-device', 'media': args.state / 'support-media',
                       'database': args.database, 'releases': args.releases}, args.watchdog, args.receipt)
    temporary = args.output.with_suffix('.pending')
    with temporary.open('w') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, args.output)


if __name__ == '__main__':
    main()
