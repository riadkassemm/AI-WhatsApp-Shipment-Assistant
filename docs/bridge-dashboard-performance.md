# Bridge persistence and dashboard refresh changes

This document includes the historical incremental-storage performance change and
the automatic journal lifecycle implementation. The latter requires implementation
review and explicit DEV deployment approval. Production is outside this task.

## DEV lifecycle investigation and rehearsal — 2026-11-04

The dedicated working branch is `feat/bridge-storage-lifecycle`, based on the
deployed streaming-reader fix `2a6a225` / PR #74. At the end of this investigation,
the implementation had not been committed, pushed, or deployed. Investigation of the running DEV bridge was
read-only; tests and storage changes used disposable synthetic directories and a
private restored copy. No customer messages or real provider connections were
made by the rehearsal.

The complete private backup from the approved `2a6a225` rollout was reverified:
SHA-256 `cdd7021e3ff47d05442492859bc17f11c5fd65a577d356373e334fbe3520c181`,
18,056 files including 18,001 media files, 4,057,439,418 uncompressed bytes. The
archive remained unchanged. Only its 51 storage files were extracted for this
rehearsal (2,656,222,430 bytes); media was retained in the complete backup. The
restore directory was mode 0700 and files mode 0600. A fresh consistent backup
and current queue baseline are still required before an approved live rollout.

The running filesystem initially had about 5.3 GB available and 11.27 million
free inodes. A 45-second sample observed approximately 490 bytes/second on Beirut
state, 414 bytes/second on one auth store, and 100 bytes/second on the shared
outbox; other stores were quieter. This short sample is not a long-term forecast.
The frozen backup contained 421,241 journal records across 17 stores. Existing
structured logs and the staff outage banner were available; no external paging
integration was configured. Old abandoned UUID temporary files were observed and
left untouched.

The principal causes were unlimited append-only history with no automatic
checkpoint scheduler, offline compaction that had no coordination with live
writes, missing storage-health telemetry, and auth-store recreation on reconnect.
Credential views also aliased committed data, and queued credential updates could
be overtaken by proactive reconnection. The lifecycle and ownership changes below
address those causes without deleting authoritative records.

The private-copy measurements used Node 24.19.0 on DEV. Each replay ran in a fresh
process; the time column is the sum of 17 independent store replays, not WhatsApp
network connection time. These are single-run observations on a shared VPS.

| Measurement | Before compaction | After compaction / optimized reader |
|---|---:|---:|
| Journal bytes | 2,500,194,563 | 0 immediately after checkpoint |
| Snapshot bytes | 78,012,755 | 144,122,148 |
| Legacy + snapshot + journal bytes, excluding media | 2,656,220,859 | 222,135,689 |
| Sum of store replay time | 116.184 s | 6.453 s |
| Largest fresh-process replay peak RSS | 365.6 MiB | 418.1 MiB |
| Beirut auth replay | 26.206 s | 0.441 s |
| Beirut auth replay peak RSS | 313.0 MiB | 184.0 MiB |
| Largest current snapshot | 19,132,204 bytes | 34,536,337 bytes |

The initial snapshot checksum implementation reserialized decoded data and peaked
at 522.3 MiB. It now hashes writer-produced snapshot bytes directly, with a strict
metadata suffix and canonical verification for differently formatted legacy
snapshots. The remaining peak RSS increase above the streaming-journal baseline
comes from loading a complete current-state snapshot. This is a measured tradeoff,
not a claim that compaction reduces every memory peak. Snapshot headroom alerts,
bounded replay records, one checkpoint worker, and a worker heap limit provide
protection; total bridge RSS and authoritative retained state still need capacity
planning.

Full materialized-state fingerprints and sequences matched across all 17 initial
compactions. The copy retained all credentials, 61,672 messages, 18,454 durable
send keys, 305 operations, 155 uncertain-send records, 117 pending placeholders,
three pending normalizations, one callback entry, and no dead letters. No pending
send needed reclassification. Eight copied lines then became connected and paired
using mocked sockets in 9.223 seconds, with zero QR codes, provider calls, network
attempts, or sends. Normal initialization added 28,707 journal bytes; compaction
does not prevent new durable work from creating a suffix.
After the final checksum and credential-admission changes, a second eight-line
mocked restart passed the same preservation checks in 6.453 seconds, with a
684.2 MiB whole-process peak RSS and again zero QR codes, network calls, or sends.
The original private baseline was retained separately before this repeated test.

The initial large-history checkpoints took 130.723 seconds in total; the largest
observed final write barrier was 152 ms. Synthetic concurrency tests separately
verify that writes proceed during reconstruction and background suffix sync.
An fsync barrier cannot promise a fixed latency on a failing or overloaded disk.

The final checkpoint implementation was also exercised on all 17 already restored
stores using same-value test transactions in the private copy. Each checkpoint
preserved the entire materialized state, survived two subsequent recovery reads,
and produced state that the deployed `2a6a225` reader restored identically. This
verifies the rollback format using the actual previous implementation, not only a
reimplementation of its parser.

Initial implementation verification (before the independent review fixes):

- **167 bridge tests passed** in 36.07 seconds, including 22 lifecycle regressions,
  13 SIGKILL stages with post-checkpoint committed writes, repeated automatic
  compaction, concurrent writes, shadow/snapshot corruption, sequence gaps,
  missing journals, disk/inode pressure, write failures, quarantine, and repeated
  recovery. Additional regressions cover auth ownership, early credential
  admission behind blocked message work, failed registration/shutdown cleanup,
  uncertain-send preservation, authenticated metrics, and storage admission.
- **841 Python unit/UI tests passed in aggregate.** The full run passed 840 tests;
  its sole remaining packaging failure was an unchanged README omitted from the
  temporary source archive. After copying that fixture, the guard passed. All
  browser assertions ran unchanged with the existing Chrome Headless Shell 149.
  Regular Chrome 149 stalled even on a minimal synthetic page; no application
  assertions were removed or weakened. No tests were skipped in the successful
  headless-shell run. Real database integration tests were excluded.
- **Seven dashboard scheduler regressions passed**, as did asset/combined-script
  syntax, Python compilation, and source whitespace checks. A synthetic screenshot
  verified the storage banner without exposing customer information or suggesting
  re-pairing.
- Checksummed snapshot fast-path tests reject damaged data, changed sequences,
  appended duplicate fields, and null checksums, while preserving older snapshot
  ordering, formatting, and checksum-free compatibility.

SIGKILL tests demonstrate process-crash recovery. They do not simulate a physical
device ignoring fsync or every filesystem/power-loss failure. Checkpoint safety
depends on the documented atomic rename and durable file/directory sync semantics.

### Independent review corrections

The initial implementation received a NO-GO review. Its four deployment blockers
were corrected as follows:

| Finding | Root cause and correction |
|---|---|
| Incoming messages and receipts lost during credential failure | Socket identity was rechecked after queued work had already been accepted. Admission now captures identity; credential failure retires authentication while accepted message/delivery work drains. The actual provider event buffer is flushed before destruction, and complete native callbacks and detached children are tracked. Decoded messages and pending stubs still reach persistence when receipt/ACK completion fails. |
| Renewal after rejected key writes | Raw key rejection did not poison the authentication owner, and the provider buffered mutations outside the journal queue. Ownership now covers actual Baileys transactions, commit retries, parent/child lifetimes, later rejected mutations, and detached work escaping completed transaction contexts. Private active/intent/failure evidence prevents unsafe renewal or restart. |
| Abandoned checkpoint temporaries | Worker cancellation bypassed worker-owned cleanup. The parent confirms termination, removes only the attempt's two known paths, synchronizes the directory, and completes failed cleanup before allowing another attempt. |
| Uploads consumed space before send admission | Pressure checks ran after Python read/encoded attachments and the bridge wrote files. Preflight now precedes Python reads and HTTP body decoding; block/inode reservations precede decoded-buffer allocation. Identical retries reuse files; pending sends retain shared media before provider submission. Failed media temporary cleanup prevents repeated allocation while essential writes remain enabled. |

The pinned provider patch and its installation checks are part of the implementation,
not an optional deployment workaround. Real-provider tests replace only the physical
WebSocket transport; they exercise installed login callbacks, offline message
processing, event buffering, delayed negative acknowledgments, Signal transactions,
commit retries, detached work, and session retirement without contacting WhatsApp.

The remaining important review items are addressed by repeated eight-line memory
measurements, overlapping shutdown deadlines and worker cancellation, an independent
systemd watchdog, missing-base recovery that refuses fresh credentials when evidence
remains, and persistent bounded health counters with explicit degraded telemetry.
Counter persistence cannot be guaranteed on an unwritable filesystem; the independent
watchdog and journald remain the alternative detection paths.

Final review-fix validation on the private DEV checkout:

The tested source archive SHA-256 was
`49fc60ffbe174aa6af3676cb43feaf73c5fcffeae1a3404dce9ac8173249ca9e`,
verified against the transferred DEV archive. Subsequent edits only record these
results in documentation; the implementation remains uncommitted.

- **254/254 bridge tests passed** in 59.01 seconds, with no skips or cancellations.
  The suite includes 13 SIGKILL checkpoint stages, actual write/fsync/rename/
  directory-sync and quarantine failures, repeated worker timeouts/exits without
  store restart, publication fencing, and missing-base recovery. Provider tests
  cover real buffered transactions, retries, rejected mutations, detached work,
  queued messages/receipts, decoded offline messages, unavailable/decryption
  recovery stubs, and shutdown during active maintenance. Media regressions cover
  pressure before allocation, essential writes under pressure, concurrent block/
  inode reservations, retry deduplication, shared-file deletion races, and cleanup
  failures. The original 167-test suite remains included.
- **864/864 Python unit/UI tests passed** in 60.44 seconds, including the watchdog,
  outage fallback, media preflight, and unchanged browser assertions. No tests
  were skipped; real database integration tests were excluded.
- **7/7 dashboard scheduler checks passed**. Asset/combined-JavaScript syntax,
  Python compilation, prepared systemd-unit validation, and whitespace checks
  passed. Units were neither installed nor started.
- The checksum-verified provider patch passed reversibility, idempotence,
  unknown-version/source rejection before mutation, interrupted installation,
  actual rename failure, symlink refusal, and read-only verification checks. The
  direct Node entry point invokes that verifier before loading provider/storage code.

The first eight-line repeat-compaction run used explicit GC only at measurement
boundaries. It completed six cycles, 114 automatic checkpoints, 6,953,390,000
journal bytes, and 3,519 writes during active workers. There was at most one
checkpoint worker, zero remaining checkpoint temporaries after each cycle, and
stable quiescent descriptor/thread counts (23/11). Post-GC heap across cycles was
235.31–235.50 MB; after removing synthetic fields and closing it was 217.67 MB,
versus 217.12 MB initially. Sampled peak total RSS was 1,953.50 MB. RSS varied
between cycles rather than growing monotonically; retained heap did not show a
compaction leak in this bounded run. This stressed all 17 loaded stores with
simultaneous 1 MiB journal updates, not typical idle traffic.

Fresh-process replay for that run changed from 2,500,194,563 journal bytes and
111.412 seconds to 38,808,281 bytes and 7.129 seconds (sum of 17 independent
replays). Largest individual replay peak RSS was 404.95 MB before and 403.75 MB
after. This repeat does not erase the earlier 366→418 MiB observation: complete
snapshot parsing can raise a particular replay peak even while greatly reducing
replay time. The first run preserved full fingerprints and sequences, all eight
credentials, 61,672 messages, 18,454 send-request keys, 305 operations, 155 uncertain
sends, 117 placeholders, three pending normalizations, one callback, and zero
dead letters. There were zero QR codes, real network/provider calls, or sends.

The final frozen implementation then completed a second **eight-line, 17-store,
six-cycle soak without exposed/forced GC** in 271.658 seconds (initial inspection
excluded). It produced 6,953,436,540 journal bytes, completed **102 automatic
compactions**, and accepted **4,114 writes while checkpoint workers were active**.
Every journal remained below its 64 MiB trigger after maintenance settled; the
largest final journal was 45,097,696 bytes. All cycle boundaries had zero workers
and zero checkpoint temporaries, with 23 descriptors and 11 threads. Peak active
worker count was one; maximum temporary checkpoint space was 45,560,977 bytes.
Available disk never fell below 3,004,006,400 bytes in the measured workload.

All **248 real Baileys key transactions** produced the expected 248 journal
commits. The first-cycle burst ran 200 transactions, at most four outstanding per
line, all overlapping checkpoint work: 1.946 seconds total, 284.1 ms median,
426.6 ms p95, 498.3 ms maximum latency, 46,225 journal bytes added, and no orphaned
intents. These latencies include the synthetic concurrent durability workload;
they are not measurements of real WhatsApp network delivery.

Final-run OS peak RSS was **1,959,206,912 bytes (1,868.4 MiB)**; sampled peak RSS
was 1,924,395,008 bytes. Cycle-end RSS was 861.3, 1596.0, 1680.1, 1585.6, 1365.3,
and 1204.2 MiB. After synthetic fields were removed and the bridge closed, heap
was 286.8 MiB versus 355.0 MiB at initialization; RSS was 936.8 MiB versus 742.0
MiB initially. The allocator retained some process memory, but neither run
showed monotonically accumulating heap, threads, descriptors, workers, or
temporary files. Six stress cycles do not establish a multi-week leak guarantee.
Maximum final publication pause was **121 ms**; copied-session shutdown took
**10 ms**, with no remaining authentication fences.

| Final comparison with the original verified copy | Before | After final soak |
|---|---:|---:|
| Total journal bytes | 2,500,194,563 | 45,100,926 |
| Journal records replayed | 421,241 | 61 |
| Sum of 17 fresh-process replay times | 111.412 s | 6.481 s |
| Largest fresh-process replay peak RSS | 386.2 MiB | 421.7 MiB |

The final materialized state, sequences, credentials, message/queue/uncertainty
counts, and full fingerprints all matched. The actual deployed `2a6a225` reader
also restored all **17** copied stores identically, after verifying that no auth
fences remained. All six deployed provider source hashes still matched their
original values: only the isolated dependency copy was patched.

The final read-only live check found all eight lines connected and paired. Their
uncertain counts remained `25,13,25,31,26,4,3,9` for lines
`90011,90012,90013,90015,90017,90023,90024,90026`; the single callback on `90015`
remained pending, with no failed callbacks. Bridge/API/tunnel/database/Redis PIDs
were unchanged. DEV still ran `2a6a225`, with 4,188,381,184 available bytes and
11,254,972 available inodes. This capacity is below the default 5% warning threshold;
it is not a reason to delete session data. A fresh capacity check and complete,
consistent backup remain mandatory before approved activation.

The isolated implementation assessment was initially GO, but the later deployment
preflight superseded it with **NO-GO**: signal handling did not own partial startup,
and a complete fresh backup/restore did not fit. The startup correction now owns
initialization and shutdown through the runtime coordinator, prepares all stores
before opening sockets, and retains the lock through accepted work and HTTP
handler cleanup. The measured [capacity and encrypted restore
plan](bridge-backup-capacity.md) provides an exact approval-only cleanup manifest.
Capacity has not been reclaimed and the fresh complete backup has not been taken.
Deployment remains **NO-GO until those gates and explicit deployment approval are
satisfied**. An unclean authentication fence still requires recovery review;
rollback must never bypass it. During that investigation and rehearsal, no
commit, push, PR, deployment, live-data modification, or live-service restart
was performed. The subsequent cleanup safety audit and current capacity blockers
are recorded in the [capacity plan](bridge-backup-capacity.md#final-read-only-cleanup-safety-audit--2026-11-05-beirut-time).

### Startup ownership follow-up — 2026-11-05 Beirut time

The executable now delegates to `runtime-lifecycle.js`, which installs both signal
handlers before asynchronous preparation and owns the bridge before awaiting
initialization. All configured stores are prepared before any socket connects.
Cancellation aborts replay reads without labelling them corruption; once tail
quarantine starts, its durability protocol finishes before cancellation returns.
Partially loaded stores, late authentication facades, accepted recovery work,
media publication and disconnected HTTP handlers remain owned through shutdown.
Repeated signals and close calls preserve one result, including a prior failure.
New requests cannot unlink sessions or publish downloaded media after admission
closes. Successfully closed historical store owners are released; a failed load
cannot allocate another owner for the same filename on retry.

The runtime's 30-second diagnostic deadline sets unsuccessful exit status without
releasing unfinished ownership. The existing 35-second systemd limit remains the
last resort for uninterruptible I/O. A failed or timed-out stop is never a clean
activation/rollback gate. Server errors after listening also enter the same
drain instead of terminating through an unhandled error event.

Final isolated DEV bridge validation passed **292/292 tests in 60.288 seconds**,
including 38 new regressions: 19 forked-process signal/startup cases, 13 bridge
ownership/admission cases, three HTTP-handler cases and three replay cancellation
cases. The process suite exercises the installed Baileys transaction wrapper,
buffered Signal-key commits and rejected commits; it verifies durable incoming
messages/receipts, recovery fences, lock retention and repeated signals. The
incomplete HTTP-body case asserts shutdown below five seconds after admitted
work is released. Two older intentional-corruption tests now explicitly require
failed shutdown for retained failed stores. This strengthens their assertions;
it does not suppress cleanup errors.

The selected Python linked-device/API/queue/watchdog regressions also passed
**163/163**, including a browser case rerun with the installed headless-shell
path after the initial invocation lacked `SUPPORT_TEST_BROWSER`. Test logs are
private DEV artifacts under `/tmp/whatsapp-startup-fixes.iDvjyX`:
`bridge-final-2.log`, `python-final.log` and `browser-final.log`. The Python log
records the initial browser-environment failure; the separate browser log records
its successful rerun. All execution used isolated test data; no live sockets or
services were restarted.

The live DEV bridge remained on `2a6a225`; all eight lines were still connected and
paired in the read-only post-check. Bridge, API, tunnel, database, and Redis PIDs
were unchanged. The final 22:17 UTC sample showed 136 uncertain sends, one pending
callback and zero failed callbacks, matching the preceding checks. Live traffic
continued, so these counts were not a frozen backup baseline. Exact preservation
assertions apply to the isolated copy. Actual
reconnection and queue reconciliation after deployment remain approval-gated.
The final read-only check matched all 15 deployed source hashes against the
`2a6a225` baseline and found zero `.auth-failure`, `.auth-intent` or `.auth-active`
markers. Systemd still allowed 35 seconds to stop; the bridge had no configured
memory maximum, used 717,840,384 bytes in its cgroup, and host available memory
was 7,890,808,832 bytes with no swap. These are point-in-time observations, not
guarantees of memory headroom during later compaction or deployment.

Implementation files:

- Persistence: `whatsapp_bridge/src/journal-store.js`, `journal-maintenance.js`,
  `journal-worker.js`, `storage.js`, `storage-tool.js`, `media-files.js`,
  `storage-rehearsal.js`, and `storage-soak.js`.
- Runtime and monitoring: `whatsapp_bridge/src/bridge.js`, `runtime-lifecycle.js`, `storage-monitor.js`,
  `config.js`, `index.js`, and `server.js`; configuration examples in `.env.example`.
- Provider installation: `whatsapp_bridge/scripts/patch-baileys-lifecycle.js`,
  `whatsapp_bridge/package.json`, and `whatsapp_bridge/package-lock.json`.
- Upload admission: `app/linked_device_client.py`, `app/whatsapp_client.py`,
  and `app/support_delivery.py`.
- Staff alerts: `app/whatsapp_line_health.py`, `app/static/support/actions.js`.
- Independent watchdog: `scripts/check_bridge_storage.py`,
  `deployment/dev-vps/whatsapp-storage-watchdog.service`, and
  `deployment/dev-vps/whatsapp-storage-watchdog.timer`.
- Regressions: `whatsapp_bridge/test/bridge.test.js`, `auth-storage.test.js`,
  `journal-maintenance.test.js`, `media-files.test.js`,
  `provider-lifecycle.test.js`, `provider-lifecycle-patch.test.js`,
  `storage-admission.test.js`, `storage-monitor.test.js`,
  `storage-rehearsal.test.js`, `storage-soak.test.js`,
  `startup-lifecycle.test.js`, `startup-replay.test.js`,
  `bridge-initialization.test.js`, `server-lifecycle.test.js`,
  `whatsapp_bridge/fixtures/startup-child.mjs`,
  `tests/test_line_outage_resilience.py`, `tests/test_linked_device_client.py`,
  and `tests/test_bridge_storage_watchdog.py`.
- Operations: this document, `docs/bridge-backup-capacity.md`, `whatsapp_bridge/README.md`, and
  `deployment/dev-vps/README.md`.

No dependency version upgrade or database migration is introduced. The pinned
Baileys installation now requires the reviewed lifecycle patch described below.
The existing versioned asset loader hashes the changed dashboard script automatically.

## Confirmed paths and causes

The API is FastAPI, the dashboard uses classic JavaScript and server-sent events
(SSE), and the linked-device bridge uses Baileys `7.0.0-rc14`. MariaDB stores
support records; the bridge owns private file-based native message/session state.
The original performance work did not change frameworks, dependency versions,
transport timeouts, or query indexes. The lifecycle work adds the provider hooks
and independent monitoring described above.

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
record surviving a crash is replayed. An incomplete final record is durably copied
to a private quarantine file with checksum/offset metadata before tail repair;
preservation failure leaves the original journal untouched. A bad checksum or
sequence gap fails closed. Startup imports
legacy state and rebuilds missing send-index entries in bounded batches, including
messages created by an older release after rollback.

Journal replay reads chunks and decodes one transaction at a time. It must never
convert the entire journal to one JavaScript string: an otherwise valid journal
can exceed Node's maximum string length and prevent a saved session reconnecting.
Replay memory still includes the materialized state and the largest transaction;
legacy JSON and snapshots are still loaded as complete documents. Checksummed
transactions and sequence validation remain mandatory, including records already
covered by a snapshot. New snapshots also carry an optional checksum over their
version, sequence, and data; old version-1 snapshots remain readable.
Replay separately caps an individual complete or incomplete record at 64 MiB,
preventing a malformed newline-free file from becoming one unbounded allocation.
An oversized source is left untouched and fails closed. A known-valid legacy
record exceeding that limit requires a reviewed explicit
`MAX_REPLAY_RECORD_BYTES` override after a verified private backup. Normal new
transactions retain the existing 8 MiB write limit.

The Beirut Office incident on 2026-11-04 reached this limit: its auth journal
contained 551,334,970 ASCII bytes, above the installed Node limit of 536,870,888
characters. Read-only inspection found 13,637 consecutive checksummed records and
no incomplete tail. The status reported a session-storage failure and manual
pairing returned HTTP 500. This storage failure does not require deleting
credentials or forcing a new pairing. Commit `2a6a225` introduced the streaming
reader and was subsequently deployed to DEV, preserving the saved sessions.
That change addressed the maximum-string failure; it did not schedule compaction
or bound journal growth.

Verification for this fix ran in an isolated DEV VPS temporary directory:
all 114 mocked bridge tests passed. A 536,956,308-byte synthetic journal reproduced
`ERR_STRING_TOO_LONG` with the deployed reader; the streaming reader replayed all
512 records. A read-only replay of Beirut's actual auth journal also succeeded,
retained paired credentials, and left the journal unchanged. These checks did not
deploy the fix, restart the running bridge, or contact WhatsApp.

Existing JSON files remain immutable import sources. Active state is now the
legacy file (or latest `.snapshot`) **plus** `.journal`; reading only `state.json`,
`auth.json`, or `outbox.json` is no longer a valid diagnostic/backup strategy.
Back up the complete private directory. The Python routing manifest remains
unchanged. Never start two processes against the same directory; the existing
process lock enforces ownership.

The original compaction command required offline ownership of the process lock
because it checkpointed a materialized store and truncated the journal without
coordinating active writes. No online scheduler, retention policy, disk/inode
monitor, or checkpoint-failure alert existed. Authentication also recreated stores
on reconnect and shared mutable provider-owned credential objects with committed
memory. These ownership gaps had to be fixed before online maintenance was safe.

## Automatic journal lifecycle

Each line now keeps one auth store throughout reconnects. Socket-specific auth
views clone credentials and key reads, capture admitted updates, and reject writes
from retired generations. Renewal drains admitted transactions before exposing
the next view. Credential events enter the auth queue before slow message/history
work, so proactive reconnect cannot overtake an already received key rotation.
Rejected credential and Signal-key writes halt renewal even when queue/record
limits prevented an append. The installed Baileys transaction wrapper is tracked
before its Signal repository begins work: retirement drains buffered commits and
retries, not merely writes already submitted to the journal. Event batches capture
socket identity at admission; incoming messages and delivery updates accepted
before credential failure still drain after that socket is retired.
The installed provider's message, notification, receipt, and app-state mutex
callbacks are also tracked through their post-transaction credential events.
The pinned provider lifecycle patch exposes complete asynchronous callback and
detached-task boundaries, including login, offline receive processing, token
issuance, and delayed negative acknowledgments. Tracking only the public mutexes
or raw key-store calls is insufficient. Retirement flushes the actual Baileys
event buffer before its listeners or buffered acknowledged messages are destroyed.
Retired subscriptions stay attached only while admitted provider work finishes;
renewal waits for the final accepted event queue before opening another socket.
LID edits/revokes retain their native input when verified routing is unavailable.
They retry from `pending_mutations` after mapping recovery; a target message ID
alone cannot bind a mutation to another conversation.

`npm ci --prefix whatsapp_bridge` runs the checked-in
`scripts/patch-baileys-lifecycle.js` postinstall hook against exactly Baileys
`7.0.0-rc14`. It validates all original source hashes before modifying an isolated
release installation, writes each patched file atomically, and rejects unknown
versions, source edits, and symlinked dependency roots. Repeated installation is
idempotent. `src/index.js` verifies the complete patch read-only before importing
the provider or opening storage, including when systemd invokes Node directly.
Do not share a writable `node_modules` tree between releases or apply the patch to
running dependencies. An installation using `--ignore-scripts` needs an explicit
patch step followed by `node whatsapp_bridge/scripts/patch-baileys-lifecycle.js --verify`;
otherwise startup intentionally fails. A provider upgrade requires
reviewing these hooks and rerunning the real-provider transaction/event regressions.

Private `auth.json.auth-active` evidence is synchronized before opening a provider
socket and removed only after retirement and a successful authentication drain.
Some installed-provider credential events follow network effects outside key
transactions. This session fence prevents silent stale-credential recovery even
when both the credential append and failure-marker write are rejected. **An
unclean bridge process exit can therefore require recovery review for all eight
sessions.** Graceful restarts preserve automatic reconnect; the fence never
deletes credentials or requests re-pairing. This conservative availability cost
is necessary without changing the provider's credential/network protocol.
The provider also shares one mutable credential object across callbacks. A benign
query failure concurrent with another callback's credential mutation can therefore
be conservatively classified as ambiguous authentication. This may require recovery
review even without proven data loss; the implementation favors preserving evidence
over automatically reusing possibly stale keys.

Private `auth.json.auth-intent` evidence is synchronized before a provider
transaction or key mutation can begin. Completed operations remove only their own
intent after keys and final credentials are durable. A rejected mutation also
preserves `auth.json.auth-failure` evidence. Startup and offline transformations
refuse unresolved authentication fences; they never silently manufacture fresh
credentials or infer the outcome of an interrupted transaction. A crash in an
otherwise successful transaction can therefore require operator review. Keep
all three marker types in every backup. Never delete them to bypass recovery, and
never roll back to an older reader that ignores them while either remains.

Shutdown and removal retire auth callbacks before their final drain.
Compaction reconstructs only durable state; it never snapshots mutable
provider memory or infers a send outcome.

The online checkpoint protocol uses the existing version-1 filenames/formats:

1. A short operation on the store's serialized queue captures a durable byte
   boundary and transaction sequence. Normal appends then continue to the same
   active journal.
2. A worker replays the existing snapshot/import source and that journal prefix.
   It checks every record, creates a private snapshot temporary file, fsyncs it,
   and verifies the written file's checksum. No authoritative file changes yet.
3. The worker copies subsequent complete transactions to a private replacement
   journal, validating checksums and sequence continuity. Catch-up runs while
   ordinary writes continue; there is at most one checkpoint worker globally.
4. A final per-store queue barrier admits only a bounded remaining suffix
   (default 1 MiB). The worker copies it and fsyncs the replacement journal.
   It atomically renames the snapshot and fsyncs the directory **before** replacing
   the active journal, then fsyncs the directory again.
5. The queue resumes with the same final sequence and live state. The previous
   journal inode is replaced only after every removed transaction is in a durable
   snapshot. There is no growing archive of obsolete journal segments.

Before publication, a crash leaves the original snapshot and complete journal.
After snapshot publication, either the complete journal or the copied suffix is
valid: replay validates old records and skips only transactions already covered
by the snapshot. No rename creates a missing active-journal interval. A failure
after publication begins makes the store require restart/recovery rather than
guessing which writes reached stable storage. Startup removes only recognized,
nonauthoritative checkpoint temporary files. During operation, the parent waits
for confirmed worker termination, removes only that attempt's two UUID temporary
paths, and synchronizes the directory. Failed cleanup retries those same paths
before another attempt can allocate files. It never sweeps arbitrary files,
credentials, quarantine evidence, or uncheckpointed records.

The worker checks service-user available disk bytes and inodes before allocating
the snapshot and before copying journal suffixes. Planned writes must leave the
configured reserve. Capacity, worker timeout, memory-limit, and pre-publication
I/O failures retain the authoritative journal and retry with exponential backoff
(60 seconds initially, capped at one hour). Corruption fails closed and is not
automatically repaired by dropping a record. Up to 32 catch-up passes avoid
waiting forever for a faster producer; sustained overload reports
`COMPACTION_BUSY` and retries. Filesystem fsync latency remains part of the short
publication barrier, so it is observable but cannot be given a hard latency bound.

Compaction starts at any configured byte, transaction-count, or age threshold.
Normal writes check size/count thresholds; the 30-second monitor checks idle
stores too. Under normal load, obsolete journal revisions are bounded by the
checkpoint threshold plus writes admitted during checkpoint work. The threshold
is a trigger, not a promise that a journal can never exceed it. Critical pressure
stops new outbound provider submissions while auth, receipt, callback, and
uncertain-send evidence can still be written.

This is lossless checkpointing, not retention deletion. Current auth keys,
messages, native IDs, pending callbacks, dead letters, delivery/uncertainty
records, and deduplication indexes remain authoritative. Their snapshots and
in-memory state can still grow with legitimate retained history. Legacy import
files remain unchanged; quarantined tails require investigation and an explicit
private evidence-retention decision. Worker heap and snapshot limits bound
maintenance resource use, not total bridge RSS. A store exceeding those limits
raises a checkpoint alert and retains its journal. Capacity planning and a
separately reviewed authoritative-data retention policy remain necessary.

### Configuration and monitoring

All settings below use the `WHATSAPP_LINKED_DEVICE_STORAGE_` prefix. Defaults are
also in [`.env.example`](../.env.example). Invalid numbers and reversed warning/
critical thresholds fail configuration loading; reserve bytes must be below the
critical free-space threshold. Snapshot warning must precede critical pressure,
and critical bytes must not exceed the new-checkpoint size cap.

| Suffix | Default | Meaning |
|---|---:|---|
| `AUTO_COMPACT` | `true` | Enable automatic checkpoint requests |
| `COMPACT_BYTES` | `67108864` | 64 MiB journal trigger |
| `COMPACT_TRANSACTIONS` | `10000` | Journal transaction trigger |
| `COMPACT_AGE_MS` | `21600000` | Six-hour age trigger for nonempty journals |
| `RETRY_MS` | `60000` | Initial retry delay; backoff caps at one hour |
| `FINAL_CATCHUP_BYTES` | `1048576` | Maximum final suffix under the write barrier |
| `RESERVE_BYTES` / `RESERVE_INODES` | `134217728` / `64` | Space remaining after planned worker writes |
| `WORKER_MEMORY_MB` | `512` | Worker old-generation heap limit; not total process RSS |
| `MAX_SNAPSHOT_BYTES` | `134217728` | Reject oversized new checkpoints without changing current state |
| `MAX_REPLAY_RECORD_BYTES` | `67108864` | Bound a single replay record; reviewed override for known-valid larger legacy records |
| `COMPACTION_TIMEOUT_MS` | `300000` | Worker execution timeout |
| `MONITOR_INTERVAL_MS` | `30000` | Disk sampling and idle maintenance interval |
| `MONITOR_TIMEOUT_MS` | `5000` | Filesystem probe deadline, at most the sampling interval |
| `JOURNAL_WARN_BYTES` / `JOURNAL_CRITICAL_BYTES` | `134217728` / `268435456` | 128/256 MiB per store |
| `SNAPSHOT_WARN_BYTES` / `SNAPSHOT_CRITICAL_BYTES` | `100663296` / `125829120` | 96/120 MiB persisted-state headroom before the checkpoint cap |
| `DISK_WARN_BYTES` / `DISK_CRITICAL_BYTES` | `2147483648` / `536870912` | 2 GiB/512 MiB available |
| `DISK_WARN_PERCENT` / `DISK_CRITICAL_PERCENT` | `5` / `1` | Available space percentage; either bytes or percent triggers |
| `INODE_WARN_PERCENT` / `INODE_CRITICAL_PERCENT` | `10` / `5` | Available inodes; unsupported inode metrics are unknown |
| `ALERT_REPEAT_MS` | `900000` | Repeat unchanged alert after 15 minutes |

Authenticated `GET /v1/storage` returns cached disk/inode samples and live safe
store statistics for each line's `auth`/`state` and the shared `outbox`. It includes
journal/snapshot bytes, append growth per second, transaction/sequence counts,
startup replay time/records, last successful checkpoint time, current-process
checkpoint duration/reclaimed bytes, cumulative failure counters, retry time, and recovery/quarantine
errors. Growth uses appended-byte deltas so rotation does not produce a misleading
negative rate. The checkpoint timestamp survives restart; bounded private
`.health.json` sidecars preserve compaction/recovery failure counters. If metadata
cannot be written, essential state writes remain enabled and
`STORAGE_HEALTH_UNAVAILABLE` reports degraded counter persistence. Corrupted
counter history remains explicitly degraded; a full filesystem cannot guarantee
durable telemetry. Checkpoint duration and reclaimed bytes describe the current
process. Filesystem probes use `bavail`, accounting
for space available to the service user. A failed/stale probe warns, becoming
critical after three sampling intervals without success.
Filesystem probes have a five-second deadline. An unresponsive underlying
`statfs` call is not duplicated while still pending; cached health routes remain
responsive, age-based maintenance continues, and monitor shutdown is bounded by
the probe deadline. The deadline cannot cancel a kernel filesystem operation.

The same summary appears in `/v1/status`. The staff health API passes through only
allowlisted issue codes, severity, and enabled-line display names. Its existing
dashboard banner and local notification show pressure even while lines remain
connected, and never recommend re-pairing for a storage issue. Structured systemd
logs record `storage_alert`, `storage_recovered`, `storage_compacted`,
`storage_compaction_failed`, `storage_replay`, `storage_tail_quarantined`, and
`storage_recovery_error`. Changed issues log immediately on the next sample;
unchanged issues repeat after the configured interval. Monitoring and maintenance
continue when no browser is open. Dashboard notifications require an active
dashboard; no external paging integration exists in this deployment.

The independent `whatsapp-storage-watchdog.timer` runs every minute even when the
bridge cannot start. Its bounded probe checks filesystem capacity/inodes and the
authenticated loopback bridge endpoint, emits safe codes to journald, and writes
a private atomic report under `/run/whatsapp-storage-watchdog`. The API consumes
only a fresh, regular, private report when the bridge is unavailable. Failed report
writes still produce a critical journald event. Install the watchdog script at
`/usr/local/libexec/whatsapp-support/check_bridge_storage.py` during the approved
deployment so a release-symlink rollback does not remove it. The prepared units
have no bridge-service dependency and a ten-second execution limit. On rollback
to `2a6a225`, the probe checks `/v1/status` when `/v1/storage` is absent and reports
the missing detailed telemetry explicitly.

Critical line-store pressure rejects new submissions for that line; shared
outbox/filesystem pressure affects every line. The pre-submission HTTP result is
503 `STORAGE_PRESSURE`, with `delivery_uncertain:false`. It does not change an
existing uncertain or confirmed result and does not auto-resend anything. Auth,
receipt, reconciliation, and callback writes remain enabled. Alerts clear when
health recovers; a store marked failed requires safe restart/recovery.

Attachment delivery checks health before Python reads or base64-encodes the file;
the HTTP upload route checks before reading the request body, and the bridge
reserves projected bytes and an inode before decoding or writing. Concurrent
reservations preserve the critical reserve. Retry uploads of the same content,
MIME type, and sanitized filename reuse one deterministic media reference, so a
later rejected send does not accumulate duplicate attachments. This changes no
send idempotency key or uncertain-send decision.
Reservations round payloads to filesystem allocation blocks and include 16 KiB
of metadata headroom. A pending send acquires its durable attachment reference
under the same queue as deletion, so deleting another message cannot remove a
shared in-flight attachment. Media writes synchronize private temporary files,
rename, and synchronize the directory. Failed cleanup must complete before the
writer allocates another attempt; startup cleans only its exact unpublished
double-UUID temporary names. Cleanup failure leaves essential session startup,
incoming messages, and authentication writes enabled.

For `DISK_SPACE_LOW`/`INODES_LOW`, expand capacity or remove separately verified
non-session files, then inspect automatic recovery. For `COMPACTION_FAILED`, inspect
the safe error code, workspace/snapshot limits, worker resource use, and retry
time. For `RECOVERY_ATTENTION`/`STORE_UNAVAILABLE`, preserve a complete private
copy and investigate with the offline tool; do not delete files to silence the
alert. `MONITOR_UNAVAILABLE` requires checking service filesystem access. Runtime
logs and metrics contain counts, durations, line IDs, and fixed codes; the storage
endpoint contains neither session contents nor filesystem paths.
`SNAPSHOT_LIMIT` warns before the configured checkpoint cap is reached. Review
retained authoritative-state growth, memory, and capacity before increasing
limits; do not delete keys or delivery records. Snapshot headroom is measured
from the latest completed snapshot, so a large intervening change can still hit
the worker's fail-closed limit before another successful checkpoint updates it.

### Recovery and quarantine

A final non-newline-terminated journal suffix is preserved as
`*.journal.tail-<timestamp>-<uuid>.bin` with JSON checksum/offset/length metadata.
The evidence file and metadata are fsynced before truncating only that incomplete
suffix and fsyncing the journal. If evidence preservation fails, loading stops
without truncation. A process may die before acknowledging a complete appended
record; that complete checksummed record is replayed normally. No result is inferred
from damaged bytes, and incomplete/corrupt send evidence is never treated as
permission to resend.

`storage-tool.js validate` reports incomplete-tail bytes without repairing them.
`import`, `compact`, and `export` refuse a source with an incomplete tail instead
of silently omitting it. Run recovery only after verifying a private backup, then
inspect the retained evidence and recovery event before proceeding. Complete
records with malformed JSON, invalid checksums, or sequence gaps require operator
investigation; restoring an older backup alone can lose newer delivery evidence.
The tool keeps its exclusive directory lock and remains an **offline** operator
command. Its compaction uses the same checkpoint publication protocol.

If a legacy base is missing but a valid snapshot remains, recover from that
snapshot and validated journal. Journal, temporary, health, or authentication
evidence without a usable base/snapshot must stop initialization before any fresh
credentials are generated. Authentication intent/failure evidence requires an
offline investigation of the recorded mutations and durable sequence against a
verified copy. Do not automatically apply possibly sent keys, discard evidence,
or re-pair to bypass the fence. No general automatic repair can prove the provider
outcome of an interrupted transaction.

Deleted content remains absent from materialized messages and callbacks, and media
purging still runs. Like backups, immutable legacy files and journal revisions can
retain previous values until checkpointing and approved retention cleanup. Do not expose
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

## Historical validation and benchmark

Run current validation on the DEV VPS, not the developer PC. The historical
measurements below predate the automatic lifecycle implementation and are not
acceptance results for it. Use synthetic configuration; never use production
sessions or customer recipients.
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

## Historical verification results

Results recorded for the earlier incremental-storage/dashboard change:

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

1. Inspect DEV data read-only first. Record all eight line IDs and connection/
   pairing states, journal/snapshot sizes and sequences, callback/dead-letter
   counts, uncertain sends, available space/inodes, and replay time/RSS. Do not
   print credentials, customer content, or bearer tokens. Keep production untouched.
2. Before changing any storage, create a complete private backup including legacy
   files, snapshots, journals, registration manifest, media, health sidecars,
   authentication active/intent/failure markers, and quarantine evidence.
   Verify archive readability and file hashes, and validate a separate restored
   copy. A filesystem snapshot or approved drain/stop supplies a consistent restore
   point; an arbitrary live recursive copy is not a point-in-time backup. Capture
   the related DEV database through its existing private backup procedure. Account
   for backup, rehearsal, checkpoint workspace, and rollback-export disk use.
3. On the DEV VPS, run synthetic regressions and failure injection, then rehearse
   compaction on isolated copies with mocked sockets and no provider networking.
   Compare full materialized-state hashes, credentials, per-line pairing flags,
   callback queues, pending/uncertain evidence, native IDs, and sequence numbers.
   Measure journal/snapshot bytes, startup replay wall time, and peak memory before
   and after. Confirm eight mocked lines reconnect from retained credentials.
   These checks do not authorize a live service restart or deployment.
4. Present the implementation and evidence for review. Commit, push, open a PR,
   and deploy only after the user approves those actions for this task. During the
   approved DEV window, drain new sends, stop the bridge/API as appropriate, verify
   a fresh backup, and validate the offline directory with
   `node whatsapp_bridge/src/storage-tool.js validate /PRIVATE/BRIDGE_DATA`.
   Checkpointing is automatic after activation; offline `compact` remains an
   optional rehearsal/recovery tool. Investigate any reported tail or corruption
   before proceeding. This lifecycle change introduces no database migration.
5. Deploy reviewed bridge and API/dashboard assets together with validated storage
   configuration. Install dependencies in the new release's private directory,
   run and verify the pinned provider lifecycle patch before switching releases.
   Install the reviewed watchdog script and prepared service/timer,
   verify private report ownership matches the API user, then enable its timer.
   Start one bridge instance under the existing directory lock.
   On the first activation, existing journals above critical thresholds pause new
   outbound submissions until automatic checkpointing catches up. Account for this
   in the approved maintenance window; an approved offline initial checkpoint is
   also possible with the same verified backup and recovery procedure.
   Confirm all eight real lines reconnect without QR scanning and retain pairing.
   Inspect `/v1/storage`, structured logs, and the staff health banner. Verify
   automatic compaction and lower retained journal bytes, then reconcile callback
   and uncertain-send counts against the pre-deployment observations; normal live
   activity may change counts, so investigate each unexplained difference.
   Do not send customer messages or retry historic uncertain sends as a check.
6. If rollback is required and approved, drain/stop first and retain a fresh private
   copy of the **current** directory. Validate it using this version's storage
   tool. Unresolved `.auth-active`, `.auth-intent`, or `.auth-failure` files prohibit rollback:
   `2a6a225` ignores these safety fences. Investigate and resolve on an isolated
   copy before considering any reader downgrade. Keep the independent watchdog
   installed and running. The streaming-reader release `2a6a225` accepts both published version-1
   snapshot/full-journal and snapshot/suffix-journal combinations; it ignores the
   optional snapshot checksum and restores indefinite journal growth. Returning
   to that release therefore restores the maintenance risk. Preserve the current
   state rather than restoring an older backup.
   Version `2a6a225` preserves the added `pending_mutations` field but does not
   process it. Resolve any pending mutations with the current code before a
   downgrade, or explicitly retain them for later reconciliation; never delete
   them to make an older release appear fully caught up.
7. For releases that predate journal support, use the current tool while offline:
   `node whatsapp_bridge/src/storage-tool.js export /PRIVATE/BRIDGE_DATA
   /PRIVATE/NEW_ROLLBACK_DIRECTORY`. The destination must not exist. Validate the
   export before using it; it contains current credentials, callbacks, messages,
   deduplication IDs, delivery evidence, and media in the legacy JSON form. Keep
   current database delivery records and existing additive columns. Never restore
   only an older backup, delete a journal, or discard an uncertain-send record to
   force startup. Those actions can lose delivery evidence and permit duplicates.

Failures before snapshot publication leave ordinary writes usable unless integrity
checks fail. Failures after publication begins deliberately stop that store. In
either case, preserve the full directory, inspect the safe error code and retained
evidence, validate an offline copy, and rehearse recovery before an approved live
restart. Do not use **Disconnect**, **Remove and unlink**, or credential deletion
as a storage repair procedure.
