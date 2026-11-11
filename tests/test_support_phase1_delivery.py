"""Run the dashboard delivery-state regressions (message merge and send queue)."""
import subprocess


def test_support_delivery_state_regressions():
    result = subprocess.run(['node', '--test', 'tests/support_phase1_delivery.test.cjs'],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
