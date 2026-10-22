"""Selective snapshot copying against disposable MariaDB; no production access."""
import os
import uuid
from decimal import Decimal

import pymysql
import pytest

from scripts import clone_support_database as migration


@pytest.fixture
def databases():
    if not os.environ.get("SUPPORT_INTEGRATION"):
        pytest.skip("Requires disposable MariaDB")
    options = dict(user=os.environ.get("TEST_DB_USER", "root"),
                   password=os.environ.get("TEST_DB_PASSWORD", ""),
                   charset="utf8mb4", autocommit=True)
    if os.environ.get("TEST_DB_SOCKET"):
        options["unix_socket"] = os.environ["TEST_DB_SOCKET"]
    else:
        options.update(host=os.environ.get("TEST_DB_HOST", "127.0.0.1"),
                       port=int(os.environ.get("TEST_DB_PORT", "3306")))
    names = ["clone_test_" + uuid.uuid4().hex for _ in range(3)]
    with pymysql.connect(**options) as source, pymysql.connect(**options) as destination:
        for name in names[:2]:
            migration.execute(destination, f"CREATE DATABASE `{name}`")
        try:
            yield source, destination, names
        finally:
            source.rollback()
            migration.execute(destination, "SET foreign_key_checks=0")
            for name in names:
                migration.execute(destination, f"DROP DATABASE IF EXISTS `{name}`")


def test_consistent_selective_snapshot_with_dependencies_and_exact_data(databases, monkeypatch):
    source, destination, (main, catalog, target) = databases
    run = lambda sql, args=(): migration.execute(destination, sql, args)
    run(f"CREATE TABLE `{catalog}`.parent (id INT PRIMARY KEY) ENGINE=InnoDB")
    run(f"INSERT INTO `{catalog}`.parent VALUES (1)")
    run(f"""CREATE TABLE `{main}`.messages (
        id INT PRIMARY KEY AUTO_INCREMENT, parent_id INT, amount DECIMAL(10,3),
        body TEXT, attachment BLOB, sent_at TIMESTAMP(6) NULL, absent VARCHAR(4),
        calculated DECIMAL(10,3) AS (amount*2) STORED,
        CONSTRAINT fk_parent FOREIGN KEY (parent_id) REFERENCES `{catalog}`.parent(id),
        INDEX ix_amount (amount), CHECK (amount >= 0)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci AUTO_INCREMENT=9""")
    run(f"INSERT INTO `{main}`.messages (id,parent_id,amount,body,attachment,sent_at) VALUES (1,1,%s,%s,%s,%s)",
        (Decimal("1.230"), "مرحبا '\\ \n🙂", b"\x00\xff\x01", "2026-10-22 12:34:56.123456"))
    run(f"CREATE TABLE `{main}`.duplicates (n INT, value VARCHAR(4)) ENGINE=InnoDB")
    run(f"INSERT INTO `{main}`.duplicates VALUES (0,NULL),(0,NULL),(1,'None')")
    run(f"CREATE TABLE `{main}`.unrelated (id INT) ENGINE=InnoDB")
    original = migration.inspect_tables

    def change_after_snapshot(connection, roots):
        plans = original(connection, roots)
        # Deterministically commit a source change after snapshot establishment.
        run(f"UPDATE `{main}`.messages SET body='later' WHERE id=1")
        run(f"INSERT INTO `{catalog}`.parent VALUES (2)")
        with pytest.raises(pymysql.err.OperationalError) as failure:
            migration.execute(source, f"UPDATE `{main}`.messages SET body='forbidden'")
        assert failure.value.args[0] == 1792
        return plans

    monkeypatch.setattr(migration, "inspect_tables", change_after_snapshot)
    report = migration.clone(source, destination, [(main, "messages"), (main, "duplicates")],
                             target, batch_size=1, pause=0)
    assert report["verified"] is True
    assert report["foreign_keys_verified"] == 1
    assert report["tables"][f"{catalog}.parent"]["rows"] == 1
    assert report["tables"][f"{main}.duplicates"]["rows"] == 3
    copied = migration.fetch(destination, f"SELECT * FROM `{target}`.messages")[0]
    assert copied["body"] == "مرحبا '\\ \n🙂"
    assert copied["attachment"] == b"\x00\xff\x01"
    assert copied["calculated"] == Decimal("2.460")
    assert migration.fetch(destination, f"SELECT body FROM `{main}`.messages")[0]["body"] == "later"
    tables = migration.fetch(destination, "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s", (target,))
    assert {row["TABLE_NAME"] for row in tables} == {"parent", "messages", "duplicates"}
    with pytest.raises(pymysql.err.IntegrityError):
        run(f"INSERT INTO `{target}`.messages (parent_id,amount) VALUES (999,1)")


@pytest.mark.parametrize("kind", ["existing", "nontransactional", "trigger", "orphan"])
def test_refuses_unsafe_or_incomplete_copy(databases, kind):
    source, destination, (main, _, target) = databases
    run = lambda sql: migration.execute(destination, sql)
    engine = "MyISAM" if kind == "nontransactional" else "InnoDB"
    run(f"CREATE TABLE `{main}`.records (id INT PRIMARY KEY) ENGINE={engine}")
    run(f"INSERT INTO `{main}`.records VALUES (1)")
    if kind == "existing":
        run(f"CREATE DATABASE `{target}`")
    elif kind == "trigger":
        run(f"CREATE TRIGGER `{main}`.test_trigger BEFORE INSERT ON `{main}`.records FOR EACH ROW SET NEW.id=NEW.id")
    elif kind == "orphan":
        run(f"CREATE TABLE `{main}`.parent (id INT PRIMARY KEY) ENGINE=InnoDB")
        run("SET foreign_key_checks=0")
        run(f"ALTER TABLE `{main}`.records ADD FOREIGN KEY (id) REFERENCES `{main}`.parent(id)")
        run("SET foreign_key_checks=1")
    with pytest.raises(ValueError):
        migration.clone(source, destination, [(main, "records")], target, pause=0)
    assert migration.fetch(destination, f"SELECT * FROM `{main}`.records") == [{"id": 1}]


def test_fingerprint_preserves_duplicates_and_types():
    first, second, duplicate = [migration.Fingerprint() for _ in range(3)]
    for row in [(None, b"a"), ("None", "a")]:
        first.add(row)
    for row in [("None", "a"), (None, b"a")]:
        second.add(row)
        duplicate.add(row)
    duplicate.add((None, b"a"))
    assert first.result() == second.result()
    assert first.result() != duplicate.result()


@pytest.mark.parametrize("change, error", [("data", "Data verification"), ("schema", "Schema verification")])
def test_verification_rejects_destination_drift(databases, monkeypatch, change, error):
    source, destination, (main, _, target) = databases
    migration.execute(destination, f"CREATE TABLE `{main}`.records (id INT PRIMARY KEY, n INT, INDEX ix_n(n)) ENGINE=InnoDB")
    migration.execute(destination, f"INSERT INTO `{main}`.records VALUES (1,10)")
    rollback = source.rollback

    def after_copy():
        rollback()
        if change == "data":
            migration.execute(destination, f"UPDATE `{target}`.records SET n=11")
        else:
            migration.execute(destination, f"ALTER TABLE `{target}`.records DROP INDEX ix_n")

    monkeypatch.setattr(source, "rollback", after_copy)
    with pytest.raises(ValueError, match=error):
        migration.clone(source, destination, [(main, "records")], target, pause=0)
    monkeypatch.setattr(source, "rollback", rollback)
