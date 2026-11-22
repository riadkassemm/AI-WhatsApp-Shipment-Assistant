# Read-only agent business data

Agent v2 is off by default. Its business adapter is deliberately unconfigured:
orders, wallets and catalogue tables in the support database are historical copies.
There is no fallback to them when a replica is absent or fails.

IT must supply a separate MariaDB database and a SELECT-only principal before a
pilot can use these tools. Configure `AI_BUSINESS_DB_HOST`, `AI_BUSINESS_DB_PORT`,
`AI_BUSINESS_DB_NAME`, `AI_BUSINESS_DB_USER`, `AI_BUSINESS_DB_PASSWORD`. These are
independent of both the support database and the optional customer-users replica.
The adapter also sets sessions to read-only transactions. No new business tables
are created by application migrations.

The following names describe the **adapter contract**, not existing live tables.
IT may expose views with these names over its authoritative schema. All rows and
watermarks must share a transactionally consistent replication snapshot.

| View | Required columns |
| --- | --- |
| `ai_data_freshness` | `dataset` (unique), `as_of` (UTC DATETIME) |
| `ai_orders` | `customer_uuid`, `order_id`, `tracking_number` (nullable), `shipment_id` (nullable), `status`, `created_at` (UTC DATETIME) |
| `ai_wallets` | `customer_uuid` (unique), `balance` (DECIMAL), `currency` (ISO uppercase three-letter code) |
| `ai_transactions` | `customer_uuid`, `reference`, `amount` (signed DECIMAL), `currency`, `occurred_at` (UTC DATETIME) |
| `ai_shipping_rates` | `id`, `destination_id`, `origin`, `destination`, `shipping_method`, `goods_type`, `price`, `transit_time` |
| `ai_destinations` | `id`, `slug`, `label` |

Use VARCHAR for customer UUIDs, order/tracking/shipment/transaction references and
status. The account source's `users.uuid` must be unique and stable across replicas;
never map customers using numeric primary keys from independent databases. IT must
ensure one UUID identifies one account. All private queries filter by that UUID,
resolved by the backend; model arguments cannot provide or override it.

Watermark datasets are `orders`, `wallet`, `payments`, and `catalog`. `as_of` is the
last successfully replicated source position/time, **not the time a query runs**.
A missing, duplicated, future (>5 seconds) or old watermark fails closed.
`AI_BUSINESS_MAX_AGE_SECONDS` defaults to 300. Update the watermark atomically with
the corresponding replicated data. Catalogue views preserve the formats used by
`ShipmentClient` for deterministic matching and Decimal per-kg arithmetic. A
catalogue exceeding 10,000 rows per view requires an adapter update; partial data
is never silently used.

L0 can use public catalogue tools; L1 adds orders/tracking/shipments; L2 adds wallet
and transactions. Every dispatch checks `AIIdentity.allows`, and the replica
adapter repeats that check. L1 selects only identifiers and shipment status, with
no payment fields. Missing or ambiguous records escalate instead of asserting a
zero balance or that another customer's order does not exist. Results carry the
replication `as_of` and a fixed safe source label. Common responses are rendered
in the requested style without another model call. Previous-ticket context remains
unavailable until the scoped memory implementation is installed.

Business mutations are absent from both AI registries. A stale
`update_shipment_mode` call requests `CHANGE_REQUIRES_HUMAN` without executing a
write. The underlying business method remains available to non-AI callers.
