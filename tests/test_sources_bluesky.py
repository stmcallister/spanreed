"""Tests for the bluesky adapter.

Bluesky ships every post with an empty title, so the interesting part is that
anything survives parsing at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import ValidationError

from newsbrief.sources.base import parse_feed, title_from_summary
from newsbrief.sources.bluesky import BlueskyAdapter, BlueskyConfig, build_url
from tests.conftest import fixture_bytes

HANDLE = "jay.bsky.team"
FEED_URL = f"https://bsky.app/profile/{HANDLE}/rss"
ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)
CONFIG = BlueskyConfig(handle=HANDLE)


# --- config -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "jay.bsky.team",
        "@jay.bsky.team",
        " jay.bsky.team ",
        "https://bsky.app/profile/jay.bsky.team",
    ],
)
def test_a_handle_is_accepted_however_it_is_pasted(value: str) -> None:
    assert BlueskyConfig(handle=value).handle == HANDLE


@pytest.mark.parametrize("value", ["", "nodots", "has spaces", "@"])
def test_something_that_is_not_a_handle_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        BlueskyConfig(handle=value)


def test_the_url_is_built_as_documented() -> None:
    assert build_url(CONFIG) == FEED_URL


# --- titles -----------------------------------------------------------------


def test_the_generic_parser_alone_recovers_nothing() -> None:
    """Every Bluesky item ships an empty title, so this is the whole problem."""
    assert parse_feed(fixture_bytes("bluesky_profile.xml")) == []


def test_titles_are_synthesized_from_the_post_text() -> None:
    items = parse_feed(fixture_bytes("bluesky_profile.xml"), synthesize_title=True)

    assert len(items) == 4
    assert all(item.title for item in items)


def test_a_short_post_becomes_its_own_title() -> None:
    assert title_from_summary("harvest moon! everyone go reap") == "harvest moon! everyone go reap"


def test_a_long_post_is_clipped_on_a_word_boundary() -> None:
    title = title_from_summary("word " * 80)

    assert title is not None
    assert title.endswith("...")
    assert not title.startswith(" ")
    assert len(title) <= 115


def test_a_first_sentence_is_preferred_to_a_clip() -> None:
    assert title_from_summary("The club won. Then a lot more happened after that.") == (
        "The club won"
    )


@pytest.mark.parametrize("value", [None, "", "   "])
def test_nothing_to_work_from_yields_no_title(value: str | None) -> None:
    assert title_from_summary(value) is None


# --- fetching ---------------------------------------------------------------


@respx.mock
def test_fetch_parses_a_recorded_profile(client: httpx.Client) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("bluesky_profile.xml"))
    )

    items = BlueskyAdapter(client).fetch(CONFIG, ANCIENT)

    assert len(items) == 4
    assert items[0].url.startswith("https://bsky.app/profile/jay.bsky.team/post/")
    assert items[0].published_at == datetime(2026, 9, 28, 8, 51, tzinfo=UTC)
    assert items[0].summary is not None


@respx.mock
def test_older_posts_are_dropped(client: httpx.Client) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("bluesky_profile.xml"))
    )

    items = BlueskyAdapter(client).fetch(CONFIG, datetime(2026, 9, 28, tzinfo=UTC))

    assert len(items) == 1


@respx.mock
def test_a_truncated_feed_yields_fewer_items(client: httpx.Client) -> None:
    raw = fixture_bytes("bluesky_profile.xml")
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=raw[: len(raw) // 2]))

    assert len(BlueskyAdapter(client).fetch(CONFIG, ANCIENT)) < 4


@respx.mock
def test_a_deleted_profile_raises_for_the_collector_to_record(client: httpx.Client) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(404))

    with pytest.raises(httpx.HTTPStatusError):
        BlueskyAdapter(client).fetch(CONFIG, ANCIENT)


def test_the_wrong_config_type_is_a_bug(client: httpx.Client) -> None:
    from newsbrief.sources.rss import RssConfig

    with pytest.raises(TypeError):
        BlueskyAdapter(client).fetch(RssConfig(url="https://a.example/feed"), ANCIENT)
