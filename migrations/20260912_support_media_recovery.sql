-- Durable WhatsApp media references for support-ticket attachments.
-- Existing local media files continue to work unchanged. These nullable fields let
-- any backend instance re-fetch an inbound image/audio attachment from WhatsApp if
-- the protected local cache is temporarily missing.
-- Safe to run more than once on MariaDB versions supporting IF NOT EXISTS.

ALTER TABLE support_ticket_messages
    ADD COLUMN IF NOT EXISTS provider_media_id VARCHAR(512) NULL AFTER message_type,
    ADD COLUMN IF NOT EXISTS provider_media_mime_type VARCHAR(191) NULL AFTER provider_media_id;
