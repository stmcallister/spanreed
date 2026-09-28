"""Stage 1: collapse the same story into one item. No LLM.

A syndicated wire story arrives under a dozen distinct URLs, so grouping keys
on the normalized title and never on the URL alone. This is deterministic and
costs nothing, which is exactly why a model has no business being here.

The composer only reads feeds through the collector. This stage reads `items`
and writes nothing except the `cluster_id` label, which exists so the grouping
can be inspected after the fact.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from itertools import groupby
from urllib.parse import urlsplit

import structlog
from rapidfuzz import fuzz

from newsbrief import db
from newsbrief.models import Cluster, Item

log = structlog.get_logger(__name__)

CLUSTER_WINDOW_HOURS = 24

# rapidfuzz token_set_ratio. Below this, two headlines about the same event
# start pulling in genuinely different stories.
SIMILARITY_THRESHOLD = 88

# CLAUDE.md says to pick the representative "by source priority", but there is
# no priority column on sources, so this is that priority. It encodes one idea:
# a feed published by the outlet itself outranks an aggregator that only points
# at one.
SOURCE_TYPE_PRIORITY: dict[str, int] = {
    "rss": 100,
    "espn": 90,
    "bluesky": 80,
    "rsshub": 60,
    "google_news": 40,
    "reddit": 30,
}
DEFAULT_SOURCE_PRIORITY = 50

AGGREGATOR_HOSTS = ("news.google.com", "reddit.com", "news.ycombinator.com")

_DIGIT_TOKEN = re.compile(r"\d+")

_SELECT_RECENT_ITEMS = """
SELECT i.*
FROM items i
JOIN topics t ON t.id = i.topic_id
WHERE t.enabled = 1 AND i.published_at >= ?
ORDER BY i.topic_id, i.id
"""

_SELECT_SOURCE_TYPES = "SELECT id, type FROM sources"

_NEXT_CLUSTER_ID = "SELECT COALESCE(MAX(cluster_id), 0) + 1 AS next FROM items"

_UPDATE_CLUSTER_ID = "UPDATE items SET cluster_id = ? WHERE id = ?"


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def is_aggregator_url(url: str) -> bool:
    """True for a link that points at an aggregator rather than at a publisher."""
    host = _host(url)
    return any(host == known or host.endswith(f".{known}") for known in AGGREGATOR_HOSTS)


def _digit_tokens(title: str) -> frozenset[str]:
    return frozenset(_DIGIT_TOKEN.findall(title))


def digits_conflict(left_title: str, right_title: str) -> bool:
    """True when two headlines disagree about the numbers in them.

    token_set_ratio scores the token intersection, so when one headline's words
    are nearly a subset of another's it approaches 100 no matter what the
    numbers say. Two scorelines or two match dates are two stories, however
    similar the words around them look. Measured on live data: `Final: Sounders
    2, Timbers 1` against `Final: Sounders 3, Timbers 0` scores 92.3, above the
    threshold, while a genuine duplicate scored 90.0. No threshold separates
    those. This does.

    Takes the raw title, not `title_norm`. Normalization strips a trailing
    source credit, and on a title like `... | June 25, 2017 Cyle Larin` the
    thing it strips is the date that distinguishes the story. `title_norm` also
    feeds `content_hash`, so teaching the stripper to keep numbers would change
    every hash and reinsert every stored item. Reading the title as published
    keeps that blast radius at zero.

    Only applies when both sides carry numbers, so a duplicate that simply
    omits a number the other mentions still merges.
    """
    left_digits, right_digits = _digit_tokens(left_title), _digit_tokens(right_title)
    return bool(left_digits and right_digits and left_digits != right_digits)


def source_types(conn: sqlite3.Connection) -> dict[int, str]:
    """Source id to source type, which is what representative ranking needs."""
    return {int(row["id"]): row["type"] for row in conn.execute(_SELECT_SOURCE_TYPES)}


def load_recent_items(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    window_hours: int = CLUSTER_WINDOW_HOURS,
) -> list[Item]:
    """Items from enabled topics published inside the window.

    The SQL bound is a bare date because two timestamp formats exist in this
    column: our own `...T...Z` writes and the `datetime('now')` column default.
    Both start with the date, so a date-only bound compares correctly against
    either. The exact cutoff is then applied in Python.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(hours=window_hours)
    coarse_bound = (cutoff - timedelta(days=1)).date().isoformat()

    items: list[Item] = []
    for row in conn.execute(_SELECT_RECENT_ITEMS, (coarse_bound,)):
        item = Item.from_row(row)
        published = db.parse_timestamp(item.published_at)
        if published is not None and published >= cutoff:
            items.append(item)
    return items


def _rank(item: Item, source_type: str) -> tuple[int, int, int, float, int]:
    """Sort key for choosing a cluster representative. Higher is better.

    Source priority first, then a publisher link over an aggregator redirect,
    then whichever copy actually carries a summary. The original usually breaks
    first, so an earlier timestamp wins the next tiebreak, and the item id
    settles the rest so the choice is stable across runs.
    """
    published = db.parse_timestamp(item.published_at)
    epoch = published.timestamp() if published is not None else float("inf")
    return (
        SOURCE_TYPE_PRIORITY.get(source_type, DEFAULT_SOURCE_PRIORITY),
        0 if is_aggregator_url(item.best_url) else 1,
        1 if item.summary_raw else 0,
        -epoch,
        -item.id,
    )


def pick_representative(members: Sequence[Item], types: Mapping[int, str]) -> Item:
    """The copy of the story that the brief should link to."""
    return max(members, key=lambda item: _rank(item, types.get(item.source_id, "")))


def _cluster_one_topic(
    topic_id: int, items: Sequence[Item], types: Mapping[int, str]
) -> list[Cluster]:
    """Greedy grouping, each item joining whichever cluster it matches best.

    Best match rather than first match, so the outcome does not depend on the
    order clusters happen to have been created in. Scoring against every member
    lets a chain of paraphrases collapse: a wire story and a rewrite of it often
    only meet through the copy in between.

    One disagreeing member rules out the whole cluster, so a scoreline cannot
    be pulled in alongside a different scoreline through a third item that
    happens to match both.
    """
    groups: list[list[Item]] = []
    for item in items:
        best_index: int | None = None
        best_score = 0.0
        for index, members in enumerate(groups):
            if any(digits_conflict(item.title, other.title) for other in members):
                continue
            score = max(
                fuzz.token_set_ratio(item.title_norm, member.title_norm) for member in members
            )
            if score >= SIMILARITY_THRESHOLD and score > best_score:
                best_index, best_score = index, score
        if best_index is None:
            groups.append([item])
        else:
            groups[best_index].append(item)

    return [
        Cluster(
            topic_id=topic_id, representative=pick_representative(members, types), members=members
        )
        for members in groups
    ]


def cluster_items(items: Sequence[Item], types: Mapping[int, str]) -> list[Cluster]:
    """Group items into stories, one topic at a time.

    Topics never merge with each other. Each one is its own section of the
    brief, and the same story can legitimately matter to two of them.
    """
    ordered = sorted(items, key=lambda item: (item.topic_id, item.published_at or "", item.id))
    clusters: list[Cluster] = []
    for topic_id, group in groupby(ordered, key=lambda item: item.topic_id):
        clusters.extend(_cluster_one_topic(topic_id, list(group), types))

    collapsed = sum(cluster.size - 1 for cluster in clusters)
    log.info(
        "clustered",
        items=len(items),
        clusters=len(clusters),
        duplicates_collapsed=collapsed,
    )
    return clusters


def representatives(clusters: Iterable[Cluster]) -> list[Item]:
    """One item per story, which is what the relevance filter is given."""
    return [cluster.representative for cluster in clusters]


def persist_clusters(conn: sqlite3.Connection, clusters: Sequence[Cluster]) -> int:
    """Label the grouping on `items` so it can be inspected after the fact.

    Ids continue from the highest already stored rather than restarting each
    run, so yesterday's groups are never confused with today's.
    """
    if not clusters:
        return 0

    row = conn.execute(_NEXT_CLUSTER_ID).fetchone()
    next_id = int(row["next"]) if row is not None else 1

    updates = [
        (cluster_id, item.id)
        for cluster_id, cluster in enumerate(clusters, start=next_id)
        for item in cluster.members
    ]
    with db.write_transaction(conn):
        conn.executemany(_UPDATE_CLUSTER_ID, updates)
    return len(clusters)
