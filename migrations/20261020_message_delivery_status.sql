-- WhatsApp delivery and read receipts for outgoing support messages.
-- delivery_status is NULL until WhatsApp reports the message delivered, then
-- 'delivered' or 'read'. It only moves forward.
ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS delivery_status VARCHAR(16) NULL AFTER send_status,
    ADD COLUMN IF NOT EXISTS delivered_at DATETIME(6) NULL AFTER delivery_status,
    ADD COLUMN IF NOT EXISTS read_at DATETIME(6) NULL AFTER delivered_at;
