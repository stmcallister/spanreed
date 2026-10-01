"""Tests for bootstrapping an empty database and exporting a full one.

The export is the only thing standing between a lost volume and retyping every
relevance hint by hand, so most of what is below is about it round tripping.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from newsbrief import seed
from newsbrief.models import slugify
from tests.conftest import add_source, add_topic

SEED = """
topics:
  - name: Seattle Sounders
    kind: team
    priority: 70
    max_items: 4
    relevance_hint: |
      Results and transfers.

      Not the academy.
    sources:
      - type: rss
        config:
          url: https://sounderatheart.com/rss/index.xml
      - type: reddit
        config:
          subreddit: r/SoundersFC
  - name: Rust
    kind: tech
    relevance_hint: Compiler and toolchain releases.
    sources: []
"""


def write(tmp_path: Path, text: str, name: str = "topics.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def topics_in(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM topics ORDER BY name"))


def sources_in(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT s.*, t.name AS topic_name FROM sources s JOIN topics t ON t.id = s.topic_id"
            " ORDER BY t.name, s.type"
        )
    )


# --- slugs ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Seattle Sounders", "seattle-sounders"),
        ("  Rust  ", "rust"),
        ("usa.1 / MLS", "usa-1-mls"),
        ("!!!", "topic"),
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert slugify(name) == expected


# --- loading ----------------------------------------------------------------


def test_a_valid_file_loads(tmp_path: Path) -> None:
    loaded = seed.load(write(tmp_path, SEED))

    assert loaded is not None
    assert [t.name for t in loaded.topics] == ["Seattle Sounders", "Rust"]


def test_a_missing_file_is_normal(tmp_path: Path) -> None:
    """The usual case for a database that has already been configured."""
    assert seed.load(tmp_path / "absent.yaml") is None


def test_an_empty_file_is_no_topics_rather_than_an_error(tmp_path: Path) -> None:
    loaded = seed.load(write(tmp_path, ""))

    assert loaded is not None
    assert loaded.topics == []


def test_malformed_yaml_is_reported_and_ignored(tmp_path: Path) -> None:
    assert seed.load(write(tmp_path, "topics: [oops\n  - broken")) is None


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    """A typo should be loud rather than a setting that silently does nothing."""
    assert seed.load(write(tmp_path, "topics:\n  - name: X\n    kinde: tech\n")) is None


def test_a_bad_kind_is_refused(tmp_path: Path) -> None:
    text = "topics:\n  - name: X\n    kind: weather\n    relevance_hint: h\n"

    assert seed.load(write(tmp_path, text)) is None


def test_an_empty_relevance_hint_is_refused(tmp_path: Path) -> None:
    text = "topics:\n  - name: X\n    kind: tech\n    relevance_hint: '   '\n"

    assert seed.load(write(tmp_path, text)) is None


def test_the_shipped_seed_file_is_valid() -> None:
    """The one in the repo bootstraps a real database, so it has to parse."""
    loaded = seed.load(Path("seed/topics.yaml"))

    assert loaded is not None
    assert loaded.topics


# --- bootstrapping ----------------------------------------------------------


def test_an_empty_database_is_seeded(conn: sqlite3.Connection, tmp_path: Path) -> None:
    created = seed.bootstrap(conn, write(tmp_path, SEED))

    assert created == 2
    rows = topics_in(conn)
    assert [r["name"] for r in rows] == ["Rust", "Seattle Sounders"]
    sounders = rows[1]
    assert sounders["slug"] == "seattle-sounders"
    assert sounders["priority"] == 70
    assert sounders["max_items"] == 4
    assert "Not the academy." in sounders["relevance_hint"]


def test_sources_are_created_with_their_topic(conn: sqlite3.Connection, tmp_path: Path) -> None:
    seed.bootstrap(conn, write(tmp_path, SEED))

    rows = sources_in(conn)
    assert [(r["topic_name"], r["type"]) for r in rows] == [
        ("Seattle Sounders", "reddit"),
        ("Seattle Sounders", "rss"),
    ]


def test_seeded_configs_are_normalized(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """The editor stores what validation produced, so this must too. A seed file
    that stored raw text would put the database in a state the UI cannot make."""
    seed.bootstrap(conn, write(tmp_path, SEED))

    reddit = next(r for r in sources_in(conn) if r["type"] == "reddit")
    assert json.loads(reddit["config"]) == {
        "subreddit": "SoundersFC",
        "listing": "top",
        "window": "day",
    }


def test_a_database_with_topics_is_left_alone(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Once the UI writes a topic the YAML is stale and irrelevant."""
    add_topic(conn, "Already Here")

    assert seed.bootstrap(conn, write(tmp_path, SEED)) == 0
    assert [r["name"] for r in topics_in(conn)] == ["Already Here"]


def test_seeding_twice_creates_nothing_the_second_time(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Three workloads start at once, and the check is inside the transaction."""
    path = write(tmp_path, SEED)

    assert seed.bootstrap(conn, path) == 2
    assert seed.bootstrap(conn, path) == 0
    assert len(topics_in(conn)) == 2


def test_a_missing_seed_file_creates_nothing(conn: sqlite3.Connection, tmp_path: Path) -> None:
    assert seed.bootstrap(conn, tmp_path / "absent.yaml") == 0


def test_one_bad_source_does_not_cost_its_topic(conn: sqlite3.Connection, tmp_path: Path) -> None:
    text = """
topics:
  - name: X
    kind: tech
    relevance_hint: h
    sources:
      - type: carrier_pigeon
        config: {}
      - type: reddit
        config:
          subreddit: has spaces
      - type: rss
        config:
          url: https://good.example/feed
"""
    assert seed.bootstrap(conn, write(tmp_path, text)) == 1
    assert [r["type"] for r in sources_in(conn)] == ["rss"]


def test_a_disabled_topic_is_seeded_disabled(conn: sqlite3.Connection, tmp_path: Path) -> None:
    text = "topics:\n  - name: X\n    kind: tech\n    relevance_hint: h\n    enabled: false\n"
    seed.bootstrap(conn, write(tmp_path, text))

    assert topics_in(conn)[0]["enabled"] == 0


def test_an_explicit_slug_is_honoured(conn: sqlite3.Connection, tmp_path: Path) -> None:
    text = "topics:\n  - name: X Y\n    slug: custom\n    kind: tech\n    relevance_hint: h\n"
    seed.bootstrap(conn, write(tmp_path, text))

    assert topics_in(conn)[0]["slug"] == "custom"


# --- exporting --------------------------------------------------------------


def test_export_produces_readable_yaml(conn: sqlite3.Connection, tmp_path: Path) -> None:
    seed.bootstrap(conn, write(tmp_path, SEED))

    text = seed.export(conn)
    parsed = yaml.safe_load(text)

    assert [t["name"] for t in parsed["topics"]] == ["Rust", "Seattle Sounders"]


def test_a_multiline_hint_is_exported_as_a_block(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Folded into one escaped line it is unreadable, and unreadable
    configuration does not get corrected."""
    seed.bootstrap(conn, write(tmp_path, SEED))

    assert "relevance_hint: |" in seed.export(conn)


def test_cache_bookkeeping_is_not_exported(conn: sqlite3.Connection) -> None:
    """An ETag is worth nothing to a database being restored from this file."""
    topic_id = add_topic(conn)
    add_source(
        conn,
        topic_id,
        "rss",
        {"url": "https://a.example/feed", "etag": 'W/"abc"', "last_modified": "Mon"},
    )

    text = seed.export(conn)

    assert "etag" not in text
    assert "last_modified" not in text
    assert "https://a.example/feed" in text


def test_a_disabled_source_is_marked(conn: sqlite3.Connection) -> None:
    topic_id = add_topic(conn)
    add_source(conn, topic_id, "rss", {"url": "https://a.example/feed"}, enabled=False)

    parsed = yaml.safe_load(seed.export(conn))

    assert parsed["topics"][0]["sources"][0]["enabled"] is False


def test_an_enabled_source_says_nothing_about_it(conn: sqlite3.Connection) -> None:
    """The common case stays quiet, so the file reads as configuration."""
    topic_id = add_topic(conn)
    add_source(conn, topic_id, "rss", {"url": "https://a.example/feed"})

    parsed = yaml.safe_load(seed.export(conn))

    assert "enabled" not in parsed["topics"][0]["sources"][0]


def test_an_empty_database_exports_an_empty_document(conn: sqlite3.Connection) -> None:
    parsed = yaml.safe_load(seed.export(conn))

    assert parsed == {"topics": []}


def test_the_export_carries_a_header_saying_what_it_is(conn: sqlite3.Connection) -> None:
    assert seed.export(conn).startswith("# Exported by")


# --- the round trip ---------------------------------------------------------


def configuration(conn: sqlite3.Connection) -> tuple[list[Any], list[Any]]:
    """Everything a restore has to reproduce. Runtime state is not part of it."""
    topics = [
        (
            r["name"],
            r["slug"],
            r["kind"],
            r["enabled"],
            r["relevance_hint"],
            r["max_items"],
            r["priority"],
        )
        for r in topics_in(conn)
    ]
    sources = [
        (r["topic_name"], r["type"], r["enabled"], json.loads(r["config"]))
        for r in sources_in(conn)
    ]
    return topics, sources


def test_export_round_trips_into_a_fresh_database(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """This is the whole point: a lost volume must be recoverable from the file."""
    from newsbrief import db

    seed.bootstrap(conn, write(tmp_path, SEED))
    # The collector has been running, so the sources carry cache state now.
    conn.execute(
        "UPDATE sources SET config = ?, last_status = 'ok', consecutive_failures = 3"
        " WHERE type = 'rss'",
        (json.dumps({"url": "https://sounderatheart.com/rss/index.xml", "etag": 'W/"x"'}),),
    )
    before = configuration(conn)

    exported = write(tmp_path, seed.export(conn), "exported.yaml")

    restored_path = tmp_path / "restored.db"
    restored = db.connect(restored_path)
    try:
        db.migrate(restored)
        assert seed.bootstrap(restored, exported) == 2
        after = configuration(restored)
    finally:
        restored.close()

    assert after[0] == before[0]
    # The restored source has the configuration and none of the cache state.
    assert [(name, kind, enabled) for name, kind, enabled, _ in after[1]] == [
        (name, kind, enabled) for name, kind, enabled, _ in before[1]
    ]
    rss_config = next(config for _, kind, _, config in after[1] if kind == "rss")
    assert rss_config == {"url": "https://sounderatheart.com/rss/index.xml"}


def test_exporting_twice_produces_the_same_file(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A file that churns between exports is a file nobody can diff."""
    seed.bootstrap(conn, write(tmp_path, SEED))

    assert seed.export(conn) == seed.export(conn)
