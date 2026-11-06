from pathlib import Path
import json
import os

from scripts.export_storage_metrics import collect


def test_metrics_do_not_follow_links_or_count_hardlinks_twice(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    journal = state / 'secret-phone.journal'
    journal.write_bytes(b'private customer state' * 1000)
    os.link(journal, state / 'duplicate')
    (state / 'outside').symlink_to(tmp_path, target_is_directory=True)
    output = collect({'bridge': state}, tmp_path / 'missing', tmp_path / 'missing')
    assert f'application_storage_bytes{{application="bridge"}} {journal.stat().st_blocks * 512}' in output
    assert f'bridge_largest_journal_bytes {journal.stat().st_size}' in output
    assert 'secret' not in output and 'customer' not in output
    assert 'bridge_watchdog_timestamp_seconds 0' in output


def test_metrics_report_missing_paths_and_require_verified_backup(tmp_path):
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'restored_at': 123, 'verified': False}))
    output = collect({'database': tmp_path / 'absent'}, tmp_path / 'missing', receipt)
    assert 'application_storage_scan_ok{application="database"} 0' in output
    assert 'local_backup_last_restore_verified_timestamp_seconds 0' in output
    receipt.write_text(json.dumps({'restored_at': 123, 'verified': True}))
    assert 'local_backup_last_restore_verified_timestamp_seconds 123.0' in collect({}, tmp_path / 'missing', receipt)
