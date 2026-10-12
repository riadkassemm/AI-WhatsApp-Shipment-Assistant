-- Shared support-team Google Contacts OAuth connection and local read-only contact cache.
-- Admin/Supervisor manage the connection; all authenticated staff may use synced contacts.
-- Manual support_contacts remain shared and unchanged.

CREATE TABLE IF NOT EXISTS support_google_accounts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    connected_by_staff_id BIGINT NOT NULL,
    google_subject VARCHAR(255) NOT NULL,
    google_email VARCHAR(320) NOT NULL,
    google_display_name VARCHAR(191) NULL,
    refresh_token_encrypted TEXT NOT NULL,
    sync_token TEXT NULL,
    last_synced_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_support_google_accounts_email (google_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS support_google_contacts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    account_id BIGINT UNSIGNED NOT NULL,
    resource_name VARCHAR(255) NOT NULL,
    normalized_phone VARCHAR(32) NOT NULL,
    display_name VARCHAR(191) NOT NULL,
    email VARCHAR(320) NULL,
    phone_label VARCHAR(64) NULL,
    etag VARCHAR(255) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_support_google_contact_phone (account_id, resource_name, normalized_phone),
    KEY idx_support_google_contacts_phone (normalized_phone),
    KEY idx_support_google_contacts_name (display_name),
    CONSTRAINT fk_support_google_contacts_account
        FOREIGN KEY (account_id) REFERENCES support_google_accounts(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
