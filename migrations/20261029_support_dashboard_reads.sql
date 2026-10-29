-- Dashboard viewing is per staff and separate from WhatsApp delivery/read receipts.
-- No historical views are inferred. Existing incoming messages start unread until viewed.
CREATE TABLE IF NOT EXISTS support_dashboard_reads (
    ticket_id BIGINT UNSIGNED NOT NULL,
    staff_id BIGINT UNSIGNED NOT NULL,
    last_message_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (ticket_id, staff_id),
    KEY idx_dashboard_reads_staff (staff_id),
    CONSTRAINT fk_dashboard_reads_ticket FOREIGN KEY (ticket_id) REFERENCES support_tickets(id) ON DELETE CASCADE,
    CONSTRAINT fk_dashboard_reads_staff FOREIGN KEY (staff_id) REFERENCES staff(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
