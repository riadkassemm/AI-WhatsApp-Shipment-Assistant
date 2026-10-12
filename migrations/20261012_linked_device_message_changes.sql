-- Apply before enabling the linked-device transport. Additive and safe for Cloud rollback.
ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS whatsapp_change_timestamp BIGINT UNSIGNED NULL,
    ADD COLUMN IF NOT EXISTS pending_change_operation_id VARCHAR(64) NULL;

-- The original message stays unchanged until WhatsApp confirms a mutation.
-- Operation IDs are deterministic, so a retry reuses the same remote request.
CREATE TABLE IF NOT EXISTS support_message_change_operations (
    operation_id VARCHAR(64) NOT NULL,
    message_id BIGINT NOT NULL,
    base_version INT UNSIGNED NOT NULL,
    user_id VARCHAR(191) NOT NULL,
    action VARCHAR(16) NOT NULL,
    content TEXT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    lease_until DATETIME(6) NULL,
    provider_timestamp BIGINT UNSIGNED NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (operation_id),
    KEY idx_support_message_change_operation_message (message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS support_message_change_events (
    event_id VARCHAR(191) NOT NULL,
    message_id BIGINT NOT NULL,
    provider_timestamp BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_id),
    KEY idx_support_message_change_event_message (message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
