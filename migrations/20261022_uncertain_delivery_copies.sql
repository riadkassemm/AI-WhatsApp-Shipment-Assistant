-- Additive and repeatable. Existing messages keep NULL (not a replacement).
ALTER TABLE support_ticket_messages
  ADD COLUMN IF NOT EXISTS replacement_for_message_id BIGINT UNSIGNED NULL,
  ADD UNIQUE INDEX IF NOT EXISTS uq_support_delivery_replacement (replacement_for_message_id);
