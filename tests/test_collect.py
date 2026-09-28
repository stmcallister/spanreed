"""Tests for normalization and for the collector loop."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from newsbrief import collect
from newsbrief.config import Settings
from newsbrief.models import RawItem
from tests.conftest import add_source, add_topic, fixture_bytes

FEED_URL = "https://arstechnica.com/feed/"
OTHER_FEED_URL = "https://midlakesunited.example/feed.xml"
# Before every date in the recorded feeds, so `since` filtering is out of the way.
LONG_AGO = "2010-01-01T00:00:00Z"
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def mock_feed(url: str = FEED_URL, fixture: str = "rss_basic.xml", **kwargs: object) -> respx.Route:
    return respx.get(url).mock(
        return_value=httpx.Response(200, content=fixture_bytes(fixture), **kwargs)  # type: ignore[arg-type]
    )


def source_row(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    assert row is not None
    return row


# --- normalization ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Rust 1.90 lands - Ars Technica", "rust 1 90 lands"),
        ("Rust 1.90 lands | Ars Technica", "rust 1 90 lands"),
        ("Rust 1.90 lands \u2013 Ars Technica", "rust 1 90 lands"),
        ("Rust 1.90 lands \u2014 Ars Technica", "rust 1 90 lands"),
        ("  Spaced   out   headline  ", "spaced out headline"),
        ("Report: Club signs a goalkeeper", "report club signs a goalkeeper"),
        ("The Verge", "the verge"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert collect.normalize_title(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Rangers 2 - 1 Celtic",
        "Midlakes United 2 - 1 Harbour City",
        "A headline - that carries on well past any plausible publisher name",
    ],
)
def test_attribution_stripping_leaves_real_headlines_alone(raw: str) -> None:
    """A scoreline is not a source credit, and neither is half a sentence."""
    assert collect.strip_attribution(raw) == raw


def test_normalize_title_never_returns_empty() -> None:
    assert collect.normalize_title("!!! ???") == "!!! ???"


def test_content_hash_ignores_a_www_prefix() -> None:
    left = collect.content_hash("same title", "https://www.example.com/a")
    right = collect.content_hash("same title", "https://example.com/b")
    assert left == right


def test_content_hash_separates_two_outlets_running_the_same_headline() -> None:
    """Collapsing those is the clustering stage's job, and it needs to see them."""
    left = collect.content_hash("same title", "https://one.example/a")
    right = collect.content_hash("same title", "https://two.example/a")
    assert left != right


def test_content_hash_prefers_the_canonical_url() -> None:
    resolved = collect.content_hash(
        "t", "https://news.google.com/rss/articles/abc", "https://theathletic.com/x"
    )
    direct = collect.content_hash("t", "https://theathletic.com/x")
    assert resolved == direct


def test_published_at_falls_back_to_fetch_time() -> None:
    item = RawItem(url="https://example.com/a", title="A")
    assert collect._published_iso(item, NOW) == "2026-09-01T12:00:00Z"


def test_a_future_published_date_is_clamped() -> None:
    """Otherwise a feed that claims tomorrow pins itself to the top of the brief."""
    item = RawItem(url="https://example.com/a", title="A", published_at=NOW + timedelta(days=5))
    assert collect._published_iso(item, NOW) == "2026-09-01T12:00:00Z"


def test_a_naive_published_date_is_read_as_utc() -> None:
    item = RawItem(url="https://example.com/a", title="A", published_at=datetime(2026, 8, 30, 9))
    assert collect._published_iso(item, NOW) == "2026-08-30T09:00:00Z"


# --- the collector loop -----------------------------------------------------


@respx.mock
def test_run_stores_items(settings: Settings, conn: sqlite3.Connection) -> None:
    topic = add_topic(conn)
    add_source(conn, topic, "rss", {"url": FEED_URL}, last_fetch_at=LONG_AGO)
    mock_feed()

    assert collect.run(settings) == 0

    rows = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    assert len(rows) == 3
    assert rows[0]["title"] == "Rust 1.90 lands with faster incremental builds - Ars Technica"
    assert rows[0]["title_norm"] == "rust 1 90 lands with faster incremental builds"
    assert rows[0]["source_name"] == "Ars Technica"
    assert rows[0]["topic_id"] == topic
    assert all(row["published_at"] for row in rows)


@respx.mock
def test_a_second_run_inserts_nothing_new(settings: Settings, conn: sqlite3.Connection) -> None:
    """The UNIQUE constraint on content_hash is the defence against this."""
    topic = add_topic(conn)
    add_source(conn, topic, "rss", {"url": FEED_URL}, last_fetch_at=LONG_AGO)
    mock_feed()

    collect.run(settings)
    collect.run(settings)

    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 3


@respx.mock
def test_two_sources_on_one_topic_do_not_duplicate_a_story(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    topic = add_topic(conn)
    add_source(conn, topic, "rss", {"url": FEED_URL}, last_fetch_at=LONG_AGO)
    add_source(conn, topic, "rss", {"url": OTHER_FEED_URL}, last_fetch_at=LONG_AGO)
    mock_feed()
    mock_feed(OTHER_FEED_URL)

    collect.run(settings)

    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 3


@respx.mock
def test_two_topics_each_keep_their_own_copy(settings: Settings, conn: sqlite3.Connection) -> None:
    """The UNIQUE constraint is per topic, so a shared source feeds both briefs."""
    first = add_topic(conn, "Midlakes United")
    second = add_topic(conn, "Rust", kind="tech")
    add_source(conn, first, "rss", {"url": FEED_URL}, last_fetch_at=LONG_AGO)
    add_source(conn, second, "rss", {"url": FEED_URL}, last_fetch_at=LONG_AGO)
    mock_feed()

    collect.run(settings)

    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 6


@respx.mock
def test_a_success_is_recorded_on_the_source(settings: Settings, conn: sqlite3.Connection) -> None:
    topic = add_topic(conn)
    source = add_source(conn, topic, "rss", {"url": FEED_URL}, consecutive_failures=4)
    mock_feed()

    collect.run(settings)

    row = source_row(conn, source)
    assert row["last_status"] == "ok"
    assert row["last_error"] is None
    assert row["consecutive_failures"] == 0
    assert row["last_fetch_at"] is not None

    logged = conn.execute("SELECT * FROM fetch_log WHERE source_id = ?", (source,)).fetchone()
    assert logged["status"] == "ok"
    assert logged["item_count"] >= 1


@respx.mock
def test_one_broken_source_does_not_stop_the_others(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """A dead source degrades the brief. It never empties it."""
    topic = add_topic(conn)
    broken = add_source(conn, topic, "rss", {"url": FEED_URL})
    working = add_source(conn, topic, "rss", {"url": OTHER_FEED_URL}, last_fetch_at=LONG_AGO)
    respx.get(FEED_URL).mock(return_value=httpx.Response(503))
    mock_feed(OTHER_FEED_URL, "atom_basic.xml")

    assert collect.run(settings) == 0

    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 2

    failed = source_row(conn, broken)
    assert failed["last_status"] == "error"
    assert "503" in failed["last_error"]
    assert failed["consecutive_failures"] == 1
    assert failed["last_fetch_at"] is None
    assert failed["enabled"] == 1

    assert source_row(conn, working)["last_status"] == "ok"


@respx.mock
def test_a_transport_error_is_recorded_rather_than_raised(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    topic = add_topic(conn)
    source = add_source(conn, topic, "rss", {"url": FEED_URL})
    respx.get(FEED_URL).mock(side_effect=httpx.ConnectTimeout("dns went away"))

    assert collect.run(settings) == 0
    assert source_row(conn, source)["last_status"] == "error"


@respx.mock
def test_a_source_is_auto_disabled_after_twenty_failures(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    topic = add_topic(conn)
    source = add_source(
        conn,
        topic,
        "rss",
        {"url": FEED_URL},
        consecutive_failures=collect.MAX_CONSECUTIVE_FAILURES - 1,
    )
    respx.get(FEED_URL).mock(return_value=httpx.Response(404))

    collect.run(settings)

    row = source_row(conn, source)
    assert row["consecutive_failures"] == collect.MAX_CONSECUTIVE_FAILURES
    assert row["enabled"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 1


@respx.mock
def test_a_304_is_not_a_failure(settings: Settings, conn: sqlite3.Connection) -> None:
    topic = add_topic(conn)
    source = add_source(
        conn,
        topic,
        "rss",
        {"url": FEED_URL, "etag": '"abc123"'},
        consecutive_failures=3,
        last_fetch_at=LONG_AGO,
    )
    respx.get(FEED_URL).mock(return_value=httpx.Response(304))

    collect.run(settings)

    row = source_row(conn, source)
    assert row["last_status"] == "not_modified"
    assert row["consecutive_failures"] == 0
    assert row["last_fetch_at"] != LONG_AGO
    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


@respx.mock
def test_validators_are_written_back_to_the_source_config(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    topic = add_topic(conn)
    source = add_source(conn, topic, "rss", {"url": FEED_URL})
    mock_feed(headers={"ETag": '"abc123"'})

    collect.run(settings)

    config = json.loads(source_row(conn, source)["config"])
    assert config["url"] == FEED_URL
    assert config["etag"] == '"abc123"'


@respx.mock
def test_an_invalid_config_is_an_error_not_a_crash(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    topic = add_topic(conn)
    source = add_source(conn, topic, "rss", {"url": "not-a-url"})

    assert collect.run(settings) == 0

    row = source_row(conn, source)
    assert row["last_status"] == "error"
    assert row["consecutive_failures"] == 1


@respx.mock
def test_an_unregistered_source_type_is_skipped_without_counting_as_a_failure(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """The remaining adapters arrive in a later phase. Until then, skip cleanly."""
    topic = add_topic(conn)
    source = add_source(conn, topic, "espn", {"sport": "soccer"})

    assert collect.run(settings) == 0

    row = source_row(conn, source)
    assert row["last_status"] == "skipped"
    assert row["consecutive_failures"] == 0
    assert row["enabled"] == 1


@respx.mock
def test_disabled_sources_and_disabled_topics_are_left_alone(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    off_topic = add_topic(conn, "Dormant", enabled=False)
    add_source(conn, off_topic, "rss", {"url": FEED_URL})
    live_topic = add_topic(conn, "Rust", kind="tech")
    add_source(conn, live_topic, "rss", {"url": FEED_URL}, enabled=False)
    route = mock_feed()

    collect.run(settings)

    assert not route.called
    assert conn.execute("SELECT COUNT(*) AS n FROM fetch_log").fetchone()["n"] == 0


@respx.mock
def test_google_news_items_land_with_their_publisher(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    topic = add_topic(conn)
    add_source(
        conn, topic, "google_news", {"query": '"Midlakes United" -realty'}, last_fetch_at=LONG_AGO
    )
    respx.route(url__startswith="https://news.google.com/rss/search").mock(
        return_value=httpx.Response(200, content=fixture_bytes("google_news.xml"))
    )
    respx.head(url__startswith="https://news.google.com/rss/articles/").mock(
        return_value=httpx.Response(301, headers={"Location": "https://theathletic.com/gk/"})
    )
    respx.head("https://theathletic.com/gk/").mock(return_value=httpx.Response(200))

    collect.run(settings)

    rows = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
    assert len(rows) == 3
    assert rows[0]["url"].startswith("https://news.google.com/rss/articles/")
    assert rows[0]["canonical_url"] == "https://theathletic.com/gk/"
    assert rows[0]["source_name"] == "The Athletic"
    assert rows[0]["title_norm"] == "midlakes united sign a goalkeeper on loan"


def test_run_exits_nonzero_when_the_database_cannot_be_opened(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    blocker = tmp_path_factory.mktemp("blocked") / "blocker"
    blocker.write_text("not a directory")
    settings = Settings(db_path=blocker / "newsbrief.db")
    assert collect.run(settings) == 1
