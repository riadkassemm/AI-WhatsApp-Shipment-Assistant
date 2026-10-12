import test from 'node:test';
import assert from 'node:assert/strict';
import SessionRecord from 'libsignal/src/session_record.js';
import queueJob from 'libsignal/src/queue_job.js';
import { installLibsignalConsoleFilter } from '../src/safe-console.js';

test('real libsignal session/key/bucket dumps are suppressed before console sinks while plain diagnostics and bridge logs remain', async t => {
  const captured = [];
  for (const method of ['log', 'info', 'warn', 'error', 'debug', 'dir', 'table', 'trace']) {
    t.mock.method(console, method, (...args) => captured.push({ method, args }));
  }
  const restore = installLibsignalConsoleFilter();
  try {
    const marker = 'REGRESSION-FAKE-PRIVATE-KEY';
    const fakeSession = { indexInfo: { closed: -1 }, currentRatchet: { rootKey: Buffer.from(marker) } };
    const record = new SessionRecord();
    record.closeSession(fakeSession); // console.info with an entire session
    record.closeSession(fakeSession); // console.warn with an already-closed session
    record.openSession(fakeSession); // console.info with an entire session
    record.openSession(fakeSession); // harmless warning plus another object dump
    for (let i = 0; i < 41; i++) record.sessions[`fake-${i}`] = {
      indexInfo: { closed: i + 1, used: i }, currentRatchet: { rootKey: Buffer.from(marker) },
    };
    record.removeOldSessions(); // console.info with the removed old session
    await queueJob({ privateKey: Buffer.from(marker) }, async () => 'done');

    console.log('WhatsApp linked-device bridge listening on 127.0.0.1:3001');
    console.error('Bridge operation failed; check the connection status.');
    assert.ok(captured.some(item => item.args[0] === 'Session already open'));
    assert.ok(captured.some(item => item.args[0] === 'WhatsApp linked-device bridge listening on 127.0.0.1:3001'));
    assert.ok(captured.some(item => item.args[0] === 'Bridge operation failed; check the connection status.'));
    assert.ok(captured.every(item => item.args.every(arg => typeof arg !== 'object')));
    assert.ok(!JSON.stringify(captured).includes(marker));
    assert.ok(!captured.some(item => /Closing session:|Opening session:|Removing old closed session:|Session already closed|Unhandled bucket type/.test(item.args[0])));
  } finally { restore(); }
});
