-- Compare PDF contents across independent copies without exposing private files.
-- Existing files are fingerprinted lazily when assigning a PDF to their owners.
ALTER TABLE support_quick_replies
    ADD COLUMN IF NOT EXISTS attachment_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL;
