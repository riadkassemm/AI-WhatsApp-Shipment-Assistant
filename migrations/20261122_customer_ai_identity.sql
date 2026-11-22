-- AI identity levels (Step 4, plan B4), without OTP.
--   L0 unknown: public information only.
--   L1 phone-matched: computed live (exact match of the WhatsApp number to exactly
--      one active users.mobile that no other account shares). Orders, tracking
--      and the shipments list for that account.
--   L2 staff-verified: a staff member confirmed the customer's identity and
--      clicked "Verify customer for AI". Wallet, payments and account details.
-- Only L2 is stored. One active verification per WhatsApp number (active_key is
-- the number while active, NULL once revoked or expired). It expires after
-- AI_IDENTITY_VERIFICATION_DAYS (30) and is revoked automatically when the
-- account's mobile changes, the account becomes inactive or another account
-- uses the number. Every change is audited. Additive; apply before the API
-- restart (schema guard).
CREATE TABLE IF NOT EXISTS customer_ai_verifications (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    whatsapp_phone VARCHAR(64) NOT NULL,
    active_key VARCHAR(64) NULL,
    customer_id VARCHAR(100) NOT NULL,
    customer_userid VARCHAR(100) NULL,
    bound_mobile VARCHAR(64) NOT NULL,
    verified_by_staff_id BIGINT NOT NULL,
    verified_by_staff_name VARCHAR(191) NULL,
    ticket_id BIGINT UNSIGNED NULL,
    verified_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6) NULL,
    revoked_reason VARCHAR(64) NULL,
    revoked_by_staff_id BIGINT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_customer_ai_verifications_active (active_key),
    KEY idx_customer_ai_verifications_phone (whatsapp_phone, verified_at),
    KEY idx_customer_ai_verifications_customer (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS customer_ai_verification_audit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    verification_id BIGINT UNSIGNED NULL,
    whatsapp_phone VARCHAR(64) NOT NULL,
    customer_id VARCHAR(100) NULL,
    action VARCHAR(32) NOT NULL,
    reason VARCHAR(64) NULL,
    actor_type VARCHAR(16) NOT NULL,
    actor_staff_id BIGINT NULL,
    actor_staff_name VARCHAR(191) NULL,
    ticket_id BIGINT UNSIGNED NULL,
    details_json LONGTEXT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_customer_ai_verification_audit_phone (whatsapp_phone, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
