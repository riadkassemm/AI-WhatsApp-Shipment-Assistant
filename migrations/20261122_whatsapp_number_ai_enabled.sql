-- Per-number AI switch. AI answers on a WhatsApp number only when the global
-- AI setting is ON *and* the number's ai_enabled flag is 1. The default is 0, so
-- every existing and newly connected number starts with AI off. Admins and
-- Super Admins change it from the dashboard (AI Assistant > WhatsApp Numbers).
ALTER TABLE whatsapp_phone_numbers
    ADD COLUMN IF NOT EXISTS ai_enabled TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ai_updated_by_staff_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS ai_updated_at DATETIME(6) NULL;
