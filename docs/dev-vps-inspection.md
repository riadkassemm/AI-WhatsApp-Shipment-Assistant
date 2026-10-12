# Development VPS deployment record

Deployment date: **2026-10-12**. Application VPS: `158.220.113.93`.
Database host: `94.156.144.65`.

The user selected **deploy the updated local project** and **start fresh with
linked-device pairings on the VPS**. The release and configuration described
below have been installed and verified. The target is active and enabled for
boot. Test line `90005` is paired, and consecutive message edits reached the
recipient phone. This record contains paths and service
relationships without credentials or private session contents.

## Selected deployment

| Component | Selected configuration |
| --- | --- |
| Release | `/opt/whatsapp-support/releases/20261012-dev-v19-ticket-history-number-check`, selected by `/opt/whatsapp-support/current`; `20261012-dev-v19-ticket-history`, `20261012-dev-v19-auth-loading`, and earlier releases retained for rollback |
| API | Retains `shipment-bot.service`; runs as `whatsapp-support` from the selected release on `127.0.0.1:8000`, one Uvicorn worker, no reload, ten-second graceful shutdown limit |
| Readiness | API pre-start runs `scripts/check_runtime.py --wait 60 --schema` against the private service environment; checks database access, required schema, and Redis without changing schema |
| Private configuration | `/etc/whatsapp-support/app.env`, root-owned mode `0600`; preserves the VPS configuration with deliberate database, bridge, and state-path overrides |
| Database access | Existing shared `shipment-db-tunnel.service`; API now uses `127.0.0.1:3307`, forwarded to database-host `127.0.0.1:3306` |
| Redis | Retains the existing development instance on `127.0.0.1:6379`; no Redis migration or Pub/Sub namespace change |
| Bridge | New `whatsapp-bridge.service`, same `whatsapp-support` account, loopback `127.0.0.1:3010` |
| Linked-device state | `/var/lib/whatsapp-support/linked-device`; fresh pairing line `90005`; no PC credentials, sessions, or callback queues copied |
| Support media | `/var/lib/whatsapp-support/support-media`; existing 242 files, approximately 33 MB, copied during cutover; original cache retained |
| HTTPS ingress | Existing `ngrok.service`, private configuration, token, and URL `https://sanction-lake-exalted.ngrok-free.dev` retained |
| Service group | Active and boot-enabled `whatsapp-dev.target` controls the API, bridge, and ngrok; it also starts the existing tunnel and Redis when needed without owning their stop/restart lifecycle |
| Rollback backup | Private `/var/backups/whatsapp-support/20261012-dev-v19` contains original private configuration, ngrok configuration, unit definitions, and metadata |
| Original source | `/root/whatsapp_chatbot_latest` retained unchanged |

The reviewed configuration sources are in
[deployment/dev-vps](../deployment/dev-vps/README.md). The generic
[deployment examples](../deployment/examples/README.md) describe a new host;
the existing VPS uses the concrete service names and shared dependencies above.

### State and account decisions

The bridge starts without a copied pairing. Line `90005` is reserved for the
fresh test account. The new manifest retains removal records for historical
lines `90001` through `90004`, consistent with database records, so unavailable
old sessions do not silently become Cloud routes. Existing message/ticket history
is retained. Pair the separate test phone through the dashboard after service
verification; pairing and actual message tests remain outstanding.

Line `90005` was later removed before pairing. Its database records showed zero
tickets and zero native messages, and its state directory had no saved session.
At the user's request, `90005` was restored as an unpaired line in the bridge
manifest and the app's inactive number registry. Its previous manifest was saved
at `/var/backups/whatsapp-support/20261012-dev-v19/lines-before-readd-90005.json`.
The manager pairing status now shows `90005` as disconnected and available to
pair; the removal records for `90001` through `90004` remain intact.

API and bridge share the absolute state path and the same private bridge secret.
Their source and installed dependencies are root-owned and read-only to the
service account. The persistent state directory remains writable only where
required, outside the release. Python's private configuration is supplied by
systemd; no project `.env` is copied into the release.

## Existing services preserved

The initial inspection found an older API running as root from
`/root/whatsapp_chatbot_latest`, directly using database-host `3306`. The existing
key-authenticated tunnel already worked, so the replacement uses that tunnel
rather than provisioning another key or opening another forward.

The tunnel uses its existing `shipment-tunnel` account, forwarding key, verified
host keys, strict host-key checking, batch authentication, and keepalives. Its
configuration is unchanged. `shipment-db-tunnel.service` and
`redis-server.service` remain independently enabled shared services. Neither has
`PartOf=whatsapp-dev.target`; stopping or restarting the target leaves them
running. This preserves other applications that use them.

The existing ngrok unit's `PartOf=shipment-bot.service` assignment was replaced
directly with `PartOf=whatsapp-dev.target`, preserving its original command and
private configuration. A drop-in cannot clear that original
unit dependency; the drop-in only configures ordering, restart behavior, and
stop timeout. Verified `systemctl show ngrok.service -p PartOf` reports only
`whatsapp-dev.target`.

The initial cutover restarted ngrok while retaining the same public endpoint.
After the dependency correction, a verified API restart left ngrok running;
a verified target restart restarted ngrok together with API and bridge. The target is
now the enabled boot entry, and the separate API/ngrok boot entries are disabled.
Their original definitions are retained. The shared tunnel, Redis, and canary
processes remained unchanged during cutover.

Nginx, its TLS sites, the API canary on `8001`, the separate Redis service on
`6380`, and other Node applications on `3000`/`3001` are untouched. The existing
TLS hostname `export-customer-service.mik-holding.co` continues to proxy to
`127.0.0.1:8000`. The canary and this development API use the same application
database; the canary is not an isolated test environment.

## Verification record

Preparation checks passed:

- Real database access and required support schema through `3307`.
- Redis access on the retained `6379` instance.
- Dependency installation and filesystem access as the new service account.
- Installed systemd unit validation with `systemd-analyze verify`.
- Remote Python tests: **374 passed, 1 skipped**.
- Remote bridge tests: **34 passed**.

Final infrastructure checks passed:

- API and bridge are active, with the target enabled at boot and separate
  API/ngrok boot entries removed.
- Startup readiness passed as the real service account under the configured
  filesystem restrictions.
- `/support` returned HTTP 200 with the updated interface through both
  `https://sanction-lake-exalted.ngrok-free.dev/support` and
  `https://export-customer-service.mik-holding.co/support`.
- Restarting only the API left bridge, ngrok, tunnel, Redis, and canary processes
  unchanged.
- Restarting the target replaced API, bridge, and ngrok processes while leaving
  the shared tunnel, Redis, and canary unchanged; the canary remained healthy.
- All 242 support-media files were preserved; original source and cache remain
  intact.
- Fresh line `90005` has no saved credentials, with zero pending and zero failed
  callbacks before pairing.

Test-account pairing, inbound/outbound messages, voice-note continuity and
speeds, and remote edit/delete behavior require an end-to-end check on this new
VPS pairing. Process/readiness and interface checks do not establish those
WhatsApp results. No production account migration or database restore was
performed.

## Message history update on 2026-10-12

The follow-up release `20261012-dev-v19-edit-history` adds repeated staff text
edits and edit/delete controls for eligible messages in customer-started tickets,
staff-started chats, and previous ticket history. History actions address the
message's original ticket. Immediate send responses expose the same eligibility
flags as refreshed details; original authors retain narrow mutation and realtime
access to their own older messages after reassignment. WhatsApp's original-send
time limits and provider confirmation remain required.

The new release was built separately with pinned Python and Node dependencies.
Both local and VPS suites passed: 381 Python tests collected with one skip, and
35 bridge tests passed. No new database migration or private session transfer was
needed. The API and bridge were stopped before the atomic `current` link change.
The selected release, all target services, and both public `/support` URLs were
verified afterward; both HTTPS endpoints returned HTTP 200. The old release
remains installed for a code-only rollback by switching `current` back and
restarting the API and bridge.

The first activation attempt encountered an unrelated database outage. MariaDB
on `94.156.144.65` had been killed by the host's out-of-memory handler. The
activation script restored the prior release; after inspecting service and memory
state, MariaDB was restarted and the existing tunnel carried a database handshake
again. The second activation passed the API's database, Redis, and schema
readiness check. The database host had exhausted its 2.5 GiB swap shortly after
recovery, so memory pressure requires monitoring and capacity work before
production. This release did not modify the database host's firewall, swap, or
other applications.

At the time of that release, live WhatsApp edit/delete verification still
required pairing line `90005` and checking a customer-side phone. The suites
verified the native bridge protocol and server update path, but did not
establish recipient display.

## Repeat-edit interface hotfix on 2026-10-12

Line `90005` is now paired, connected, and active. A live test in customer-origin
ticket `200` confirmed two WhatsApp-acknowledged edits to the same staff message
and one Delete for everyone operation, with no pending change or failed bridge
callback. The first edit appeared on the recipient phone. The staff page kept its
new Edit button disabled after a successful edit until the user switched away
and back: it rendered the button while `state.busy` was still true and did not
update that button when the request finished.

Release `20261012-dev-v19-repeat-edit-ui` removes that render-time disabled
assignment. The edit handler still blocks clicks during an active request. A
regression test now renders the post-edit row while busy and completes a second
edit without navigation. Focused UI/message tests passed locally and on the VPS.
This API-only cutover passed database/schema readiness and local/public HTTP
checks; the bridge and ngrok processes stayed running, preserving the paired
session. The staff browser needs one page refresh to load the new interface.
After that refresh, the user edited a fresh staff message in ticket `200` twice
in a row without switching conversations and confirmed both edits appeared on
the recipient phone. Message `1333` reached version `3` with two confirmed edit
operations, no pending change, and no failed bridge callback.

## Refresh navigation update on 2026-10-12

Release `20261012-dev-v19-refresh-navigation` keeps the selected queue tab,
page, and ticket or staff chat when the browser reloads. Its tab-scoped session
storage contains only the verified staff ID's view, page, conversation ID, and
conversation type. After reload, the normal API request checks current access
and permissions. A missing or inaccessible conversation clears the saved
selection and leaves the queue open; signing out clears saved selections.

Focused UI and message tests passed locally and on the VPS, with the existing
Chromium-dependent test skipped. The staged release passed database, Redis,
and schema readiness. An initial cutover automatically rolled back because an
immediate HTTP check ran before Uvicorn opened its port; the service itself
started normally. The retry waited for startup and selected the new release.
The API, bridge, ngrok, database tunnel, Redis, and target were active after
cutover. Bridge, ngrok, tunnel, and Redis PIDs stayed unchanged. Local and both
public `/support` URLs returned HTTP 200, and the served page contained the
new navigation code. The user confirmed that Ctrl+R reopened ticket `200`
with the same queue tab. A tab still running the previous JavaScript needs one
reload to receive this update; after reopening a conversation, later reloads
can restore it.

## Reload error update on 2026-10-12

The user saw a brief red `NetworkError when attempting to fetch resource`
while reloading an open ticket. The API logs around those reloads showed
successful `/me`, queue, and ticket-detail responses, with no server error.
Firefox can reject a pending old-page fetch when navigation unloads that page;
the old ticket refresh handler displayed that cancellation as an error.

Release `20261012-dev-v19-reload-network` marks the page as leaving before
unload, invalidates pending ticket and queue fetch results, and does not show
their late cancellation errors or clear the saved workspace. Real network
failures while the page remains active still show an error. Focused UI and
message tests passed locally and on the VPS, including cases for cancelled
ticket fetches, late authentication responses, and active network failures.
The existing Chromium-dependent test was skipped. The staged release passed
database, Redis, and schema readiness. The API-only cutover selected the new
release after a three-second startup wait; bridge, ngrok, tunnel, and Redis
PIDs stayed unchanged. The local and both public `/support` endpoints returned
HTTP 200 with the new code. The user confirmed that ticket `200` reopened and
the red network error no longer appeared on repeated refresh.

## Session check loading update on 2026-10-12

The user observed a brief login form flash before the dashboard appeared on
refresh. The HTML previously displayed the login form while `/me` checked a
valid staff session. Release `20261012-dev-v19-auth-loading` starts with a
neutral session-check view instead. The dashboard or login form appears only
after authentication has been checked. A temporary connection failure offers
Retry without clearing the saved conversation; a confirmed 401 shows sign-in.
Focused UI and message tests passed locally and on the VPS, with the existing
Chromium-dependent test skipped. The staged release passed database, Redis,
and schema readiness. The API-only cutover selected the new release after a
three-second startup wait; bridge, ngrok, tunnel, and Redis PIDs stayed
unchanged. Local and both public `/support` endpoints returned HTTP 200 and
served the new session-check markup. The user confirmed that Ctrl+R returned to
ticket `200` without a login-form flash or red network error.

## Staff-started chat live test on 2026-10-12

The user started staff-origin chat `201` on test line `90005` and confirmed on
the recipient phone that two consecutive edits and Delete for everyone worked.
The same chat reopened after browser refresh. Message `1336` reached version
`4`, with two confirmed edit operations and one confirmed everyone deletion;
no change remains pending.

An initial chat-start request returned HTTP 409 while customer ticket `200`
was still active. The user then resolved that ticket and separately deleted
its saved history before starting chat `201`. The logs show no failed chat
attempt between resolution and deletion. Resolving releases the ticket's
active slot; deleting its history was not required to start the chat.

## Daily operation after verified cutover

```sh
# Control the API, bridge, and development ingress together.
systemctl start whatsapp-dev.target
systemctl stop whatsapp-dev.target
systemctl restart whatsapp-dev.target

# Restart the API without restarting the tunnel, Redis, or ngrok.
systemctl restart shipment-bot.service

# Restart only the bridge; saved pairings remain in persistent state.
systemctl restart whatsapp-bridge.service

# Check the services, including shared dependencies.
systemctl status shipment-bot.service whatsapp-bridge.service ngrok.service shipment-db-tunnel.service redis-server.service
journalctl -u shipment-bot.service -u whatsapp-bridge.service -u ngrok.service -n 100 --no-pager
journalctl -u shipment-bot.service -u whatsapp-bridge.service -f
```

An active target is not proof that every service or external dependency is
healthy. Check child states, startup readiness, bridge connections, and pending
or failed callbacks. Administrative SSH can be disconnected after deployment;
systemd supervises the running services independently.

## Rollback for this server

Keep the private backup directory and the original source intact. Rollback must
stop the replacement API and bridge, restore the prior API and ngrok unit
configuration and boot settings, then restart the old API and existing ngrok.
The original API reads its unchanged private `.env` in
`/root/whatsapp_chatbot_latest`. The shared tunnel, Redis, Nginx, canary, and other
applications do not need to be stopped or replaced.

The backup contains `shipment-bot.service`, `ngrok.service`, `old-app.env`,
`ngrok.yml`, and `deployment.json`. A preexisting `ngrok.service.d` directory was
also backed up if present. The prior API and ngrok boot entries were enabled.
Review the backup metadata before using this sequence if their configuration
has changed since this deployment.

Run the following as root on the application VPS to restore the recorded
predeployment service arrangement:

```sh
set -eu
backup=/var/backups/whatsapp-support/20261012-dev-v19

test -f "$backup/shipment-bot.service"
test -f "$backup/ngrok.service"
test -f "$backup/deployment.json"

systemctl stop whatsapp-dev.target
systemctl disable whatsapp-dev.target

install -o root -g root -m 0644 "$backup/shipment-bot.service" /etc/systemd/system/shipment-bot.service
install -o root -g root -m 0644 "$backup/ngrok.service" /etc/systemd/system/ngrok.service
rm -f /etc/systemd/system/ngrok.service.d/dev-stack.conf
if [ -d "$backup/ngrok.service.d" ]; then
    install -d -o root -g root -m 0755 /etc/systemd/system/ngrok.service.d
    cp -a "$backup/ngrok.service.d/." /etc/systemd/system/ngrok.service.d/
fi

systemctl daemon-reload
systemctl enable shipment-bot.service ngrok.service
systemctl start shipment-bot.service ngrok.service
systemctl status shipment-bot.service ngrok.service --no-pager
```

This restores the original unit definitions, including their original source
and private-configuration paths. The original source `.env` and ngrok config
were retained unchanged, so the backup private files are available for recovery
without needing to overwrite the live originals in this sequence. The shared
tunnel, Redis, Nginx, and canary remain running. No database restore is included.

Preserve the new state and media directories; rollback must not delete them or
unlink accounts. Keep one API on `8000` and one ngrok endpoint instance. Any new
linked-device pairing will be offline while the old build, which lacks this
bridge, is running. Restoring code does not undo delivered WhatsApp
edits/deletions or reverse database changes. Confirm the old dashboard and public
endpoint after rollback, then retain the newer state for a later redeployment.

## Returning-customer history update

Release `20261012-dev-v19-ticket-history-number-check` is active. Opening a ticket or chat
shows earlier resolved conversations with the same customer on the same physical
WhatsApp number, including a Meta-to-linked-device change in connection ID.
When both tickets store a business display number, the app checks those
numbers even if their internal line IDs are equal, so reusing a line ID cannot
mix history from different physical numbers.
Regular staff see only conversations they handled; supervisors and admins see
all matching earlier conversations. The dashboard loads older conversations and
older messages on request, without discarding loaded pages during polling or
stopping a playing voice note.

The database was not changed. A read-only check found 124 tickets on Meta line
`1375394715655384`; each stores display number `96176063972` and a canonical
customer phone, so those records can match a future linked-device connection
for that same number. Existing tickets and media remain in their original
records. The staged release passed the focused history, privacy, message, and
UI checks, plus the database/Redis/schema readiness check. After activation, a
read-only application smoke check found prior ticket 202 while viewing ticket
203 as its recorded staff member. Local and public `/support` responded, and
the API, bridge, ngrok, and database tunnel were active. The previous
`20261012-dev-v19-ticket-history` and `20261012-dev-v19-auth-loading` releases
remain available for code rollback.

## Production follow-up

Reuse the host's existing Nginx/TLS and Certbot infrastructure after reviewing
hostname ownership, proxy headers, SSE handling, certificate renewal, and access
controls. No Nginx changes are part of this development deployment. Caddy remains
an optional example for a new host with free `80`/`443` ports.

Give production a separate application database, Redis instance, private
configuration, media/state directories, and appropriate WhatsApp accounts.
Distinct Redis logical database numbers do not isolate the app's fixed Pub/Sub
channel. Review queue recovery and worker coordination before increasing API
workers or running multiple instances. See the
[general deployment guide](dev-deployment.md) for release updates, production
readiness, environment isolation, monitoring, and rollback requirements.
