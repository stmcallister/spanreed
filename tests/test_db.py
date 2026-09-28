"""Smoke tests for the connection pragmas, the migration runner, and timestamps."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from newsbrief import db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "nested" / "newsbrief.db"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("2026-08-30T17:30:00Z", datetime(2026, 8, 30, 17, 30, tzinfo=UTC)),
        ("2026-08-30 17:30:00", datetime(2026, 8, 30, 17, 30, tzinfo=UTC)),
        ("2026-08-30T17:30:00+00:00", datetime(2026, 8, 30, 17, 30, tzinfo=UTC)),
        ("not a timestamp", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_timestamp_reads_both_stored_formats(
    stored: str | None, expected: datetime | None
) -> None:
    """Our own writes end in Z, the column defaults use a space."""
    assert db.parse_timestamp(stored) == expected


def test_to_iso_utc_converts_from_another_zone() -> None:
    """Storage is always UTC. Pacific time only exists at render time."""
    pacific = datetime(2026, 8, 30, 10, 30, tzinfo=timezone(timedelta(hours=-7)))
    assert db.to_iso_utc(pacific) == "2026-08-30T17:30:00Z"


def test_to_iso_utc_treats_a_naive_datetime_as_utc() -> None:
    assert db.to_iso_utc(datetime(2026, 8, 30, 17, 30)) == "2026-08-30T17:30:00Z"


def test_connect_applies_required_pragmas(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_connect_creates_the_parent_directory(db_path: Path) -> None:
    conn = db.connect(db_path)
    conn.close()
    assert db_path.exists()


def test_migrate_reaches_the_expected_schema_version(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        expected = max(version for version, _ in db.discover_migrations())
        assert db.migrate(conn) == expected
        assert db.current_version(conn) == expected
    finally:
        conn.close()


def test_migrate_creates_every_table_and_index(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
        names = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "schema_version",
            "topics",
            "sources",
            "items",
            "briefs",
            "brief_items",
            "fetch_log",
        } <= names

        indexes = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            "idx_items_topic_published",
            "idx_items_fetched",
            "idx_fetch_log_source",
        } <= indexes
    finally:
        conn.close()


def test_migrate_is_idempotent(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        first = db.migrate(conn)
        second = db.migrate(conn)
        assert first == second
        rows = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
        assert rows == len(db.discover_migrations())
    finally:
        conn.close()


def test_concurrent_starts_apply_each_migration_once(db_path: Path) -> None:
    """All three entrypoints run migrations at start. Only one may apply them."""
    first = db.connect(db_path)
    second = db.connect(db_path)
    try:
        assert db.migrate(first) == db.migrate(second)
        versions = [row["version"] for row in second.execute("SELECT version FROM schema_version")]
        assert sorted(versions) == sorted({*versions})
    finally:
        first.close()
        second.close()


def test_foreign_keys_are_enforced(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sources (topic_id, type, config) VALUES (?, ?, ?)",
                (999, "rss", "{}"),
            )
    finally:
        conn.close()


def test_content_hash_is_unique_per_topic(db_path: Path) -> None:
    """The UNIQUE constraint is the defence against reinserting on every run."""
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO topics (id, name, slug, kind, relevance_hint)"
                " VALUES (1, 'Test', 'test', 'tech', 'anything')"
            )
            conn.execute(
                "INSERT INTO sources (id, topic_id, type, config) VALUES (1, 1, 'rss', '{}')"
            )

        insert = (
            "INSERT INTO items (source_id, topic_id, url, title, title_norm, content_hash)"
            " VALUES (1, 1, ?, ?, ?, ?)"
        )
        conn.execute(insert, ("https://a.example/1", "A", "a", "hash-1"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("https://b.example/2", "A", "a", "hash-1"))
    finally:
        conn.close()


def test_write_transaction_rolls_back_on_error(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
        with pytest.raises(RuntimeError), db.write_transaction(conn):
            conn.execute(
                "INSERT INTO topics (name, slug, kind, relevance_hint)"
                " VALUES ('Doomed', 'doomed', 'tech', 'x')"
            )
            raise RuntimeError("boom")

        assert conn.execute("SELECT COUNT(*) AS n FROM topics").fetchone()["n"] == 0
    finally:
        conn.close()


def test_topic_kind_is_constrained(db_path: Path) -> None:
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO topics (name, slug, kind, relevance_hint)"
                " VALUES ('Bad', 'bad', 'weather', 'x')"
            )
    finally:
        conn.close()


def test_split_statements_keeps_semicolons_inside_literals() -> None:
    sql = "INSERT INTO t (v) VALUES ('a;b');\nSELECT 1;\n"
    assert list(db._split_statements(sql)) == ["INSERT INTO t (v) VALUES ('a;b');", "SELECT 1;"]


def test_split_statements_rejects_a_truncated_file() -> None:
    with pytest.raises(db.MigrationError):
        list(db._split_statements("CREATE TABLE t (\n"))


def test_discover_migrations_rejects_a_bad_filename(tmp_path: Path) -> None:
    (tmp_path / "initial.sql").write_text("SELECT 1;")
    with pytest.raises(db.MigrationError):
        db.discover_migrations(tmp_path)
