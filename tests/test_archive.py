"""Tests for the archive write, which happens before any other delivery."""

from __future__ import annotations

import sqlite3

from newsbrief import db
from newsbrief.compose.filter import Scored
from newsbrief.deliver import archive
from tests.conftest import NOW, add_source, add_topic, make_topic
from tests.test_cluster import insert_item, make_item

TOPIC = make_topic(1, "Midlakes United")


def store(conn: sqlite3.Connection, count: int) -> list[Scored]:
    """Items have to exist before brief_items can reference them."""
    topic_id = add_topic(conn)
    source_id = add_source(conn, topic_id)
    entries: list[Scored] = []
    for index in range(1, count + 1):
        item = make_item(
            index,
            f"Story {index}",
            f"https://a.example/{index}",
            topic_id=topic_id,
            source_id=source_id,
        )
        insert_item(conn, item)
        entries.append(Scored(item=item, topic=TOPIC, score=0.9, reason="on topic"))
    return entries


def save(conn: sqlite3.Connection, entries: list[Scored], **overrides: object) -> int:
    kwargs: dict[str, object] = {
        "brief_date": "2026-09-01",
        "generated_at": db.to_iso_utc(NOW),
        "markdown": "## Midlakes United\n\n- [Story 1](https://a.example/1) It happened.",
        "selected": entries,
        "model": "claude-sonnet-5",
        "input_tokens": 1200,
        "output_tokens": 400,
        "degraded": False,
        "notes": None,
    }
    kwargs.update(overrides)
    return archive.save(conn, **kwargs)  # type: ignore[arg-type]


# --- rendering --------------------------------------------------------------


def test_markdown_is_rendered_to_html() -> None:
    html = archive.render_html("## Topic\n\n- [A story](https://a.example/1) It happened.")

    assert "<h2>Topic</h2>" in html
    assert '<a href="https://a.example/1">A story</a>' in html


# --- persistence ------------------------------------------------------------


def test_the_brief_and_its_items_are_stored(conn: sqlite3.Connection) -> None:
    entries = store(conn, 3)

    brief_id = save(conn, entries)

    row = conn.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["brief_date"] == "2026-09-01"
    assert row["item_count"] == 3
    assert row["html"].startswith("<h2>")
    assert row["degraded"] == 0

    items = conn.execute(
        "SELECT item_id, rank FROM brief_items WHERE brief_id = ? ORDER BY rank", (brief_id,)
    ).fetchall()
    assert [(r["item_id"], r["rank"]) for r in items] == [(1, 1), (2, 2), (3, 3)]


def test_token_counts_are_recorded(conn: sqlite3.Connection) -> None:
    """The token record is how a few cents a day gets noticed when it is not."""
    brief_id = save(conn, store(conn, 1))

    row = conn.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert (row["input_tokens"], row["output_tokens"]) == (1200, 400)
    assert row["model"] == "claude-sonnet-5"


def test_a_degraded_brief_records_why(conn: sqlite3.Connection) -> None:
    brief_id = save(conn, store(conn, 1), degraded=True, notes="relevance filter unavailable")

    row = conn.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["degraded"] == 1
    assert row["notes"] == "relevance filter unavailable"


def test_regenerating_a_date_replaces_it(conn: sqlite3.Connection) -> None:
    """The generate now button in the UI has to be safe to press twice."""
    entries = store(conn, 3)
    first = save(conn, entries)
    second = save(conn, entries[:1], markdown="## Rewritten")

    assert first == second
    count = conn.execute("SELECT COUNT(*) AS n FROM briefs").fetchone()["n"]
    assert count == 1

    row = conn.execute("SELECT * FROM briefs WHERE id = ?", (first,)).fetchone()
    assert row["markdown"] == "## Rewritten"
    assert row["item_count"] == 1


def test_regenerating_does_not_keep_the_old_item_set(conn: sqlite3.Connection) -> None:
    entries = store(conn, 3)
    brief_id = save(conn, entries)
    save(conn, entries[:1])

    items = conn.execute(
        "SELECT item_id FROM brief_items WHERE brief_id = ?", (brief_id,)
    ).fetchall()
    assert [row["item_id"] for row in items] == [1]


def test_an_empty_brief_is_still_archived(conn: sqlite3.Connection) -> None:
    """A quiet day produces a brief that says so, not a missing row."""
    brief_id = save(conn, [], markdown="Nothing to report today.")

    row = conn.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["item_count"] == 0
