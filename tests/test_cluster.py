"""Tests for clustering and dedupe. No LLM and no network are involved."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from spanreed import collect, db
from spanreed.compose import cluster
from spanreed.models import Item
from tests.conftest import add_source, add_topic

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

# The same story as it actually arrives: one event, six outlets, six URLs.
SYNDICATED = [
    (
        "https://apnews.com/article/sounders-de-rosario",
        "Sounders loan Osaze De Rosario to Danish club",
    ),
    (
        "https://sports.yahoo.com/sounders-loan-de-rosario",
        "Seattle Sounders loan Osaze De Rosario to Danish club",
    ),
    (
        "https://www.seattletimes.com/sports/sounders/de-rosario-loan",
        "Sounders loan De Rosario to a Danish club",
    ),
    (
        "https://sounderatheart.com/2026/9/de-rosario",
        "Osaze De Rosario goes on loan to Danish club",
    ),
    (
        "https://www.mlssoccer.com/news/de-rosario-loan",
        "Seattle Sounders loan forward Osaze De Rosario to Danish club",
    ),
    (
        "https://theathletic.com/sounders-de-rosario-loan",
        "Sounders send Osaze De Rosario on loan to Danish club",
    ),
]


def make_item(
    item_id: int,
    title: str,
    url: str,
    *,
    topic_id: int = 1,
    source_id: int = 1,
    published_at: str | None = None,
    summary: str | None = None,
    canonical_url: str | None = None,
) -> Item:
    """An Item as `collect` would have stored it, normalization included."""
    title_norm = collect.normalize_title(title)
    return Item(
        id=item_id,
        source_id=source_id,
        topic_id=topic_id,
        url=url,
        canonical_url=canonical_url,
        title=title,
        title_norm=title_norm,
        summary_raw=summary,
        author=None,
        source_name=None,
        published_at=published_at or db.to_iso_utc(NOW - timedelta(hours=2)),
        fetched_at=db.to_iso_utc(NOW),
        content_hash=collect.content_hash(title_norm, url, canonical_url),
        cluster_id=None,
    )


def syndicated_items(source_id: int = 1) -> list[Item]:
    return [
        make_item(index, title, url, source_id=source_id)
        for index, (url, title) in enumerate(SYNDICATED, start=1)
    ]


# --- grouping ---------------------------------------------------------------


def test_a_syndicated_story_collapses_to_one_item() -> None:
    """Six sources, six URLs, one surviving story."""
    clusters = cluster.cluster_items(syndicated_items(), {1: "rss"})

    assert len(clusters) == 1
    assert clusters[0].size == 6
    assert len(cluster.representatives(clusters)) == 1


def test_url_only_dedupe_would_have_let_all_six_through() -> None:
    """The reason grouping keys on the normalized title."""
    items = syndicated_items()
    assert len({item.url for item in items}) == 6
    assert len({item.content_hash for item in items}) == 6
    assert len(cluster.cluster_items(items, {1: "rss"})) == 1


def test_different_stories_stay_apart() -> None:
    items = [
        make_item(1, "Sounders loan Osaze De Rosario to Danish club", "https://a.example/1"),
        make_item(2, "Sounders name a new sporting director", "https://b.example/2"),
        make_item(3, "Sounders fall to Austin FC on the road", "https://c.example/3"),
    ]
    assert len(cluster.cluster_items(items, {1: "rss"})) == 3


def test_topics_never_merge_with_each_other() -> None:
    """The same story can legitimately matter to two topics."""
    items = [
        make_item(1, "Kubernetes 1.35 is released", "https://a.example/1", topic_id=1),
        make_item(2, "Kubernetes 1.35 is released", "https://a.example/1", topic_id=2),
    ]
    clusters = cluster.cluster_items(items, {1: "rss"})

    assert len(clusters) == 2
    assert {c.topic_id for c in clusters} == {1, 2}


def test_a_paraphrase_chain_collapses() -> None:
    """The two ends only meet through the copy in between."""
    items = [
        make_item(1, "Sounders loan Osaze De Rosario to Danish club", "https://a.example/1"),
        make_item(2, "Sounders loan Osaze De Rosario to a Danish club side", "https://b.example/2"),
        make_item(3, "Osaze De Rosario joins a Danish club side on loan", "https://c.example/3"),
    ]
    assert len(cluster.cluster_items(items, {1: "rss"})) == 1


def test_grouping_does_not_depend_on_input_order() -> None:
    items = syndicated_items()
    forward = cluster.cluster_items(items, {1: "rss"})
    backward = cluster.cluster_items(list(reversed(items)), {1: "rss"})

    assert len(forward) == len(backward) == 1
    assert forward[0].representative.id == backward[0].representative.id


def test_an_empty_input_yields_no_clusters() -> None:
    assert cluster.cluster_items([], {}) == []


# --- the digit guard --------------------------------------------------------
#
# token_set_ratio scores the token intersection, so a headline whose words are
# nearly a subset of another's approaches 100 whatever the numbers say. On live
# data the false merges scored HIGHER than the true one (96.9 and 92.3 against
# 90.0), so no threshold separates them. These are the cases that motivated the
# guard, with the measured scores recorded so a future change cannot quietly
# undo it.


def test_two_different_final_scores_are_two_stories() -> None:
    """token_set_ratio scores these 92.3, above the threshold."""
    items = [
        make_item(1, "Final: Sounders 2, Timbers 1", "https://a.example/1"),
        make_item(2, "Final: Sounders 3, Timbers 0", "https://b.example/2"),
    ]
    assert len(cluster.cluster_items(items, {1: "espn"})) == 2


def test_the_same_final_score_still_collapses() -> None:
    items = [
        make_item(1, "Final: Sounders 2, Timbers 1", "https://a.example/1"),
        make_item(2, "Final: Sounders 2, Timbers 1", "https://b.example/2"),
    ]
    assert len(cluster.cluster_items(items, {1: "espn"})) == 1


def test_highlight_reels_from_different_years_do_not_merge() -> None:
    """The live false positive: three matches collapsed into one story."""
    items = [
        make_item(
            1,
            "Highlights: Portland Timbers 2-1 Seattle Sounders FC (MLS) - Yahoo Sports",
            "https://a.example/1",
        ),
        make_item(
            2,
            "Highlights: Portland Timbers FC Vs. Seattle Sounders FC | June 25, 2017",
            "https://b.example/2",
        ),
    ]
    assert len(cluster.cluster_items(items, {1: "google_news"})) == 2


def test_a_duplicate_that_omits_a_number_still_merges() -> None:
    """The guard only applies when both sides carry numbers."""
    items = [
        make_item(1, "Sounders beat Timbers 2-1 at home", "https://a.example/1"),
        make_item(2, "Sounders beat Timbers at home", "https://b.example/2"),
    ]
    assert len(cluster.cluster_items(items, {1: "rss"})) == 1


def test_a_shared_version_number_does_not_block_a_merge() -> None:
    items = [
        make_item(1, "Kubernetes 1.35 is released", "https://a.example/1"),
        make_item(2, "Kubernetes 1.35 released today", "https://b.example/2"),
    ]
    assert len(cluster.cluster_items(items, {1: "rss"})) == 1


def test_two_versions_of_the_same_project_are_two_stories() -> None:
    items = [
        make_item(1, "Kubernetes 1.35 is released", "https://a.example/1"),
        make_item(2, "Kubernetes 1.36 is released", "https://b.example/2"),
    ]
    assert len(cluster.cluster_items(items, {1: "rss"})) == 2


def test_a_scoreline_cannot_join_through_a_third_item_that_matches_both() -> None:
    """One disagreeing member rules out the whole cluster."""
    items = [
        make_item(1, "Final: Sounders 2, Timbers 1", "https://a.example/1"),
        make_item(2, "Final: Sounders Timbers", "https://b.example/2"),
        make_item(3, "Final: Sounders 3, Timbers 0", "https://c.example/3"),
    ]
    clusters = cluster.cluster_items(items, {1: "espn"})

    for group in clusters:
        scorelines = {
            frozenset(token for token in item.title_norm.split() if token.isdigit())
            for item in group.members
        }
        assert len({s for s in scorelines if s}) <= 1


@pytest.mark.parametrize(
    ("left", "right", "conflicts"),
    [
        ("Final: Sounders 2, Timbers 1", "Final: Sounders 3, Timbers 0", True),
        ("Final: Sounders 2, Timbers 1", "Final: Sounders 2, Timbers 1", False),
        ("Sounders beat Timbers 2-1", "Sounders beat Timbers", False),
        ("Sounders beat Timbers", "Timbers lose to Sounders", False),
        ("Rust 1.90 lands - Ars Technica", "Rust 1.90 released | The Register", False),
        ("Highlights | June 25, 2017", "Highlights | August 23, 2019", True),
    ],
)
def test_digits_conflict(left: str, right: str, conflicts: bool) -> None:
    assert cluster.digits_conflict(left, right) is conflicts


def test_the_guard_reads_the_title_before_attribution_is_stripped() -> None:
    """Normalization would have removed the date that distinguishes these."""
    left = "Highlights: Portland Timbers FC Vs. Seattle Sounders FC | June 25, 2017 Cyle Larin"
    right = "Portland Timbers Vs. Seattle Sounders FC | HIGHLIGHTS - August 23, 2019 Coppa Italia"

    assert collect.normalize_title(left) == "highlights portland timbers fc vs seattle sounders fc"
    assert cluster.digits_conflict(left, right) is True


# --- representative selection -----------------------------------------------


def test_the_publisher_outranks_the_aggregator() -> None:
    """CLAUDE.md: prefer the original publisher over an aggregator."""
    items = [
        make_item(
            1,
            "Sounders loan Osaze De Rosario to Danish club",
            "https://news.google.com/rss/articles/abc",
            source_id=2,
        ),
        make_item(
            2,
            "Sounders loan Osaze De Rosario to Danish club",
            "https://sounderatheart.com/de-rosario",
            source_id=1,
        ),
    ]
    clusters = cluster.cluster_items(items, {1: "rss", 2: "google_news"})

    assert clusters[0].representative.id == 2


def test_a_resolved_google_link_is_judged_on_its_publisher_url() -> None:
    items = [
        make_item(
            1,
            "Sounders loan Osaze De Rosario to Danish club",
            "https://news.google.com/rss/articles/abc",
            source_id=2,
        ),
        make_item(
            2,
            "Sounders loan Osaze De Rosario to Danish club",
            "https://news.google.com/rss/articles/def",
            source_id=2,
            canonical_url="https://theathletic.com/de-rosario",
        ),
    ]
    clusters = cluster.cluster_items(items, {2: "google_news"})

    assert clusters[0].representative.id == 2


def test_a_copy_with_a_summary_wins_the_next_tiebreak() -> None:
    items = [
        make_item(1, "Kubernetes 1.35 is released", "https://a.example/1"),
        make_item(2, "Kubernetes 1.35 is released", "https://b.example/2", summary="What changed."),
    ]
    clusters = cluster.cluster_items(items, {1: "rss"})

    assert clusters[0].representative.id == 2


def test_the_earlier_copy_wins_when_all_else_is_equal() -> None:
    """The outlet that broke it usually published first."""
    early = db.to_iso_utc(NOW - timedelta(hours=6))
    late = db.to_iso_utc(NOW - timedelta(hours=1))
    items = [
        make_item(1, "Kubernetes 1.35 is released", "https://a.example/1", published_at=late),
        make_item(2, "Kubernetes 1.35 is released", "https://b.example/2", published_at=early),
    ]
    clusters = cluster.cluster_items(items, {1: "rss"})

    assert clusters[0].representative.id == 2


def test_an_unknown_source_type_still_ranks() -> None:
    """A source type from a later phase must not break the composer."""
    items = syndicated_items(source_id=9)
    clusters = cluster.cluster_items(items, {9: "something_new"})
    assert len(clusters) == 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://news.google.com/rss/articles/abc", True),
        ("https://www.reddit.com/r/soccer/x", True),
        ("https://theathletic.com/x", False),
        ("https://googlenews.example.com/x", False),
    ],
)
def test_aggregator_detection(url: str, expected: bool) -> None:
    assert cluster.is_aggregator_url(url) is expected


# --- loading and persistence ------------------------------------------------


def insert_item(conn: sqlite3.Connection, item: Item) -> None:
    conn.execute(
        "INSERT INTO items (id, source_id, topic_id, url, canonical_url, title, title_norm,"
        " summary_raw, published_at, fetched_at, content_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item.id,
            item.source_id,
            item.topic_id,
            item.url,
            item.canonical_url,
            item.title,
            item.title_norm,
            item.summary_raw,
            item.published_at,
            item.fetched_at,
            item.content_hash,
        ),
    )


def test_load_recent_items_applies_the_window(conn: sqlite3.Connection) -> None:
    topic = add_topic(conn)
    source = add_source(conn, topic)
    inside = make_item(
        1,
        "Fresh story",
        "https://a.example/1",
        topic_id=topic,
        source_id=source,
        published_at=db.to_iso_utc(NOW - timedelta(hours=5)),
    )
    outside = make_item(
        2,
        "Yesterday's story",
        "https://b.example/2",
        topic_id=topic,
        source_id=source,
        published_at=db.to_iso_utc(NOW - timedelta(hours=30)),
    )
    insert_item(conn, inside)
    insert_item(conn, outside)

    loaded = cluster.load_recent_items(conn, now=NOW)

    assert [item.id for item in loaded] == [1]


def test_load_recent_items_reads_the_sqlite_default_timestamp_format(
    conn: sqlite3.Connection,
) -> None:
    """Rows written by a column default use a space, not a T."""
    topic = add_topic(conn)
    source = add_source(conn, topic)
    item = make_item(1, "Fresh story", "https://a.example/1", topic_id=topic, source_id=source)
    insert_item(conn, item)
    conn.execute(
        "UPDATE items SET published_at = ? WHERE id = 1",
        ((NOW - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S"),),
    )

    assert [item.id for item in cluster.load_recent_items(conn, now=NOW)] == [1]


def test_load_recent_items_skips_disabled_topics(conn: sqlite3.Connection) -> None:
    topic = add_topic(conn, "Dormant", enabled=False)
    source = add_source(conn, topic)
    insert_item(
        conn, make_item(1, "Fresh story", "https://a.example/1", topic_id=topic, source_id=source)
    )

    assert cluster.load_recent_items(conn, now=NOW) == []


def test_source_types_are_read_from_the_database(conn: sqlite3.Connection) -> None:
    topic = add_topic(conn)
    rss_source = add_source(conn, topic, "rss")
    news_source = add_source(conn, topic, "google_news", {"query": "x"})

    assert cluster.source_types(conn) == {rss_source: "rss", news_source: "google_news"}


def test_persist_clusters_labels_every_member(conn: sqlite3.Connection) -> None:
    topic = add_topic(conn)
    source = add_source(conn, topic)
    for item in syndicated_items(source_id=source):
        insert_item(
            conn, make_item(item.id, item.title, item.url, topic_id=topic, source_id=source)
        )

    clusters = cluster.cluster_items(
        cluster.load_recent_items(conn, now=NOW), cluster.source_types(conn)
    )
    assert cluster.persist_clusters(conn, clusters) == 1

    labels = {row["cluster_id"] for row in conn.execute("SELECT cluster_id FROM items")}
    assert labels == {1}


def test_cluster_ids_continue_across_runs(conn: sqlite3.Connection) -> None:
    """Yesterday's groups must never be confused with today's."""
    topic = add_topic(conn)
    source = add_source(conn, topic)
    insert_item(
        conn, make_item(1, "Story one", "https://a.example/1", topic_id=topic, source_id=source)
    )
    insert_item(
        conn,
        make_item(
            2, "A wholly separate story", "https://b.example/2", topic_id=topic, source_id=source
        ),
    )

    items = cluster.load_recent_items(conn, now=NOW)
    types = cluster.source_types(conn)
    cluster.persist_clusters(conn, cluster.cluster_items(items, types))
    first = sorted(row["cluster_id"] for row in conn.execute("SELECT cluster_id FROM items"))

    cluster.persist_clusters(conn, cluster.cluster_items(items, types))
    second = sorted(row["cluster_id"] for row in conn.execute("SELECT cluster_id FROM items"))

    assert first == [1, 2]
    assert second == [3, 4]


def test_persist_clusters_handles_an_empty_run(conn: sqlite3.Connection) -> None:
    assert cluster.persist_clusters(conn, []) == 0
