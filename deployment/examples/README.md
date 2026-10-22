# Deployment examples

These examples use a new service account and deployment layout. They are not an
installer and are not the configuration of any currently running server.

| File | Installed destination / purpose |
| --- | --- |
| `whatsapp-dev.target` | `/etc/systemd/system/whatsapp-dev.target`, controls API, bridge, and ingress; starts local MariaDB |
| `whatsapp-api.service` | `/etc/systemd/system/whatsapp-api.service`, loopback API, one worker |
| `whatsapp-bridge.service` | `/etc/systemd/system/whatsapp-bridge.service`, persistent linked accounts |
| `whatsapp-db-tunnel.service` | `/etc/systemd/system/whatsapp-db-tunnel.service`, optional legacy SSH database forward; not used by the local DB setup |
| `whatsapp-ngrok.service` | `/etc/systemd/system/whatsapp-ngrok.service`, development HTTPS endpoint |
| `app.env.example` | Overrides to merge into private `/etc/whatsapp-support/app.env` |
| `ssh-client.conf.example` | `/etc/whatsapp-support/ssh/client.conf`, optional legacy forwarding key |
| `ngrok.yml.example` | Private `/etc/whatsapp-support/ngrok.yml`, existing endpoint and token |
| `Caddyfile.production.example` | Future permanent HTTPS domain; replaces ngrok |

See [the deployment guide](../../docs/dev-deployment.md) for prerequisites,
configuration ownership, readiness, cutover, operating commands, and rollback.

Replace every `REPLACE_...` value and adapt absolute binary paths before use.
The hardening requires the application and persistent state to be outside
`/root` and `/home`. Do not install these examples over an existing deployment
without first recording its services, paths, active numbers, and state locations.
