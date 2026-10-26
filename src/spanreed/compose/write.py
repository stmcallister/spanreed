"""Stage 4: turn the selected items into the brief itself.

One call, everything grouped by topic. Sonnet rather than Haiku, because this
is the only part of the output a person actually reads and the difference shows.

The style rules are firm, and the two that can be checked mechanically are
checked mechanically. The item cap is applied before this stage ever runs, and
the ban on em dashes and double hyphens is enforced on the way out. A prompt is
a request. Neither of those is left as a request.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import structlog

from spanreed.compose.filter import Scored
from spanreed.llm import LlmClient
from spanreed.models import Item, Topic

log = structlog.get_logger(__name__)

WRITE_MAX_TOKENS = 4000

MAX_MATERIAL_CHARS = 1500
"""Per item. The deep read is already capped, this bounds the feed summaries."""

SYSTEM = """You write a short personal news brief. You are given items that
have already been chosen. Write about all of them and only them.

Rules:
- Lead with what happened. Never open with framing such as "in a recent
  development" or "in a move that".
- One or two sentences per item. Three at most, and only for an item that comes
  with article text rather than a summary.
- Invent nothing. If the material does not say it, it does not appear. No
  speculation about what something means or what happens next.
- Never use an em dash or a double hyphen. Use commas, colons, parentheses, or
  separate sentences.
- No preamble, no sign off, no horizontal rules, no closing summary.

Format as markdown. Each topic is a level two heading, in the order given. Each
item is a bullet whose first element is a markdown link on a short headline,
then the sentences. Write nothing else."""

# A line that is only dashes is a horizontal rule, which is punctuation for the
# document rather than for a sentence. Everything else gets scrubbed.
_RULE_LINE = re.compile(r"^\s*-{3,}\s*$")
_DASHES = re.compile(r"\s*(?:[\u2014\u2013]|-{2,})\s*")


def scrub_dashes(markdown: str) -> str:
    """Remove em dashes, en dashes, and double hyphens from generated prose.

    The model is told not to produce these and mostly does not. Mostly is not
    the standard, and this is one of the few style rules that a few lines of
    code can guarantee outright.
    """
    return "\n".join(
        line if _RULE_LINE.match(line) else _DASHES.sub(", ", line) for line in markdown.split("\n")
    )


def order_topics(topics: Sequence[Topic]) -> list[Topic]:
    """Sports results first, then everything else by priority."""
    return sorted(topics, key=lambda topic: (topic.kind != "team", -topic.priority, topic.name))


def group_by_topic(selected: Sequence[Scored]) -> list[tuple[Topic, list[Scored]]]:
    """Selected items bucketed into the order the brief presents them in."""
    buckets: dict[int, list[Scored]] = {}
    topics: dict[int, Topic] = {}
    for entry in selected:
        buckets.setdefault(entry.topic.id, []).append(entry)
        topics[entry.topic.id] = entry.topic
    return [(topic, buckets[topic.id]) for topic in order_topics(list(topics.values()))]


def _material(item: Item, extracts: Mapping[int, str]) -> tuple[str, str]:
    """The text the writer works from, and where it came from."""
    extract = extracts.get(item.id)
    if extract:
        return "article text", extract[:MAX_MATERIAL_CHARS]
    summary = " ".join((item.summary_raw or "").split())
    return "feed summary", summary[:MAX_MATERIAL_CHARS]


def build_prompt(grouped: Sequence[tuple[Topic, list[Scored]]], extracts: Mapping[int, str]) -> str:
    """Everything the writer is allowed to use, and nothing else."""
    sections: list[str] = []
    for topic, entries in grouped:
        lines = [f"## {topic.name}"]
        for entry in entries:
            item = entry.item
            kind, material = _material(item, extracts)
            lines.append(
                f"\ntitle: {item.title}\n"
                f"url: {item.best_url}\n"
                f"source: {item.source_name or 'unknown'}\n"
                f"{kind}: {material or '(none, use the title only)'}"
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def run(
    llm: LlmClient,
    grouped: Sequence[tuple[Topic, list[Scored]]],
    extracts: Mapping[int, str],
) -> str:
    """Write the brief body. Raises if the call fails, so the caller can degrade."""
    markdown = llm.write(
        system=SYSTEM,
        prompt=build_prompt(grouped, extracts),
        max_tokens=WRITE_MAX_TOKENS,
    )
    return scrub_dashes(markdown).strip()


def fallback_markdown(grouped: Sequence[tuple[Topic, list[Scored]]]) -> str:
    """The degraded body: a plain list of what was selected, written by no one.

    A brief that is a list of headlines is worse than a brief that reads well
    and better than no brief at all.
    """
    sections: list[str] = []
    for topic, entries in grouped:
        lines = [f"## {topic.name}", ""]
        for entry in entries:
            item = entry.item
            source = f" ({item.source_name})" if item.source_name else ""
            lines.append(f"- [{item.title}]({item.best_url}){source}")
        sections.append("\n".join(lines))
    return scrub_dashes("\n\n".join(sections)).strip()


def render_footer(failed_sources: Sequence[str], notes: Sequence[str] = ()) -> str:
    """The whole observability story, and it is sufficient.

    One line naming anything that failed in the last day. If it is empty, the
    line says so, because a footer that disappears when things are fine is a
    footer nobody notices when things are not.
    """
    lines = ["", "---", ""]
    if failed_sources:
        lines.append(f"Sources that failed in the last 24 hours: {', '.join(failed_sources)}.")
    else:
        lines.append("All sources fetched cleanly in the last 24 hours.")
    lines.extend(f"Note: {note}" for note in notes)
    return "\n".join(lines)
