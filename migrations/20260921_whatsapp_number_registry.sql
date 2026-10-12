-- Dynamic WhatsApp Business Account / phone-number registry.
-- Safe defaults: discovered numbers are disabled until a Supervisor/Admin enables them.

CREATE TABLE IF NOT EXISTS whatsapp_business_accounts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    waba_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    last_synced_at DATETIME(6) NULL,
    created_by_staff_id BIGINT NULL,
    updated_by_staff_id BIGINT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_whatsapp_business_accounts_waba_id (waba_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS whatsapp_phone_numbers (
    phone_number_id VARCHAR(64) NOT NULL,
    waba_id VARCHAR(64) NULL,
    display_phone_number VARCHAR(64) NULL,
    verified_name VARCHAR(255) NULL,
    quality_rating VARCHAR(32) NULL,
    is_enabled TINYINT(1) NOT NULL DEFAULT 0,
    provider_present TINYINT(1) NOT NULL DEFAULT 1,
    source VARCHAR(32) NOT NULL DEFAULT 'META_SYNC',
    last_synced_at DATETIME(6) NULL,
    updated_by_staff_id BIGINT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (phone_number_id),
    KEY idx_whatsapp_phone_numbers_waba_id (waba_id),
    KEY idx_whatsapp_phone_numbers_enabled (is_enabled, provider_present),
    CONSTRAINT fk_whatsapp_phone_numbers_waba
      FOREIGN KEY (waba_id) REFERENCES whatsapp_business_accounts (waba_id)
      ON UPDATE CASCADE ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS whatsapp_integration_audit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    action VARCHAR(64) NOT NULL,
    actor_staff_id BIGINT NULL,
    waba_id VARCHAR(64) NULL,
    phone_number_id VARCHAR(64) NULL,
    details_json JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_whatsapp_integration_audit_created_at (created_at),
    KEY idx_whatsapp_integration_audit_phone (phone_number_id),
    KEY idx_whatsapp_integration_audit_waba (waba_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
