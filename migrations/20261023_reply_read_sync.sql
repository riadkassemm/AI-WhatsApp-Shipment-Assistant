-- Queue read receipts only for new confirmed staff replies. Existing receipts
-- and messages remain unchanged; no historical conversations are marked read.
ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS read_sync_pending TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS read_sync_attempted_at DATETIME(6) NULL,
    ADD INDEX IF NOT EXISTS ix_support_read_sync (read_sync_pending,read_sync_attempted_at);

-- Receipts can arrive before the outbound HTTP response is saved.
CREATE TABLE IF NOT EXISTS support_delivery_receipts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    provider_message_id VARCHAR(191) NOT NULL,
    line_id VARCHAR(64) NOT NULL,
    phone VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    occurred_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT UTC_TIMESTAMP(6),
    checked_at DATETIME(6) NULL,
    UNIQUE KEY uq_delivery_receipt (provider_message_id,line_id,status),
    KEY ix_delivery_receipt_checked (checked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
