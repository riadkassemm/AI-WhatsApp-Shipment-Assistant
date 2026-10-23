-- Keep actionable errors even when the callback precedes send persistence.
ALTER TABLE support_delivery_receipts
    ADD COLUMN IF NOT EXISTS failure_code INT UNSIGNED NULL;
