-- Multi-number WhatsApp routing metadata for support tickets.
-- Transfer history itself uses the existing support_ticket_events table.
-- Safe to run more than once on MariaDB versions supporting IF NOT EXISTS.

ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS business_phone_number_id VARCHAR(64) NULL AFTER whatsapp_phone,
    ADD COLUMN IF NOT EXISTS business_display_phone_number VARCHAR(64) NULL AFTER business_phone_number_id;

CREATE INDEX IF NOT EXISTS idx_support_tickets_business_channel
    ON support_tickets (business_phone_number_id, whatsapp_phone);
