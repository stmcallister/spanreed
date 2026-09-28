"""Tests for the reddit adapter, including the rate limit it shares."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import ValidationError

from newsbrief.sources import reddit
from newsbrief.sources.reddit import RedditAdapter, RedditConfig, build_url
from tests.conftest import fixture_bytes

FEED_URL = "https://reddit.com/r/rust/top/.rss?t=week"
ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)
CONFIG = RedditConfig(subreddit="rust", listing="top", window="week")


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """The throttle is module level, so it has to be reset between tests.

    Sleeping is recorded rather than performed, because a test suite that
    honours a two second rate limit takes two seconds per request.
    """
    reddit.throttle.reset()
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    yield slept
    reddit.throttle.reset()


# --- config -----------------------------------------------------------------


@pytest.mark.parametrize("value", ["rust", "/r/rust", "r/rust", " rust "])
def test_a_subreddit_is_accepted_however_it_is_pasted(value: str) -> None:
    assert RedditConfig(subreddit=value).subreddit == "rust"


@pytest.mark.parametrize("value", ["", "r/", "has spaces", "way-too-punctuated!"])
def test_a_subreddit_that_is_not_a_name_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        RedditConfig(subreddit=value)


def test_an_unknown_listing_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RedditConfig(subreddit="rust", listing="popular")


def test_an_unknown_window_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RedditConfig(subreddit="rust", window="fortnight")


def test_the_defaults_match_the_documented_shape() -> None:
    config = RedditConfig(subreddit="rust")

    assert (config.listing, config.window) == ("top", "day")


def test_the_url_is_built_as_documented() -> None:
    assert build_url(CONFIG) == FEED_URL


# --- fetching ---------------------------------------------------------------


@respx.mock
def test_fetch_parses_a_recorded_listing(client: httpx.Client) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("reddit_top.xml"))
    )

    items = RedditAdapter(client).fetch(CONFIG, ANCIENT)

    assert len(items) == 4
    assert items[0].title.startswith("The `allocator_api` feature has been stabilized")
    assert items[0].url.startswith("https://www.reddit.com/r/rust/comments/")
    assert items[0].published_at == datetime(2026, 9, 23, 16, 29, 43, tzinfo=UTC)


@respx.mock
def test_the_user_agent_is_sent(client: httpx.Client) -> None:
    """Reddit blocks requests without one, and this is the first thing to break."""
    route = respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("reddit_top.xml"))
    )

    RedditAdapter(client).fetch(CONFIG, ANCIENT)

    assert "newsbrief" in route.calls[0].request.headers["User-Agent"]


@respx.mock
def test_older_posts_are_dropped(client: httpx.Client) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("reddit_top.xml"))
    )

    items = RedditAdapter(client).fetch(CONFIG, datetime(2026, 9, 26, tzinfo=UTC))

    assert 0 < len(items) < 4


@respx.mock
def test_a_truncated_listing_yields_fewer_items(client: httpx.Client) -> None:
    raw = fixture_bytes("reddit_top.xml")
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=raw[: len(raw) // 2]))

    items = RedditAdapter(client).fetch(CONFIG, ANCIENT)

    assert len(items) < 4


@respx.mock
def test_a_blocked_request_raises_for_the_collector_to_record(
    client: httpx.Client,
) -> None:
    """A 429 is a real failure. The collector counts it and carries on."""
    respx.get(FEED_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(httpx.HTTPStatusError):
        RedditAdapter(client).fetch(CONFIG, ANCIENT)


# --- the shared rate limit --------------------------------------------------


@respx.mock
def test_the_first_request_does_not_wait(
    client: httpx.Client, _reset_throttle: list[float]
) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("reddit_top.xml"))
    )

    RedditAdapter(client).fetch(CONFIG, ANCIENT)

    assert _reset_throttle == []


@respx.mock
def test_a_second_request_waits(client: httpx.Client, _reset_throttle: list[float]) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("reddit_top.xml"))
    )

    RedditAdapter(client).fetch(CONFIG, ANCIENT)
    RedditAdapter(client).fetch(CONFIG, ANCIENT)

    assert len(_reset_throttle) == 1
    assert 0 < _reset_throttle[0] <= reddit.RATE_LIMIT_SECONDS


@respx.mock
def test_the_limit_is_shared_across_every_reddit_source(
    client: httpx.Client, _reset_throttle: list[float]
) -> None:
    """The collector builds a new adapter per source, so per instance state
    would not limit anything. Reddit limits the client, not the feed."""
    other = "https://reddit.com/r/golang/top/.rss?t=day"
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("reddit_top.xml"))
    )
    respx.get(other).mock(return_value=httpx.Response(200, content=fixture_bytes("reddit_top.xml")))

    RedditAdapter(client).fetch(CONFIG, ANCIENT)
    RedditAdapter(client).fetch(RedditConfig(subreddit="golang"), ANCIENT)

    assert len(_reset_throttle) == 1


def test_the_wrong_config_type_is_a_bug(client: httpx.Client) -> None:
    from newsbrief.sources.rss import RssConfig

    with pytest.raises(TypeError):
        RedditAdapter(client).fetch(RssConfig(url="https://a.example/feed"), ANCIENT)
