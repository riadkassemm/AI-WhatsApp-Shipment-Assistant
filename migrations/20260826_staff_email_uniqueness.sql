-- Inspect duplicate case-insensitive emails first. Do NOT add the unique index until
-- this query returns zero rows. Resolve duplicates from /support when possible.
SELECT LOWER(TRIM(email)) AS canonical_email,
       COUNT(*) AS account_count,
       GROUP_CONCAT(CONCAT('id=', id, ', role=', role, ', active=', is_active)
                    ORDER BY id SEPARATOR '; ') AS accounts
FROM staff
GROUP BY LOWER(TRIM(email))
HAVING COUNT(*) > 1;

-- After duplicate groups have been resolved:
UPDATE staff
SET email_normalized = LOWER(TRIM(email)),
    updated_at = UTC_TIMESTAMP(6);

ALTER TABLE staff
ADD UNIQUE KEY uq_staff_email_normalized (email_normalized);
