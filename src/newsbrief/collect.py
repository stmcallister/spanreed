"""Collector workload: fetch every enabled source and store new items.

Runs every three hours, independently of the brief, so that news arriving at
2pm is captured at 3pm rather than waiting for one overnight fetch that might
fail.

A dead source degrades the brief, it never empties it. Every failure is caught,
recorded against the source, and written to `fetch_log`. The process exits 0
even when every source fails. It exits non-zero only when the database cannot
be opened.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import urlsplit

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from newsbrief import db
from newsbrief.config import Settings
from newsbrief.db import parse_timestamp, to_iso_utc
from newsbrief.models import FetchResult, FetchStatus, RawItem, Source
from newsbrief.sources import build_client, get_adapter
from newsbrief.sources.base import NotModifiedError

log = structlog.get_logger(__name__)

# How far back to look on a source that has never been fetched successfully.
LOOKBACK_HOURS = 48

# A source that has failed this many times running is disabled and surfaced in
# the UI. It is never deleted, because the fix is usually a new URL.
MAX_CONSECUTIVE_FAILURES = 20

MAX_ERROR_CHARS = 500

# A trailing attribution such as " - The Verge" or " | Ars Technica". Written
# with escapes so the en and em dash cases are unambiguous in source.
_ATTRIBUTION = re.compile(r"^(?P<head>.*\S)\s+[-|\u2013\u2014]\s+(?P<tail>\S.*)$")
_ATTRIBUTION_MAX_CHARS = 45
_ATTRIBUTION_MAX_WORDS = 6

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)

_SELECT_DUE_SOURCES = """
SELECT s.*
FROM sources s
JOIN topics t ON t.id = s.topic_id
WHERE s.enabled = 1 AND t.enabled = 1
ORDER BY s.id
"""

_INSERT_ITEM = """
INSERT OR IGNORE INTO items (
    source_id, topic_id, url, canonical_url, title, title_norm,
    summary_raw, author, source_name, published_at, fetched_at, content_hash
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPDATE_SOURCE = """
UPDATE sources SET
    last_status          = ?,
    last_error           = ?,
    consecutive_failures = ?,
    enabled              = ?,
    last_fetch_at        = COALESCE(?, last_fetch_at),
    config               = COALESCE(?, config)
WHERE id = ?
"""

_INSERT_FETCH_LOG = """
INSERT INTO fetch_log (source_id, run_at, status, item_count, error)
VALUES (?, ?, ?, ?, ?)
"""


def strip_attribution(title: str) -> str:
    """Remove a trailing source credit, keeping the headline itself.

    Deliberately conservative. A scoreline such as `Rangers 2 - 1 Celtic` must
    survive, so the tail has to start with a letter and read like a publisher
    name rather than like the rest of a sentence.
    """
    match = _ATTRIBUTION.match(title)
    if match is None:
        return title
    tail = match.group("tail").strip()
    head = match.group("head").strip()
    if not head or len(tail) > _ATTRIBUTION_MAX_CHARS:
        return title
    if not tail[0].isalpha() or len(tail.split()) > _ATTRIBUTION_MAX_WORDS:
        return title
    return head


def normalize_title(title: str) -> str:
    """Lowercase, drop the source credit and punctuation, collapse whitespace.

    This is the dedupe key. Keying on URL alone lets a syndicated wire story in
    a dozen times under a dozen distinct links.
    """
    normalized = _PUNCTUATION.sub(" ", strip_attribution(title.strip()).lower())
    collapsed = " ".join(normalized.split())
    return collapsed or " ".join(title.lower().split())


def content_hash(title_norm: str, url: str, canonical_url: str | None = None) -> str:
    """Hash the normalized title plus the publishing host.

    With `UNIQUE (topic_id, content_hash)` this is the primary defence against
    reinserting the same item on every run. The host is part of the hash so
    that the same headline from two outlets stays two rows: collapsing those is
    the clustering stage's job, and it needs to see them.
    """
    host = urlsplit(canonical_url or url).netloc.lower().removeprefix("www.")
    return sha256(f"{title_norm}\n{host}".encode()).hexdigest()


def _published_iso(item: RawItem, now: datetime) -> str:
    """Resolve `published_at`, which feeds get wrong constantly.

    Missing dates fall back to fetch time. Dates in the future are clamped,
    because a feed that claims tomorrow would otherwise pin itself to the top
    of every brief.
    """
    published = item.published_at
    if published is None:
        return to_iso_utc(now)
    aware = published if published.tzinfo is not None else published.replace(tzinfo=UTC)
    return to_iso_utc(min(aware, now))


def _short(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_ERROR_CHARS:
        return collapsed
    return f"{collapsed[:MAX_ERROR_CHARS]}..."


def _item_row(source: Source, item: RawItem, now: datetime) -> tuple[object, ...] | None:
    title = " ".join(item.title.split())
    if not title or not item.url:
        return None
    title_norm = normalize_title(title)
    if not title_norm:
        return None
    return (
        source.id,
        source.topic_id,
        item.url,
        item.canonical_url,
        title,
        title_norm,
        item.summary,
        item.author,
        item.source_name,
        _published_iso(item, now),
        to_iso_utc(now),
        content_hash(title_norm, item.url, item.canonical_url),
    )


def load_due_sources(conn: sqlite3.Connection) -> list[Source]:
    """Every enabled source belonging to an enabled topic."""
    return [Source.from_row(row) for row in conn.execute(_SELECT_DUE_SOURCES)]


def _since(source: Source, now: datetime) -> datetime:
    """Fetch from the last success, or from the lookback window on a new source.

    `last_fetch_at` only advances on success, so a source that has been failing
    picks up where it last worked rather than skipping the gap.
    """
    last = parse_timestamp(source.last_fetch_at)
    return last if last is not None else now - timedelta(hours=LOOKBACK_HOURS)


def _config_json(source: Source, config: BaseModel | None) -> str | None:
    """Serialize the config only when the adapter changed it, such as an ETag."""
    if config is None:
        return None
    updated = config.model_dump(mode="json")
    if updated == source.config:
        return None
    return json.dumps(updated, sort_keys=True)


def _persist(
    conn: sqlite3.Connection,
    source: Source,
    status: FetchStatus,
    now: datetime,
    *,
    error: str | None = None,
    items: Sequence[RawItem] = (),
    config: BaseModel | None = None,
) -> FetchResult:
    """Write the items and the source outcome in one short transaction.

    Everything slow already happened. No HTTP call is ever held open across
    this, which is what keeps three pods on one SQLite file workable.
    """
    rows = [row for row in (_item_row(source, item, now) for item in items) if row is not None]
    config_json = _config_json(source, config)

    succeeded = status in ("ok", "not_modified")
    if status == "error":
        failures = source.consecutive_failures + 1
    elif succeeded:
        failures = 0
    else:
        failures = source.consecutive_failures
    enabled = source.enabled and failures < MAX_CONSECUTIVE_FAILURES

    with db.write_transaction(conn):
        # total_changes rather than cursor.rowcount: an INSERT OR IGNORE that
        # ignores leaves rowcount reporting the previous statement's count.
        before = conn.total_changes
        conn.executemany(_INSERT_ITEM, rows)
        inserted = conn.total_changes - before
        conn.execute(
            _UPDATE_SOURCE,
            (
                status,
                error,
                failures,
                int(enabled),
                to_iso_utc(now) if succeeded else None,
                config_json,
                source.id,
            ),
        )
        conn.execute(_INSERT_FETCH_LOG, (source.id, to_iso_utc(now), status, inserted, error))

    if source.enabled and not enabled:
        log.error(
            "source auto disabled after repeated failures",
            source_id=source.id,
            source_type=source.type,
            consecutive_failures=failures,
            last_error=error,
        )

    return FetchResult(source_id=source.id, status=status, item_count=inserted, error=error)


def collect_source(
    conn: sqlite3.Connection,
    client: httpx.Client,
    source: Source,
    now: datetime,
) -> FetchResult:
    """Fetch one source and record the outcome. Never raises."""
    bound = log.bind(source_id=source.id, source_type=source.type, topic_id=source.topic_id)

    adapter = get_adapter(source.type, client)
    if adapter is None:
        bound.warning("no adapter registered for this source type")
        return _persist(
            conn,
            source,
            "skipped",
            now,
            error=f"no adapter registered for source type {source.type!r}",
        )

    try:
        config = adapter.ConfigModel.model_validate(source.config)
    except ValidationError as exc:
        bound.warning("source config is not valid", error=str(exc))
        return _persist(conn, source, "error", now, error=_short(str(exc)))

    since = _since(source, now)
    try:
        items = adapter.fetch(config, since)
    except NotModifiedError:
        bound.info("source unchanged since the last fetch")
        return _persist(conn, source, "not_modified", now, config=config)
    except Exception as exc:
        bound.warning("source fetch failed", exc_info=True)
        return _persist(conn, source, "error", now, error=_short(f"{type(exc).__name__}: {exc}"))

    result = _persist(conn, source, "ok", now, items=items, config=config)
    bound.info("source fetched", fetched=len(items), stored=result.item_count)
    return result


def run(settings: Settings) -> int:
    """Fetch all enabled sources. Returns the process exit code."""
    try:
        conn = db.connect(settings.db_path)
    except (sqlite3.Error, OSError):
        log.exception("could not open the database", db_path=str(settings.db_path))
        return 1

    started = datetime.now(UTC)
    counts: dict[str, int] = {"ok": 0, "error": 0, "skipped": 0, "not_modified": 0}
    stored = 0

    try:
        sources = load_due_sources(conn)
        log.info("collector starting", source_count=len(sources))
        with build_client(settings) as client:
            for source in sources:
                result = collect_source(conn, client, source, datetime.now(UTC))
                counts[result.status] = counts.get(result.status, 0) + 1
                stored += result.item_count
    finally:
        conn.close()

    log.info(
        "collector finished",
        stored=stored,
        elapsed_seconds=round((datetime.now(UTC) - started).total_seconds(), 2),
        **counts,
    )
    return 0
