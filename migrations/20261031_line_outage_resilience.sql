-- A staff reply that could not be sent because its WhatsApp line was offline
-- waits until held_until and is sent automatically once the line reconnects.
ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS held_until DATETIME(6) NULL;
CREATE INDEX IF NOT EXISTS idx_support_messages_held ON support_ticket_messages (send_status, held_until);

-- Deleting a history ticket or chat removes its timeline. Keep who deleted
-- what, with no customer content, in a table the deletion does not touch.
CREATE TABLE IF NOT EXISTS support_deletion_audit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ticket_id BIGINT UNSIGNED NOT NULL,
    ticket_reference VARCHAR(64) NULL,
    conversation_origin VARCHAR(16) NULL,
    status VARCHAR(32) NULL,
    business_phone_number_id VARCHAR(64) NULL,
    message_count INT UNSIGNED NOT NULL DEFAULT 0,
    deleted_by_staff_id BIGINT UNSIGNED NULL,
    deleted_by_name VARCHAR(255) NULL,
    deleted_at DATETIME(6) NOT NULL,
    KEY idx_support_deletion_audit_ticket (ticket_id),
    KEY idx_support_deletion_audit_staff (deleted_by_staff_id, deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
