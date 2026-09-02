"""Shared test fixtures. No test in this suite touches the network."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from newsbrief import db
from newsbrief.config import Settings
from newsbrief.sources.base import build_client

FIXTURES = Path(__file__).parent / "fixtures"

# The recorded feeds carry dates around here, so tests anchor to a fixed clock
# rather than to the wall clock.
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "newsbrief.db", anthropic_api_key=None)


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    connection = db.connect(settings.db_path)
    db.migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(settings: Settings) -> Iterator[httpx.Client]:
    with build_client(settings) as http_client:
        yield http_client


def add_topic(
    conn: sqlite3.Connection,
    name: str = "Midlakes United",
    *,
    slug: str | None = None,
    kind: str = "team",
    enabled: bool = True,
) -> int:
    cursor = conn.execute(
        "INSERT INTO topics (name, slug, kind, enabled, relevance_hint)"
        " VALUES (?, ?, ?, ?, 'anything about the club')",
        (name, slug or name.lower().replace(" ", "-"), kind, int(enabled)),
    )
    return int(cursor.lastrowid or 0)


def add_source(
    conn: sqlite3.Connection,
    topic_id: int,
    source_type: str = "rss",
    config: dict[str, object] | None = None,
    *,
    enabled: bool = True,
    consecutive_failures: int = 0,
    last_fetch_at: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO sources (topic_id, type, config, enabled, consecutive_failures, last_fetch_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            topic_id,
            source_type,
            json.dumps(config or {"url": "https://arstechnica.com/feed/"}),
            int(enabled),
            consecutive_failures,
            last_fetch_at,
        ),
    )
    return int(cursor.lastrowid or 0)
