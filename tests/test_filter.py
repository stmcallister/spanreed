"""Tests for the relevance filter, with the model stubbed."""

from __future__ import annotations

import sqlite3

import pytest

from spanreed.compose import filter as relevance
from spanreed.models import Cluster, Item
from tests.conftest import StubLlm, add_source, add_topic, make_topic
from tests.test_cluster import make_item

TEAM = make_topic(1, "Midlakes United", kind="team")
TECH = make_topic(2, "Rust", kind="tech")


def make_cluster(item: Item, topic_id: int = 1) -> Cluster:
    return Cluster(topic_id=topic_id, representative=item, members=[item])


def clusters_for(count: int, topic_id: int = 1, start: int = 1) -> list[Cluster]:
    return [
        make_cluster(
            make_item(
                index, f"Story number {index}", f"https://a.example/{index}", topic_id=topic_id
            ),
            topic_id,
        )
        for index in range(start, start + count)
    ]


# --- batching and prompts ---------------------------------------------------


def test_batches_are_capped_at_forty() -> None:
    items = [c.representative for c in clusters_for(95)]
    batches = relevance._batches(items)

    assert [len(batch) for batch in batches] == [40, 40, 15]


def test_the_prompt_carries_the_relevance_hint() -> None:
    """The hint is the most important input the application has."""
    topic = make_topic(1, "Midlakes United", relevance_hint="transfers and match results only")
    prompt = relevance.build_prompt(topic, [c.representative for c in clusters_for(1)])

    assert "transfers and match results only" in prompt
    assert "Midlakes United" in prompt
    assert "id: 1" in prompt


def test_the_prompt_truncates_a_long_summary() -> None:
    item = make_item(1, "A story", "https://a.example/1", summary="x" * 2000)
    prompt = relevance.build_prompt(TEAM, [item])

    assert "x" * relevance.MAX_SUMMARY_CHARS in prompt
    assert "x" * (relevance.MAX_SUMMARY_CHARS + 1) not in prompt


# --- judging ----------------------------------------------------------------


def test_items_the_model_rejects_are_dropped() -> None:
    llm = StubLlm(verdicts={1: (True, 0.9), 2: (False, 0.9)})
    kept = relevance.judge_batch(llm, TEAM, [c.representative for c in clusters_for(2)])

    assert [entry.item.id for entry in kept] == [1]


def test_a_low_score_is_dropped_even_when_kept() -> None:
    """Below 0.5 the model is not confident, and an unread brief is the cost."""
    llm = StubLlm(verdicts={1: (True, 0.49), 2: (True, 0.5)})
    kept = relevance.judge_batch(llm, TEAM, [c.representative for c in clusters_for(2)])

    assert [entry.item.id for entry in kept] == [2]


def test_a_score_outside_the_range_is_clamped() -> None:
    llm = StubLlm(verdicts={1: (True, 4.2)})
    kept = relevance.judge_batch(llm, TEAM, [c.representative for c in clusters_for(1)])

    assert kept[0].score == 1.0


def test_an_id_that_was_not_in_the_batch_is_ignored() -> None:
    """A hallucinated id is not worth failing a batch over."""
    items = [c.representative for c in clusters_for(1)]

    class Inventive(StubLlm):
        def classify(self, system: str, prompt: str, schema: type, max_tokens: int):  # type: ignore[no-untyped-def]
            return schema(verdicts=[{"id": 999, "keep": True, "score": 0.9, "reason": "?"}])

    assert relevance.judge_batch(Inventive(), TEAM, items) == []


# --- ranking and caps -------------------------------------------------------


def test_topic_priority_weights_the_ranking() -> None:
    low = make_topic(1, "Low", priority=25)
    high = make_topic(2, "High", priority=100)
    entries = [
        relevance.Scored(make_item(1, "a", "https://a.example/1"), low, 0.9, ""),
        relevance.Scored(make_item(2, "b", "https://a.example/2"), high, 0.6, ""),
    ]

    assert [e.item.id for e in relevance.by_weighted_score(entries)] == [2, 1]


def test_a_topic_cannot_exceed_its_own_max_items() -> None:
    """One busy topic must not crowd out everything else."""
    topic = make_topic(1, "Busy", max_items=2)
    entries = [
        relevance.Scored(make_item(i, f"s{i}", f"https://a.example/{i}"), topic, 0.9, "")
        for i in range(1, 6)
    ]

    assert len(relevance.select(entries, max_items=15)) == 2


def test_the_brief_is_capped_overall() -> None:
    topics = [make_topic(i, f"T{i}", max_items=10) for i in range(1, 5)]
    entries = [
        relevance.Scored(make_item(i, f"s{i}", f"https://a.example/{i}"), topics[i % 4], 0.9, "")
        for i in range(1, 40)
    ]

    assert len(relevance.select(entries, max_items=15)) == 15


# --- the stage as a whole ----------------------------------------------------


def test_run_selects_across_topics() -> None:
    clusters = clusters_for(2, topic_id=1) + clusters_for(2, topic_id=2, start=10)
    outcome = relevance.run(StubLlm(), clusters, {1: TEAM, 2: TECH}, max_items=15)

    assert outcome.considered == 4
    assert len(outcome.selected) == 4
    assert outcome.degraded is False


def test_a_topic_with_no_items_is_not_sent_to_the_model() -> None:
    llm = StubLlm()
    relevance.run(llm, clusters_for(1, topic_id=1), {1: TEAM, 2: TECH}, max_items=15)

    assert len(llm.classify_prompts) == 1


def test_one_failed_batch_shortens_the_brief_and_marks_it_degraded() -> None:
    """Degrade rather than abort: the other topic still makes the brief."""

    class Flaky(StubLlm):
        def classify(self, system: str, prompt: str, schema: type, max_tokens: int):  # type: ignore[no-untyped-def]
            if "Rust" in prompt:
                raise RuntimeError("overloaded")
            return super().classify(system, prompt, schema, max_tokens)

    clusters = clusters_for(2, topic_id=1) + clusters_for(2, topic_id=2, start=10)
    outcome = relevance.run(Flaky(), clusters, {1: TEAM, 2: TECH}, max_items=15)

    assert outcome.failed_batches == 1
    assert outcome.degraded is True
    assert [e.item.topic_id for e in outcome.selected] == [1, 1]


def test_every_batch_failing_raises_so_the_caller_falls_back() -> None:
    llm = StubLlm(classify_error=RuntimeError("overloaded"))

    with pytest.raises(relevance.FilterError):
        relevance.run(llm, clusters_for(2), {1: TEAM}, max_items=15)


def test_no_clusters_is_not_a_failure() -> None:
    outcome = relevance.run(StubLlm(), [], {1: TEAM}, max_items=15)

    assert outcome.selected == []
    assert outcome.degraded is False


# --- the degraded path ------------------------------------------------------


def test_the_fallback_prefers_the_publisher_over_the_aggregator() -> None:
    publisher = make_item(1, "Story from the paper", "https://a.example/1", source_id=1)
    aggregator = make_item(2, "Story from the aggregator", "https://b.example/2", source_id=2)
    clusters = [make_cluster(publisher), make_cluster(aggregator)]

    selected = relevance.fallback_select(
        clusters, {1: TEAM}, {1: "rss", 2: "google_news"}, max_items=15
    )

    assert [entry.item.id for entry in selected] == [1, 2]


def test_the_fallback_still_respects_the_caps() -> None:
    topic = make_topic(1, "Busy", max_items=2)
    selected = relevance.fallback_select(clusters_for(6), {1: topic}, {1: "rss"}, max_items=15)

    assert len(selected) == 2


def test_the_fallback_skips_a_cluster_whose_topic_is_gone() -> None:
    assert relevance.fallback_select(clusters_for(2), {}, {1: "rss"}, max_items=15) == []


# --- loading ----------------------------------------------------------------


def test_load_topics_skips_the_disabled_ones(conn: sqlite3.Connection) -> None:
    enabled = add_topic(conn, "Enabled", priority=70, max_items=4)
    add_topic(conn, "Disabled", enabled=False)
    add_source(conn, enabled)

    topics = relevance.load_topics(conn)

    assert list(topics) == [enabled]
    assert topics[enabled].priority == 70
    assert topics[enabled].max_items == 4
