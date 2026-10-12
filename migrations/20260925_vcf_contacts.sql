-- Shared VCF-imported contacts. Imported contacts are kept separate from
-- manually saved contacts and Google Contacts so all three sources remain
-- independently maintainable while the UI presents one de-duplicated list.

CREATE TABLE IF NOT EXISTS support_vcf_contacts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    normalized_phone VARCHAR(32) NOT NULL,
    display_name VARCHAR(191) NOT NULL,
    email VARCHAR(320) NULL,
    phone_label VARCHAR(64) NULL,
    source_filename VARCHAR(255) NULL,
    imported_by_staff_id BIGINT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_support_vcf_contacts_phone (normalized_phone),
    KEY idx_support_vcf_contacts_name (display_name),
    KEY idx_support_vcf_contacts_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
