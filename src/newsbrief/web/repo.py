"""Every query the web UI makes.

The routes do not write SQL. Keeping it here means one place to look when a
screen shows the wrong number, and it keeps the write transactions short and
visible rather than scattered through request handlers.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from newsbrief import db
from newsbrief.models import Brief, Item, Source, Topic

ACTIVITY_WINDOW_DAYS = 7
"""The item count on the topics list, which is how you spot a dead topic."""

MAX_CONSECUTIVE_FAILURES = 20
"""Matches the collector. A source at this count has been auto disabled."""

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True)
class TopicSummary:
    """A row on the topics list."""

    topic: Topic
    source_count: int
    enabled_source_count: int
    recent_item_count: int

    @property
    def has_disabled_sources(self) -> bool:
        return self.enabled_source_count < self.source_count


@dataclass(slots=True)
class SourceHealth:
    """A row on the source health screen."""

    source: Source
    topic_name: str
    last_ok_at: str | None

    @property
    def auto_disabled(self) -> bool:
        """Disabled by the collector rather than by a person."""
        return not self.source.enabled and self.source.consecutive_failures >= (
            MAX_CONSECUTIVE_FAILURES
        )


def slugify(name: str) -> str:
    """A URL safe name. Uniqueness is the database's job, not this function's."""
    slug = _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")
    return slug or "topic"


# --- topics -----------------------------------------------------------------

_SELECT_TOPIC_SUMMARIES = """
SELECT
    t.*,
    (SELECT COUNT(*) FROM sources s WHERE s.topic_id = t.id) AS source_count,
    (SELECT COUNT(*) FROM sources s WHERE s.topic_id = t.id AND s.enabled = 1)
        AS enabled_source_count,
    (SELECT COUNT(*) FROM items i WHERE i.topic_id = t.id AND i.fetched_at >= ?)
        AS recent_item_count
FROM topics t
ORDER BY t.enabled DESC, t.priority DESC, t.name
"""


def topic_summaries(conn: sqlite3.Connection, now: datetime) -> list[TopicSummary]:
    """Every topic with its source and item counts, enabled ones first."""
    cutoff = db.to_iso_utc(now - timedelta(days=ACTIVITY_WINDOW_DAYS))
    return [
        TopicSummary(
            topic=Topic.from_row(row),
            source_count=int(row["source_count"]),
            enabled_source_count=int(row["enabled_source_count"]),
            recent_item_count=int(row["recent_item_count"]),
        )
        for row in conn.execute(_SELECT_TOPIC_SUMMARIES, (cutoff,))
    ]


def get_topic(conn: sqlite3.Connection, topic_id: int) -> Topic | None:
    row = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
    return None if row is None else Topic.from_row(row)


def create_topic(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    relevance_hint: str,
    max_items: int,
    priority: int,
) -> int:
    with db.write_transaction(conn):
        cursor = conn.execute(
            "INSERT INTO topics (name, slug, kind, relevance_hint, max_items, priority)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, slugify(name), kind, relevance_hint, max_items, priority),
        )
    return int(cursor.lastrowid or 0)


def update_topic(
    conn: sqlite3.Connection,
    topic_id: int,
    *,
    name: str,
    kind: str,
    relevance_hint: str,
    max_items: int,
    priority: int,
) -> None:
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE topics SET name = ?, slug = ?, kind = ?, relevance_hint = ?,"
            " max_items = ?, priority = ? WHERE id = ?",
            (name, slugify(name), kind, relevance_hint, max_items, priority, topic_id),
        )


def set_topic_enabled(conn: sqlite3.Connection, topic_id: int, enabled: bool) -> None:
    with db.write_transaction(conn):
        conn.execute("UPDATE topics SET enabled = ? WHERE id = ?", (int(enabled), topic_id))


def delete_topic(conn: sqlite3.Connection, topic_id: int) -> None:
    """Sources and items go with it, by way of ON DELETE CASCADE."""
    with db.write_transaction(conn):
        conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))


# --- sources ----------------------------------------------------------------


def sources_for_topic(conn: sqlite3.Connection, topic_id: int) -> list[Source]:
    return [
        Source.from_row(row)
        for row in conn.execute(
            "SELECT * FROM sources WHERE topic_id = ? ORDER BY type, id", (topic_id,)
        )
    ]


def get_source(conn: sqlite3.Connection, source_id: int) -> Source | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return None if row is None else Source.from_row(row)


def create_source(
    conn: sqlite3.Connection, topic_id: int, source_type: str, config: dict[str, object]
) -> int:
    with db.write_transaction(conn):
        cursor = conn.execute(
            "INSERT INTO sources (topic_id, type, config) VALUES (?, ?, ?)",
            (topic_id, source_type, json.dumps(config, sort_keys=True)),
        )
    return int(cursor.lastrowid or 0)


def update_source(
    conn: sqlite3.Connection, source_id: int, source_type: str, config: dict[str, object]
) -> None:
    """Editing a source clears its failure history.

    A source that has been failing has usually been failing because of the
    setting the user just changed, so holding the old count against it would
    keep it auto disabled after it was fixed.
    """
    with db.write_transaction(conn):
        conn.execute(
            "UPDATE sources SET type = ?, config = ?, consecutive_failures = 0,"
            " last_error = NULL WHERE id = ?",
            (source_type, json.dumps(config, sort_keys=True), source_id),
        )


def set_source_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool) -> None:
    """Re-enabling also clears the failure count, which is what un-disables it."""
    with db.write_transaction(conn):
        if enabled:
            conn.execute(
                "UPDATE sources SET enabled = 1, consecutive_failures = 0 WHERE id = ?",
                (source_id,),
            )
        else:
            conn.execute("UPDATE sources SET enabled = 0 WHERE id = ?", (source_id,))


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    with db.write_transaction(conn):
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


_SELECT_SOURCE_HEALTH = """
SELECT
    s.*,
    t.name AS topic_name,
    (SELECT MAX(f.run_at) FROM fetch_log f WHERE f.source_id = s.id AND f.status = 'ok')
        AS last_ok_at
FROM sources s
JOIN topics t ON t.id = s.topic_id
ORDER BY s.consecutive_failures DESC, s.last_status = 'error' DESC, t.name, s.type
"""


def source_health(conn: sqlite3.Connection) -> list[SourceHealth]:
    """Worst first, so an auto disabled source is the first thing on the page."""
    rows = [
        SourceHealth(
            source=Source.from_row(row),
            topic_name=row["topic_name"],
            last_ok_at=row["last_ok_at"],
        )
        for row in conn.execute(_SELECT_SOURCE_HEALTH)
    ]
    return sorted(rows, key=lambda row: (not row.auto_disabled,))


# --- briefs -----------------------------------------------------------------


def brief_dates(conn: sqlite3.Connection, limit: int = 90) -> list[Brief]:
    """The archive index. Markdown and HTML are not loaded for a list view."""
    return [
        Brief(
            id=int(row["id"]),
            brief_date=row["brief_date"],
            generated_at=row["generated_at"],
            markdown="",
            html="",
            item_count=int(row["item_count"]),
            model=row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            degraded=bool(row["degraded"]),
            notes=row["notes"],
        )
        for row in conn.execute(
            "SELECT id, brief_date, generated_at, item_count, model, input_tokens,"
            " output_tokens, degraded, notes FROM briefs ORDER BY brief_date DESC LIMIT ?",
            (limit,),
        )
    ]


def get_brief(conn: sqlite3.Connection, brief_date: str) -> Brief | None:
    row = conn.execute("SELECT * FROM briefs WHERE brief_date = ?", (brief_date,)).fetchone()
    return None if row is None else Brief.from_row(row)


def brief_items(conn: sqlite3.Connection, brief_id: int) -> list[Item]:
    """The items a brief was built from, in the order it ranked them."""
    return [
        Item.from_row(row)
        for row in conn.execute(
            "SELECT i.* FROM brief_items b JOIN items i ON i.id = b.item_id"
            " WHERE b.brief_id = ? ORDER BY b.rank",
            (brief_id,),
        )
    ]


def latest_brief_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(brief_date) AS latest FROM briefs").fetchone()
    return None if row is None else row["latest"]


def source_types_in_use(conn: sqlite3.Connection) -> Sequence[str]:
    return [row["type"] for row in conn.execute("SELECT DISTINCT type FROM sources ORDER BY type")]
