"""The dashboard marks customer messages received well after they were sent."""
import os
from pathlib import Path
import subprocess

STATE = Path('app/static/support/state.js').read_text()
HELPERS = STATE[STATE.index('const NAIVE_SERVER_TIMESTAMP'):STATE.index('function fmt(')]


def test_arrived_late_rule_uses_send_and_receipt_times():
    script = HELPERS + r'''
const assert = require('node:assert/strict');
const sent = Date.UTC(2026, 9, 29, 10, 48, 0) / 1000;
const at = minutes => new Date((sent + minutes * 60) * 1000).toISOString().replace('T', ' ').slice(0, 19);
const inbound = (minutes, extra = {}) => ({direction: 'INBOUND', whatsapp_timestamp: String(sent), created_at: at(minutes), ...extra});
assert.equal(messageArrivedLate(inbound(14)), false);
assert.equal(messageArrivedLate(inbound(15)), true);
assert.equal(messageArrivedLate(inbound(29)), true);
// Outgoing, missing or unusable times are never labelled.
assert.equal(messageArrivedLate(inbound(29, {direction: 'OUTBOUND'})), false);
// Without a send time, receipt time is used for both: never labelled, in any browser timezone.
assert.equal(messageArrivedLate(inbound(29, {whatsapp_timestamp: null})), false);
assert.equal(messageArrivedLate(inbound(29, {created_at: null})), false);
assert.equal(messageArrivedLate(inbound(29, {created_at: 'not a date'})), false);
// Shown at its send time, not its receipt time.
assert.equal(messageOccurredAt(inbound(29)), new Date(sent * 1000).toISOString());
'''
    # Staff browsers run in Asia/Beirut; naive server times must still be read as UTC.
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10,
                            env={**os.environ, 'TZ': 'Asia/Beirut'})
    assert result.returncode == 0, result.stderr

