-- Per-number limits for staff-started chats (the outreach guard).
-- NULL means "use the server default" (WHATSAPP_OUTREACH_* settings).
-- Admins and Super Admins change these from the dashboard.
ALTER TABLE whatsapp_phone_numbers
    ADD COLUMN IF NOT EXISTS outreach_daily_limit SMALLINT UNSIGNED NULL,
    ADD COLUMN IF NOT EXISTS outreach_new_number_daily_limit SMALLINT UNSIGNED NULL,
    ADD COLUMN IF NOT EXISTS outreach_same_text_limit SMALLINT UNSIGNED NULL;
