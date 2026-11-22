ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS ai_memory_summary TEXT NULL,
    ADD COLUMN IF NOT EXISTS ai_memory_through_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS ai_memory_fingerprint CHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS ai_memory_language VARCHAR(16) NULL,
    ADD COLUMN IF NOT EXISTS ai_memory_updated_at DATETIME(6) NULL;
