"""Domain objects.

Plain dataclasses with explicit constructors from `sqlite3.Row`. The schema is
small enough that SQL is clearer than an ORM, so these types exist to give the
rest of the code something typed to pass around, nothing more.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

TopicKind = Literal["team", "tech"]
FetchStatus = Literal["ok", "error", "skipped", "not_modified"]


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """A URL safe name for a topic.

    Uniqueness is the database's job, not this function's, so two topics whose
    names slug the same way collide on the UNIQUE constraint rather than being
    silently renamed here.
    """
    slug = _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")
    return slug or "topic"


@dataclass(slots=True)
class Topic:
    """A thing the brief follows, such as a club or a technology subject."""

    id: int
    name: str
    slug: str
    kind: TopicKind
    enabled: bool
    relevance_hint: str
    max_items: int
    priority: int
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Topic:
        kind = row["kind"]
        if kind not in ("team", "tech"):
            raise ValueError(f"unexpected topic kind in database: {kind!r}")
        return cls(
            id=int(row["id"]),
            name=row["name"],
            slug=row["slug"],
            kind=kind,
            enabled=bool(row["enabled"]),
            relevance_hint=row["relevance_hint"],
            max_items=int(row["max_items"]),
            priority=int(row["priority"]),
            created_at=row["created_at"],
        )


@dataclass(slots=True)
class Source:
    """One feed or API endpoint belonging to a topic.

    `config` is the decoded JSON blob whose shape depends on `type`. Each
    adapter validates it against its own Pydantic model.
    """

    id: int
    topic_id: int
    type: str
    config: dict[str, object]
    enabled: bool
    last_fetch_at: str | None
    last_status: str | None
    last_error: str | None
    consecutive_failures: int
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Source:
        return cls(
            id=int(row["id"]),
            topic_id=int(row["topic_id"]),
            type=row["type"],
            config=json.loads(row["config"]),
            enabled=bool(row["enabled"]),
            last_fetch_at=row["last_fetch_at"],
            last_status=row["last_status"],
            last_error=row["last_error"],
            consecutive_failures=int(row["consecutive_failures"]),
            created_at=row["created_at"],
        )


@dataclass(slots=True)
class RawItem:
    """What an adapter returns. Not yet normalized and not yet stored.

    Adapters never touch the database. `collect.py` turns these into `Item`
    rows, which is where title normalization and hashing happen.
    """

    url: str
    title: str
    summary: str | None = None
    author: str | None = None
    source_name: str | None = None
    published_at: datetime | None = None
    canonical_url: str | None = None
    """Set only by adapters whose `url` is a redirect, notably google_news."""


@dataclass(slots=True)
class Item:
    """A stored, normalized item."""

    id: int
    source_id: int
    topic_id: int
    url: str
    canonical_url: str | None
    title: str
    title_norm: str
    summary_raw: str | None
    author: str | None
    source_name: str | None
    published_at: str | None
    fetched_at: str
    content_hash: str
    cluster_id: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Item:
        return cls(
            id=int(row["id"]),
            source_id=int(row["source_id"]),
            topic_id=int(row["topic_id"]),
            url=row["url"],
            canonical_url=row["canonical_url"],
            title=row["title"],
            title_norm=row["title_norm"],
            summary_raw=row["summary_raw"],
            author=row["author"],
            source_name=row["source_name"],
            published_at=row["published_at"],
            fetched_at=row["fetched_at"],
            content_hash=row["content_hash"],
            cluster_id=row["cluster_id"],
        )

    @property
    def best_url(self) -> str:
        """The publisher URL when link resolution succeeded, else the raw link."""
        return self.canonical_url or self.url


@dataclass(slots=True)
class Brief:
    """One generated brief."""

    id: int
    brief_date: str
    generated_at: str
    markdown: str
    html: str
    item_count: int
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    degraded: bool = False
    notes: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Brief:
        return cls(
            id=int(row["id"]),
            brief_date=row["brief_date"],
            generated_at=row["generated_at"],
            markdown=row["markdown"],
            html=row["html"],
            item_count=int(row["item_count"]),
            model=row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            degraded=bool(row["degraded"]),
            notes=row["notes"],
        )


@dataclass(slots=True)
class BriefItem:
    """An item selected for a brief, with its rank and generated blurb."""

    brief_id: int
    item_id: int
    rank: int
    blurb: str | None = None


@dataclass(slots=True)
class FetchResult:
    """The outcome of fetching one source, written to `fetch_log`."""

    source_id: int
    status: FetchStatus
    item_count: int = 0
    error: str | None = None


@dataclass(slots=True)
class Cluster:
    """A group of items that tell the same story, plus its representative."""

    topic_id: int
    representative: Item
    members: list[Item] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)
