-- AI usage and budget (dashboard: AI Assistant > Usage & Costs).
--
-- ai_model_prices: USD prices per 1M tokens (and per audio minute) for each
-- OpenAI model prefix. A model may be called only once an Admin/Super Admin has
-- VERIFIED its price row; the seeded rows below come from the official pricing
-- page (2026-11-22) but start unverified, so AI stays blocked until verified.
CREATE TABLE IF NOT EXISTS ai_model_prices (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    model_prefix VARCHAR(100) NOT NULL,
    input_per_million DECIMAL(12,6) NOT NULL,
    cached_input_per_million DECIMAL(12,6) NOT NULL,
    output_per_million DECIMAL(12,6) NOT NULL,
    audio_per_minute DECIMAL(12,6) NULL,
    batch_factor DECIMAL(5,4) NOT NULL DEFAULT 0.5000,
    effective_from DATETIME(6) NOT NULL,
    source VARCHAR(255) NULL,
    verified_by_staff_id BIGINT NULL,
    verified_by_staff_name VARCHAR(120) NULL,
    verified_at DATETIME(6) NULL,
    retired_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_ai_model_prices_prefix (model_prefix, effective_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO ai_model_prices (model_prefix, input_per_million, cached_input_per_million, output_per_million,
                             audio_per_minute, effective_from, source)
SELECT seed.model_prefix, seed.input_price, seed.cached_price, seed.output_price, seed.audio_price,
       '2026-11-22 00:00:00', 'developers.openai.com/api/docs/pricing (2026-11-22)'
FROM (
    SELECT 'gpt-6-luna' AS model_prefix, 0.100000 AS input_price, 0.010000 AS cached_price, 0.500000 AS output_price, NULL AS audio_price
    UNION ALL SELECT 'gpt-6.1-terra', 1.000000, 0.050000, 5.000000, NULL
    UNION ALL SELECT 'gpt-6.2-sol', 2.000000, 0.100000, 10.000000, NULL
    UNION ALL SELECT 'gpt-6.1-sol', 2.000000, 0.100000, 10.000000, NULL
    UNION ALL SELECT 'gpt-6-sol', 2.000000, 0.200000, 10.000000, NULL
    UNION ALL SELECT 'gpt-6.2-astra', 8.000000, 0.400000, 40.000000, NULL
    UNION ALL SELECT 'gpt-transcribe', 0.000000, 0.000000, 0.000000, 0.004500
) AS seed
WHERE NOT EXISTS (SELECT 1 FROM ai_model_prices existing WHERE existing.model_prefix = seed.model_prefix);

-- One row of budget settings. Amounts are integer micro-USD (1 USD = 1,000,000).
CREATE TABLE IF NOT EXISTS ai_budget_settings (
    id TINYINT UNSIGNED NOT NULL,
    monthly_limit_micros BIGINT UNSIGNED NOT NULL DEFAULT 300000000,
    daily_limit_micros BIGINT UNSIGNED NULL,
    warning_thresholds VARCHAR(64) NOT NULL DEFAULT '50,80,95',
    enforcement_enabled TINYINT(1) NOT NULL DEFAULT 1,
    budget_timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Beirut',
    override_until DATETIME(6) NULL,
    version INT UNSIGNED NOT NULL DEFAULT 1,
    updated_by_staff_id BIGINT NULL,
    updated_by_staff_name VARCHAR(120) NULL,
    updated_at DATETIME(6) NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO ai_budget_settings (id) VALUES (1);

-- One row per billable OpenAI call. Written as `pending` with the full cost
-- estimate BEFORE the call; only real provider usage may lower it afterwards.
-- No message text, prompts or keys are stored here.
CREATE TABLE IF NOT EXISTS ai_usage_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    reservation_id CHAR(32) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    settled_at DATETIME(6) NULL,
    duration_ms INT UNSIGNED NULL,
    purpose VARCHAR(32) NOT NULL,
    tier VARCHAR(16) NOT NULL,
    requested_model VARCHAR(100) NOT NULL,
    model VARCHAR(100) NULL,
    input_tokens INT UNSIGNED NULL,
    cached_input_tokens INT UNSIGNED NULL,
    output_tokens INT UNSIGNED NULL,
    reasoning_tokens INT UNSIGNED NULL,
    audio_seconds DECIMAL(10,2) NULL,
    modalities VARCHAR(32) NULL,
    estimate_micros BIGINT UNSIGNED NOT NULL,
    cost_micros BIGINT UNSIGNED NOT NULL,
    price_id BIGINT UNSIGNED NULL,
    cost_basis VARCHAR(24) NOT NULL,
    outcome VARCHAR(24) NOT NULL,
    openai_request_id VARCHAR(128) NULL,
    phone_number_id VARCHAR(64) NULL,
    ticket_id BIGINT UNSIGNED NULL,
    batch_id VARCHAR(64) NULL,
    budget_day DATE NOT NULL,
    budget_month CHAR(7) NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_ai_usage_reservation (reservation_id),
    KEY idx_ai_usage_created (created_at),
    KEY idx_ai_usage_month (budget_month, outcome),
    KEY idx_ai_usage_day (budget_day),
    KEY idx_ai_usage_line (phone_number_id, created_at),
    KEY idx_ai_usage_model (model, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
