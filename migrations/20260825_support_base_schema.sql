-- Core staff and support tables used by the support API before the additive
-- migrations dated 2026-08-26 and later. Reconstructed from the live support
-- schema; no existing table or row is changed when this file is applied.
-- Apply before the later support migrations when bootstrapping a new database.
-- The customer/shipping tables belong to the existing business database.

CREATE TABLE IF NOT EXISTS staff (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(320) NOT NULL,
    email_normalized VARCHAR(320) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'agent',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    last_login_at DATETIME(6) NULL,
    failed_login_count INT UNSIGNED NOT NULL DEFAULT 0,
    locked_until DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_staff_active_role (is_active, role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS staff_sessions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    staff_id BIGINT UNSIGNED NOT NULL,
    token_hash CHAR(64) NOT NULL,
    csrf_hash CHAR(64) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    last_seen_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_staff_sessions_token_hash (token_hash),
    KEY idx_staff_sessions_staff (staff_id, revoked_at, expires_at),
    KEY idx_staff_sessions_expiry (expires_at),
    CONSTRAINT fk_staff_sessions_staff FOREIGN KEY (staff_id)
        REFERENCES staff (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS support_tickets (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    ticket_reference VARCHAR(64) NOT NULL,
    active_key VARCHAR(191) NULL,
    whatsapp_phone VARCHAR(64) NOT NULL,
    customer_id VARCHAR(100) NULL,
    customer_userid VARCHAR(100) NULL,
    customer_name VARCHAR(255) NULL,
    reason TEXT NOT NULL,
    ai_summary TEXT NOT NULL,
    tracking_reference VARCHAR(191) NULL,
    requested_action VARCHAR(500) NULL,
    current_language VARCHAR(32) NULL,
    communication_style VARCHAR(32) NULL,
    context_json LONGTEXT NULL,
    source_whatsapp_message_id VARCHAR(191) NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'NEW',
    current_assignee_id VARCHAR(100) NULL,
    current_assignee_name VARCHAR(255) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    last_activity_at DATETIME(6) NOT NULL,
    assigned_at DATETIME(6) NULL,
    resolved_at DATETIME(6) NULL,
    resolved_by_staff_id BIGINT UNSIGNED NULL,
    closed_at DATETIME(6) NULL,
    deleted_at DATETIME(6) NULL,
    deleted_by_staff_id BIGINT NULL,
    deleted_by_staff_name VARCHAR(120) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_support_tickets_reference (ticket_reference),
    UNIQUE KEY uq_support_tickets_active_key (active_key),
    UNIQUE KEY uq_support_tickets_source_message (source_whatsapp_message_id),
    KEY idx_support_tickets_status_activity (status, last_activity_at),
    KEY idx_support_tickets_assignee_activity (current_assignee_id, last_activity_at),
    KEY idx_support_tickets_phone_created (whatsapp_phone, created_at),
    KEY idx_support_tickets_customer (customer_id),
    KEY idx_support_tickets_resolved_by_staff (resolved_by_staff_id),
    KEY idx_support_tickets_history_visible (status, deleted_at, last_activity_at),
    CONSTRAINT fk_support_tickets_resolved_by_staff FOREIGN KEY (resolved_by_staff_id)
        REFERENCES staff (id) ON DELETE SET NULL,
    CONSTRAINT chk_support_tickets_status CHECK (
        status IN ('NEW', 'ASSIGNED', 'IN_PROGRESS', 'WAITING_CUSTOMER',
                   'WAITING_INTERNAL', 'RESOLVED', 'CLOSED')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS support_ticket_messages (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    ticket_id BIGINT UNSIGNED NOT NULL,
    sender_type VARCHAR(32) NOT NULL,
    sender_id VARCHAR(100) NULL,
    sender_name VARCHAR(255) NULL,
    direction VARCHAR(16) NOT NULL,
    message_type VARCHAR(32) NOT NULL DEFAULT 'text',
    body TEXT NOT NULL,
    whatsapp_message_id VARCHAR(191) NULL,
    client_message_id VARCHAR(191) NULL,
    whatsapp_timestamp VARCHAR(64) NULL,
    send_status VARCHAR(32) NULL,
    error_message VARCHAR(500) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_support_messages_whatsapp_id (whatsapp_message_id),
    UNIQUE KEY uq_support_messages_client_id (client_message_id),
    KEY idx_support_messages_ticket_id (ticket_id, id),
    KEY idx_support_messages_ticket_created (ticket_id, created_at),
    CONSTRAINT fk_support_messages_ticket FOREIGN KEY (ticket_id)
        REFERENCES support_tickets (id),
    CONSTRAINT chk_support_messages_sender CHECK (
        sender_type IN ('CUSTOMER', 'AGENT', 'AI', 'SYSTEM')
    ),
    CONSTRAINT chk_support_messages_direction CHECK (
        direction IN ('INBOUND', 'OUTBOUND', 'INTERNAL')
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS support_ticket_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    ticket_id BIGINT UNSIGNED NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    actor_type VARCHAR(32) NOT NULL,
    actor_id VARCHAR(100) NULL,
    actor_name VARCHAR(255) NULL,
    details_json LONGTEXT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_support_events_ticket_id (ticket_id, id),
    KEY idx_support_events_ticket_created (ticket_id, created_at),
    CONSTRAINT fk_support_events_ticket FOREIGN KEY (ticket_id)
        REFERENCES support_tickets (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
