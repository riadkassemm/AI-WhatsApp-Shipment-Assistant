-- Preserve existing PDF replies; image MIME types are set on new writes.
ALTER TABLE support_quick_replies
    ADD COLUMN IF NOT EXISTS attachment_mime_type VARCHAR(64) NOT NULL DEFAULT 'application/pdf';
