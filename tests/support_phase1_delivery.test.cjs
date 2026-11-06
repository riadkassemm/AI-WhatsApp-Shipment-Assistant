const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/static/support/delivery-state.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source + '\nglobalThis.exports = {mergeSupportMessage, sameClientMessageScope, ScopedSupportSendQueue};', context);
const {mergeSupportMessage, sameClientMessageScope, ScopedSupportSendQueue} = context.exports;
const plain = value => JSON.parse(JSON.stringify(value));
const gate = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };

test('stale HTTP snapshots cannot regress receipts or resurrect deleted content', () => {
  const old = {id: 1, version: 1, updated_at: '2026-11-06T20:00:00.000001', body: 'old', send_status: 'SENT', media_url: '/private'};
  const read = {...old, updated_at: '2026-11-06T20:00:00.000002', delivery_status: 'read'};
  assert.equal(mergeSupportMessage(read, old).delivery_status, 'read');
  const edit = {...old, version: 2, body: 'edited', updated_at: '2026-11-06T20:00:01'};
  const mixed = mergeSupportMessage(edit, read);
  assert.equal(mixed.body, 'edited'); assert.equal(mixed.version, 2); assert.equal(mixed.delivery_status, 'read');
  const deleted = {...edit, version: 3, deleted_for_everyone_at: '2026-11-06T20:00:02', body: null};
  delete deleted.media_url;
  for (const pair of [[deleted, read], [read, deleted]]) {
    const merged = mergeSupportMessage(...pair);
    assert.equal(merged.body, null); assert.equal(merged.media_url, undefined); assert.deepEqual(plain(merged.reactions), []);
  }
  assert.equal(mergeSupportMessage({send_status: 'PENDING'}, {send_status: 'CONFIRMATION_PENDING'}).send_status, 'CONFIRMATION_PENDING');
  assert.equal(sameClientMessageScope({client_message_id: 'x', sender_id: 'a', sender_type: 'AGENT'},
    {client_message_id: 'x', sender_id: 'b', sender_type: 'AGENT'}), false);
});

test('one blocked identity does not hold another; same identity retains FIFO and duplicate IDs share work', async () => {
  const queue = new ScopedSupportSendQueue({maxActive: 2});
  const blocked = gate(), other = gate(), calls = [];
  const one = queue.enqueue({id: 'one', conversation: 'a', group: 'office-a'}, async () => { calls.push('one'); await blocked.promise; });
  assert.equal(queue.enqueue({id: 'one', conversation: 'a', group: 'office-a'}, () => { throw Error('duplicate'); }), one);
  const two = queue.enqueue({id: 'two', conversation: 'a', group: 'office-a'}, () => calls.push('two'));
  const three = queue.enqueue({id: 'three', conversation: 'b', group: 'office-b'}, async () => { calls.push('three'); other.resolve(); });
  await other.promise;
  assert.deepEqual(calls, ['one', 'three']);
  blocked.resolve(); await Promise.all([one, two, three]);
  assert.deepEqual(calls, ['one', 'three', 'two']);
});

test('queue capacity is bounded, paused work is retained and logout cancels only unsubmitted work', async () => {
  const queue = new ScopedSupportSendQueue({maxPending: 2, perConversation: 1});
  const active = gate(); let unwanted = 0;
  const first = queue.enqueue({id: 'first', conversation: 'a', group: 'a'}, () => active.promise);
  assert.throws(() => queue.enqueue({id: 'extra', conversation: 'a', group: 'a'}, () => {}), /queue is full/);
  queue.pause();
  const waiting = queue.enqueue({id: 'waiting', conversation: 'b', group: 'b'}, () => unwanted++);
  await Promise.resolve(); assert.equal(unwanted, 0);
  queue.cancelWaiting(); assert.equal(await waiting, false);
  active.resolve(); await first; queue.resume(); assert.equal(unwanted, 0);
});

test('unknown persistence holds only its identity and permits only the original-key retry', async () => {
  const queue = new ScopedSupportSendQueue({maxActive: 1});
  const calls = [];
  queue.hold('a', 'original');
  const later = queue.enqueue({id: 'later', conversation: 'a', group: 'a'}, () => calls.push('later'));
  await assert.rejects(queue.waitForGroup('a'), /needs confirmation/);
  await queue.enqueue({id: 'other', conversation: 'b', group: 'b'}, () => calls.push('other'));
  assert.deepEqual(calls, ['other']);
  await queue.enqueue({id: 'original', conversation: 'a', group: 'a'}, () => {
    calls.push('original'); queue.release('a', 'original');
  });
  await later;
  assert.deepEqual(calls, ['other', 'original', 'later']);
});
