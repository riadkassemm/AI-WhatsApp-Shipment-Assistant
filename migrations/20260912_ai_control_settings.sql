-- Persistent, deployment-wide AI control configuration.
-- Defaults preserve pre-migration behavior: MANUAL mode with AI enabled.

CREATE TABLE IF NOT EXISTS ai_control_settings (
    id TINYINT UNSIGNED NOT NULL,
    control_mode VARCHAR(16) NOT NULL,
    manual_ai_enabled TINYINT(1) NOT NULL,
    schedule_start_time CHAR(5) NOT NULL,
    schedule_end_time CHAR(5) NOT NULL,
    schedule_timezone VARCHAR(128) NOT NULL,
    version BIGINT UNSIGNED NOT NULL,
    updated_by_staff_id BIGINT UNSIGNED NULL,
    updated_by_staff_name VARCHAR(120) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO ai_control_settings (
    id,
    control_mode,
    manual_ai_enabled,
    schedule_start_time,
    schedule_end_time,
    schedule_timezone,
    version,
    updated_by_staff_id,
    updated_by_staff_name,
    created_at,
    updated_at
) VALUES (
    1,
    'MANUAL',
    1,
    '08:00',
    '18:00',
    'UTC',
    1,
    NULL,
    NULL,
    UTC_TIMESTAMP(6),
    UTC_TIMESTAMP(6)
);

CREATE TABLE IF NOT EXISTS ai_control_settings_audit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    settings_id TINYINT UNSIGNED NOT NULL,
    action VARCHAR(64) NOT NULL,
    actor_staff_id BIGINT UNSIGNED NOT NULL,
    actor_staff_name VARCHAR(120) NOT NULL,
    old_configuration_json LONGTEXT NOT NULL,
    new_configuration_json LONGTEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_ai_control_settings_audit_created (created_at),
    KEY idx_ai_control_settings_audit_actor (actor_staff_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
