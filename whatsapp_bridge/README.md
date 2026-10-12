# WhatsApp linked-device bridge

This companion process uses the pinned Baileys `7.0.0-rc14` linked-device protocol to send messages and relay real message edits and revocations. Python keeps the staff dashboard and ticket database. Cloud API lines continue to use the existing transport; only configured or dashboard-created linked-device line IDs use this bridge.

## Install and start

Use Node.js 22 or newer (a maintained release is recommended). Install the exact lockfile, including optional platform binaries needed for image processing:

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

The data directory resolves relative to the project root, regardless of the current working directory. It stores session credentials, native message keys, captured customer media, operation results, and a callback outbox. The process uses directory permissions `0700`, file permissions `0600`, atomic writes with filesystem synchronization, and one process lock. A restart preserves its linked session. Run one process per data directory.

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

Callback delivery is durable and uses a 5-second timeout and exponential backoff capped at 60 seconds. A delayed callback does not prevent unrelated callbacks from being delivered. Transient failures retry for seven days. Permanent HTTP 4xx failures other than 429, and events exceeding the retry period, remain in the durable `outbox.json` dead-letter list for investigation. The status API reports `pending_callbacks` and `failed_callbacks` per line.

The bridge accepts media up to 25 MiB. It stores customer content privately and streams incoming downloads with a size limit. Confirmed deletion removes the original message payload, unreferenced attachment files and references, prior edit payloads, and superseded callbacks from the bridge. It retains native keys, operation IDs, fingerprints, and results needed for deduplication. File deletion is durable and resumes after a crash. The dashboard separately removes its database content and attachment cache.

## API

All endpoints require `Authorization: Bearer <WHATSAPP_LINKED_DEVICE_SECRET>`. All POST requests use JSON.

| Endpoint | Result |
| --- | --- |
| `GET /v1/status` | Lines, status, phone, pairing QR, callback counters |
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

The Node tests use mock WhatsApp sockets and temporary local HTTP servers. They cover real protocol dispatch/ACK gating, timeout and rejection behavior, durable idempotence, rapid edits, ownership and time windows, incoming changes, LID mapping, protected media, request limits, callback retries, phone binding, corruption detection, process locking, concurrent registration, restart-safe allocation, and isolation of broken line storage. They never send messages to a real number.

Before moving a production number, use two test phones to verify send, edit, delete-for-everyone, and incoming edits/revocations. Restart both processes and verify that the test session and operation results survive. Check that the original Cloud API line still sends normally.

To remove one number completely, click **Remove and unlink** for it in the dashboard and confirm closing its active conversations while retaining their history. Python deactivates its app route before native unlinking, then archives its active conversations after confirmation. A failed unlink retains the inactive entry and conversations for retry. The bridge deletes this number's session files and persists removal IDs to prevent a restart from reopening it.

To stop the test integration, remove its linked numbers, stop this process, and set `WHATSAPP_LINKED_DEVICE_ENABLED=false`. Keep the Cloud API configuration unchanged. A filesystem backup restores code and local configuration; it does not reverse changes already delivered to phones or restore provider registrations after moving a production number.

Baileys is unofficial. Review its [current source](https://github.com/WhiskeySockets/Baileys/tree/v7.0.0-rc14), [message relay implementation](https://github.com/WhiskeySockets/Baileys/blob/v7.0.0-rc14/src/Socket/messages-send.ts), and [protocol handling](https://github.com/WhiskeySockets/Baileys/blob/v7.0.0-rc14/src/Utils/process-message.ts) when upgrading the pinned dependency.
