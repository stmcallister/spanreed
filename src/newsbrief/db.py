"""SQLite connection handling, pragmas, and the migration runner.

Three workloads share one database file on one PVC. WAL plus a busy timeout is
what makes that safe, so `connect` is the only supported way to open the
database and it always applies the same pragmas.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
)

SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_MIGRATION_NAME = re.compile(r"^(\d+)_[\w.]+\.sql$")


class MigrationError(RuntimeError):
    """Raised when the migrations directory is not usable."""


def to_iso_utc(value: datetime) -> str:
    """Every timestamp in the database is an ISO 8601 UTC string.

    Conversion to America/Los_Angeles happens at render time and nowhere else.
    """
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: str | None) -> datetime | None:
    """Read a stored timestamp, tolerating the SQLite `datetime('now')` format.

    Column defaults produce `YYYY-MM-DD HH:MM:SS` while our own writes produce
    `...Z`, so both have to be readable.
    """
    if not value:
        return None
    text = value.strip().replace(" ", "T")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the database with the pragmas every connection must have.

    The connection is left in autocommit mode. Writes go through
    `write_transaction` so that the transaction boundary is always explicit and
    always short.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction, taking the write lock up front.

    BEGIN IMMEDIATE means a competing writer waits on the busy timeout here
    rather than failing partway through with a deferred lock upgrade.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _split_statements(sql: str) -> Iterator[str]:
    """Split a migration file into complete statements.

    `sqlite3.complete_statement` understands semicolons inside string literals,
    which naive splitting does not.
    """
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    trailing = buffer.strip()
    if trailing:
        raise MigrationError(f"migration ends with an incomplete statement: {trailing[:80]!r}")


def discover_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """Return every migration as (version, path), ordered by version."""
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")

    found: dict[int, Path] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise MigrationError(f"migration filename must be NNN_name.sql: {path.name}")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version}: {found[version].name} and {path.name}"
            )
        found[version] = path
    return sorted(found.items())


def current_version(conn: sqlite3.Connection) -> int:
    """Highest applied migration version, or 0 on an empty database."""
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_version").fetchone()
    return int(row["version"])


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> int:
    """Apply every pending migration and return the resulting schema version.

    Safe to call from all three entrypoints at once. The whole run happens
    inside one immediate transaction, so a second process either waits and then
    finds nothing to do, or waits and applies the rest.
    """
    migrations = discover_migrations(migrations_dir)
    conn.execute(SCHEMA_VERSION_DDL)

    with write_transaction(conn):
        applied = {
            int(row["version"]) for row in conn.execute("SELECT version FROM schema_version")
        }
        for version, path in migrations:
            if version in applied:
                continue
            for statement in _split_statements(path.read_text(encoding="utf-8")):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                (version, path.name),
            )
            log.info("migration applied", version=version, migration=path.name)

    version_now = current_version(conn)
    log.info("schema ready", schema_version=version_now)
    return version_now
