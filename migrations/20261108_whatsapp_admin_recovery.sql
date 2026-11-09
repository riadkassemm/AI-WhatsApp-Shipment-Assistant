-- Additive Phase B schema. Apply only in the approved maintenance window.
-- No existing message, delivery claim, authentication or registry state is reset.
CREATE TABLE IF NOT EXISTS whatsapp_bridge_controls (
    line_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    paused TINYINT(1) NOT NULL DEFAULT 0,
    active_operation_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS whatsapp_bridge_operations (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    line_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    operator_id VARCHAR(20) NOT NULL,
    operator_role VARCHAR(16) NOT NULL,
    action VARCHAR(16) NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    parameters_json TEXT NOT NULL,
    previous_paused TINYINT(1) NOT NULL DEFAULT 0,
    state VARCHAR(16) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    completed_at DATETIME(6) NULL,
    INDEX idx_bridge_operation_work (state, updated_at),
    INDEX idx_bridge_operation_line (line_id, created_at),
    INDEX idx_bridge_operation_operator (operator_id, created_at)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS whatsapp_bridge_operation_audit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    operation_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    actor_id VARCHAR(20) NOT NULL,
    actor_role VARCHAR(16) NOT NULL,
    state VARCHAR(16) NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    INDEX idx_bridge_operation_audit (operation_id, id)
) ENGINE=InnoDB;

ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS admin_hold TINYINT(1) NOT NULL DEFAULT 0,
    ADD INDEX IF NOT EXISTS idx_support_admin_hold (admin_hold, send_status, id);

-- Customer reply contents belong in the protected delivery store, never in
-- management operation parameters, audit responses or operational logs.
CREATE TABLE IF NOT EXISTS whatsapp_paused_bot_replies (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    line_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_message_id VARCHAR(191) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    transport_id VARCHAR(191) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    recipient VARCHAR(20) NOT NULL,
    body TEXT NOT NULL,
    state VARCHAR(16) NOT NULL,
    provider_message_id VARCHAR(191) NULL,
    dispatch_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_paused_bot_source (line_id, source_message_id),
    UNIQUE KEY uq_paused_bot_transport (transport_id),
    INDEX idx_paused_bot_line (line_id, state, id)
) ENGINE=InnoDB;
