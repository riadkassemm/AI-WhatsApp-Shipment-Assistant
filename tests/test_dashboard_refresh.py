"""Exercise the shipped scheduler and API singleflight with a deterministic clock."""
import subprocess


def test_dashboard_refresh_regressions():
    result = subprocess.run(['node', '--test', 'tests/dashboard_refresh.test.cjs'],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
