-- Private quick replies for the /support dashboard. Each row belongs to one staff
-- account; the API only reads and changes rows for the signed-in staff member.
-- Staff are never hard-deleted, so rows keep a valid staff_id.
CREATE TABLE IF NOT EXISTS support_quick_replies (
    id BIGINT NOT NULL AUTO_INCREMENT,
    staff_id BIGINT NOT NULL,
    title VARCHAR(80) NULL,
    body TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_support_quick_replies_staff (staff_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
