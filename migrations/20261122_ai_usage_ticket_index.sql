-- Per-ticket AI cost (shown to Admins and Super Admins in the ticket details)
-- reads ai_usage_events by ticket_id. Additive index only; safe to apply while
-- the API runs.
ALTER TABLE ai_usage_events
    ADD INDEX IF NOT EXISTS idx_ai_usage_ticket (ticket_id);
