-- WhatsApp stickers use the existing durable message/media columns. Reactions are
-- current state attached to their original message, never separate timeline rows.
-- Empty emoji values are retained as tombstones so removals and webhook retries do
-- not resurrect an older reaction.

CREATE TABLE IF NOT EXISTS support_message_reactions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    ticket_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    target_whatsapp_message_id VARCHAR(191) NOT NULL,
    reactor_key VARCHAR(255) NOT NULL,
    reactor_type VARCHAR(32) NOT NULL,
    reactor_id VARCHAR(191) NULL,
    reactor_name VARCHAR(191) NULL,
    emoji VARCHAR(64) NOT NULL DEFAULT '',
    provider_reaction_id VARCHAR(191) NULL,
    client_reaction_id VARCHAR(191) NULL,
    provider_timestamp BIGINT UNSIGNED NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_support_reaction_actor (message_id, reactor_key),
    UNIQUE KEY uq_support_reaction_provider_event (provider_reaction_id),
    UNIQUE KEY uq_support_reaction_client_event (client_reaction_id),
    KEY idx_support_reactions_ticket_message (ticket_id, message_id),
    KEY idx_support_reactions_target (target_whatsapp_message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
