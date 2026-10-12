-- Per-staff customer-facing automatic support messages.
-- NULL/empty values mean: use the existing language-aware default message.
ALTER TABLE staff
    ADD COLUMN IF NOT EXISTS custom_greeting_message TEXT NULL,
    ADD COLUMN IF NOT EXISTS custom_resolution_message TEXT NULL;
