-- One-time reset of previously configured staff automatic messages.
-- Apply after deploying code that treats NULL as "send no automatic message".
-- Historical ticket and chat messages are not changed.
UPDATE staff
SET custom_greeting_message = NULL,
    custom_resolution_message = NULL,
    updated_at = UTC_TIMESTAMP(6)
WHERE custom_greeting_message IS NOT NULL
   OR custom_resolution_message IS NOT NULL;
