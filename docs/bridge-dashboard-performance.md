# Bridge persistence and dashboard refresh changes

Development changes only. Production activation requires explicit approval.

## Confirmed paths and causes

The API is FastAPI, the dashboard uses classic JavaScript and server-sent events
(SSE), and the linked-device bridge uses Baileys `7.0.0-rc14`. MariaDB stores
support records; the bridge owns private file-based native message/session state.
No framework, dependency, infrastructure, transport timeout, or query index changes
are required.

A staff send travels through `support_service.send_agent_message`,
`send_agent_location`, or `send_agent_media`, `support_repository.add_ticket_message`,
`support_delivery.deliver`, the routed WhatsApp client, the bridge's `/messages`
handler, `Bridge.send`/`dispatchSend`, and Baileys `sendMessage`. Baileys relay
completion is distinct from the server ACK tracked by `AckTracker`. Authenticated
bridge confirmations reconcile the original MariaDB transport ID; recipient
receipts independently establish delivered/read/played state.

Confirmed in the code before this change:

- `storage.js:FileStore.update` copied and serialized the complete account state,
  then rewrote and fsynced it. Reservations, native return values, ACKs, receipts,
  and callback markers each repeated this work. It blocked the shared Node loop.
- `Bridge.send` waited for callback-outbox maintenance after its ACK was already
  durable. History-wide client-ID scans also sat on send/status paths.
- Durable request uniqueness and delivery ownership already existed in MariaDB,
  but duplicate requests did not compare payloads. Bridge deduplication compared
  recipients only. Concurrent receipt updates checked rank before, rather than
  inside, the serialized state change.
- `actions.js` independently polled conversations, Unassigned, counts, and settings.
  Most timers continued in hidden tabs. `events.js` added immediate refreshes;
  queue rendering requested counts again. `queue_counts` loaded one ticket and
  joined read/name/search state for every badge instead of just counting.
- Connection generation fencing, actual WebSocket readiness, bounded ACK/submission
  waits, per-line reconnect backoff and authentication-stop behavior already exist
  and pass the mocked regression suite. No additional connection defect was proven.

These findings support a local processing bottleneck. They do not establish the
external cause of Tripoli/Saida interruptions or rule out future VPS, disk,
network, browser, or staff-device bottlenecks.

## Incremental storage and recovery

`journal-store.js:JournalStore` preserves the callback update API but records only
changed paths in checksummed, sequenced transactions. Each transaction is appended
and fsynced before publishing it in memory. Per-store queues serialize concurrent
updates without copying the messages collection. Accounts have independent state
and auth queues. A stalled account write does not hold another account's queue.
The bounded registration manifest still uses `FileStore` (128 KiB maximum).

Queues reject overload at 256 pending writes and 8 MiB per transaction; per-line
submission queues reject above 64 requests. Writes never silently drop. An append
failure stops further writes to that store until restart/recovery. A complete
record surviving a crash is replayed; an incomplete final record is truncated
before new writes. A bad checksum or sequence gap fails closed. Startup imports
legacy state and rebuilds missing send-index entries in bounded batches, including
messages created by an older release after rollback.

Existing JSON files remain immutable import sources. Active state is now the
legacy file (or latest `.snapshot`) **plus** `.journal`; reading only `state.json`,
`auth.json`, or `outbox.json` is no longer a valid diagnostic/backup strategy.
Back up the complete private directory. The Python routing manifest remains
unchanged. Never start two processes against the same directory; the existing
process lock enforces ownership.

Snapshots/compaction run **offline**, under the same process lock, outside normal
message handling. Journals grow until compacted; monitor disk use and schedule
approved maintenance. Startup still parses retained history. Callback backlogs
retain their existing ordering/retry policy and shared outbox. Very large backlog
maintenance and full recovery scans can still consume CPU; this change does not
claim constant-time recovery or eliminate all shared-resource contention.

Deleted content remains absent from materialized messages and callbacks, and media
purging still runs. Like backups, immutable legacy files and journal revisions can
retain previous values until approved compaction/retention cleanup. Do not expose
these files or treat logical deletion as secure erasure. All new files use private
permissions; logs/diagnostics contain IDs, durations and counts, never bodies,
recipient numbers, credentials or QR/session payloads.

## Send semantics and compatibility

The successful HTTP contract remains `{messages:[{id:...}]}`. Success requires
confirmed, durable ACK/echo evidence; queued or accepted work is not relabeled
as delivered. The outbox obligation is recoverable from the acknowledged row,
so callback maintenance no longer blocks that response.

The browser's stable `client_message_id` is scoped in MariaDB to ticket, sender
type and sender identity. Its durable `transport_client_id` reaches the selected
bridge account. The bridge persists a native provider ID and payload fingerprint
before submission. Concurrent identical requests share a flight; repeated requests
and restarts consult the durable index. A changed payload under the same key is a
409 conflict. New keys permit intentionally identical sends. Media fingerprints
use original API bytes and bridge uploaded-content hashes, not temporary upload IDs.

The nullable `request_fingerprint` column deliberately does not invent legacy
payloads. Existing records remain visible and existing delivery recovery works;
replaying an unverifiable legacy creation request returns a conflict. Use normal
message status/recovery actions to inspect it. This is not permission to resend.

- Proven non-submission or explicit rejection remains a safe failure for an
  explicit retry. No automatic retry of uncertain external sends is added.
- After possible submission, HTTP deadlines, missing ACKs, cancellation and
  interruptions remain **UNCERTAIN**, retaining the same native/transport IDs.
- Late genuine ACKs/echoes/receipts reconcile that original row without dispatch.
  An older timeout or lower receipt cannot downgrade confirmed evidence.
- A crash after a durable reservation, even before actual submission, can be
  indistinguishable from a crash after submission. It stays uncertain until
  evidence or the existing operator workflow resolves it. No exactly-once claim.
- Cloud API semantics and delivery fencing remain unchanged; API payload identity
  checks apply to staff sends through either transport. Legacy callers without a
  client ID retain their old contract; they do not gain durable logical-send
  deduplication. Dashboard staff sends always supply the durable ID.

## Dashboard refresh behavior

`refresh.js:DashboardRefreshCoordinator` coalesces SSE invalidations into one
100 ms refresh batch and provides one fallback timer. Routine HTTP polling stops
when hidden; visibility restoration requests one queue/conversation/counts batch.
Only the selected conversation is fetched; speculative hover prefetch is removed.
Existing request counters reject stale navigation/filter responses. Session epochs
reject old-account responses, including late 401s. Logout/page exit stops scheduled
work. SSE transport heartbeats and activity/read tracking retain their separate
purpose and existing visibility checks.

`api.js` shares identical in-flight GETs using session epoch, agent, role, URL
(including filters/pagination), and headers. Sidebar counts share a flight and a
three-second cache scoped to session/agent/role; mutations/SSE invalidate it.
`list_tickets(count_only=True)` uses exactly the existing view/access/filter
predicates and skips loading ticket/read/name rows. No visibility policy changes.

`SUPPORT_QUEUE_POLL_SECONDS` defaults to **30** (minimum dashboard fallback **5**).
Existing installations explicitly setting `2` will use 5 until configuration is
reviewed; set `30` during an approved rollout to obtain the default benchmark rate.
The open pairing modal retains a visible-only two-second status refresh. In a
browser console, `supportRequestDiagnostics()` returns per-page queue, conversation,
and sidebar HTTP request counts without identifiers or content.

## Local validation and benchmark

Use synthetic configuration; never use production sessions or customer recipients.
Run Python unit and integration suites in separate processes (legacy unit doubles).

```bash
OPENAI_API_KEY=unused-test-key WHATSAPP_TOKEN=unused-test-token \
  SUPPORT_TEST_BROWSER=chromium venv/bin/python -m pytest --ignore=tests/integration -o addopts='' -q
# Start disposable services as described in support-platform.md, then:
OPENAI_API_KEY=unused-test-key WHATSAPP_TOKEN=unused-test-token \
  SUPPORT_INTEGRATION=1 TEST_DB_SOCKET=/tmp/support-dashboard-db.sock \
  TEST_REDIS_URL=redis://127.0.0.1:16379/15 \
  venv/bin/python -m pytest tests/integration -o addopts='' -q
npm test --prefix whatsapp_bridge
node --test tests/dashboard_refresh.test.cjs
node whatsapp_bridge/src/performance-benchmark.js
venv/bin/python scripts/check_support_assets.py
venv/bin/python -m compileall -q app scripts tests
git diff --check
```

Measured locally on Node **26.12.1**, using temporary storage, identical 19,856,711
byte (18.9 MiB) synthetic history, and five sends with three fsynced updates each.
Setup/loading is excluded. The benchmark compares the retained original FileStore
implementation with JournalStore; it includes copying/encoding/writing costs and
samples event-loop delay at 1 ms. These are one local run, not production or
Node 22 CI performance measurements.

| Measurement | Original FileStore | JournalStore |
|---|---:|---:|
| 15 updates | 10,787 ms | 15 ms |
| Bytes written | 297,853,815 | 3,046 |
| Maximum event-loop delay | 608.17 ms | 5.32 ms |
| p99 event-loop delay | 498.86 ms | 5.32 ms |

The deterministic 15-minute idle, open-conversation polling model compares the old
3s conversation / 2s Unassigned / 5s sidebar timers (930 requests per tab) with
30s coordinated fallback (90 requests: 30 of each scope), a **90.3% reduction**.
Hidden-tab fallback requests fall from 930 to zero; returning requests three
resources once. The scheduler tests execute with an injected clock; this is a
synthetic request model, not a browser/network load test or a prediction of the
reported 11,500 production requests. SSE activity, mutations, pagination, settings,
media, pairing and read/activity heartbeats have additional legitimate requests.

## Verification results

Local results for this development branch:

- Full unit/UI suite: **683 passed** in 52.51s, including real Chromium regressions.
- Full disposable MariaDB/Redis suite: **237 passed** in 91.23s; 25 expected warnings
  from intentionally reapplied additive migrations. External HTTP is fixture-blocked.
- Full mocked bridge suite after final storage changes: **104 passed** in 9.31s.
- Final targeted UI/linked-device suite after the starvation guard: **89 passed**
  in 14.95s. The scheduler wrapper includes six deterministic Node regressions.
- Asset/combined JavaScript syntax, Python compilation, and whitespace checks pass.

Earlier runs exposed the expected obsolete test assumption that changed audio
bytes under the same request ID should be silently accepted. That test now requires
409 conflict and separately verifies identical-byte reuse. Extracted UI fixtures
were updated for the coordinator dependency, the historical 2s fallback assumption
was replaced by the 30s SSE fallback, and schema doubles include the new column.
No tests were skipped or assertions weakened to hide implementation failures.

Physical phones, staff networks, production throughput, and new production
connection behavior have **not** been tested. Local results are separate from CI;
consult the PR checks for the exact pushed commit's CI outcome.

## Staged deployment and rollback — explicit approval required

1. Review the PR, run CI on its exact commit, then obtain production approval.
   Rehearse on an isolated copy with mocked clients; do not connect a copied live
   WhatsApp session. Check available disk space for journals and a rollback export.
2. During an approved maintenance window, pause new dashboard sends and gracefully
   drain/stop the API and bridge. Keep all uncertain attempts unchanged. Back up the
   complete bridge directory and database using established private procedures.
3. With the bridge offline, validate first (read-only except the process lock):
   `node whatsapp_bridge/src/storage-tool.js validate /PRIVATE/BRIDGE_DATA`.
   Optional `import` creates idempotent snapshots; `compact` checkpoints and fsyncs
   journal truncation. Neither removes the legacy import files. Validation reports
   incomplete final-tail byte counts; inspect unexpected corruption before proceeding.
4. Apply `migrations/20261029_support_send_fingerprints.sql` to the app's **local**
   database before the new API. This additive nullable column is repeatable. Do not
   use the remote historical database. Deploy code/assets together and review the
   polling interval. Start one bridge process; keep all existing sessions.
5. Check durable status/reconciliation, per-line connection states, queue/disk
   timings, event-loop responsiveness and dashboard request counts. After approval,
   use separate test phones to check sends/receipts and two-account isolation.
   Never replay historic uncertain customer sends as a deployment check.
6. To roll back, stop/drain both applications again. Export **current** journal-aware
   state with `node whatsapp_bridge/src/storage-tool.js export /PRIVATE/BRIDGE_DATA
   /PRIVATE/NEW_ROLLBACK_DIRECTORY`. The destination must not exist. This includes
   post-cutover messages, current auth, IDs, callbacks and media in the old JSON
   format. Validate the export before pointing the previous release at it. Retain
   the additive DB column and current database records. Do not restore only an old
   backup: it would lose post-cutover delivery evidence and could permit duplicates.
   An older bridge again has the original performance costs. Re-upgrading indexes
   messages written during rollback without assuming their payload identities.
