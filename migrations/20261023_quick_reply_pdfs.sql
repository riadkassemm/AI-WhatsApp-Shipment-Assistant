-- Private PDF quick replies. Files live under the existing private media root.
ALTER TABLE support_quick_replies
    ADD COLUMN IF NOT EXISTS attachment_key CHAR(32) NULL,
    ADD COLUMN IF NOT EXISTS attachment_filename VARCHAR(180) NULL,
    ADD COLUMN IF NOT EXISTS attachment_size INT UNSIGNED NULL;
