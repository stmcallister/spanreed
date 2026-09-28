"""Tests for the prose stage and the style rules that are enforced in code."""

from __future__ import annotations

from newsbrief.compose import write
from newsbrief.compose.filter import Scored
from tests.conftest import StubLlm, make_topic
from tests.test_cluster import make_item

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

TEAM = make_topic(1, "Midlakes United", kind="team", priority=50)
TECH = make_topic(2, "Rust", kind="tech", priority=90)


def scored(item_id: int, topic=TEAM, summary: str | None = None) -> Scored:  # type: ignore[no-untyped-def]
    return Scored(
        item=make_item(
            item_id,
            f"Story {item_id}",
            f"https://a.example/{item_id}",
            topic_id=topic.id,
            summary=summary,
        ),
        topic=topic,
        score=0.9,
        reason="on topic",
    )


# --- the dash rule ----------------------------------------------------------


def test_an_em_dash_is_replaced() -> None:
    """The model is told not to produce these. Told is not the standard."""
    assert write.scrub_dashes(f"The club won{EM_DASH}again") == "The club won, again"


def test_an_en_dash_is_replaced() -> None:
    assert write.scrub_dashes(f"The club won {EN_DASH} again") == "The club won, again"


def test_a_double_hyphen_is_replaced() -> None:
    assert write.scrub_dashes("The club won -- again") == "The club won, again"


def test_a_single_hyphen_is_left_alone() -> None:
    assert write.scrub_dashes("a well-known result") == "a well-known result"


def test_a_horizontal_rule_survives() -> None:
    """A line of dashes is punctuation for the document, not for a sentence."""
    assert write.scrub_dashes("before\n---\nafter") == "before\n---\nafter"


def test_a_bullet_list_survives() -> None:
    assert write.scrub_dashes("- an item\n- another") == "- an item\n- another"


def test_the_model_output_is_scrubbed_on_the_way_out() -> None:
    llm = StubLlm(prose=f"## Topic\n\n- [A story](https://a.example/1) It happened{EM_DASH}twice.")
    markdown = write.run(llm, [(TEAM, [scored(1)])], {})

    assert EM_DASH not in markdown
    assert "happened, twice" in markdown


# --- grouping and order -----------------------------------------------------


def test_sports_results_come_first() -> None:
    """Even though the tech topic has the higher priority."""
    ordered = write.order_topics([TECH, TEAM])

    assert [topic.name for topic in ordered] == ["Midlakes United", "Rust"]


def test_grouping_buckets_items_by_topic() -> None:
    grouped = write.group_by_topic([scored(1, TEAM), scored(2, TECH), scored(3, TEAM)])

    assert [(topic.name, len(entries)) for topic, entries in grouped] == [
        ("Midlakes United", 2),
        ("Rust", 1),
    ]


# --- the prompt -------------------------------------------------------------


def test_the_prompt_prefers_the_deep_read_over_the_summary() -> None:
    entry = scored(1, summary="the short feed blurb")
    prompt = write.build_prompt([(TEAM, [entry])], {1: "the full article body"})

    assert "article text: the full article body" in prompt
    assert "the short feed blurb" not in prompt


def test_the_prompt_falls_back_to_the_summary() -> None:
    entry = scored(1, summary="the short feed blurb")
    prompt = write.build_prompt([(TEAM, [entry])], {})

    assert "feed summary: the short feed blurb" in prompt


def test_the_prompt_handles_an_item_with_no_material_at_all() -> None:
    prompt = write.build_prompt([(TEAM, [scored(1)])], {})

    assert "use the title only" in prompt


def test_the_prompt_carries_the_link() -> None:
    prompt = write.build_prompt([(TEAM, [scored(1)])], {})

    assert "url: https://a.example/1" in prompt


# --- the degraded body ------------------------------------------------------


def test_the_fallback_lists_every_selected_item() -> None:
    """Worse than a written brief, better than no brief."""
    markdown = write.fallback_markdown(write.group_by_topic([scored(1), scored(2, TECH)]))

    assert "## Midlakes United" in markdown
    assert "[Story 1](https://a.example/1)" in markdown
    assert "## Rust" in markdown


# --- the footer -------------------------------------------------------------


def test_the_footer_names_what_failed() -> None:
    footer = write.render_footer(["Midlakes United (rss)", "Rust (google_news)"])

    assert "Midlakes United (rss), Rust (google_news)" in footer


def test_the_footer_says_so_when_nothing_failed() -> None:
    """A footer that vanishes when things are fine is one nobody notices."""
    assert "All sources fetched cleanly" in write.render_footer([])


def test_the_footer_carries_the_degradation_notes() -> None:
    footer = write.render_footer([], ["relevance filter unavailable"])

    assert "Note: relevance filter unavailable" in footer
