# Saida 90062: false restriction warning

Investigated on November 25, 2026. Production inspection was read-only. The
source changes described below have not been deployed or runtime-tested.

## Evidence and limits

Production `whatsapp-bridge.service` journal events for line 90062 show:

| November 24, 2026, Asia/Beirut | Event | Recorded evidence |
| --- | --- | --- |
| 09:44:04 | Generation 107 ready | Successful automatic recovery |
| 19:13:04 | Generation 107 closed | `428`, `connection_terminated`, preceded by `transport_closed` |
| 19:13:06 | Generation 108 connecting | Automatic retry of the saved session |
| 19:13:09 | Generation 108 closed | `403`, `connection_failure` |

The November 24–25 journal query returned 11 closes with code 408, two with
428, and one with 403. Earlier transient failures recovered automatically.
The last recorded event in the inspected connection timeline is the 403.
No matching dashboard connection-management actions were found in the API
journal between 09:40 and 19:14 Beirut time on November 24. This is a log
search, not proof that no person took an action on the phone.

The immediate cause of sustained unavailability is rejection of the saved
linked-device login after the transport closed. The records do not establish
why WhatsApp rejected that login or what originally closed the transport.
They contain no `401`, `conflict:device_removed`, or `conflict:replaced` event
in this incident sequence. They cannot distinguish phone-side manual removal,
automatic session invalidation, or another provider-side authentication issue.
No actor can responsibly be identified from this evidence.

Staff's report that WhatsApp works normally on the phone supports treating
this as a linked-device problem. There is no reliable account-ban evidence.
The three pending updates shown in the warning are the callback queue count;
that counter is independent of account standing and disconnect classification.

A proposed authenticated live-status GET was rejected by automatic approval
review because it would obtain the bearer secret from the production process
environment. It was not retried, and no secret was printed. The findings above
come from journal metadata and source inspection, not that status endpoint.

## Root cause of the false warning

`isRestrictedByWhatsApp()` in the dashboard equated every
`authentication_failure` with a restriction. The bridge assigns that category
to a generic 403. Existing UI tests also asserted this incorrect equivalence.

The installed Baileys 7.0.0-rc14 implementation names 403 `forbidden` and
handles `CB:failure` as `Connection Failure`, passing the numeric reason and
attributes. It does not establish that a ban occurred. The upstream
[disconnect enum](https://github.com/WhiskeySockets/Baileys/blob/master/src/Types/index.ts)
and [failure handler](https://github.com/WhiskeySockets/Baileys/blob/master/src/Socket/socket.ts)
show the same distinction; the pinned local source was also inspected.

## Implemented behavior

| Evidence | Display and recovery |
| --- | --- |
| Generic 403 | Linked-device login rejected; restriction unconfirmed. Stop automatic retries, retain credentials, check the phone, then IT can retry or reset and re-pair. |
| 401 without a specific cause | Login ended, cause unknown; re-pair with the same phone. |
| Structured `conflict:device_removed` | Device removed, initiator unknown; re-pair, including when the numeric code is 403 or 440. |
| Dashboard unlink request | Explicit dashboard source and staff-ID log; no claim that the provider confirmed remote unlinking. |
| 440 without device removal | Session conflict; check for competing session ownership. |
| 411 / 500 | Session or protocol failure; administrator review. |
| Transient transport failure / 515 | Existing automatic recovery with backoff and jitter. |
| Storage failure | Existing storage fence takes precedence, including over device-removal evidence. |

There is no reliable account-ban signal in these disconnect events. The
dashboard therefore never declares a ban from them. A genuine restriction
shown on the phone follows WhatsApp's phone-side review process; a limited
outreach restriction is not evidence of a full account ban or a logout.

Terminal session failures now persist a recovery hold across bridge/worker
restarts. An explicit recovery action clears it. Logout status and timestamps
also survive restart. A bounded metadata record retains the last disconnect
code, reason, source, category, and time without provider payloads or keys.
Re-pairing after a rejected login retains messages and marks the offline gap
for the existing bounded history-recovery mechanism. Recovery remains subject
to what WhatsApp supplies; it cannot guarantee delivery of every missing event.

This prevents misleading restriction warnings and restart-driven retries of
terminal failures. It cannot prevent deliberate phone-side unlinking or
provider invalidation, and the available evidence does not justify changing
keepalive timing, copying credentials, or aggressively retrying rejected logins.

## Verification and rollout

Local source review and `git diff --check` completed. Added/updated regressions
cover generic 403, structured device removal, storage precedence, restart
holds, explicit recovery, dashboard unlink provenance, offline history
boundaries, API metadata filtering, UI wording, and recovery controls.

Runtime tests and a dashboard screenshot remain pending. Per `AGENTS.md`,
execution belongs on DEV and deployment requires reviewed, committed source
and explicit approval. Commit, push, PR creation, and DEV deployment have not
been authorized. No database migration is required. New line-state fields are
additive; older bridge code ignores the hold, so rolling back would restore its
older automatic-start behavior.

After approval, run the repository's unit/bridge suites, support asset check,
compilation, and browser regressions on DEV with mock WhatsApp sockets and
disposable services. Verify a simulated 403 shows session rejection, and
capture the updated dashboard. Do not copy production sessions to DEV.
Production deployment and any live Saida recovery require separate approval.
For Saida, first check the phone's Linked devices list: if the device is gone,
an authorized reset and re-pair is appropriate; if still listed and the account
works normally, one reviewed saved-session retry is available. Preserve
conversation records, pending callbacks, and uncertain-send safeguards.
