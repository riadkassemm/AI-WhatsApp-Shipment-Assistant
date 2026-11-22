-- OpenAI API key managed from the dashboard (AI Assistant > OpenAI Configuration).
-- The key is stored encrypted with Fernet (server secret AI_SECRETS_ENCRYPTION_KEY);
-- only its last four characters and a SHA-256 fingerprint prefix are stored in
-- clear for display and audit. Changes are audited in ai_control_settings_audit.
-- `version` increases on every save or removal; the app reads it on each OpenAI
-- call, so a new key applies on all instances without a restart.
CREATE TABLE IF NOT EXISTS ai_provider_credentials (
    provider VARCHAR(32) NOT NULL,
    api_key_encrypted TEXT NULL,
    key_last4 VARCHAR(8) NULL,
    key_fingerprint CHAR(16) NULL,
    version INT UNSIGNED NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'NOT_CONFIGURED',
    last_tested_at DATETIME(6) NULL,
    last_error_code VARCHAR(64) NULL,
    updated_by_staff_id BIGINT NULL,
    updated_by_staff_name VARCHAR(120) NULL,
    updated_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (provider)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
