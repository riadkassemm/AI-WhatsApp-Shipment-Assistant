-- Stop API workers before applying. Existing ambiguous failures are quarantined.
-- Repeatable: only legacy rows without transport correlation are backfilled.
ALTER TABLE support_ticket_messages
  ADD COLUMN IF NOT EXISTS transport_client_id VARCHAR(191) NULL,
  ADD COLUMN IF NOT EXISTS sender_scope VARCHAR(100) AS (COALESCE(sender_id, '')) PERSISTENT,
  ADD COLUMN IF NOT EXISTS delivery_token CHAR(32) NULL,
  ADD COLUMN IF NOT EXISTS delivery_claimed_at DATETIME(6) NULL,
  ADD COLUMN IF NOT EXISTS delivery_started_at DATETIME(6) NULL,
  ADD COLUMN IF NOT EXISTS delivery_retry_safe TINYINT(1) NOT NULL DEFAULT 0;
UPDATE support_ticket_messages
SET send_status = CASE WHEN direction = 'OUTBOUND' AND send_status IN ('PENDING', 'FAILED')
                       THEN 'UNCERTAIN' ELSE send_status END,
    transport_client_id = client_message_id
WHERE transport_client_id IS NULL AND client_message_id IS NOT NULL;
ALTER TABLE support_ticket_messages
  ADD UNIQUE INDEX IF NOT EXISTS uq_support_transport_client (transport_client_id),
  ADD UNIQUE INDEX IF NOT EXISTS uq_support_scoped_client (ticket_id, sender_type, sender_scope, client_message_id),
  ADD INDEX IF NOT EXISTS idx_support_delivery_recovery (send_status, delivery_claimed_at),
  DROP INDEX IF EXISTS uq_support_messages_client_id;
