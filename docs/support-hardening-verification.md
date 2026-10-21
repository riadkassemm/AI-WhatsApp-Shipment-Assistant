# Support hardening verification — 2026-10-21

## Findings

| Hypothesis | Result |
| --- | --- |
| Concurrent keyed text/media/location sends can dispatch the same saved row twice | Confirmed; callers ignored the insert's `created` result. Fixed with scoped uniqueness, explicit duplicate returns, and fenced database delivery claims. |
| A global browser client ID can return another ticket/sender's row | Confirmed; lookup did not scope or verify ownership. Replaced with ticket/sender lookup and a separate unique transport correlation ID. |
| Retry ownership is missing | Partly already fixed: FAILED → PENDING was atomic. However, uncertain failures were eligible for resend and there was no abandoned-work fencing. Now only proven failures are retryable; retries compare the observed attempt token. |
| Linked-device client IDs already deduplicate provider dispatch | Disproved; the bridge reserved native IDs but created a new ID on each call. It now reuses confirmed reservations and refuses uncertain duplicate dispatch. |
| Concurrent ticket claims can give ownership to two staff | Already protected by row locking. Verified against real MariaDB: one claim and one ownership event win. |
| Open SSE connections keep stale staff authority | Confirmed; they captured the initial agent. Session/account/current role now revalidate on events and idle heartbeats; failures terminate and clean up. |
| Concurrent message inserts can deadlock | Found during integration verification. Locking the ticket before inserting avoids the shared FK lock → activity-update lock upgrade race. |

The delivery coordinator now covers persisted support text, location, media,
retry, and system lifecycle messages. No framework or visibility-policy change
was made. The UI extraction was committed separately from the behavioral fixes.

## Verification

GitHub Actions run for code commit `41d8e9d`:
[Support regression tests — successful](https://github.com/riadkassemm/AI-WhatsApp-Shipment-Assistant/actions/runs/39544473905).
It installed requirements and Node dependencies from scratch, used MariaDB 10.11
and Redis 7.4 containers, and passed all steps.

Local commands/results (Python 3.10, MariaDB 13.0.2, Redis 7.4.2):

```bash
venv/bin/python -m pytest --ignore=tests/integration -o addopts='' -q
# 602 passed

SUPPORT_INTEGRATION=1 \
TEST_DB_SOCKET=/tmp/mik-hardening-db.sock \
TEST_REDIS_URL=unix:///tmp/mik-hardening-real-redis.sock \
venv/bin/python -m pytest tests/integration -o addopts='' -q
# 35 passed; 4 expected IF NOT EXISTS notices from migration replay

npm test --prefix whatsapp_bridge
# 56 passed, 0 failed

venv/bin/python -m compileall -q app scripts tests
venv/bin/python scripts/check_support_assets.py
git diff --check
# Passed
```

CI totals: 602 unit tests, 35 integration tests, 56 bridge tests. MariaDB/Redis
integration tests ran separately from legacy unit dependency doubles. Concurrency
uses barriers/events; expiry/crash tests change stored timestamps directly.
External HTTP is blocked except explicit mock transports. No real customer
messages were sent.

A headless Chromium smoke test against a local stub API loaded all extracted
assets, completed signed-out session bootstrap, and reported no JavaScript
errors/unhandled rejections. Existing Node DOM regression tests cover interaction
behavior. A full signed-in browser/real-phone end-to-end test was not performed.
The local disposable MariaDB and Redis processes were shut down after testing.

## Dev deployment

Deployed the tested code on `158.220.113.93`:

* Release: `/opt/whatsapp-support/releases/20261021-dev-v19-support-hardening`.
* Previous release retained: `20261021-dev-v19-migration-audit`.
* Root-owned source; persistent bridge sessions and support attachments retained
  outside the release. No pairing was removed or recreated.
* API and bridge were stopped before backup/migration, then restarted together.
* Private backup: `/var/backups/whatsapp-support/20261021-delivery-before-hardening.sql`
  (root-only file containing the pre-migration message table and its schema).
* Applied `migrations/20261021_outbound_delivery_ownership.sql` to the dev database
  (MariaDB 10.11.15). Required columns and both unique index shapes verified.
* **11 legacy pending/failed messages** were conservatively marked `UNCERTAIN`.
  Message content/history was preserved. None was resent.
* `scripts/check_runtime.py --schema`: passed against live database and Redis.
* API, bridge, and database-tunnel systemd services: active.
* `/health`, `/support`, all nine new assets, and the existing image editor:
  HTTP 200. Unauthenticated `/api/support/events`: HTTP 401.
* Bridge status endpoint: HTTP 200; all four configured lines connected.
* Public dev `/health` through the existing ngrok endpoint: HTTP 200, status ok.
* Deployed UI version: `a3898aa926ed373f`.

## Tradeoffs and remaining limits

A committed dispatch marker favors avoiding duplicates over automatically
recovering every unsent message. Crashes immediately after that marker are
uncertain even if no bytes reached WhatsApp. Cloud acceptance without a persisted
provider ID cannot be automatically reconciled; linked-device confirmation can
reconcile by its separate durable correlation ID. This is not an exactly-once
provider guarantee.

New client IDs represent new intentional sends. The state machine does not cover
all bot/provider calls or the separate existing reaction flow. Edit/revoke
operations keep their existing durable protocol. No provider acceptance or
recipient-device delivery was tested against real WhatsApp during this work.

See [the support guide](support-platform.md) for retry/recovery commands,
permissions, CI/local setup, and migration steps. In particular, **do not perform
a code-only rollback to pre-hardening send code against the migrated database**;
its global client-ID assumptions and retry semantics are incompatible. Prefer a
forward fix or plan a stopped-writes database restore with delivery reconciliation.
