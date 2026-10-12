-- Persist WhatsApp-style forwarded-message provenance in the support timeline.
-- Additive/backward-compatible: existing messages remain original (not forwarded).

ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS is_forwarded TINYINT(1) NOT NULL DEFAULT 0 AFTER error_message,
    ADD COLUMN IF NOT EXISTS forwarded_from_ticket_id BIGINT NULL AFTER is_forwarded,
    ADD COLUMN IF NOT EXISTS forwarded_from_message_id BIGINT NULL AFTER forwarded_from_ticket_id;

CREATE INDEX IF NOT EXISTS idx_support_messages_forward_source
    ON support_ticket_messages (forwarded_from_ticket_id, forwarded_from_message_id);
