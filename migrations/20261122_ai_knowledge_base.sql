-- AI knowledge base (Step 4c, plan B2). One short article per customer topic
-- (intent): the approved answer, the data it needs, when to hand over to staff,
-- and a few approved example replies. The AI reads APPROVED articles only.
--
-- Drafts come from staff or from the offline drafting tool. Admins and Super
-- Admins approve them. An approved article is never edited in place: a change
-- is a new draft version of the same topic, and approving it retires the old
-- one. "Observed practice" (what agents actually did) is kept separately and is
-- never shown to the AI. source_ticket_ids are internal references only.
-- Every change is audited. Additive; apply before the API restart.
CREATE TABLE IF NOT EXISTS ai_kb_articles (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    intent_key VARCHAR(64) NOT NULL,
    version INT UNSIGNED NOT NULL DEFAULT 1,
    status VARCHAR(16) NOT NULL DEFAULT 'DRAFT',
    title VARCHAR(191) NOT NULL,
    approved_policy TEXT NOT NULL,
    data_needed VARCHAR(500) NULL,
    escalate_when TEXT NULL,
    examples_json LONGTEXT NULL,
    observed_practice TEXT NULL,
    source VARCHAR(16) NOT NULL DEFAULT 'STAFF',
    source_ticket_count INT UNSIGNED NULL,
    source_ticket_ids LONGTEXT NULL,
    owner_staff_id BIGINT NULL,
    created_by_staff_id BIGINT NULL,
    updated_by_staff_id BIGINT NULL,
    approved_by_staff_id BIGINT NULL,
    approved_by_staff_name VARCHAR(191) NULL,
    approved_at DATETIME(6) NULL,
    retired_at DATETIME(6) NULL,
    row_version INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_ai_kb_articles_version (intent_key, version),
    KEY idx_ai_kb_articles_status (status, intent_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ai_kb_audit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    article_id BIGINT UNSIGNED NOT NULL,
    intent_key VARCHAR(64) NOT NULL,
    action VARCHAR(32) NOT NULL,
    actor_staff_id BIGINT NULL,
    actor_staff_name VARCHAR(191) NULL,
    details_json LONGTEXT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    KEY idx_ai_kb_audit_article (article_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
