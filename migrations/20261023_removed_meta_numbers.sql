-- Keep historical labels and a durable retirement marker. Meta sync, webhooks,
-- and legacy environment seeding must never clear this marker automatically.
ALTER TABLE whatsapp_phone_numbers
    ADD COLUMN IF NOT EXISTS removed_at DATETIME(6) NULL;
