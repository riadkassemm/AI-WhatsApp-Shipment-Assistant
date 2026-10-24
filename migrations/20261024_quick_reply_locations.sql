-- Locations and manager-created copies retain the existing per-staff ownership.
ALTER TABLE support_quick_replies
    ADD COLUMN IF NOT EXISTS location_latitude DECIMAL(10,7) NULL,
    ADD COLUMN IF NOT EXISTS location_longitude DECIMAL(10,7) NULL,
    ADD COLUMN IF NOT EXISTS location_name VARCHAR(255) NULL,
    ADD COLUMN IF NOT EXISTS location_address VARCHAR(1000) NULL;
