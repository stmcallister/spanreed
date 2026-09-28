"""Stage 2: the relevance filter. The load bearing stage.

Keyword search invites noise. A topic named after a common word pulls in
unrelated news no matter how the query is written, so something has to read the
headline and decide whether it is actually about the thing. That judgement is
the only part of gathering a brief that a model does.

Haiku, because this is classification and nothing else. Structured output,
because a model asked for JSON in a free text response eventually answers with
prose and a regex is not a parser.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import structlog
from pydantic import BaseModel

from newsbrief.compose.cluster import (
    DEFAULT_SOURCE_PRIORITY,
    SOURCE_TYPE_PRIORITY,
)
from newsbrief.llm import LlmClient
from newsbrief.models import Cluster, Item, Topic

log = structlog.get_logger(__name__)

BATCH_SIZE = 40
"""Cluster representatives per call, grouped by topic."""

MIN_SCORE = 0.5
"""Below this the model is not confident the item is on topic."""

BASELINE_PRIORITY = 50
"""The default topic priority, so weighting is neutral at the default."""

MAX_SUMMARY_CHARS = 300
"""Enough for the filter to judge, short enough to keep the batch cheap."""

FILTER_MAX_TOKENS = 4096

SYSTEM = """You decide whether news items belong in a personal daily brief.

You are given one topic, a description of what the reader considers relevant to
that topic, and a numbered list of items. Judge each item on its own.

Keep an item only when it is genuinely about the topic as the reader described
it. A passing mention is not enough. Items that merely share a word with the
topic name, such as a business or a place with a similar name, are not relevant.

Score your confidence that the reader wants to see the item, from 0.0 to 1.0.
Give a reason of at most six words. Return a verdict for every item you are
given, using the id supplied with each one."""

_SELECT_TOPICS = "SELECT * FROM topics WHERE enabled = 1"


class Verdict(BaseModel):
    """One item's judgement. This is the model's output shape."""

    id: int
    keep: bool
    score: float
    reason: str


class Verdicts(BaseModel):
    """Structured output needs an object at the root, so the list is wrapped."""

    verdicts: list[Verdict]


@dataclass(slots=True)
class Scored:
    """A representative the filter kept, with the score that ranks it."""

    item: Item
    topic: Topic
    score: float
    reason: str

    @property
    def weighted(self) -> float:
        """Score scaled by how much the reader cares about the topic."""
        return self.score * (self.topic.priority / BASELINE_PRIORITY)


@dataclass(slots=True)
class FilterOutcome:
    """What the stage produced, and whether anything went wrong producing it."""

    selected: list[Scored] = field(default_factory=list)
    considered: int = 0
    kept: int = 0
    failed_batches: int = 0

    @property
    def degraded(self) -> bool:
        return self.failed_batches > 0


class FilterError(RuntimeError):
    """Every batch failed, so there is no judgement to rank."""


def load_topics(conn: sqlite3.Connection) -> dict[int, Topic]:
    """Enabled topics by id. The filter needs the hint, the ranking needs the rest."""
    return {int(row["id"]): Topic.from_row(row) for row in conn.execute(_SELECT_TOPICS)}


def _batches(items: Sequence[Item], size: int = BATCH_SIZE) -> list[list[Item]]:
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


def _describe(item: Item) -> str:
    """One item as the filter sees it."""
    lines = [f"id: {item.id}", f"title: {item.title}"]
    if item.source_name:
        lines.append(f"source: {item.source_name}")
    if item.summary_raw:
        summary = " ".join(item.summary_raw.split())
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = f"{summary[:MAX_SUMMARY_CHARS]}..."
        lines.append(f"summary: {summary}")
    return "\n".join(lines)


def build_prompt(topic: Topic, items: Sequence[Item]) -> str:
    """The user message for one batch.

    The hint is the reader's own words about what counts, and it is the most
    important input the application has.
    """
    blocks = "\n\n".join(_describe(item) for item in items)
    return (
        f"Topic: {topic.name}\n"
        f"What the reader considers relevant: {topic.relevance_hint}\n\n"
        f"Items:\n\n{blocks}"
    )


def _clamp(score: float) -> float:
    return min(1.0, max(0.0, score))


def judge_batch(llm: LlmClient, topic: Topic, items: Sequence[Item]) -> list[Scored]:
    """Score one batch. Raises if the call fails, so the caller can count it."""
    verdicts = llm.classify(
        system=SYSTEM,
        prompt=build_prompt(topic, items),
        schema=Verdicts,
        max_tokens=FILTER_MAX_TOKENS,
    )

    by_id = {item.id: item for item in items}
    kept: list[Scored] = []
    for verdict in verdicts.verdicts:
        item = by_id.get(verdict.id)
        if item is None:
            # A hallucinated id is not worth failing the batch over.
            log.warning("filter returned an id that was not in the batch", item_id=verdict.id)
            continue
        score = _clamp(verdict.score)
        if not verdict.keep or score < MIN_SCORE:
            continue
        kept.append(Scored(item=item, topic=topic, score=score, reason=verdict.reason.strip()))
    return kept


def by_weighted_score(scored: Iterable[Scored]) -> list[Scored]:
    """Rank by score scaled by topic priority, newest first within a tie."""
    by_recency = sorted(
        scored, key=lambda entry: (entry.item.published_at or "", entry.item.id), reverse=True
    )
    return sorted(by_recency, key=lambda entry: -entry.weighted)


def select(ordered: Iterable[Scored], max_items: int) -> list[Scored]:
    """Apply the caps to an already ranked list, keeping that order.

    Ranking is the caller's, because the degraded path ranks differently. What
    happens here is only the two limits: each topic's `max_items` as the list is
    built, and the hard cap on the brief as a whole. One busy topic cannot crowd
    everything else out.
    """
    per_topic: dict[int, int] = {}
    chosen: list[Scored] = []
    for entry in ordered:
        if len(chosen) >= max_items:
            break
        taken = per_topic.get(entry.topic.id, 0)
        if taken >= entry.topic.max_items:
            continue
        per_topic[entry.topic.id] = taken + 1
        chosen.append(entry)
    return chosen


def run(
    llm: LlmClient,
    clusters: Sequence[Cluster],
    topics: Mapping[int, Topic],
    max_items: int,
) -> FilterOutcome:
    """Judge every cluster representative and return the selection.

    A batch that fails is counted and its items are dropped, which shortens the
    brief and marks it degraded. Only a total failure raises, because at that
    point there is nothing to rank and the caller must fall back.
    """
    outcome = FilterOutcome()
    scored: list[Scored] = []
    attempted = 0

    for topic_id, topic in sorted(topics.items()):
        representatives = [c.representative for c in clusters if c.topic_id == topic_id]
        if not representatives:
            continue
        for batch in _batches(representatives):
            attempted += 1
            outcome.considered += len(batch)
            try:
                scored.extend(judge_batch(llm, topic, batch))
            except Exception:
                outcome.failed_batches += 1
                log.warning(
                    "relevance filter batch failed",
                    topic_id=topic_id,
                    batch_size=len(batch),
                    exc_info=True,
                )

    if attempted and outcome.failed_batches == attempted:
        raise FilterError(f"all {attempted} filter batches failed")

    outcome.kept = len(scored)
    outcome.selected = select(by_weighted_score(scored), max_items)
    log.info(
        "filtered",
        considered=outcome.considered,
        kept=outcome.kept,
        selected=len(outcome.selected),
        failed_batches=outcome.failed_batches,
    )
    return outcome


def fallback_select(
    clusters: Sequence[Cluster],
    topics: Mapping[int, Topic],
    source_types: Mapping[int, str],
    max_items: int,
) -> list[Scored]:
    """The degraded path: top N by source priority and recency, no model.

    Scores are synthetic and exist only so the rest of the pipeline has one
    shape to work with. Ranking here is the best that plain code can do.
    """
    candidates: list[Scored] = []
    for cluster in clusters:
        topic = topics.get(cluster.topic_id)
        if topic is None:
            continue
        item = cluster.representative
        priority = SOURCE_TYPE_PRIORITY.get(
            source_types.get(item.source_id, ""), DEFAULT_SOURCE_PRIORITY
        )
        candidates.append(Scored(item=item, topic=topic, score=priority / 100, reason="unfiltered"))

    return select(by_weighted_score(candidates), max_items)
