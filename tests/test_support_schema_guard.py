from app.support_repository import _missing_support_schema_migrations


def add_linked_device_schema(columns):
    columns["support_ticket_messages"].update({"whatsapp_change_timestamp", "pending_change_operation_id"})
    columns["support_message_change_operations"] = {
        "operation_id", "message_id", "base_version", "user_id", "action", "content", "status", "lease_until", "provider_timestamp"
    }
    columns["support_message_change_events"] = {"event_id", "message_id", "provider_timestamp"}


def test_support_schema_guard_accepts_current_schema():
    columns = {
        "support_tickets": {
            "business_phone_number_id",
            "business_display_phone_number",
            "conversation_origin",
            "initiated_by_staff_id",
            "initiated_by_staff_name",
        },
        "support_ticket_messages": {
            "provider_media_id",
            "provider_media_mime_type",
            "reply_to_whatsapp_message_id",
            "location_latitude",
            "location_longitude",
            "location_name",
            "location_address",
            "provider_media_filename",
            "is_forwarded",
            "forwarded_from_ticket_id",
            "forwarded_from_message_id",
        },
        "support_message_reactions": {
            "ticket_id",
            "message_id",
            "target_whatsapp_message_id",
            "reactor_key",
            "reactor_type",
            "emoji",
            "provider_reaction_id",
            "client_reaction_id",
            "provider_timestamp",
        },
        "support_message_user_deletions": {"message_id", "user_id", "deleted_at"},
        "whatsapp_phone_numbers": {"display_name"},
        "staff": {"deleted_at"},
        "support_quick_replies": {"id", "staff_id", "title", "body", "created_at", "updated_at"},
        "support_contacts": {"replaces_phone"},
    }
    columns["support_ticket_messages"].update({"edited_at", "deleted_for_everyone_at", "deleted_by", "version"})
    add_linked_device_schema(columns)
    assert _missing_support_schema_migrations(columns) == []


def test_support_schema_guard_identifies_whatsapp_features_migration():
    columns = {
        "support_tickets": {
            "business_phone_number_id",
            "business_display_phone_number",
            "conversation_origin",
            "initiated_by_staff_id",
            "initiated_by_staff_name",
        },
        "support_ticket_messages": {
            "provider_media_id",
            "provider_media_mime_type",
            "is_forwarded",
            "forwarded_from_ticket_id",
            "forwarded_from_message_id",
        },
        "support_message_reactions": {
            "ticket_id",
            "message_id",
            "target_whatsapp_message_id",
            "reactor_key",
            "reactor_type",
            "emoji",
            "provider_reaction_id",
            "client_reaction_id",
            "provider_timestamp",
        },
        "support_message_user_deletions": {"message_id", "user_id", "deleted_at"},
        "whatsapp_phone_numbers": {"display_name"},
        "staff": {"deleted_at"},
        "support_quick_replies": {"id", "staff_id", "title", "body", "created_at", "updated_at"},
        "support_contacts": {"replaces_phone"},
    }
    columns["support_ticket_messages"].update({"edited_at", "deleted_for_everyone_at", "deleted_by", "version"})
    add_linked_device_schema(columns)
    assert _missing_support_schema_migrations(columns) == [
        "migrations/20260917_whatsapp_message_features.sql"
    ]


def test_support_schema_guard_identifies_reaction_migration():
    columns = {
        "support_tickets": {
            "business_phone_number_id",
            "business_display_phone_number",
            "conversation_origin",
            "initiated_by_staff_id",
            "initiated_by_staff_name",
        },
        "support_ticket_messages": {
            "provider_media_id",
            "provider_media_mime_type",
            "reply_to_whatsapp_message_id",
            "location_latitude",
            "location_longitude",
            "location_name",
            "location_address",
            "provider_media_filename",
            "is_forwarded",
            "forwarded_from_ticket_id",
            "forwarded_from_message_id",
        },
        "support_message_user_deletions": {"message_id", "user_id", "deleted_at"},
        "whatsapp_phone_numbers": {"display_name"},
        "staff": {"deleted_at"},
        "support_quick_replies": {"id", "staff_id", "title", "body", "created_at", "updated_at"},
        "support_contacts": {"replaces_phone"},
    }
    columns["support_ticket_messages"].update({"edited_at", "deleted_for_everyone_at", "deleted_by", "version"})
    add_linked_device_schema(columns)
    assert _missing_support_schema_migrations(columns) == [
        "migrations/20260926_whatsapp_stickers_and_reactions.sql"
    ]


def test_support_schema_guard_identifies_linked_device_migration():
    from app.support_repository import _SUPPORT_SCHEMA_MIGRATIONS

    columns = {}
    for path, requirements in _SUPPORT_SCHEMA_MIGRATIONS:
        if "20261012" not in path:
            for table, required in requirements.items():
                columns.setdefault(table, set()).update(required)
    assert _missing_support_schema_migrations(columns) == [
        "migrations/20261012_linked_device_message_changes.sql"
    ]


def test_support_schema_guard_identifies_number_display_name_migration():
    from app.support_repository import _SUPPORT_SCHEMA_MIGRATIONS

    columns = {}
    for path, requirements in _SUPPORT_SCHEMA_MIGRATIONS:
        if "20261014" not in path:
            for table, required in requirements.items():
                columns.setdefault(table, set()).update(required)
    assert _missing_support_schema_migrations(columns) == [
        "migrations/20261014_whatsapp_number_display_names.sql"
    ]


def test_support_schema_guard_identifies_staff_deletion_migration():
    from app.support_repository import _SUPPORT_SCHEMA_MIGRATIONS

    columns = {}
    for path, requirements in _SUPPORT_SCHEMA_MIGRATIONS:
        if "20261017" not in path:
            for table, required in requirements.items():
                columns.setdefault(table, set()).update(required)
    assert _missing_support_schema_migrations(columns) == [
        "migrations/20261017_staff_account_deletion.sql"
    ]


def test_support_schema_guard_identifies_quick_replies_migration():
    from app.support_repository import _SUPPORT_SCHEMA_MIGRATIONS

    columns = {}
    for path, requirements in _SUPPORT_SCHEMA_MIGRATIONS:
        if "20261019" not in path:
            for table, required in requirements.items():
                columns.setdefault(table, set()).update(required)
    assert _missing_support_schema_migrations(columns) == [
        "migrations/20261019_staff_quick_replies.sql"
    ]


def test_support_schema_guard_identifies_editable_contacts_migration():
    from app.support_repository import _SUPPORT_SCHEMA_MIGRATIONS

    columns = {}
    for path, requirements in _SUPPORT_SCHEMA_MIGRATIONS:
        if "20261020_editable_contacts" not in path:
            for table, required in requirements.items():
                columns.setdefault(table, set()).update(required)
    assert _missing_support_schema_migrations(columns) == [
        "migrations/20261020_editable_contacts.sql"
    ]
