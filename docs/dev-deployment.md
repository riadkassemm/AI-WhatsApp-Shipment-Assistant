# Development VPS and production deployment

## Recommended layout

Run the development VPS with supervised API, linked-device bridge, database
access, and HTTPS ingress. Services restart independently, and journal logs
replace separate terminals. Persistent server deployments run the API without
source reload. A systemd target provides a single command for the components
whose lifecycle belongs to this app.

The [development VPS deployment record](dev-vps-inspection.md) describes the
verified rollout on 2026-10-12. The target is boot-enabled, recovery behavior and
both public ingress paths passed checks. Test line `90005` is paired on the VPS,
and two consecutive message edits reached the recipient phone. The user
selected this updated local project and fresh VPS linked-device pairings. Its
concrete service names, paths, and ports take precedence over the generic
examples. On this existing host the target controls API, bridge, and ngrok;
local MariaDB and the dedicated Redis start as dependencies but are never stopped
or restarted by target operations.

The development VPS `158.220.113.93` now hosts the dedicated
`whatsapp_support_dev` database in local MariaDB at `127.0.0.1:3306`. Both the
support application and chatbot use it directly. `SHIPPING_DB_SCHEMA` points to
the same database. There is no runtime connection or tunnel to `94.156.144.65`.
See [local database migration](local-database-migration.md) for the selected
tables, verification, independent-snapshot behavior, and rollback limits.

| Component | Development VPS | Production |
| --- | --- | --- |
| HTTPS ingress | Existing stable ngrok HTTPS endpoint | Permanent domain using existing Nginx/TLS |
| Python API | systemd, loopback `127.0.0.1:8000`, one worker | Same, without reload |
| Linked-device bridge | `whatsapp-bridge.service`, loopback `127.0.0.1:3010`, fresh VPS pairing | One bridge for its state directory |
| MariaDB access | Local dedicated database, loopback TCP | Local/private dedicated database |
| Redis | Managed local/private persistent Redis | Separate managed persistent Redis instance |
| Deployment/state | Dedicated `whatsapp-support` account, versioned releases, persistent state outside source | Same separation, isolated production state |

The concrete [VPS units](../deployment/dev-vps/README.md) manage
`shipment-bot.service`, `whatsapp-bridge.service`, `ngrok.service`, and
`whatsapp-dev.target`. `/opt/whatsapp-support/current` selects the release.
The bridge keeps its persistent state outside the release. The generic examples
use bridge port `3001`, already occupied on this VPS; do not install both sets.

```text
/opt/whatsapp-support/releases/<release>/    application, venv, Node dependencies
/opt/whatsapp-support/current               link to the selected release
/etc/whatsapp-support/app.env               private app configuration
/etc/whatsapp-support/ngrok.yml             private development ingress configuration
/etc/whatsapp-support/ssh/                  forwarding key and verified host keys
/var/lib/whatsapp-support/linked-device/    sessions, line manifest, callbacks, media
/var/lib/whatsapp-support/support-media/    authenticated support attachment cache
```

API and bridge run as `whatsapp-support`, sharing only their required application
state. Keep source and dependencies read-only to that account; systemd creates
owned state directories. Keep `app.env` owned by root with mode `0600`.
On the actual VPS, ngrok retains its existing root service and private config
under `/root/.config/ngrok`, and the retired tunnel retains its
`shipment-tunnel` account and configuration under `/etc/shipment-db-tunnel`.
The generic ngrok and SSH paths above apply to a clean new-host layout.

For a new host, the generic examples use separate `whatsapp-tunnel` and
`whatsapp-ingress` system accounts. Give each access only to its required
private configuration; use `0640` for a root/group-readable ngrok file and
`0750` or tighter for private configuration directories. Keep forwarding keys
and verified host keys private. Use system accounts without interactive login.

## Consolidating the existing development VPS

Start by recording current service names, project/venv paths, environment files,
Node/ngrok binaries, database forwarding method, Redis service, and enabled
numbers. Record state paths without displaying credential or session files.
Preserve the current private `.env`, ngrok endpoint and token, support media,
and the entire linked-device state directory, including its line manifest and
queued callbacks. Stop the bridge before copying its state for a deployment
move; do not run its old and new copies together.

For the selected VPS rollout, the API service name is preserved while its
working directory moves from `/root/whatsapp_chatbot_latest` into the versioned
release layout. This permits the new service account and `ProtectHome=true`
without giving it access to the root home directory. The old project is retained
unchanged for rollback. Existing support media is copied into persistent state
at cutover, retaining the original cache. Fresh bridge state is created on the
VPS; no pairing credentials or callback queues from the PC are copied.

Merge deployment overrides from
[app.env.example](../deployment/examples/app.env.example) into the existing
private configuration; this partial example is not a replacement for the full
configuration. Both services must receive the same bridge secret and absolute
state directory. Their callback stays on loopback. Set Python's working
directory to the project root so its existing `.env` lookup remains predictable.
In the clean layout, keep the full configuration in the private environment
file supplied by systemd. Do not leave an unreadable `.env` in the release:
Python attempts to read it even when its values are supplied by the environment.
Keep the original private configuration in the secured rollback backup.

Do not start another `npm start` when the persistent bridge service is active.
Only one process may own a bridge state directory. A saved linked-device pairing
reconnects after service restarts; restarting is not an unlink operation.

### Local database service

`mariadb.service` owns the loopback database listener. The application account
has CRUD access only to `whatsapp_support_dev`; schema changes use a separate
administrator connection. `shipment-db-tunnel.service` is disabled after the
verified cutover. Keep its private rollback configuration protected, but do
not restart it as part of the app's normal operation. The earlier tunnel setup
is historical; it is no longer required for this deployment.

### Development HTTPS endpoint

Keep the exact current ngrok HTTPS address so Meta webhook and OAuth callbacks
continue to match. A stable address must be allocated to this ngrok account;
an arbitrary domain cannot simply be selected. Keep the token in the private
server config, not in command arguments or committed files.

The [ngrok configuration example](../deployment/examples/ngrok.yml.example)
names the endpoint `support-dev`. Validate the populated configuration with
`ngrok config check --config /etc/whatsapp-support/ngrok.yml` and start it by
name. The example disables storage of inspected request bodies and keeps the
local agent interface on loopback. See ngrok's
[version 3 configuration reference](https://ngrok.com/docs/gateway/agent/config/v3).

Leave the app's authenticated dashboard accessible through its usual HTTPS
address. Do not put an interactive login gate over the WhatsApp webhook route;
Meta must be able to deliver signed callbacks. The bridge port and SSH database
forward remain internal and are never ngrok endpoints.

## Readiness and cutover

Install dependencies from the pinned manifests, using the supported Python
environment, Node.js 22+, `npm ci`, and ffmpeg. Do not run database migrations
on every process restart. Apply pending additive migrations as a deliberate
deployment step with an appropriately privileged database administrator.

Validate service files with `systemd-analyze verify` after adapting their paths.
Load the units with `systemctl daemon-reload`. Before enabling the target:

1. Back up the original unit/configuration files and record enabled states.
   Stop the previous API before assigning its replacement to `8000`; ensure
   only one bridge owns the intended state directory. On this VPS, retain the
   existing ngrok endpoint and keep local MariaDB, Redis, and unrelated services running.
2. Check MariaDB `SELECT 1`, required support schema, and Redis `PING` through
   the exact retained private configuration without printing credentials.
3. Start the new API and bridge. Verify API reachability, authenticated bridge
   status, new line configuration, state ownership, and callback counters.
4. Verify the existing public ngrok URL reaches the login page. Pair the separate
   test phone on the fresh VPS line and check inbound delivery, a staff reply,
   edit/delete synchronization, and voice playback. Record any remaining
   end-to-end checks separately from service/readiness results.
5. After service cutover verification, enable `whatsapp-dev.target` and disable
   the separate API/ngrok boot entries. Preserve their definitions for rollback
   and keep local MariaDB and Redis independently enabled.

After that validation, enable the new target at boot:

```sh
sudo systemctl enable --now whatsapp-dev.target
```

The API unit runs `scripts/check_runtime.py --wait 60 --schema` before starting
Uvicorn, using the same private environment as the app. It checks MariaDB,
required schema, and Redis with bounded retries; failures block this start and
systemd retries it. This check does not send WhatsApp messages or require every
paired account to be online.

The examples use `After=` for process ordering and `Restart=always` for
supervision. Ordering alone does not wait for HTTP readiness, and an active
target does not guarantee every child is healthy. The existing `/health` route
reports only API liveness. Verify each service state and repeat dependency
checks after a network outage; monitor callback failures as well as process
availability.

Systemd manages the services on the VPS after the administration SSH session
closes. Redis remains a separate managed service because it has its own
persistence, backups, and lifecycle; do not replace it with an ephemeral process.

## Daily operation

For the concrete development VPS units:

```sh
# Start or stop the API, bridge, and ngrok; shared MariaDB/Redis remain running.
sudo systemctl start whatsapp-dev.target
sudo systemctl stop whatsapp-dev.target

# Restart those three services, preserving saved pairings and state.
sudo systemctl restart whatsapp-dev.target

# Deploying Python changes only needs the API restarted.
sudo systemctl restart shipment-bot.service

# Deploying bridge changes needs the bridge restarted.
sudo systemctl restart whatsapp-bridge.service

# Inspect child states and shared dependencies; target status is insufficient.
sudo systemctl status shipment-bot.service whatsapp-bridge.service ngrok.service mariadb.service shipment-redis.service

# Read recent logs, then follow live output if required.
sudo journalctl -u shipment-bot.service -u whatsapp-bridge.service -u ngrok.service -n 100 --no-pager
sudo journalctl -u shipment-bot.service -u whatsapp-bridge.service -f
```

On a clean host installed from the generic examples, substitute
`whatsapp-api.service` and `whatsapp-ngrok.service` for the corresponding
concrete service names. MariaDB and Redis remain independent in both configurations.

The API start runs the dependency/schema check automatically. From a local
project directory with its private `.env`, the same read-only check can be run
directly:

```sh
venv/bin/python scripts/check_runtime.py --wait 60 --schema
```

For the clean server layout, run checks through the configured API service
environment; do not `source` a private environment file or print it for debugging.

The ten-second Uvicorn graceful shutdown limit prevents open dashboard event
streams holding a restart indefinitely. A restart may interrupt an in-flight
request; the durable bridge callback queue retries transient failures. Perform
deployments while test traffic is quiet and check delivery after each restart.

Local development can still use reload. Keep its watch scope on application
code, excluding mutable bridge/session/media state:

```sh
venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app --timeout-graceful-shutdown 10
```

## Updating the development release

Keep deployed source separate from persistent state. Prepare a new dated release
under `/opt/whatsapp-support/releases`, build its own `venv` and Node
dependencies, and retain the current release until the replacement is verified.
The services keep their private configuration in `/etc/whatsapp-support/app.env`
and their media/sessions in `/var/lib/whatsapp-support`; those are not copied
from a developer PC or replaced by a source upload.

For example, run this upload from the local project root, choosing a new release
name for each deployment:

```sh
release=20261013-dev-v19
ssh root@158.220.113.93 "install -d -m 0755 /opt/whatsapp-support/releases/$release"
rsync -a --relative --exclude='.env*' --exclude='__pycache__' --exclude='*.pyc' \
  app/ scripts/ migrations/ tests/ docs/ deployment/ \
  requirements.txt requirements-dev.txt pytest.ini README.md \
  whatsapp_bridge/src/ whatsapp_bridge/test/ \
  whatsapp_bridge/package.json whatsapp_bridge/package-lock.json \
  whatsapp_bridge/README.md \
  "root@158.220.113.93:/opt/whatsapp-support/releases/$release/"
```

Review what will be uploaded with `rsync --dry-run` first. The explicit source
list selects application and deployment files. Do not upload private configuration, local
pairing state, cached customer media, logs, or development tooling credentials.
Build dependencies on the VPS from the pinned manifests rather than uploading
PC-built binaries. The concrete units use the release's `venv` and Node.js 24
at `/usr/bin/node`; use the already verified Python version for compatibility.

Install pinned requirements into the new `venv`, run `npm ci` within its
`whatsapp_bridge` directory, and run the relevant Python/bridge tests. Review
pending database migrations explicitly. Keep the release root-owned and
read-only to `whatsapp-support`, while retaining executable access to the
virtual environment and Node dependencies. Run the read-only dependency/schema
checker with the private service environment before switching; do not print or
source that configuration.

Switch `current` only after preparation. For an update affecting both Python and
bridge code, stop those two services first so they do not load source from a
changed link midway through processing. Leave ngrok, local MariaDB, and Redis running. An example root command sequence on the VPS is:

```sh
set -eu
release=/opt/whatsapp-support/releases/20261013-dev-v19
test -x "$release/venv/bin/python"
test -f "$release/whatsapp_bridge/src/index.js"
previous=$(readlink -f /opt/whatsapp-support/current)
printf '%s\n' "$previous"

systemctl stop whatsapp-bridge.service shipment-bot.service
next_link=/opt/whatsapp-support/current.next.$$
ln -s "$release" "$next_link"
mv -Tf "$next_link" /opt/whatsapp-support/current
systemctl start shipment-bot.service whatsapp-bridge.service
systemctl status shipment-bot.service whatsapp-bridge.service --no-pager
```

Record the printed previous-release path securely before switching. The API's
pre-start readiness gate runs again automatically. Verify the dashboard, bridge,
public endpoint, callbacks, and delivery afterward. If verification fails, stop
the API and bridge, select the recorded previous release with the same temporary
link/rename procedure, and start them again. Preserve all state and confirm
schema/state compatibility before a code rollback. If the update changes only
one component, restart only that component after switching the prepared release.

The first migration from the old VPS layout has a separate documented
[service/configuration rollback](dev-vps-inspection.md#rollback-for-this-server).
A later code rollback normally changes the release link without restoring the
old root-run service arrangement. Never unlink a WhatsApp account to roll back
a source release.

## Production plan

### Separate the environments

Give development and production separate application databases, Redis
instances, private configuration, media directories, bridge state, and test/live
numbers. Do not pair the same WhatsApp session by copying its credentials into
two running installations. A shared shipping catalogue may remain read-only
where the database permissions support that arrangement.

Changing only Redis's logical database number does not isolate realtime
messages: the current code publishes to `support:events:v1`, and Redis Pub/Sub
channels span logical databases. Use separate Redis instances until the app has
an explicitly implemented and tested environment namespace. See the
[Redis Pub/Sub reference](https://redis.io/docs/latest/develop/pubsub/).

Use persistent Redis storage and backups appropriate to conversation state,
idempotency reservations, and locks. Retain MariaDB backups plus private media
and linked-session backups; a copy of source code alone is not a rollback of
those stores. Protect backups as credentials/customer data.

### Replace development ingress with a permanent domain

The inspected VPS already runs Nginx on ports `80` and `443` and hosts other
applications. Its TLS hostname `export-customer-service.mik-holding.co` already
proxies to the API on `8000`. Reuse its existing Nginx/Certbot infrastructure
after reviewing site configuration, certificate renewal, and hostname ownership.
Add a scoped server block only if a new hostname is selected; do not replace
the global Nginx configuration or bind another web server to its occupied ports.
The development service consolidation needs no Nginx change.

Point the production hostname to the application VPS and proxy to loopback
Uvicorn. Preserve the original `Host`, use the HTTPS scheme in forwarded headers,
and trust proxy headers only from loopback in Uvicorn. Keep API, bridge, Redis,
and database ports private. Do not enable proxy caching for support data.
Ensure the dashboard event stream is not buffered: the application emits
`X-Accel-Buffering: no`, and the SSE location can explicitly set
`proxy_buffering off` and an appropriate read timeout. Validate with `nginx -t`
before reloading only Nginx. See the [Nginx proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
and [Nginx HTTPS configuration](https://nginx.org/en/docs/http/configuring_https_servers.html).

Caddy is an optional choice for a new host where HTTP/HTTPS ports are available.
For that case, validate
[Caddyfile.production.example](../deployment/examples/Caddyfile.production.example)
with `caddy validate --config <installed-file> --adapter caddyfile` before
reloading it. Configure the real host and certificate contact address.

Caddy can obtain and renew certificates for a qualifying domain and redirects
HTTP to HTTPS. Certificate issuance requires correct DNS and the selected
challenge's reachability. See [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https).

The proxy uses loopback Uvicorn and leaves authentication in the application.
The example prevents public access to the internal linked-device callback and
monitoring paths; it does not expose files from application/state directories.
Caddy flushes `text/event-stream` responses immediately, so the dashboard SSE
feed works without a special buffering override. See the
[Caddy reverse proxy documentation](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

Use `SUPPORT_COOKIE_SECURE=true` and update Meta's webhook and any Google
Contacts OAuth redirect URI to the exact production HTTPS address. Test the new
endpoint before retiring the existing ngrok endpoint. Remove the ngrok service
from the production target once the permanent ingress is verified; API, bridge,
and private database forwarding keep their existing responsibilities.

### Release and rollback discipline

Begin with one API worker and one bridge process. The app has Redis-backed
coordination, but its inbound queue/recovery and database pool behavior need
explicit verification before scaling workers or instances. Measure request
latency, Redis/MariaDB capacity, and delivery recovery before changing these
counts. Message edits/deletions are confirmed remotely, but durable operations
and callbacks must also be checked after restart and network-failure tests.

Pin dependencies, deploy a versioned release, compile/test it, review migrations,
then switch the release link and restart only the affected services. Monitor
HTTP errors, dependency checks, bridge connection state, pending/failed callback
counts, and application logs. Add an external HTTPS availability check and
alerts for sustained dependency or delivery failures.

To roll back the service consolidation, stop the new target and restore the
recorded previous service definitions, environment/state paths, and autostart
settings. Keep the same preserved pairing directory and private configuration;
do not scan a new QR or unlink a number as a rollback step. Ensure only one
bridge and ingress instance are active. Restore the previous source release
only if it is compatible with the current database/state formats. Previously
delivered WhatsApp edits or deletions cannot be undone by restoring code.
