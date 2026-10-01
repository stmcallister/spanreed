"""Bootstrap an empty database, and export a full one back to YAML.

The database is the source of truth for topics. This file exists for the two
moments when it is not: the first start, when there is nothing to be the truth
yet, and the recovery after a lost volume, when the truth has to come from
somewhere.

Seeding happens once, on a database with no topics at all. Once the UI writes a
topic the YAML is stale and irrelevant, so it is never consulted again and
never merged. Exporting is the other direction and is how the configuration
becomes recoverable: `newsbrief export-topics` writes the live topics and
sources to stdout as YAML that this same module can read back.

Nothing here writes to git. A running application that pushes to a repository
is a running application that can corrupt one.
"""

from __future__ import annotations

import json
import sqlite3
from io import StringIO
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from newsbrief import db
from newsbrief.models import TopicKind, slugify
from newsbrief.sources import CONFIG_MODELS
from newsbrief.sources.base import BOOKKEEPING_FIELDS

log = structlog.get_logger(__name__)


class SeedSource(BaseModel):
    """One source in the seed file."""

    model_config = ConfigDict(extra="forbid")

    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SeedTopic(BaseModel):
    """One topic in the seed file, with the sources that feed it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: TopicKind
    relevance_hint: str
    slug: str | None = None
    enabled: bool = True
    max_items: int = Field(default=3, ge=1, le=15)
    priority: int = Field(default=50, ge=1, le=100)
    sources: list[SeedSource] = Field(default_factory=list)

    @field_validator("name", "relevance_hint")
    @classmethod
    def _require_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    def resolved_slug(self) -> str:
        return self.slug or slugify(self.name)


class SeedFile(BaseModel):
    """The document itself."""

    model_config = ConfigDict(extra="forbid")

    topics: list[SeedTopic] = Field(default_factory=list)


def load(path: Path) -> SeedFile | None:
    """Read and validate the seed file, or None when there is nothing usable.

    A missing file is the normal case for a database that has already been
    configured. A malformed one is reported and then ignored, because failing
    to start is a worse answer than starting with no topics.
    """
    if not path.is_file():
        log.info("no seed file, nothing to bootstrap", seed_path=str(path))
        return None

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.error("seed file could not be read", seed_path=str(path), error=str(exc))
        return None

    if raw is None:
        return SeedFile()

    try:
        return SeedFile.model_validate(raw)
    except ValidationError as exc:
        log.error("seed file is not valid", seed_path=str(path), error=str(exc))
        return None


def _valid_sources(topic: SeedTopic) -> list[tuple[SeedSource, dict[str, Any]]]:
    """Drop sources the adapters would reject, and normalize the ones they accept.

    One mistyped source should cost that source, not the topic it belongs to
    and not the startup of the process reading the file.

    Each surviving config is the one its own model produced, not the text that
    was in the file. Validation is where `r/rust` becomes `rust` and a pasted
    profile URL becomes a handle, and the source editor already stores configs
    that way. A seed file that stored them differently would put the database
    into a state the UI can never produce.
    """
    kept: list[tuple[SeedSource, dict[str, Any]]] = []
    for source in topic.sources:
        model = CONFIG_MODELS.get(source.type)
        if model is None:
            log.warning(
                "seed source has no adapter, skipping it",
                topic=topic.name,
                source_type=source.type,
            )
            continue
        try:
            validated = model.model_validate(source.config)
        except ValidationError as exc:
            log.warning(
                "seed source config is not valid, skipping it",
                topic=topic.name,
                source_type=source.type,
                error=str(exc),
            )
            continue
        normalized: dict[str, Any] = validated.model_dump(mode="json", exclude_none=True)
        kept.append((source, normalized))
    return kept


def is_empty(conn: sqlite3.Connection) -> bool:
    """True when no topic has ever been created."""
    row = conn.execute("SELECT COUNT(*) AS n FROM topics").fetchone()
    return int(row["n"]) == 0


def bootstrap(conn: sqlite3.Connection, path: Path) -> int:
    """Seed an empty database. Returns the number of topics created.

    Does nothing at all once a single topic exists, which is what keeps this
    from ever fighting the UI for ownership of the configuration.

    The emptiness check happens inside the write transaction, so three
    workloads starting at once cannot each decide the database is empty and
    seed it three times. The second and third find the topics already there.
    """
    seed = load(path)
    if seed is None or not seed.topics:
        return 0

    created = 0
    with db.write_transaction(conn):
        if not is_empty(conn):
            log.info("database already has topics, leaving the seed file alone")
            return 0

        for topic in seed.topics:
            cursor = conn.execute(
                "INSERT INTO topics (name, slug, kind, enabled, relevance_hint,"
                " max_items, priority) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    topic.name,
                    topic.resolved_slug(),
                    topic.kind,
                    int(topic.enabled),
                    topic.relevance_hint,
                    topic.max_items,
                    topic.priority,
                ),
            )
            topic_id = int(cursor.lastrowid or 0)
            created += 1

            for source, config in _valid_sources(topic):
                conn.execute(
                    "INSERT INTO sources (topic_id, type, config, enabled) VALUES (?, ?, ?, ?)",
                    (
                        topic_id,
                        source.type,
                        json.dumps(config, sort_keys=True),
                        int(source.enabled),
                    ),
                )

    log.info("database bootstrapped from the seed file", topics=created, seed_path=str(path))
    return created


# --- exporting --------------------------------------------------------------

_SELECT_TOPICS = "SELECT * FROM topics ORDER BY name"
_SELECT_SOURCES = "SELECT * FROM sources WHERE topic_id = ? ORDER BY type, id"


def _literal_block(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    """Render a multi-line string as a block, so a person can read it back.

    A relevance hint is the longest and most important text in the file. Folded
    into one quoted line with escapes it is unreadable, and unreadable
    configuration does not get corrected.
    """
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


class _Dumper(yaml.SafeDumper):
    """A dumper that keeps long text readable."""


_Dumper.add_representer(str, _literal_block)


def export(conn: sqlite3.Connection) -> str:
    """Dump every topic and source as YAML this module can read back.

    Cache bookkeeping is left out. An ETag is worth nothing to a database being
    restored from this file, and it would go stale the moment it was written.
    """
    topics: list[dict[str, Any]] = []
    for row in conn.execute(_SELECT_TOPICS):
        sources: list[dict[str, Any]] = []
        for source_row in conn.execute(_SELECT_SOURCES, (row["id"],)):
            config = {
                key: value
                for key, value in json.loads(source_row["config"]).items()
                if key not in BOOKKEEPING_FIELDS
            }
            entry: dict[str, Any] = {"type": source_row["type"], "config": config}
            if not source_row["enabled"]:
                entry["enabled"] = False
            sources.append(entry)

        topic: dict[str, Any] = {
            "name": row["name"],
            "slug": row["slug"],
            "kind": row["kind"],
            "relevance_hint": row["relevance_hint"],
            "max_items": int(row["max_items"]),
            "priority": int(row["priority"]),
        }
        if not row["enabled"]:
            topic["enabled"] = False
        topic["sources"] = sources
        topics.append(topic)

    stream = StringIO()
    stream.write("# Exported by `newsbrief export-topics`.\n")
    stream.write("# The database is the source of truth. This is a copy of it.\n")
    yaml.dump(
        {"topics": topics},
        stream,
        Dumper=_Dumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=88,
    )
    return stream.getvalue()
