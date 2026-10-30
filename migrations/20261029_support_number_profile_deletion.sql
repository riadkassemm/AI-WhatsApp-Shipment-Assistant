-- Preserve historical labels and read-only status when a directory profile is
-- physically deleted. Removal audit records remain the anti-resurrection guard.
ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS business_display_name VARCHAR(80) NULL,
    ADD COLUMN IF NOT EXISTS business_number_retired_at DATETIME(6) NULL;
