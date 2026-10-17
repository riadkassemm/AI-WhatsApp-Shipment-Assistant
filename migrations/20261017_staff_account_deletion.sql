-- Deleting a staff account from /support removes it for good without hard-deleting
-- the row: tickets, sessions and audit records keep their staff references.
-- A deleted account is hidden from staff management, cannot sign in, edit or be
-- reactivated, and its email can be used again for a new account.
ALTER TABLE staff
    ADD COLUMN IF NOT EXISTS deleted_at DATETIME(6) NULL AFTER is_active;
