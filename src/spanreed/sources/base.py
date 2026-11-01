"""The source adapter protocol and the helpers every adapter shares.

An adapter takes a validated config and returns `RawItem`s. It never touches
the database: normalization and persistence happen once, centrally, in
`collect.py`. That separation is what stops a restructured feed from being able
to write anything bad.

An adapter raising an unhandled exception is a bug. Parsing here is defensive
throughout, so a truncated or restructured response yields fewer items rather
than an error.
"""

from __future__ import annotations

import calendar
import time
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol

import feedparser
import httpx
import structlog
from pydantic import BaseModel, ConfigDict
from selectolax.lexbor import LexborHTMLParser

from spanreed.config import Settings
from spanreed.models import RawItem

log = structlog.get_logger(__name__)

FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml;q=0.9, application/xml;q=0.8,"
    " text/xml;q=0.8, */*;q=0.5"
)


BOOKKEEPING_FIELDS = frozenset({"etag", "last_modified"})
"""Written by the collector after a fetch. Cache state, not user settings.

The source editor hides these, and exporting configuration leaves them out,
because an ETag is worth nothing to a database being restored from YAML.
"""


# Every adapter names its own class attribute `type`, which shadows the builtin
# inside those class bodies. Annotations use this alias instead.
ConfigModelType = type[BaseModel]


class SourceConfig(BaseModel):
    """Base for every adapter config model.

    Extras are forbidden so that a typo in the source editor surfaces as a
    validation error instead of a setting that silently does nothing.
    """

    model_config = ConfigDict(extra="forbid")


class NotModifiedError(Exception):
    """The server answered 304, so there is nothing new to parse.

    Not a failure. The collector records it and leaves the failure counter
    alone.
    """


class SourceAdapter(Protocol):
    """What `collect.py` requires of a source type.

    `fetch` is typed against `BaseModel` rather than the concrete config so
    that every adapter satisfies the same protocol. Implementations narrow the
    argument on entry.
    """

    type: ClassVar[str]
    ConfigModel: ClassVar[ConfigModelType]

    def __init__(self, client: httpx.Client) -> None: ...

    def fetch(self, config: BaseModel, since: datetime) -> list[RawItem]: ...


def build_client(settings: Settings) -> httpx.Client:
    """One client for the whole collector run, so connections are reused.

    The descriptive User-Agent is not optional. Reddit blocks requests without
    one, and several other hosts throttle them.
    """
    return httpx.Client(
        headers={"User-Agent": settings.user_agent, "Accept": FEED_ACCEPT},
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        follow_redirects=True,
    )


def strip_html(value: str | None) -> str | None:
    """Flatten a feed summary to text, since most of them carry markup."""
    if not value:
        return None
    text = value
    if "<" in text:
        try:
            text = LexborHTMLParser(text).text(separator=" ")
        except Exception:
            log.debug("could not parse summary markup, using it verbatim", exc_info=True)
            text = value
    collapsed = " ".join(text.split())
    return collapsed or None


def to_datetime(parsed: time.struct_time | None) -> datetime | None:
    """Convert a feedparser time tuple, which is always UTC, to an aware datetime."""
    if parsed is None:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def drop_older_than(items: list[RawItem], since: datetime) -> list[RawItem]:
    """Discard items already known to predate `since`.

    An item with no date is kept. Missing dates are common and are not evidence
    that the item is old. The UNIQUE constraint on `content_hash` is what stops
    those from being reinserted every run.
    """
    return [item for item in items if item.published_at is None or item.published_at >= since]


def _entry_link(entry: Any) -> str | None:
    link = entry.get("link")
    if isinstance(link, str) and link.strip():
        return link.strip()
    for candidate in entry.get("links") or []:
        href = candidate.get("href") if isinstance(candidate, dict) else None
        if isinstance(href, str) and href.strip():
            return href.strip()
    return None


def _entry_source_name(entry: Any, feed_title: str | None) -> str | None:
    """Prefer the per-entry publisher, which is how Google News names outlets."""
    source = entry.get("source")
    if isinstance(source, dict):
        name = source.get("title")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return feed_title


MAX_SYNTHETIC_TITLE_CHARS = 110
"""A synthesized title is a headline, not the whole post."""


def title_from_summary(summary: str | None) -> str | None:
    """Make a headline out of a post that has no title of its own.

    Bluesky publishes every post with an empty `<title>`, so without this the
    generic parser drops a whole feed on the floor. The first sentence, or the
    first hundred or so characters on a word boundary, reads like a headline
    often enough to be worth it.
    """
    if not summary:
        return None
    text = " ".join(summary.split())
    if not text:
        return None

    sentence, separator, _ = text.partition(". ")
    if separator and len(sentence) <= MAX_SYNTHETIC_TITLE_CHARS:
        return sentence

    if len(text) <= MAX_SYNTHETIC_TITLE_CHARS:
        return text
    clipped = text[:MAX_SYNTHETIC_TITLE_CHARS]
    head, space, _ = clipped.rpartition(" ")
    return f"{head if space else clipped}..."


def _entry_to_raw_item(
    entry: Any, feed_title: str | None, synthesize_title: bool = False
) -> RawItem | None:
    url = _entry_link(entry)
    if not url:
        return None

    summary = strip_html(entry.get("summary"))
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        title = title_from_summary(summary) if synthesize_title else None
    if not title:
        return None

    author = entry.get("author")
    return RawItem(
        url=url,
        title=" ".join(title.split()),
        summary=summary,
        author=author.strip() if isinstance(author, str) and author.strip() else None,
        source_name=_entry_source_name(entry, feed_title),
        published_at=to_datetime(entry.get("published_parsed") or entry.get("updated_parsed")),
    )


def parse_feed(payload: bytes, *, synthesize_title: bool = False) -> list[RawItem]:
    """Parse an RSS or Atom document into raw items, skipping anything unusable.

    feedparser recovers entries from documents that are not well formed, so a
    truncated response usually still yields the entries that arrived. Whatever
    it cannot make sense of is dropped one entry at a time.

    `synthesize_title` is for feeds whose entries carry no title at all. It is
    opt in, because for an ordinary feed a missing title means a broken entry
    and dropping it is right.
    """
    parsed = feedparser.parse(payload)
    if parsed.get("bozo"):
        log.debug("feed is not well formed", detail=str(parsed.get("bozo_exception")))

    feed = parsed.get("feed") or {}
    feed_title = feed.get("title") if isinstance(feed, dict) else None
    if not isinstance(feed_title, str) or not feed_title.strip():
        feed_title = None
    else:
        feed_title = feed_title.strip()

    items: list[RawItem] = []
    for entry in parsed.get("entries") or []:
        try:
            item = _entry_to_raw_item(entry, feed_title, synthesize_title)
        except Exception:
            log.warning("skipping an entry that could not be read", exc_info=True)
            continue
        if item is not None:
            items.append(item)
    return items
