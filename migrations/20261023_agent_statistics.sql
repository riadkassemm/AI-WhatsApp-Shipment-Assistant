-- Local dashboard activity only. No historical activity time is inferred.
CREATE TABLE IF NOT EXISTS support_statistics_metadata (
    id TINYINT NOT NULL PRIMARY KEY,
    activity_tracking_started_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;
INSERT IGNORE INTO support_statistics_metadata VALUES (1, UTC_TIMESTAMP(6));

CREATE TABLE IF NOT EXISTS support_agent_heartbeats (
    staff_id BIGINT NOT NULL,
    session_id BIGINT NOT NULL,
    tab_id CHAR(36) NOT NULL,
    sequence_number BIGINT UNSIGNED NOT NULL,
    last_seen_at DATETIME(6) NOT NULL,
    active_until DATETIME(6) NULL,
    PRIMARY KEY (staff_id, session_id, tab_id),
    KEY idx_activity_heartbeat_cleanup (staff_id, last_seen_at)
) ENGINE=InnoDB;

-- Canonical union of activity intervals per staff/day, merged under a staff lock.
CREATE TABLE IF NOT EXISTS support_agent_activity (
    staff_id BIGINT NOT NULL,
    activity_day DATE NOT NULL,
    started_at DATETIME(6) NOT NULL,
    ended_at DATETIME(6) NOT NULL,
    PRIMARY KEY (staff_id, activity_day, started_at),
    KEY idx_activity_report (activity_day, staff_id),
    CONSTRAINT chk_activity_interval CHECK (ended_at > started_at)
) ENGINE=InnoDB;

CREATE INDEX IF NOT EXISTS idx_statistics_ticket_events
    ON support_ticket_events (event_type, created_at, actor_id, ticket_id);
-- Dispatch time survives later edits/receipts; legacy rows fall back to creation.
ALTER TABLE support_ticket_messages ADD COLUMN IF NOT EXISTS statistics_sent_at
    DATETIME(6) GENERATED ALWAYS AS (COALESCE(delivery_started_at, created_at)) STORED;
CREATE INDEX IF NOT EXISTS idx_statistics_messages
    ON support_ticket_messages (sender_type, direction, send_status, statistics_sent_at, sender_id);
