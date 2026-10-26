"""Composer workload: cluster, filter, deep read, write, deliver.

Four stages, and every one of them degrades rather than aborts. The composer
never fetches feeds. It reads what the collector stored, which is the entire
reliability story of this application: a feed that broke at 3am cannot stop the
brief from being written at 6am, it can only make it shorter.

The exit code is 0 whenever a brief was archived, degraded or not. A degraded
brief is a successful run that says so in its own footer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

import structlog

from spanreed import db, llm
from spanreed.compose import cluster, deepread, write
from spanreed.compose.filter import (
    FilterError,
    Scored,
    fallback_select,
    load_topics,
)
from spanreed.compose.filter import run as run_filter
from spanreed.config import Settings
from spanreed.llm import LlmClient
from spanreed.models import Cluster, Topic
from spanreed.sources.base import build_client

log = structlog.get_logger(__name__)

FAILURE_WINDOW_HOURS = 24

_SELECT_RECENT_FAILURES = """
SELECT DISTINCT t.name AS topic_name, s.type AS source_type
FROM fetch_log f
JOIN sources s ON s.id = f.source_id
JOIN topics t ON t.id = s.topic_id
WHERE f.status = 'error' AND f.run_at >= ?
ORDER BY t.name, s.type
"""

_DELETE_OLD_ITEMS = "DELETE FROM items WHERE fetched_at < ?"


def recent_failures(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """Sources that failed in the last day, named for the footer."""
    cutoff = db.to_iso_utc(now - timedelta(hours=FAILURE_WINDOW_HOURS))
    return [
        f"{row['topic_name']} ({row['source_type']})"
        for row in conn.execute(_SELECT_RECENT_FAILURES, (cutoff,))
    ]


def purge_old_items(conn: sqlite3.Connection, now: datetime, retention_days: int) -> int:
    """Drop items past the retention window. Briefs are kept forever, they are small."""
    cutoff = db.to_iso_utc(now - timedelta(days=retention_days))
    with db.write_transaction(conn):
        before = conn.total_changes
        conn.execute(_DELETE_OLD_ITEMS, (cutoff,))
        deleted = conn.total_changes - before
    if deleted:
        log.info("old items purged", deleted=deleted, retention_days=retention_days)
    return deleted


def brief_date_for(now: datetime, timezone: str) -> str:
    """The date the reader will call this brief, in their own timezone.

    Stored timestamps are UTC, but a brief generated at 06:00 Pacific belongs to
    the Pacific date, not to the UTC date that is already tomorrow.
    """
    zone: tzinfo
    try:
        zone = ZoneInfo(timezone)
    except Exception:
        log.warning("unknown render timezone, using UTC", timezone=timezone)
        zone = UTC
    return now.astimezone(zone).date().isoformat()


def _select(
    client: LlmClient | None,
    clusters: Sequence[Cluster],
    topics: dict[int, Topic],
    source_types: dict[int, str],
    settings: Settings,
    notes: list[str],
) -> tuple[list[Scored], bool]:
    """Stage 2 with its degraded path. Returns the selection and whether it degraded."""
    if client is None:
        notes.append("relevance filter skipped, no model configured")
        return (
            fallback_select(clusters, topics, source_types, settings.max_brief_items),
            True,
        )

    try:
        outcome = run_filter(client, clusters, topics, settings.max_brief_items)
    except FilterError as exc:
        log.error("relevance filter failed, ranking without it", error=str(exc))
        notes.append("relevance filter unavailable, items ranked by source and recency")
        return (
            fallback_select(clusters, topics, source_types, settings.max_brief_items),
            True,
        )

    if outcome.failed_batches:
        notes.append(f"{outcome.failed_batches} relevance batches failed and were dropped")
    return outcome.selected, outcome.degraded


def _write_body(
    client: LlmClient | None,
    grouped: Sequence[tuple[Topic, list[Scored]]],
    extracts: dict[int, str],
    notes: list[str],
) -> tuple[str, str | None, bool]:
    """Stage 4 with its degraded path. Returns markdown, the model used, and degradation."""
    if not grouped:
        return ("Nothing to report today.", None, False)

    if client is None:
        notes.append("brief not written by a model, no model configured")
        return (write.fallback_markdown(grouped), None, True)

    try:
        return (write.run(client, grouped, extracts), client.write_model, False)
    except Exception:
        log.error("write stage failed, emitting a plain list", exc_info=True)
        notes.append("brief could not be written, items listed as found")
        return (write.fallback_markdown(grouped), None, True)


def compose(conn: sqlite3.Connection, settings: Settings, now: datetime) -> tuple[str, int]:
    """Run the four stages and archive the result. Returns the markdown and brief id."""
    from spanreed.deliver import archive, email

    notes: list[str] = []

    items = cluster.load_recent_items(conn, now)
    source_types = cluster.source_types(conn)
    clusters = cluster.cluster_items(items, source_types)
    cluster.persist_clusters(conn, clusters)

    topics = load_topics(conn)
    client = llm.build(settings)

    selected, filter_degraded = _select(client, clusters, topics, source_types, settings, notes)

    extracts: dict[int, str] = {}
    if selected and settings.deep_read_items:
        with build_client(settings) as http_client:
            extracts = deepread.run(
                http_client,
                [entry.item for entry in selected],
                settings.deep_read_items,
            )

    grouped = write.group_by_topic(selected)
    body, model, write_degraded = _write_body(client, grouped, extracts, notes)

    markdown = body + "\n" + write.render_footer(recent_failures(conn, now), notes)
    usage = client.usage if client is not None else None

    brief_date = brief_date_for(now, settings.render_timezone)
    degraded = filter_degraded or write_degraded

    brief_id = archive.save(
        conn,
        brief_date=brief_date,
        generated_at=db.to_iso_utc(now),
        markdown=markdown,
        selected=selected,
        model=model,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        degraded=degraded,
        notes="; ".join(notes) or None,
    )

    # Archive first, then email. The brief is safe in the database before any
    # delivery is attempted, so a mail server that is down costs a delivery and
    # never a brief. `send` does not raise.
    email.send(
        settings,
        brief_date=brief_date,
        markdown=markdown,
        html=archive.render_html(markdown),
        degraded=degraded,
    )
    return markdown, brief_id


def run(settings: Settings) -> int:
    """Generate and deliver today's brief. Returns the process exit code."""
    try:
        conn = db.connect(settings.db_path)
    except (sqlite3.Error, OSError):
        log.exception("could not open the database", db_path=str(settings.db_path))
        return 1

    started = datetime.now(UTC)
    try:
        markdown, brief_id = compose(conn, settings, started)
        purge_old_items(conn, started, settings.item_retention_days)
    finally:
        conn.close()

    # Phase 4 delivers to stdout. Email and the feed arrive in phase 8, and the
    # archive write has already happened either way.
    print(markdown)

    log.info(
        "composer finished",
        brief_id=brief_id,
        elapsed_seconds=round((datetime.now(UTC) - started).total_seconds(), 2),
    )
    return 0
