-- Extend the existing audit; do not reconstruct or fabricate historical activity.
ALTER TABLE support_ticket_events ADD COLUMN IF NOT EXISTS audit_key VARCHAR(191) NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_support_event_audit_key ON support_ticket_events(audit_key);
ALTER TABLE support_statistics_metadata ADD COLUMN IF NOT EXISTS ticket_activity_tracking_started_at DATETIME(6) NULL;
UPDATE support_statistics_metadata SET ticket_activity_tracking_started_at=UTC_TIMESTAMP(6)
WHERE id=1 AND ticket_activity_tracking_started_at IS NULL;
