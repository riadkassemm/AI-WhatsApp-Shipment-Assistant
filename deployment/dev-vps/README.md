# Development VPS service configuration

These reviewed units target the application VPS `158.220.113.93`. They retain
the existing API service name and use local MariaDB and the dedicated
`shipment-redis.service`. Files in this directory are configuration sources; installation and
cutover are separate deployment steps.

| Source | Installation destination |
| --- | --- |
| `shipment-bot.service` | `/etc/systemd/system/shipment-bot.service` |
| `whatsapp-bridge.service` | `/etc/systemd/system/whatsapp-bridge.service` |
| `whatsapp-dev.target` | `/etc/systemd/system/whatsapp-dev.target` |
| `ngrok-dev-stack.conf` | `/etc/systemd/system/ngrok.service.d/dev-stack.conf` |

## Required release and private configuration

The root-owned release is selected by `/opt/whatsapp-support/current`. Its
Python 3.10 environment is `venv`, and Node.js 24 is installed at `/usr/bin/node`.
The API and bridge run as the system account and group `whatsapp-support`; their
source and dependencies must be readable but not writable by that account.

Systemd reads the complete private `/etc/whatsapp-support/app.env`, owned by
root with mode `0600`, for both processes. Do not copy credentials into these
unit files. Keep a readable project `.env` out of the release: Python attempts
to read that file even when environment variables are already supplied.

The populated environment must use these agreed deployment values:

| Setting | Value |
| --- | --- |
| `DB_HOST` / `DB_PORT` | `127.0.0.1` / `3306` |
| `DB_NAME` / `SHIPPING_DB_SCHEMA` | `whatsapp_support_dev` (both) |
| `REDIS_URL` | Preserve the existing dedicated development Redis URL |
| `SUPPORT_MEDIA_DIRECTORY` | `/var/lib/whatsapp-support/support-media` |
| `WHATSAPP_LINKED_DEVICE_URL` | `http://127.0.0.1:3010` |
| `WHATSAPP_LINKED_DEVICE_CALLBACK_URL` | `http://127.0.0.1:8000/api/whatsapp/linked-device/events` |
| `WHATSAPP_LINKED_DEVICE_DATA_DIRECTORY` | `/var/lib/whatsapp-support/linked-device` |
| `WHATSAPP_LINKED_DEVICE_LINES` | `{"90005":""}` for the fresh test pairing |

Enable the linked-device integration and retain the same private bridge secret
in both processes. The API keeps the existing Cloud provider configuration;
line `90005` starts without copied credentials from another installation.
The bridge derives port `3010` from its configured URL and remains on loopback.
Systemd owns `/var/lib/whatsapp-support` for the service account with mode `0700`.
Preserved media must have corresponding ownership and remain outside the source
release. Preserve the old release, private configuration, service definitions,
and relevant state before switching.

## Existing ingress and shared dependencies

Keep the existing `ngrok.service` binary, private configuration, authentication,
and public URL `https://sanction-lake-exalted.ngrok-free.dev`. Before installing
the drop-in, back up `/etc/systemd/system/ngrok.service` and edit its existing
`[Unit]` section: replace `PartOf=shipment-bot.service` with
`PartOf=whatsapp-dev.target`. Keep the existing private `ExecStart` and all other
settings unchanged. The complete ngrok unit is kept on the VPS because it can
contain private configuration.

This dependency must be changed in the existing unit file. An empty `PartOf=`
assignment in a drop-in does not clear dependencies declared in the base unit.
The supplied drop-in sets restart behavior and startup ordering. After
`systemctl daemon-reload`, verify the effective dependency:

```sh
systemctl show ngrok.service --property=PartOf --value
```

The result must contain only `whatsapp-dev.target`. Restarting the API alone
then leaves ngrok running; confirm its process ID stays unchanged during the
restart check. The existing Nginx/TLS sites remain configured separately.

`mariadb.service` and `shipment-redis.service` are shared dependencies. The target
starts them when necessary; stopping or restarting `whatsapp-dev.target` does not
stop or restart them. The target controls the API, bridge, and ngrok together.
The old database tunnel is retired after the [verified database cutover](../../docs/local-database-migration.md).
Do not re-enable it or point a rollback release at the former remote database.

## Validation and daily commands

After installing the files, validate their actual installed paths with
`systemd-analyze verify`, then run `systemctl daemon-reload`. During cutover,
stop the superseded API before assigning the new process to port `8000`, and
ensure port `3010` is free and only one bridge owns its new state directory.
The API's pre-start check verifies database access, required support schema,
and Redis with a sixty-second retry window. It makes no schema changes.

Verify the dashboard and authenticated bridge status, public ingress, saved
configuration, and dependency checks before enabling the target at boot:

```sh
systemctl start whatsapp-dev.target
systemctl status shipment-bot.service whatsapp-bridge.service ngrok.service
systemctl enable whatsapp-dev.target
```

Enable the target as the boot entry for the group. The existing API/ngrok boot
entries can remain during validation and be disabled after successful cutover;
their services are then started by the enabled target. Keep local MariaDB
and the dedicated Redis boot entries enabled.

```sh
# Restart or stop the app, bridge, and ingress together.
systemctl restart whatsapp-dev.target
systemctl stop whatsapp-dev.target

# Restart Python after a source deployment without restarting ingress.
systemctl restart shipment-bot.service

# Follow API and bridge logs.
journalctl -u shipment-bot.service -u whatsapp-bridge.service -f
```

An active target does not prove that every child is healthy. Verify service
states and callback counters after restarts. Pair the separate test account
through the dashboard and verify actual message delivery, voice playback, and
edit/delete synchronization before moving production accounts. Do not unlink
accounts as a rollback step.
