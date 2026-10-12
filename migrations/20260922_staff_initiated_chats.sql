-- Staff-initiated WhatsApp conversations.
-- Existing customer-created support tickets remain CUSTOMER origin by default.
ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS conversation_origin VARCHAR(32) NOT NULL DEFAULT 'CUSTOMER' AFTER business_display_phone_number,
    ADD COLUMN IF NOT EXISTS initiated_by_staff_id BIGINT NULL AFTER conversation_origin,
    ADD COLUMN IF NOT EXISTS initiated_by_staff_name VARCHAR(191) NULL AFTER initiated_by_staff_id;

CREATE INDEX IF NOT EXISTS idx_support_tickets_origin_activity
    ON support_tickets (conversation_origin, last_activity_at);
