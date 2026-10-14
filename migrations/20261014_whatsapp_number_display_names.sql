-- Staff-facing labels for WhatsApp support lines. Keep provider phone data intact.
-- The column is independent of Meta sync and linked-device pairing state.
ALTER TABLE whatsapp_phone_numbers
    ADD COLUMN IF NOT EXISTS display_name VARCHAR(80) NULL;
