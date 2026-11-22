-- One durable row per started v2 turn. No prompts, arguments, media or replies.
CREATE TABLE IF NOT EXISTS ai_agent_turns (
    turn_id CHAR(64) NOT NULL PRIMARY KEY,
    ticket_id BIGINT UNSIGNED NOT NULL,
    started_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    latency_ms INT UNSIGNED NULL,
    outcome VARCHAR(32) NOT NULL DEFAULT 'started',
    reason_code VARCHAR(80) NULL,
    models_json TEXT NULL,
    reservation_ids_json TEXT NULL,
    tools_json TEXT NULL,
    guards_json TEXT NULL,
    input_tokens BIGINT UNSIGNED NULL,
    cached_input_tokens BIGINT UNSIGNED NULL,
    output_tokens BIGINT UNSIGNED NULL,
    cost_micros BIGINT UNSIGNED NOT NULL DEFAULT 0,
    pending_usage_count INT UNSIGNED NOT NULL DEFAULT 0,
    KEY idx_ai_agent_turns_ticket (ticket_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE ai_usage_events ADD COLUMN IF NOT EXISTS agent_turn_id CHAR(64) NULL;
CREATE INDEX IF NOT EXISTS idx_ai_usage_agent_turn ON ai_usage_events (agent_turn_id);
