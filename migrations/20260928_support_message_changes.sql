-- Additive support-console message changes. Apply before deploying the new code.
-- Provider-delivered WhatsApp copies are outside this local timeline.
ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS edited_at DATETIME(6) NULL,
    ADD COLUMN IF NOT EXISTS deleted_for_everyone_at DATETIME(6) NULL,
    ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(191) NULL,
    ADD COLUMN IF NOT EXISTS version INT UNSIGNED NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS support_message_user_deletions (
    message_id BIGINT NOT NULL,
    user_id VARCHAR(191) NOT NULL,
    deleted_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (message_id, user_id),
    KEY idx_support_message_user_deletions_user (user_id, message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
