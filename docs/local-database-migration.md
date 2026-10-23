# Local support/chatbot database

## Runtime configuration

The dev VPS uses a dedicated `whatsapp_support_dev` database on its existing
MariaDB service, bound to `127.0.0.1:3306`. Customer support and the chatbot run
in the same FastAPI service and share `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`,
and `DB_PASSWORD`. Set `SHIPPING_DB_SCHEMA=whatsapp_support_dev` too; otherwise
shipping catalog queries still target another schema.

Store credentials only in root-owned `/etc/whatsapp-support/app.env` (0600).
Use a newly generated local-only account with SELECT/INSERT/UPDATE/DELETE on
the application tables and SELECT only on `destinations` and `shipping_rates`.
Use local administrator access for migrations. The bridge shares this environment
but does not open database connections. Preserve Redis, pairing state, callbacks,
media, signing secrets, and cookies during migration.

This is an **independent point-in-time copy**, not replication. New customers,
wallet changes, orders, shipment updates, and prices subsequently entered in the
old business system will not arrive automatically. Chatbot order-mode/password
updates now affect the local copy only. Any future synchronization requires a
separate explicit design; neither app queries the old server during operation.

## Selected tables

`scripts/clone_support_database.py` contains the reviewed allowlist and follows
outgoing foreign keys recursively. It excludes unrelated tables that merely
reference an included table.

| Source | Tables |
| --- | --- |
| `mikexport`: authentication/customer/shipment lookup | `staff`, `staff_sessions`, `users`, `user_wallet`, `orders_new`, `shipments` |
| `mikexport`: support | `support_tickets`, `support_ticket_messages`, `support_ticket_events`, `support_contacts`, `support_quick_replies` |
| `mikexport`: message operations | `support_message_change_events`, `support_message_change_operations`, `support_message_reactions`, `support_message_user_deletions` |
| `mikexport`: contact imports | `support_google_accounts`, `support_google_contacts`, `support_vcf_contacts` |
| `mikexport`: configuration | `ai_control_settings`, `ai_control_settings_audit`, `whatsapp_business_accounts`, `whatsapp_phone_numbers`, `whatsapp_integration_audit` |
| `shipping_db`: read-only catalog | `destinations`, `shipping_rates` |

The inspected foreign keys add no further tables. The main-schema tables have
no triggers. Existing source credentials have only SELECT on the catalog, so
catalog trigger metadata cannot be verified; these tables remain read-only for
the local application too. `prohibited_items` is not queried by this code and
is excluded. Indexes, foreign keys, checks, generated columns, auto-increment
values, and table/column collations are preserved through `SHOW CREATE TABLE`.

## Copy and cutover procedure

1. Check available space, local MariaDB health, source privileges, table engines,
   and all consumers of the old database connection. Back up private environment
   and systemd files without putting secrets in the repository.
2. Drain in-flight sends and stop `shipment-bot.service` for the final snapshot.
   Leave the bridge collecting durable callbacks. Never relabel or resend
   existing uncertain deliveries. During the pause staff requests are unavailable;
   Cloud webhook redelivery depends on Meta's retry behavior.
3. Run the script **on the dev VPS**, using existing credentials from the private
   service environment and a direct TCP connection to the source. Root's local
   MariaDB socket supplies destination administration. Example inside that
   environment (not a shell export of secrets):

   ```sh
   venv/bin/python scripts/clone_support_database.py \
     --source-host 94.156.144.65 --target whatsapp_support_dev \
     --report /var/backups/whatsapp-support/database-copy.json
   ```

   The target must not exist. One READ ONLY/REPEATABLE READ snapshot streams
   each selected InnoDB table once in 500-row batches with a 20ms pause. There
   are no source writes, table locks, global locks, flushes, or source checksum
   scans. A snapshot can retain source undo history until copying finishes.
   This source currently has TLS disabled; the authorized direct one-time SQL
   transfer is not encrypted. Runtime traffic after cutover is loopback only.
4. The script compares row counts and typed SHA-512 multiset fingerprints
   against a complete destination scan, verifies exact normalized table DDL,
   and checks each foreign key for orphans. A failed copy remains isolated for
   inspection; never activate it. The private report contains counts/digests,
   not customer records or credentials.
5. Create the restricted local account, update the five DB settings and shipping
   schema atomically, install the concrete dev units, and reload systemd.
   Run `venv/bin/python scripts/check_runtime.py --schema` under the new
   environment. Start the API and restart the bridge to reload its environment.
6. Verify support history/staff queries, chatbot customer/wallet/tracking/catalog
   queries, API health, bridge connectivity and callback draining. Use disposable
   databases for mutation tests; never send customer messages as test traffic.
   Confirm process sockets point to local 3306 and no clients remain on 3307.
   Disable/stop `shipment-db-tunnel.service` after confirming exclusive ownership.

There is no application schema migration: this copies the existing schema.
Local MariaDB and dedicated `shipment-redis.service` replace the tunnel dependency
in the dev units. Other databases/services on the VPS are unchanged.

## Recovery and tests

Before traffic resumes, a failed cutover can restore the private environment and
old unit files. **After new local writes, never point an old release back at the
remote database:** that loses visibility of new conversations. Roll back code
while keeping the new local DB configuration. Reconcile data explicitly before
any later database reversal. Back up the new database and persistent state together;
the untouched source stops being a current backup after cutover.

Use the disposable services/environment in [support-platform.md](support-platform.md):

```sh
SUPPORT_INTEGRATION=1 venv/bin/python -m pytest tests/integration/test_database_clone.py
SUPPORT_INTEGRATION=1 venv/bin/python -m pytest tests/integration
venv/bin/python -m pytest --ignore=tests/integration
```

The copy tests cover a concurrent committed source change after snapshot start,
database-enforced read-only protection, cross-schema FK closure, generated values,
binary/Unicode/decimal/timestamp data, duplicate rows, existing-target rejection,
nontransactional tables, unexpected triggers, orphan rejection, and destination
data/schema corruption detection.

## Dev cutover record — 2026-10-22

Deployed release: `20261022-dev-v19-local-database`. Source MariaDB 10.11.15;
destination MariaDB 10.11.14. The copy verified **25 tables, 558,720 rows, seven
foreign keys**, and all table definitions and content fingerprints. Largest
tables: `orders_new` 515,264 rows, `user_wallet` 11,724, `users` 11,704,
`support_ticket_messages` 9,730, and `support_ticket_events` 6,129.
There were 1,355 tickets and 1,596 shipment records at the snapshot.

Readiness/schema validation and real support history/staff queries, chatbot
wallet/tracking/catalog queries, and invalid-customer-login checks passed using
the new local account. Public `/health` and `/support` returned 200. All six
paired lines reconnected. The 17 existing uncertain database deliveries were
preserved. No customer messages were sent as test traffic.
Two callbacks on line `90020` predate the cutover by over two hours: reactions
whose target messages are absent from support. They retain the existing
24-hour target-recovery policy; they are not failed database writes. Other
lines had no pending/failed callbacks at verification, and live callbacks
returned HTTP 200 after cutover.

The local account was verified unable to write the read-only catalog or access
an unrelated local database. The API's database sessions use local port 3306.
The old tunnel is stopped and disabled. An unrelated source-server monitoring
connection to this VPS's node exporter is outside the app and was left intact.

Private before/after environments, original units, complete verification report,
and an initial compressed local database backup are in
`/var/backups/whatsapp-support/20261022-local-database/` (root-only). The backup
passed `gzip -t`; a restore drill and scheduled backups remain operational tasks.
Application mutation workflows were tested against disposable MariaDB/Redis,
not real customer conversations. A fresh manual staff login and phone-to-phone
delivery were not performed during this migration.

Local verification commands (MariaDB 13.0.2 and Redis 7.4.2 disposable instances):

```sh
SUPPORT_TEST_BROWSER=chromium venv/bin/python -m pytest --ignore=tests/integration -q
# 619 passed
SUPPORT_INTEGRATION=1 TEST_DB_SOCKET=/tmp/mik-cancel-db.sock \
  TEST_REDIS_URL=redis://127.0.0.1:16379/15 \
  venv/bin/python -m pytest tests/integration -q
# 127 passed; existing idempotent-migration warnings only
venv/bin/python -m compileall -q app scripts tests
venv/bin/python scripts/check_support_assets.py
git diff --check
```

CI runs the same integration suite on MariaDB 10.11. Its result must be checked
for the final commit separately from these local and deployed checks.

### Statistics tables added after the database cutover

`migrations/20261023_agent_statistics.sql` creates `support_statistics_metadata`,
`support_agent_heartbeats`, and `support_agent_activity` locally. The clone tool's
application-table allowlist includes these tables for future moves/backups. The
original cutover table counts above describe that earlier snapshot. Grant the
runtime account CRUD on the new tables and retain them in local database backups.

### Delivery receipt inbox

`20261023_reply_read_sync.sql` adds local `support_delivery_receipts` and two
message read-sync columns. Include the inbox in backups/moves and grant the
existing application account CRUD on it; table-level grants do not automatically
cover new tables. Historical read state is not backfilled.
