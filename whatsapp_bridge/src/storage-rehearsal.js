// Private copies only. This tool never connects a real WhatsApp transport.
// Each replay runs in a separate process so before/after peak RSS is comparable.
import { lstat, readdir, readFile, realpath } from 'node:fs/promises';
import { fork } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { performance } from 'node:perf_hooks';
import { createHash } from 'node:crypto';
import { EventEmitter } from 'node:events';
import path from 'node:path';
import { acquireDataDirectory, atomicWrite } from './storage.js';
import { JournalStore, readJournal, encode } from './journal-store.js';
import { validateLineManifest } from './config.js';

const MARKER = '.storage-rehearsal-copy';
const MARKER_CONTENT = 'WHATSAPP_STORAGE_REHEARSAL_COPY\n';
const BASELINE = '.storage-rehearsal-baseline.json';
const REPORT = '.storage-rehearsal-report.json';
const fail = code => { throw Object.assign(new Error(code), { code }); };
export const digest = value => createHash('sha256').update(encode(value)).digest('hex');
const peakRss = () => process.resourceUsage().maxRSS * 1024;
const safeCode = error => {
  const value = error?.code ?? error?.message;
  return typeof value === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(value) ? value : 'REHEARSAL_FAILED';
};

export async function verifyRehearsalCopy(directory) {
  const resolved = path.resolve(directory);
  const temporary = await realpath('/tmp');
  if (!resolved.startsWith(temporary + path.sep) || await realpath(resolved) !== resolved) fail('REHEARSAL_PRIVATE_TMP_COPY_REQUIRED');
  const root = await lstat(resolved);
  if (!root.isDirectory() || (root.mode & 0o077)) fail('REHEARSAL_PRIVATE_DIRECTORY_REQUIRED');
  const marker = await lstat(path.join(resolved, MARKER)).catch(() => fail('REHEARSAL_MARKER_REQUIRED'));
  if (!marker.isFile() || marker.nlink !== 1 || (marker.mode & 0o077) ||
      await readFile(path.join(resolved, MARKER), 'utf8') !== MARKER_CONTENT) fail('REHEARSAL_MARKER_INVALID');
  const live = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..',
    process.env.WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY ?? 'whatsapp_bridge/data');
  if (resolved === live || resolved.startsWith(live + path.sep)) fail('REHEARSAL_LIVE_DIRECTORY_FORBIDDEN');
  // A copied tree must not contain aliases back to the backup or live state.
  async function walk(current) {
    for (const entry of await readdir(current, { withFileTypes: true })) {
      const filename = path.join(current, entry.name);
      const info = await lstat(filename);
      if (info.isSymbolicLink() || (!info.isFile() && !info.isDirectory()) ||
          (info.isFile() && info.nlink !== 1)) fail('REHEARSAL_ALIASED_FILE_FORBIDDEN');
      if (info.mode & 0o077) fail('REHEARSAL_PRIVATE_FILES_REQUIRED');
      if (info.isDirectory()) await walk(filename);
    }
  }
  await walk(resolved);
  return resolved;
}

export async function layout(directory, expectedLines) {
  const manifest = validateLineManifest(JSON.parse(await readFile(path.join(directory, 'lines.json'), 'utf8')));
  const ids = Object.keys(manifest.lines).sort();
  if (ids.length !== expectedLines) fail('REHEARSAL_LINE_COUNT_MISMATCH');
  const stores = [{ relative: 'outbox.json', label: 'shared/outbox', kind: 'outbox' }];
  for (const [index, id] of ids.entries()) {
    for (const kind of ['auth', 'state']) stores.push({ relative: path.join(id, kind + '.json'), label: `line_${index + 1}/${kind}`, kind });
  }
  for (const store of stores) if (!(await lstat(path.join(directory, store.relative))).isFile()) fail('REHEARSAL_STORE_MISSING');
  return { manifest, stores };
}

export function summary(data) {
  const messages = Object.values(data.messages ?? {});
  return {
    messages: messages.length,
    pending_sends: messages.filter(record => record.pending_send).length,
    uncertain_sends: messages.filter(record => record.delivery_uncertain).length,
    pending_or_uncertain_sends: messages.filter(record => record.pending_send || record.delivery_uncertain).length,
    pending_placeholders: messages.filter(record => record.pending_placeholder).length,
    pending_normalization: messages.filter(record => record.pending_normalization).length,
    operations: Object.keys(data.operations ?? {}).length,
    send_requests: Object.keys(data.send_requests ?? {}).length,
    callback_entries: (data.entries ?? []).length,
    dead_letters: (data.dead_letters ?? []).length,
  };
}

// Restart legitimately converts interrupted submissions into uncertain sends.
// All remaining message/delivery/idempotency fields must stay byte-equivalent.
export function deliveryFingerprint(data, afterRestart = false) {
  const messages = Object.fromEntries(Object.entries(data.messages ?? {}).map(([id, record]) =>
    [id, afterRestart && record.pending_send ? { ...record, pending_send: false, delivery_uncertain: true } : record]));
  return digest({ messages, media: data.media ?? {}, operations: data.operations ?? {}, send_requests: data.send_requests ?? {} });
}

async function inspectStore(filename) {
  const started = performance.now();
  const restored = await readJournal(filename, () => fail('REHEARSAL_INITIAL_STATE_MISSING'));
  const replayMs = performance.now() - started;
  const replayPeak = peakRss(), replayRss = process.memoryUsage().rss;
  const bytes = async suffix => {
    try { return (await lstat(filename + suffix)).size; }
    catch (error) { if (error.code === 'ENOENT') return 0; throw error; }
  };
  return {
    sequence: restored.seq,
    legacy_bytes: await bytes(''), snapshot_bytes: await bytes('.snapshot'), journal_bytes: await bytes('.journal'),
    journal_records: restored.journalRecords, incomplete_tail_bytes: restored.incompleteTailBytes ?? 0,
    replay_ms: Math.round(replayMs), replay_rss_bytes: replayRss, replay_peak_rss_bytes: replayPeak,
    counts: summary(restored.data), fingerprint: digest(restored.data),
    delivery_fingerprint: deliveryFingerprint(restored.data),
    restarted_delivery_fingerprint: deliveryFingerprint(restored.data, true),
  };
}

function child(mode, directory, argument) {
  return new Promise((resolve, reject) => {
    const process = fork(fileURLToPath(import.meta.url), ['--worker', mode, directory, String(argument)],
      { stdio: ['ignore', 'ignore', 'ignore', 'ipc'] });
    let result;
    process.on('message', message => { result = message; });
    process.on('error', () => reject(new Error('REHEARSAL_WORKER_FAILED')));
    process.on('exit', code => {
      if (code !== 0 || !result?.ok) reject(Object.assign(new Error(result?.code ?? 'REHEARSAL_WORKER_FAILED'), { code: result?.code ?? 'REHEARSAL_WORKER_FAILED' }));
      else resolve(result.result);
    });
  });
}

export async function inspectAll(directory, stores) {
  const results = [];
  for (const store of stores) results.push({ ...store, ...await child('inspect', directory, store.relative) });
  return results;
}

function matching(before, after) {
  if (before.length !== after.length || before.some((store, index) => store.relative !== after[index].relative ||
      store.sequence !== after[index].sequence || store.fingerprint !== after[index].fingerprint)) fail('REHEARSAL_STATE_CHANGED');
}

const publicStore = ({ label, kind, sequence, legacy_bytes, snapshot_bytes, journal_bytes, journal_records,
  incomplete_tail_bytes, replay_ms, replay_rss_bytes, replay_peak_rss_bytes, counts }) =>
  ({ store: label, kind, sequence, legacy_bytes, snapshot_bytes, journal_bytes, journal_records,
    incomplete_tail_bytes, replay_ms, replay_rss_bytes, replay_peak_rss_bytes, counts });

export async function blockNetwork() {
  let networkAttempts = 0;
  const deny = () => { networkAttempts++; fail('REHEARSAL_NETWORK_FORBIDDEN'); };
  const [net, tls, http, https, dgram, dns, module] = await Promise.all([
    import('node:net'), import('node:tls'), import('node:http'), import('node:https'), import('node:dgram'), import('node:dns'), import('node:module'),
  ]);
  net.default.Socket.prototype.connect = deny;
  net.default.connect = net.default.createConnection = deny;
  tls.default.connect = deny;
  http.default.request = http.default.get = deny;
  https.default.request = https.default.get = deny;
  dgram.default.createSocket = deny;
  dns.default.lookup = dns.default.resolve = deny;
  dns.default.promises.lookup = dns.default.promises.resolve = deny;
  globalThis.fetch = deny;
  globalThis.WebSocket = class { constructor() { deny(); } };
  module.syncBuiltinESMExports();
  return () => networkAttempts;
}

async function reconnectCopy(directory, expectedLines) {
  const { manifest } = await layout(directory, expectedLines);
  const networkAttempts = await blockNetwork();
  const { installLibsignalConsoleFilter } = await import('./safe-console.js');
  installLibsignalConsoleFilter();
  const { Bridge } = await import('./bridge.js');
  let sends = 0, providerCalls = 0;
  const providerForbidden = () => { providerCalls++; fail('REHEARSAL_PROVIDER_CALL_FORBIDDEN'); };
  const sendForbidden = () => { sends++; fail('REHEARSAL_SEND_FORBIDDEN'); };
  const errors = [];
  const bridge = new Bridge({ dataDir: directory, lines: [], ackTimeoutMs: 30,
    callbackUrl: 'http://127.0.0.1/rehearsal-never-requested',
    storage: { maintenance: { enabled: false }, monitoring: {} } }, {
    fetch: providerForbidden, downloadMediaMessage: providerForbidden,
    log: () => {}, onError: error => { errors.push(safeCode(error)); },
    makeSocket: ({ auth }) => {
      if (!auth.creds.me?.id || !(auth.creds.registered || auth.creds.me.id)) fail('REHEARSAL_UNPAIRED_SESSION');
      const ws = new EventEmitter();
      ws.isOpen = true;
      // Shutdown requires proof that the transport closed (the patched provider
      // exposes bridgeTransportClosed). This mock proves it only once ended.
      let transportClosed;
      const bridgeTransportClosed = new Promise(resolve => { transportClosed = resolve; });
      return {
        ws, user: { id: auth.creds.me.id }, ev: { process: () => () => {} }, bridgeTransportClosed,
        end: () => { ws.isOpen = false; transportClosed(); },
        sendMessage: sendForbidden, relayMessage: sendForbidden, query: providerForbidden,
        logout: providerForbidden, onWhatsApp: providerForbidden,
        fetchMessageHistory: providerForbidden, requestPlaceholderResend: providerForbidden,
      };
    },
  });
  const started = performance.now();
  let result;
  try {
    await bridge.initialize({ autoConnect: false });
    if (bridge.lines.size !== expectedLines || errors.length) fail('REHEARSAL_INITIALIZATION_FAILED');
    for (const id of Object.keys(manifest.lines)) {
      const line = bridge.getLine(id);
      if (!bridge.lineStatus(line).is_paired) fail('REHEARSAL_UNPAIRED_SESSION');
      await bridge.connect(id);
      await bridge.connectionUpdate(line, line.socket, { connection: 'open' });
    }
    const statuses = bridge.status().lines;
    if (statuses.length !== expectedLines || statuses.some(status => status.status !== 'connected' || !status.is_paired || status.qr) ||
        sends || providerCalls || networkAttempts() || errors.length) fail('REHEARSAL_RECONNECT_VERIFICATION_FAILED');
    result = { mock_connected_lines: statuses.length, paired_lines: statuses.filter(status => status.is_paired).length,
      qr_codes: 0, sends, provider_calls: providerCalls, network_attempts: networkAttempts(),
      reconnect_ms: Math.round(performance.now() - started), peak_rss_bytes: peakRss() };
  } finally { await bridge.close(); }
  if (sends || providerCalls || networkAttempts() || errors.length) fail('REHEARSAL_SHUTDOWN_VERIFICATION_FAILED');
  return result;
}

export async function storageRehearsal(mode, input, expectedLines = 8) {
  if (!['inspect', 'compact', 'reconnect'].includes(mode) || !Number.isSafeInteger(expectedLines) || expectedLines < 1) fail('REHEARSAL_INVALID_ARGUMENT');
  const directory = await verifyRehearsalCopy(input);
  const release = await acquireDataDirectory(directory);
  try {
    const { stores } = await layout(directory, expectedLines);
    const before = await inspectAll(directory, stores);
    let baseline;
    try { baseline = JSON.parse(await readFile(path.join(directory, BASELINE), 'utf8')); }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      baseline = { version: 1, expected_lines: expectedLines, stores: before };
      await atomicWrite(path.join(directory, BASELINE), baseline);
    }
    if (baseline.version !== 1 || baseline.expected_lines !== expectedLines) fail('REHEARSAL_BASELINE_INVALID');
    matching(baseline.stores, before);
    if (mode === 'inspect') return { mode, lines: expectedLines, stores: before.map(publicStore) };
    if (before.some(store => store.incomplete_tail_bytes)) fail('REHEARSAL_INCOMPLETE_TAIL_REQUIRES_REVIEW');
    let verification;
    if (mode === 'compact') {
      const compactions = [];
      for (const store of stores) compactions.push({ store: store.label, ...await child('compact', directory, store.relative) });
      verification = { compactions };
    } else verification = await child('reconnect', directory, expectedLines);
    const after = await inspectAll(directory, stores);
    if (mode === 'compact') matching(before, after);
    else {
      for (const [index, store] of before.entries()) {
        const restored = after[index];
        if (store.kind !== 'state') {
          if (store.fingerprint !== restored.fingerprint) fail('REHEARSAL_AUTH_OR_OUTBOX_CHANGED');
        } else if (store.restarted_delivery_fingerprint !== restored.delivery_fingerprint ||
          restored.counts.pending_sends !== 0 || restored.counts.uncertain_sends !== store.counts.pending_or_uncertain_sends) fail('REHEARSAL_DELIVERY_STATE_CHANGED');
      }
      verification.interrupted_sends_reclassified_uncertain = before.reduce((sum, store) => sum + store.counts.pending_sends, 0);
    }
    const report = { mode, lines: expectedLines, state_preserved: true, verification,
      before: before.map(publicStore), after: after.map(publicStore) };
    await atomicWrite(path.join(directory, REPORT), report);
    return report;
  } finally { await release(); }
}

async function worker(mode, input, argument) {
  const directory = await verifyRehearsalCopy(input);
  if (mode === 'reconnect') return reconnectCopy(directory, Number(argument));
  if (!/^(outbox\.json|\d{5,32}\/(auth|state)\.json)$/.test(argument)) fail('REHEARSAL_STORE_ARGUMENT_INVALID');
  const filename = path.join(directory, argument);
  if (mode === 'inspect') return inspectStore(filename);
  if (mode !== 'compact') fail('REHEARSAL_WORKER_MODE_INVALID');
  const before = await inspectStore(filename);
  if (before.incomplete_tail_bytes) fail('REHEARSAL_INCOMPLETE_TAIL_REQUIRES_REVIEW');
  let finalPauseMs = null;
  const store = await new JournalStore(filename, () => fail('REHEARSAL_INITIAL_STATE_MISSING'), {
    maintenance: { enabled: false },
    diagnostics: event => { if (event.event === 'storage_compacted') finalPauseMs = event.final_pause_ms; },
  }).load();
  const started = performance.now();
  try {
    const compacted = await store.compact();
    const compactionMs = Math.round(performance.now() - started);
    const after = await inspectStore(filename);
    if (before.sequence !== after.sequence || before.fingerprint !== after.fingerprint) fail('REHEARSAL_COMPACTION_CHANGED_STATE');
    return { compacted, compaction_ms: compactionMs, final_pause_ms: finalPauseMs, peak_rss_bytes: peakRss() };
  } finally { await store.close(); }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  process.umask(0o077);
  if (process.argv[2] === '--worker') {
    worker(...process.argv.slice(3)).then(result => {
      process.send({ ok: true, result });
      process.disconnect();
    }).catch(error => {
      process.send?.({ ok: false, code: safeCode(error) });
      process.disconnect?.();
      process.exitCode = 1;
    });
  } else {
    const [mode, directory, count] = process.argv.slice(2);
    if (!directory) { console.error('Usage: node src/storage-rehearsal.js inspect|compact|reconnect /tmp/PRIVATE_COPY [EXPECTED_LINES]'); process.exitCode = 1; }
    else storageRehearsal(mode, directory, count === undefined ? 8 : Number(count))
      .then(result => console.log(JSON.stringify(result)))
      .catch(error => { console.error(JSON.stringify({ error: safeCode(error) })); process.exitCode = 1; });
  }
}
