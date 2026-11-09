# WhatsApp linked-device bridge

This companion process uses the pinned Baileys `7.0.0-rc14` linked-device protocol to send messages and relay real message edits and revocations. Python keeps the staff dashboard and ticket database. Cloud API lines continue to use the existing transport; only configured or dashboard-created linked-device line IDs use this bridge.

## Install and start

Use Node.js 22 or newer (a maintained release is recommended). Run installation, tests, and services on the DEV VPS; the developer PC is for source editing and Git. Install the exact lockfile, including optional platform binaries needed for image processing:

```sh
cd whatsapp_bridge
npm ci --include=optional --ignore-scripts
npm test
npm start
```

`npm start` reads the project's root `.env` without printing it. Configure these variables there:

```dotenv
WHATSAPP_LINKED_DEVICE_ENABLED=true
WHATSAPP_LINKED_DEVICE_URL=http://127.0.0.1:3001
WHATSAPP_LINKED_DEVICE_SECRET=<same-random-secret-of-at-least-32-characters-in-Python-and-bridge>
WHATSAPP_LINKED_DEVICE_CALLBACK_URL=http://127.0.0.1:8000/api/whatsapp/linked-device/events
WHATSAPP_LINKED_DEVICE_LINES={"90001":""}
WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY=./var/whatsapp-linked-device
```

Line IDs are logical numeric routing IDs, 5–32 digits. They do not have to be Meta phone IDs. With an empty expected phone, the first paired account pins that line to its actual phone number. Later connections reject a different account until an explicit **Disconnect** clears that binding. Set the map value to an international phone number to require a particular test account from the start.

Additional numbers are created from the dashboard without editing `.env` or restarting existing connections. The bridge allocates an unused logical ID starting at `90001`, excluding configured lines, previously registered lines, and IDs reserved by the app. Each number has independent session credentials, pairing QR, message keys, and media storage.

The bridge is the sole writer of its private `lines.json` manifest. Python reads its current `lines` map for routing. Registrations carry a UUID request ID; retries and process restarts return the same number for the same request, preventing duplicate empty pairing rows. The manifest atomically stores `{version:1,lines:{line_id:expected_phone},requests:{request_uuid:{line_id,expected_phone}}}` and is limited to 128 KiB. Optional `removed_line_ids` and `removed_request_ids` arrays retain only numeric IDs and registration UUIDs after complete removal, preventing old `.env` entries or stale registration retries from resurrecting a removed number. Conflicting configured and persisted expected phones fail closed. A line with damaged session storage reports its own error while other numbers continue to work.

Complete number removal uses an authenticated native `remove-companion-device` IQ and requires WhatsApp's explicit reply or provider logout confirmation. A disconnected paired session reconnects temporarily for unlinking, without forwarding customer messages. If WhatsApp cannot confirm unlinking, removal fails and retains the saved session for retry. Once confirmed, the bridge stops only that number, deletes its session directory, media, native keys and callbacks, and removes its active registration records. Minimal removal IDs remain in the manifest so restarts can finish interrupted file cleanup and never reopen the number. Other numbers keep their sessions.

Both processes must use the same `WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY` so Python sees new registrations immediately. Preserve `lines.json` along with the saved sessions during backups and moves.

The data directory resolves relative to the project root, regardless of the current working directory. It stores session credentials, native message keys, captured customer media, operation results, and a callback outbox. Active stores combine legacy JSON/snapshots with fsynced incremental journals; JSON files alone are no longer current. See [incremental storage operations and rollback](../docs/bridge-dashboard-performance.md). The process uses directory permissions `0700`, file permissions `0600`, atomic writes with filesystem synchronization, and one process lock. A clean restart preserves its linked session; an unclean exit requires checking the authentication recovery fences described below. Run one process per data directory.

### Automatic storage maintenance

Every auth store, line-state store, and the shared callback outbox automatically checkpoints at **64 MiB**, **10,000 transactions**, or **six hours** since the previous checkpoint. A 30-second monitor also triggers maintenance on idle stores. One worker at a time reconstructs a durable journal prefix while normal writes continue. After copying concurrent writes into a replacement journal, a short per-store write barrier publishes the fsynced snapshot, syncs its directory, then atomically replaces the journal and syncs the directory again. This needs no WhatsApp logout or re-pairing. Socket generations share one auth journal owner; retired sockets cannot append keys or credentials.

Maintenance preserves current credentials, keys, callbacks, pending sends, delivery evidence, and deduplication records. It removes obsolete journal revisions, not authoritative state. Snapshot size and live-state memory can still grow as retained records grow. Worker memory, snapshot size, workspace, final catch-up bytes, and execution time have explicit limits; a blocked checkpoint retains the original journal and reports an alert. See the [protocol, limits, and recovery procedure](../docs/bridge-dashboard-performance.md#automatic-journal-lifecycle) before changing them.

Configuration uses the `WHATSAPP_LINKED_DEVICE_STORAGE_` prefix. Defaults and every option appear in [`.env.example`](../.env.example). `COMPACT_BYTES`, `COMPACT_TRANSACTIONS`, and `COMPACT_AGE_MS` trigger checkpoints; `RETRY_MS` controls backoff. Leave `AUTO_COMPACT=true`. Free-space checks retain a 128 MiB reserve and 64 inodes after planned worker writes. Default journal warning/critical limits are 128/256 MiB, snapshot limits are 96/120 MiB before the 128 MiB new-checkpoint cap, disk limits are 2 GiB/512 MiB or 5%/1% available, and inode limits are 10%/5% available. Either byte or percentage threshold applies.

The authenticated `GET /v1/storage` endpoint exposes per-line auth/state and shared-outbox sizes, growth rate, replay duration, checkpoint status/failures, recovery errors, available disk bytes, and inodes. `GET /v1/status` includes the same storage health. Structured systemd logs emit `storage_alert`, `storage_recovered`, replay, compaction, and recovery events. Repeated unchanged alerts are limited to every 15 minutes. The existing staff dashboard health banner and local notification report storage pressure automatically while the dashboard is active. No external alert service or message destination is configured by this change.

Critical pressure rejects new outbound submissions with HTTP 503 `STORAGE_PRESSURE` and `delivery_uncertain:false` before provider invocation. A line-specific issue blocks that line; a filesystem or outbox issue affects all lines. Auth updates, receipts, callbacks, and reconciliation still persist. Existing uncertain sends retain their evidence and are never blindly resent. An unavailable filesystem probe becomes critical after 90 seconds without a successful sample. Resolve capacity or compaction problems through the operator procedure; a storage alert never calls for deleting credentials or pairing again.

Start the Python app and open the manager's **WhatsApp numbers** controls. Select the configured test line, click **Pair WhatsApp**, and scan its QR from the test phone's WhatsApp **Linked devices** screen. After pairing, click **Add to app** to register the test line before accepting customer callbacks. QR codes and all bridge endpoints require the shared bearer secret. The bridge binds only to loopback; do not expose this port directly.

For each additional number, click **Link another number**, scan the new QR with that number's phone, and choose **Add to app** after it connects. A failed registration can be resumed with **Retry linking**. Existing numbers remain connected while another number is paired.

### Keep the local bridge running

On a Linux desktop with systemd, run this from the project root after installing the dependencies:

```sh
venv/bin/python scripts/install_whatsapp_bridge_service.py
systemctl --user status whatsapp-support-bridge.service
```

The installer creates a user service with the actual project and Node paths, starts it immediately, and enables it for subsequent logins. The service reads the existing private `.env`, reconnects the saved WhatsApp session, and restarts after a process failure. It stays running when a terminal or coding session closes. Do not run a second `npm start` while this service is active. A normal user service stops at logout unless that user's systemd manager is configured to remain running; use the system service below for a server that must run continuously.

```sh
# Restart after changing the bridge configuration or code:
systemctl --user restart whatsapp-support-bridge.service
# View recent logs:
journalctl --user -u whatsapp-support-bridge.service -n 50 --no-pager
# Stop and disable the local service when rolling back:
systemctl --user disable --now whatsapp-support-bridge.service
```

For a server, adapt [whatsapp-bridge.service.example](whatsapp-bridge.service.example) to the deployment's user, project directory, and absolute Node binary path. Its `ProtectHome=true` setting requires the project and session data to be outside home directories. The example unit is not installed automatically.

## Message guarantees and limits

Outbound IDs use `ld:<line_id>:<native_whatsapp_id>`. Original native keys are captured on every send or receive. Messages sent previously through Cloud API cannot be edited or recalled through this connection because their native keys are unavailable.

Edits relay WhatsApp's `MESSAGE_EDIT` protocol message and revocations relay `REVOKE`, with Baileys's required native edit attributes. The bridge returns `confirmed: true` only after receiving an explicit WhatsApp server acknowledgment for the operation's native ID. This confirms server acceptance; a recipient who is offline will receive the update when WhatsApp delivers it. A timeout or disconnect is reported as uncertain and does not fabricate a local success.

Each mutation has a durable caller-supplied `operation_id`. Retries reuse the same native operation ID, timestamp, original key, and protocol payload. The same ID cannot be reused for a different change. An uncertain operation can be retried for reconciliation after the normal eligibility window expires; a new operation cannot. Text edits require an original text message sent by the paired account within 15 minutes; deletion is limited conservatively to 48 hours. WhatsApp itself can impose further restrictions. Deletion leaves WhatsApp's standard deleted-message placeholder on the recipient's phone.

Real edits and revocations arriving from the customer or the paired account's other linked devices create authenticated callbacks into Python. The bridge captures native edit timestamps before Baileys coalesces events and rounds its derived updates to seconds, so rapid changes keep their original millisecond order. Local chat deletion events are never treated as deletion for everyone. Manual outgoing messages from the WhatsApp app or another linked device create separate `outgoing_message` callbacks for the staff dashboard; dashboard sends are reserved before network dispatch so their echoes cannot appear twice. Staff sends with a `client_message_id` also create an idempotent `outgoing_confirmation` after WhatsApp acknowledges the send, or after a real echo confirms an uncertain send. This repairs a ticket message if the bridge response is lost. If a phone-origin message is revoked before its callback reaches the dashboard, the bridge removes its content but delivers a redacted original marker before the revoke, so the ticket still shows a deleted-message placeholder. A customer message revoked before its normal callback instead creates a content-free `message_tombstone` before the revoke. This callback bypasses the AI flow and creates only a deleted placeholder. New messages, media, locations, reactions, and reply references from both sides are normalized to the app's existing webhook shape. Unsupported outgoing content appears as a timeline placeholder without copying its private native payload into the callback. LID addresses resolve through WhatsApp's verified phone mapping before ticket routing.

Callback delivery is durable and uses a 5-second timeout and exponential backoff capped at 60 seconds. A delayed callback does not prevent unrelated callbacks from being delivered. Transient failures retry for seven days. Permanent HTTP 4xx failures other than 429, and events exceeding the retry period, remain in the durable outbox dead-letter list (base JSON plus journal) for investigation. The status API reports `pending_callbacks` and `failed_callbacks` per line.

The bridge accepts media up to 192 MiB so videos can use the dashboard's 192 MB limit. The application still enforces its separate image, audio, sticker, and document limits. It stores customer content privately and streams incoming downloads with a size limit. Confirmed deletion removes the original message payload, unreferenced attachment files and references, prior edit payloads, and superseded callbacks from the bridge. It retains native keys, operation IDs, fingerprints, and results needed for deduplication. File deletion is durable and resumes after a crash. The dashboard separately removes its database content and attachment cache.

## API

All endpoints require `Authorization: Bearer <WHATSAPP_LINKED_DEVICE_SECRET>`. All POST requests use JSON.

| Endpoint | Result |
| --- | --- |
| `GET /v1/status` | Lines, status, phone, pairing QR, callback counters, storage health |
| `GET /v1/storage` | Cached filesystem sample, live per-store storage counters, warning/critical issue codes; no credentials or file paths |
| `POST /v1/lines` | `{request_id:UUID,expected_phone:"",reserved_line_ids:[]}`; allocate and durably register a line, return its status |
| `DELETE /v1/lines/:line` | Confirm remote unlink, purge this number and persist removal; returns `{line_id,removed:true,unlinked:true}` |
| `POST /v1/lines/:line/connect` | Start or resume pairing; query status for the QR |
| `POST /v1/lines/:line/disconnect` | Explicit logout and clear pinned account binding |
| `POST /v1/lines/:line/messages` | Send app's Meta-shaped text/media/location/reaction payload |
| `POST /v1/lines/:line/media` | Upload `{content_base64,mime_type,filename}`; returns private `ldmedia:` ID |
| `GET /v1/lines/:line/media/:media_id` | Authenticated bytes and MIME type |
| `POST /v1/lines/:line/messages/:message_id/edit` | `{to,body,operation_id}`; ACK-confirmed original ID and native operation ID |
| `POST /v1/lines/:line/messages/:message_id/delete` | `{to,operation_id}`; ACK-confirmed revoke |

Statuses are `disconnected`, `connecting`, `qr`, `connected`, `logged_out`, and `error`. Status objects include `is_paired` so retained disconnected sessions can offer explicit unlinking, and `uncertain_sends` counts dashboard sends still awaiting confirmation. Number registration returns per-call `created:true` for a new number and `created:false` for a UUID retry. Complete removal is idempotent for tombstoned IDs, including after a crash during cleanup; a removed registration UUID cannot be reused. Callback events contain stable `event_id`, `type`, `line_id`, `display_phone_number`, and integer millisecond timestamps. `outgoing_message` events include the wrapped `message_id`, `from_me:true`, the chat counterpart's `sender_phone`, and a normalized `payload`. `outgoing_confirmation` events include the wrapped `message_id`, original `client_message_id`, counterpart's `sender_phone`, and `from_me:true`. `message_tombstone` events contain only the original wrapped `message_id`, customer `sender_phone`, `from_me:false`, original timestamp, and `deleted_timestamp`; they carry no message body or media. Mutation events include the wrapped original `message_id`, `from_me`, the chat counterpart's `sender_phone`, and edited `body`. App-initiated changes also include their `operation_id`.

## Verify and roll back

Create and verify a complete private backup before changing session storage. Rehearse synthetic and copied-state compaction on the DEV VPS using mocked sockets and isolated directories; never connect a copied live WhatsApp session to the provider. Capture all eight lines' pairing state, callback queues, uncertain-send counts, and durable-state hashes before and after the isolated rehearsal. Tests and source preparation do not authorize a service restart, deployment, commit, push, or PR. DEV deployment requires explicit review and approval; do not modify production services or data.

The snapshot and journal remain version 1, and published snapshot/full-journal and snapshot/suffix-journal combinations are readable by the streaming reader in `2a6a225`. Older readers ignore the new optional snapshot checksum, so they do not provide the same protection. During an approved rollback, stop and drain first, validate with the new storage tool, and preserve current data. For pre-journal releases, export current materialized state into a new private directory with the current tool. Never restore only an old backup over new delivery records; never delete journals or quarantined tails to force startup. Full steps are in [deployment and rollback](../docs/bridge-dashboard-performance.md#staged-deployment-and-rollback--explicit-approval-required).

The new authentication owner tracks the installed Baileys transaction wrapper, including buffered keys and commit retries. Already admitted incoming messages and receipts drain even if credential persistence retires the socket. Private `.auth-active`, `.auth-intent`, and `.auth-failure` fences prevent reconnecting with uncertain authentication state. A clean shutdown clears completed work. By default (`WHATSAPP_LINKED_DEVICE_AUTH_RESUME_AFTER_UNCLEAN_EXIT=true`) the bridge never makes staff re-pair a number for work that was merely in progress:
- After an unclean exit (crash, SIGKILL, power loss), a line resumes from its fsynced journal when its markers are `.auth-active`, `.auth-intent` (an uncommitted change in flight), a torn journal tail (already quarantined as `.journal.tail-*.bin`), or an `.auth-failure` that began with a drain timeout. Markers are renamed with a `.resumed-<ms>`/`.abandoned-<ms>` suffix, never deleted, and `auth_session_resumed_after_unclean_exit` is logged with the reason.
- Retiring a socket waits up to `WHATSAPP_LINKED_DEVICE_AUTH_DRAIN_TIMEOUT_MS` (default 60000, minimum 30000) for running provider work. Work still running at the deadline is abandoned (`auth_work_abandoned`), its intents are kept as `.auth-intent.abandoned-<ms>`, and the next socket starts from the committed state.
- After a dead or closed transport, the reconnect waits only `WHATSAPP_LINKED_DEVICE_AUTH_RECONNECT_DRAIN_TIMEOUT_MS` (default 15000, minimum 5000), because work stuck on a dead socket cannot finish. A text send or edit/delete that WhatsApp does not acknowledge within 30 s marks the transport stale and reconnects; media sends keep 120 s for the upload.
- Provider pipelines write an intent only when they first start a key transaction or mutation, so a large initial or offline sync does not create thousands of intents.
- An `.auth-failure` caused by a real write error (disk or journal) still fences that line only and logs `auth_session_fenced_after_unclean_exit`.

The discarded in-flight change can make a few chats show "Waiting for this message" until WhatsApp's retry receipts resend them. Set the variable to `false` for the previous strict behaviour, where crashes and stuck work fence the line until it is reviewed. Back up these files, retain their evidence, and never bypass them with `2a6a225`, which does not understand the fences. Offline validation and export refuse unresolved fences.

The pinned Baileys `7.0.0-rc14` dependency requires the checked-in lifecycle patch. `npm ci` installs it through `postinstall`; direct `node src/index.js` startup verifies it before loading the provider or session storage. The patch exposes complete native callback and detached-task lifetimes so retirement can drain them safely. Unknown versions/source hashes and symlinked dependency roots are rejected. Install into a private release dependency directory, never shared running dependencies. If installation disables scripts, run `node scripts/patch-baileys-lifecycle.js` and then the same command with `--verify` before activation. Provider upgrades require reviewing this patch and its real-provider regressions.

Startup signal handling is installed before asynchronous verification or lock
acquisition. The runtime owns a bridge before initialization, and all configured
stores finish preparation before any provider socket opens. SIGTERM/SIGINT stop
new admission and cancel replay reads, then drain accepted events, credentials,
background recovery, media publication and HTTP handlers. Repeated signals share
the same shutdown result. A disconnected HTTP client does not release ownership
of its unfinished handler. The storage lock is released only after cleanup has
settled successfully; a failed cleanup remains unsuccessful on repeated calls.

The runtime reports `shutdown_deadline_exceeded` after 30 seconds and exits
unsuccessfully even if cleanup later finishes. It never releases a still-live
owner's lock just to meet a deadline. DEV's unchanged systemd stop limit is
35 seconds: uninterruptible filesystem I/O can still cause a forced termination
and require authentication recovery review. Replay cancellation finishes any
already-started tail-evidence synchronization protocol. Checkpoint publication
receives a five-second grace period; interrupted publication fails closed.
The [measured backup capacity plan](../docs/bridge-backup-capacity.md) requires
separate cleanup and deployment approval; it does not authorize live changes.

The prepared independent systemd watchdog checks capacity and bridge availability even when the bridge fails to start. Install its script outside release directories and enable its timer only during the approved DEV deployment. Its private `/run` report feeds the existing staff health banner; safe alerts also reach journald. Outgoing uploads are rejected before attachment allocation under critical pressure, reserve capacity across concurrent requests, and reuse identical media on retries. Essential authentication, incoming-message, and delivery-evidence writes remain enabled.

The Node tests use mock WhatsApp sockets and temporary local HTTP servers. They cover real protocol dispatch/ACK gating, timeout and rejection behavior, durable idempotence, rapid edits, ownership and time windows, incoming changes, LID mapping, protected media, request limits, callback retries, phone binding, corruption detection, process locking, concurrent registration, restart-safe allocation, and isolation of broken line storage. They never send messages to a real number.

Before moving a production number, use two test phones to verify send, edit, delete-for-everyone, and incoming edits/revocations. Restart both processes and verify that the test session and operation results survive. Check that the original Cloud API line still sends normally.

To remove one number completely, click **Remove and unlink** for it in the dashboard and confirm closing its active conversations while retaining their history. Python deactivates its app route before native unlinking, then archives its active conversations after confirmation. A failed unlink retains the inactive entry and conversations for retry. The bridge deletes this number's session files and persists removal IDs to prevent a restart from reopening it.

To stop the test integration, remove its linked numbers, stop this process, and set `WHATSAPP_LINKED_DEVICE_ENABLED=false`. Keep the Cloud API configuration unchanged. A filesystem backup restores code and local configuration; it does not reverse changes already delivered to phones or restore provider registrations after moving a production number.

Baileys is unofficial. Review its [current source](https://github.com/WhiskeySockets/Baileys/tree/v7.0.0-rc14), [message relay implementation](https://github.com/WhiskeySockets/Baileys/blob/v7.0.0-rc14/src/Socket/messages-send.ts), and [protocol handling](https://github.com/WhiskeySockets/Baileys/blob/v7.0.0-rc14/src/Utils/process-message.ts) when upgrading the pinned dependency.
