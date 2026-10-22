from pathlib import Path
import re

from app.support_repository import _SUPPORT_SCHEMA_MIGRATIONS, _missing_support_schema_migrations


def add_base_schema(columns):
    for table, required in _SUPPORT_SCHEMA_MIGRATIONS[0][1].items():
        columns.setdefault(table, set()).update(required)


def add_linked_device_schema(columns):
    for path, required_tables in _SUPPORT_SCHEMA_MIGRATIONS:
        if "20261023_agent_statistics" in path or "20261023_quick_reply_pdfs" in path:
            for table, required in required_tables.items():
                columns.setdefault(table, set()).update(required)
    columns['support_ticket_messages'].add('replacement_for_message_id')
    columns["support_ticket_messages"].update({"transport_client_id", "sender_scope", "delivery_token", "delivery_claimed_at", "delivery_started_at", "delivery_retry_safe"})
    columns["support_ticket_messages"].update({"whatsapp_change_timestamp", "pending_change_operation_id"})
    columns["support_ticket_messages"].update({"delivery_status", "delivered_at", "read_at"})
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
    add_base_schema(columns)
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
    add_base_schema(columns)
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
    add_base_schema(columns)
    assert _missing_support_schema_migrations(columns) == [
        "migrations/20260926_whatsapp_stickers_and_reactions.sql"
    ]


def test_support_schema_guard_identifies_missing_base_tables():
    columns = {}
    for path, requirements in _SUPPORT_SCHEMA_MIGRATIONS:
        if "20260825" not in path:
            for table, required in requirements.items():
                columns.setdefault(table, set()).update(required)
    assert _missing_support_schema_migrations(columns) == [
        "migrations/20260825_support_base_schema.sql"
    ]


def test_every_support_schema_guard_migration_file_exists():
    for path, _requirements in _SUPPORT_SCHEMA_MIGRATIONS:
        assert Path(path).is_file(), path


def test_every_documented_migration_file_exists():
    root = Path(__file__).resolve().parents[1]
    sources = [root / "README.md"]
    for directory, suffix in (("app", "*.py"), ("scripts", "*.py"), ("docs", "*.md")):
        sources.extend((root / directory).glob(suffix))
    references = {
        match
        for source in sources
        for match in re.findall(
            r"migrations/\d{8}_[a-z0-9_]+\.sql",
            source.read_text(encoding="utf-8"),
        )
    }
    assert references
    for path in sorted(references):
        assert (root / path).is_file(), path


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


def test_support_schema_guard_identifies_delivery_status_migration():
    columns = {"support_ticket_messages": {"id"}}
    assert "migrations/20261020_message_delivery_status.sql" in _missing_support_schema_migrations(columns)


def test_support_schema_guard_identifies_delivery_ownership_migration():
    columns = {}
    for path, requirements in _SUPPORT_SCHEMA_MIGRATIONS:
        if "20261021" not in path:
            for table, required in requirements.items():
                columns.setdefault(table, set()).update(required)
    assert _missing_support_schema_migrations(columns) == ["migrations/20261021_outbound_delivery_ownership.sql"]


def test_support_schema_guard_requires_statistics_migration():
    columns = {}
    for path, tables in _SUPPORT_SCHEMA_MIGRATIONS:
        if "20261023_agent_statistics" not in path:
            for table, required in tables.items():
                columns.setdefault(table, set()).update(required)
    assert _missing_support_schema_migrations(columns) == ["migrations/20261023_agent_statistics.sql"]
