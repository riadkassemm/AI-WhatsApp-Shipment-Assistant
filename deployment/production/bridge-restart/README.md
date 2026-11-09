# Dashboard "Restart WhatsApp service"

Admins and Super Admins can restart the linked-device bridge from WhatsApp settings. The API is unprivileged (`NoNewPrivileges=true`), so it only writes `/var/lib/whatsapp-support/control/bridge-restart.request`. This root-owned path unit consumes the file and runs exactly `systemctl restart --no-block whatsapp-bridge.service`, at most once every 5 minutes, then writes `bridge-restart.result` for the dashboard.

Install (as root):

```bash
install -d -o whatsapp-support -g whatsapp-support -m 0700 /var/lib/whatsapp-support/control
install -m 0755 restart_bridge_on_request.py /usr/local/libexec/whatsapp-support/
install -m 0644 whatsapp-bridge-restart.path whatsapp-bridge-restart.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now whatsapp-bridge-restart.path
# then set WHATSAPP_BRIDGE_RESTART_ENABLED=true in /etc/whatsapp-support/app.env and restart shipment-bot
```

Pair it with the bounded stop drop-in (`../whatsapp-bridge.service.d/30-safe-retirement.conf`), so a restart of a frozen bridge completes within 90 s.

Logs: `journalctl -u whatsapp-bridge-restart.service` (who and when) and `journalctl -u whatsapp-bridge`.
