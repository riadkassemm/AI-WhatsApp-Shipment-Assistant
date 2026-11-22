-- AI tickets (Step 3). Every conversation the AI assistant handles is a support
-- ticket with handling_mode = 'AI'. It is listed in the AI Tickets queue, not in
-- Unassigned. A staff member can take it over (handling_mode becomes 'HUMAN' and
-- the ticket is assigned to them), and the AI can escalate it (handling_mode
-- becomes 'HUMAN' and the ticket moves to Unassigned with the escalation reason).
--
-- Additive only. Every existing ticket keeps handling_mode = 'HUMAN', so nothing
-- changes until AI is enabled for a number. Apply before restarting the API: the
-- schema guard refuses to serve support routes without these columns.
ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS handling_mode VARCHAR(16) NOT NULL DEFAULT 'HUMAN',
    ADD COLUMN IF NOT EXISTS ai_awaiting_customer TINYINT(1) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ai_last_reply_at DATETIME(6) NULL,
    ADD COLUMN IF NOT EXISTS ai_escalation_reason VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS ai_escalated_at DATETIME(6) NULL,
    ADD INDEX IF NOT EXISTS idx_support_tickets_handling (handling_mode, status, last_activity_at);
