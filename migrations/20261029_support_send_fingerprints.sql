-- Additive; do not invent fingerprints for legacy requests.
ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS request_fingerprint CHAR(64) NULL;
