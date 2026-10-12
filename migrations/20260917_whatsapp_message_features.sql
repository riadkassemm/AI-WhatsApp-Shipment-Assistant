-- WhatsApp-style message features: reply context, customer locations, PDFs.
-- Additive and backward compatible; existing rows remain unchanged.

ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS reply_to_whatsapp_message_id VARCHAR(191) NULL AFTER body,
    ADD COLUMN IF NOT EXISTS location_latitude DECIMAL(10,7) NULL AFTER reply_to_whatsapp_message_id,
    ADD COLUMN IF NOT EXISTS location_longitude DECIMAL(10,7) NULL AFTER location_latitude,
    ADD COLUMN IF NOT EXISTS location_name VARCHAR(255) NULL AFTER location_longitude,
    ADD COLUMN IF NOT EXISTS location_address VARCHAR(1000) NULL AFTER location_name,
    ADD COLUMN IF NOT EXISTS provider_media_filename VARCHAR(255) NULL AFTER provider_media_mime_type;

CREATE INDEX IF NOT EXISTS idx_support_messages_reply_context
    ON support_ticket_messages (ticket_id, reply_to_whatsapp_message_id);
