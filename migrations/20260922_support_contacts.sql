CREATE TABLE IF NOT EXISTS support_contacts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    normalized_phone VARCHAR(32) NOT NULL,
    display_name VARCHAR(191) NOT NULL,
    created_by_staff_id BIGINT NULL,
    updated_by_staff_id BIGINT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_support_contacts_phone (normalized_phone),
    KEY idx_support_contacts_name (display_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
