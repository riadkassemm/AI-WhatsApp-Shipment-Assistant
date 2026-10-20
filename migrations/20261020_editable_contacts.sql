-- Any contact in /support can be edited. Editing a Google or VCF contact saves a
-- dashboard contact; when its number was corrected, replaces_phone keeps the
-- original number so the Contacts list stops showing the outdated entry.
-- Google and VCF data itself is never changed.
ALTER TABLE support_contacts
    ADD COLUMN IF NOT EXISTS replaces_phone VARCHAR(32) NULL AFTER display_name,
    ADD INDEX IF NOT EXISTS idx_support_contacts_replaces_phone (replaces_phone);
