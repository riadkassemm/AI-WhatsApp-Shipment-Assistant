"""Clone the app's tables into a NEW local database; never write to the source.

Run on the destination host using its root MariaDB socket and the existing app
environment for source credentials. See docs/local-database-migration.md.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

import pymysql


APP_TABLES = (
    "ai_control_settings", "ai_control_settings_audit", "staff", "staff_sessions",
    "support_contacts", "support_google_accounts", "support_google_contacts",
    "support_message_change_events", "support_message_change_operations",
    "support_message_reactions", "support_message_user_deletions", "support_quick_replies",
    "support_ticket_events", "support_ticket_messages", "support_tickets",
    "support_vcf_contacts", "whatsapp_business_accounts", "whatsapp_integration_audit",
    "whatsapp_phone_numbers", "users", "user_wallet", "orders_new", "shipments",
)
CATALOG_TABLES = ("destinations", "shipping_rates")


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError("Unsafe SQL identifier")
    return f"`{value}`"


def qualified(schema: str, table: str) -> str:
    return f"{identifier(schema)}.{identifier(table)}"


def fetch(connection, sql, args=()):
    with connection.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(sql, args)
        return cursor.fetchall()


def execute(connection, sql, args=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, args)


class Fingerprint:
    """Order-independent multiset digest, including duplicate rows and types."""

    def __init__(self):
        self.count = 0
        self.total = 0

    def add(self, row):
        digest = hashlib.sha512()
        for value in row:
            payload = value if isinstance(value, bytes) else str(value).encode("utf-8")
            tag = type(value).__name__.encode("ascii")
            digest.update(len(tag).to_bytes(4, "big") + tag)
            digest.update(len(payload).to_bytes(8, "big") + payload)
        self.count += 1
        self.total = (self.total + int.from_bytes(digest.digest(), "big")) % (1 << 512)

    def result(self):
        return {"rows": self.count, "sha512_sum": f"{self.total:0128x}"}


def inspect_tables(source, roots):
    """Follow outgoing FKs only; unrelated referencing tables are not needed."""
    plans = {}
    pending = list(roots)
    while pending:
        schema, table = pending.pop(0)
        if (schema, table) in plans:
            continue
        name = qualified(schema, table)
        meta = fetch(source, """SELECT ENGINE, TABLE_TYPE FROM information_schema.TABLES
                             WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s""", (schema, table))
        if not meta or meta[0]["ENGINE"] != "InnoDB" or meta[0]["TABLE_TYPE"] != "BASE TABLE":
            raise ValueError(f"Requires an InnoDB base table: {name}")
        # Trigger bodies/routines can have dependencies not represented by FKs.
        # This repository's selected tables have none. Refuse to silently omit any.
        if fetch(source, """SELECT TRIGGER_NAME FROM information_schema.TRIGGERS
                           WHERE EVENT_OBJECT_SCHEMA=%s AND EVENT_OBJECT_TABLE=%s""", (schema, table)):
            raise ValueError(f"Review trigger dependencies before copying {name}")
        columns = fetch(source, """SELECT COLUMN_NAME, EXTRA FROM information_schema.COLUMNS
                                 WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION""",
                        (schema, table))
        keys = fetch(source, """SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_SCHEMA,
                              REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                              FROM information_schema.KEY_COLUMN_USAGE
                              WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
                              AND REFERENCED_TABLE_NAME IS NOT NULL
                              ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION""", (schema, table))
        ddl = list(fetch(source, f"SHOW CREATE TABLE {name}")[0].values())[1]
        plans[schema, table] = {"columns": columns, "keys": keys, "ddl": ddl}
        pending.extend((key["REFERENCED_TABLE_SCHEMA"], key["REFERENCED_TABLE_NAME"]) for key in keys)
    names = [table for _, table in plans]
    if len(names) != len(set(names)):
        raise ValueError("Source schemas contain colliding table names")
    return plans


def remap_ddl(ddl, plans, target):
    # Only rewrite qualified REFERENCES clauses, never defaults/comments/data.
    def replace(match):
        schema, table = match.groups()
        if (schema, table) not in plans:
            raise ValueError("Foreign-key dependency missing from plan")
        return "REFERENCES " + qualified(target, table)
    return re.sub(r"REFERENCES\s+`([^`]+)`\.`([^`]+)`", replace, ddl)


def check_foreign_keys(destination, plans, target):
    checked = 0
    for (_, table), plan in plans.items():
        constraints = defaultdict(list)
        for key in plan["keys"]:
            constraints[key["CONSTRAINT_NAME"]].append(key)
        for name, keys in constraints.items():
            parent = keys[0]["REFERENCED_TABLE_NAME"]
            nonnull = " AND ".join(f"c.{identifier(k['COLUMN_NAME'])} IS NOT NULL" for k in keys)
            equal = " AND ".join(
                f"p.{identifier(k['REFERENCED_COLUMN_NAME'])}=c.{identifier(k['COLUMN_NAME'])}" for k in keys)
            orphan = fetch(destination, f"""SELECT 1 FROM {qualified(target, table)} c
                          WHERE {nonnull} AND NOT EXISTS
                          (SELECT 1 FROM {qualified(target, parent)} p WHERE {equal}) LIMIT 1""")
            if orphan:
                raise ValueError(f"Foreign-key validation failed: {table}.{name}")
            checked += 1
    return checked


def clone(source, destination, roots, target, *, batch_size=500, pause=0.02):
    """Connections must be separate and dedicated to this operation.

    Failure leaves a partial destination for inspection, never an automatic drop
    or overwrite. Do not configure an application to use it until verified.
    """
    identifier(target)
    if batch_size < 1 or pause < 0:
        raise ValueError("Invalid batch settings")
    if fetch(destination, "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (target,)):
        raise ValueError("Destination database already exists; refusing to overwrite")
    if target in {schema for schema, _ in roots}:
        raise ValueError("Destination must have a distinct database name")
    execute(source, "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    execute(source, "SET SESSION TRANSACTION READ ONLY")
    execute(source, "SET SESSION time_zone='+00:00'")
    execute(source, "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
    try:
        plans = inspect_tables(source, roots)
        if target in {schema for schema, _ in plans}:
            raise ValueError("Destination collides with a dependency schema")
        mode = fetch(source, "SELECT @@SESSION.sql_mode AS mode")[0]["mode"]
        execute(destination, "SET SESSION sql_mode=%s", (mode + ",NO_AUTO_VALUE_ON_ZERO",))
        execute(destination, "SET SESSION time_zone='+00:00'")
        execute(destination, f"CREATE DATABASE {identifier(target)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        execute(destination, f"USE {identifier(target)}")
        execute(destination, "SET SESSION foreign_key_checks=0")
        for plan in plans.values():
            execute(destination, remap_ddl(plan["ddl"], plans, target))
        report = {"target": target, "tables": {}, "verified": False}
        for (schema, table), plan in plans.items():
            fingerprint = Fingerprint()
            names = [column["COLUMN_NAME"] for column in plan["columns"]]
            writable = [i for i, column in enumerate(plan["columns"])
                        if not any(kind in column["EXTRA"] for kind in ("VIRTUAL GENERATED", "STORED GENERATED"))]
            insert = (f"INSERT INTO {qualified(target, table)} ("
                      + ",".join(identifier(names[i]) for i in writable) + ") VALUES ("
                      + ",".join(["%s"] * len(writable)) + ")")
            with source.cursor(pymysql.cursors.SSCursor) as reader, destination.cursor() as writer:
                reader.execute("SELECT " + ",".join(map(identifier, names)) + " FROM " + qualified(schema, table))
                while True:
                    rows = reader.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        fingerprint.add(row)
                    writer.executemany(insert, [tuple(row[i] for i in writable) for row in rows])
                    destination.commit()
                    if pause:
                        time.sleep(pause)
            report["tables"][f"{schema}.{table}"] = fingerprint.result()
            print(f"Copied {schema}.{table}: {fingerprint.count} rows", flush=True)
    finally:
        source.rollback()  # End the snapshot as soon as streaming finishes.
    # Full scans/checks happen on the destination, without re-reading the source.
    for (schema, table), plan in plans.items():
        fingerprint = Fingerprint()
        columns = ",".join(identifier(c["COLUMN_NAME"]) for c in plan["columns"])
        with destination.cursor(pymysql.cursors.SSCursor) as reader:
            reader.execute(f"SELECT {columns} FROM {qualified(target, table)}")
            for row in reader:
                fingerprint.add(row)
        if fingerprint.result() != report["tables"][f"{schema}.{table}"]:
            raise ValueError(f"Data verification failed: {table}")
        actual = list(fetch(destination, f"SHOW CREATE TABLE {qualified(target, table)}")[0].values())[1]
        expected = remap_ddl(plan["ddl"], plans, target)
        # MariaDB prints a same-schema FK unqualified after consolidation.
        expected = expected.replace("REFERENCES " + identifier(target) + ".", "REFERENCES ")
        if actual != expected:
            raise ValueError(f"Schema verification failed: {table}")
    report["foreign_keys_verified"] = check_foreign_keys(destination, plans, target)
    execute(destination, "SET SESSION foreign_key_checks=1")
    report["verified"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-host", required=True, help="Direct database address; never an SSH forward")
    parser.add_argument("--source-port", type=int, default=3306)
    parser.add_argument("--target", required=True)
    parser.add_argument("--destination-socket", default="/run/mysqld/mysqld.sock")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.config import settings

    # Exclusive private output; never dump customer records/credentials to disk.
    with os.fdopen(os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        with pymysql.connect(host=args.source_host, port=args.source_port,
                             user=settings.db_user, password=settings.db_password,
                             charset="utf8mb4", connect_timeout=10, read_timeout=120,
                             autocommit=True) as source:
            # Without TRIGGER visibility an empty trigger query is not proof.
            grants = [str(next(iter(row.values()))) for row in fetch(source, "SHOW GRANTS")]
            # The app owns/writes its main schema. The shipping catalog is a
            # SELECT-only reference snapshot; its source grants cannot expose
            # triggers. Keep these two tables SELECT-only for the new app user.
            schemas = {settings.db_name}
            for schema in schemas:
                if not any(re.search(r"^GRANT (?:[^\n]*\bTRIGGER\b|ALL PRIVILEGES)[^\n]* ON "
                                     + pattern + r"\.\* TO ", grant)
                           for grant in grants
                           for pattern in (r"\*", re.escape(identifier(schema)))):
                    raise ValueError("Source credentials require trigger metadata visibility")
            with pymysql.connect(unix_socket=args.destination_socket, user="root", charset="utf8mb4",
                                 autocommit=True) as destination:
                roots = [(settings.db_name, table) for table in APP_TABLES]
                roots += [(settings.shipping_db_schema, table) for table in CATALOG_TABLES]
                report = clone(source, destination, roots, args.target)
                report["catalog_policy"] = "SELECT-only reference snapshot; source trigger metadata unavailable"
                json.dump(report, output, indent=2)
                output.write("\n")
    print("Snapshot data, table definitions, and foreign keys verified.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Driver errors can contain data values or credentials. Keep diagnostics private.
        print(f"Clone failed ({type(exc).__name__}); do not activate the destination.", file=sys.stderr)
        sys.exit(1)
