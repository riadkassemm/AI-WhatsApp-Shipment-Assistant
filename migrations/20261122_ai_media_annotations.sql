-- Additive, repeatable; application rollout requires separate migration approval.
ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS ai_media_text TEXT NULL,
    ADD COLUMN IF NOT EXISTS ai_media_kind VARCHAR(16) NULL,
    ADD COLUMN IF NOT EXISTS ai_media_processed_at DATETIME(6) NULL;
