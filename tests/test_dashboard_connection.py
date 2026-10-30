"""Run the shipped live-update reconnection and connection-notice regressions."""
import subprocess


def test_dashboard_connection_regressions():
    result = subprocess.run(['node', '--test', 'tests/dashboard_connection.test.cjs'],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
