"""Tests for the google_news adapter, against a recorded search feed."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import ValidationError

from newsbrief.sources.google_news import GoogleNewsAdapter, GoogleNewsConfig, build_url
from tests.conftest import fixture_bytes

ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)
SEARCH = respx.patterns.M(url__startswith="https://news.google.com/rss/search")


def test_build_url_preserves_quoting_and_negative_terms() -> None:
    """Both are how a topic named after a common word gets tamed."""
    url = build_url(GoogleNewsConfig(query='"Midlakes United" -realty', window="1d"))
    assert url == (
        "https://news.google.com/rss/search"
        "?q=%22Midlakes%20United%22%20-realty+when:1d"
        "&hl=en-US&gl=US&ceid=US:en"
    )


def test_build_url_defaults_to_a_one_day_window() -> None:
    assert "+when:1d" in build_url(GoogleNewsConfig(query="kubernetes"))


@pytest.mark.parametrize("window", ["1d", "12h", "7d", "48h"])
def test_valid_windows(window: str) -> None:
    assert GoogleNewsConfig(query="x", window=window).window == window


@pytest.mark.parametrize("window", ["", "d", "1w", "0d", "tomorrow", "1 d"])
def test_invalid_windows_are_rejected(window: str) -> None:
    with pytest.raises(ValidationError):
        GoogleNewsConfig(query="x", window=window)


def test_an_empty_query_is_rejected() -> None:
    with pytest.raises(ValidationError):
        GoogleNewsConfig(query="   ")


@respx.mock
def test_fetch_parses_the_search_feed(client: httpx.Client) -> None:
    respx.route(SEARCH).mock(
        return_value=httpx.Response(200, content=fixture_bytes("google_news.xml"))
    )
    respx.head(url__startswith="https://news.google.com/rss/articles/").mock(
        side_effect=httpx.ConnectError("no resolution")
    )

    items = GoogleNewsAdapter(client).fetch(GoogleNewsConfig(query="Midlakes United"), ANCIENT)

    assert len(items) == 3
    assert items[0].title == "Midlakes United sign a goalkeeper on loan - The Athletic"
    assert items[0].url.startswith("https://news.google.com/rss/articles/")
    assert items[0].source_name == "The Athletic"
    assert items[1].source_name == "Local Herald"
    assert items[0].published_at == datetime(2026, 8, 31, 14, 2, tzinfo=UTC)


@respx.mock
def test_a_resolved_link_populates_canonical_url(client: httpx.Client) -> None:
    respx.route(SEARCH).mock(
        return_value=httpx.Response(200, content=fixture_bytes("google_news.xml"))
    )
    respx.head(url__startswith="https://news.google.com/rss/articles/").mock(
        return_value=httpx.Response(
            301, headers={"Location": "https://theathletic.com/goalkeeper/"}
        )
    )
    respx.head("https://theathletic.com/goalkeeper/").mock(return_value=httpx.Response(200))

    items = GoogleNewsAdapter(client).fetch(GoogleNewsConfig(query="Midlakes United"), ANCIENT)

    assert items[0].url.startswith("https://news.google.com/rss/articles/")
    assert items[0].canonical_url == "https://theathletic.com/goalkeeper/"


@respx.mock
def test_failed_resolution_is_normal_and_leaves_the_redirect_in_place(
    client: httpx.Client,
) -> None:
    respx.route(SEARCH).mock(
        return_value=httpx.Response(200, content=fixture_bytes("google_news.xml"))
    )
    respx.head(url__startswith="https://news.google.com/rss/articles/").mock(
        side_effect=httpx.ReadTimeout("slow")
    )

    items = GoogleNewsAdapter(client).fetch(GoogleNewsConfig(query="Midlakes United"), ANCIENT)

    assert len(items) == 3
    assert all(item.canonical_url is None for item in items)


@respx.mock
def test_resolution_stops_after_repeated_failures(client: httpx.Client) -> None:
    """Twenty unresolvable links must not cost twenty timeouts."""
    respx.route(SEARCH).mock(
        return_value=httpx.Response(200, content=fixture_bytes("google_news.xml"))
    )
    heads = respx.head(url__startswith="https://news.google.com/rss/articles/").mock(
        side_effect=httpx.ReadTimeout("slow")
    )

    GoogleNewsAdapter(client).fetch(GoogleNewsConfig(query="Midlakes United"), ANCIENT)

    assert heads.call_count == 3


@respx.mock
def test_a_link_that_does_not_leave_google_is_not_canonical(client: httpx.Client) -> None:
    respx.route(SEARCH).mock(
        return_value=httpx.Response(200, content=fixture_bytes("google_news.xml"))
    )
    respx.head(url__startswith="https://news.google.com/rss/articles/").mock(
        return_value=httpx.Response(200)
    )

    items = GoogleNewsAdapter(client).fetch(GoogleNewsConfig(query="Midlakes United"), ANCIENT)
    assert all(item.canonical_url is None for item in items)


@respx.mock
def test_a_restructured_response_yields_fewer_items_and_does_not_raise(
    client: httpx.Client,
) -> None:
    respx.route(SEARCH).mock(
        return_value=httpx.Response(200, content=b'{"items": [{"title": "not rss"}]}')
    )
    assert GoogleNewsAdapter(client).fetch(GoogleNewsConfig(query="x"), ANCIENT) == []


@respx.mock
def test_items_older_than_since_are_dropped(client: httpx.Client) -> None:
    respx.route(SEARCH).mock(
        return_value=httpx.Response(200, content=fixture_bytes("google_news.xml"))
    )
    respx.head(url__startswith="https://news.google.com/rss/articles/").mock(
        side_effect=httpx.ConnectError("no resolution")
    )

    since = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    items = GoogleNewsAdapter(client).fetch(GoogleNewsConfig(query="x"), since)

    assert [item.source_name for item in items] == ["The Athletic"]


def test_fetch_rejects_the_wrong_config_type(client: httpx.Client) -> None:
    from newsbrief.sources.rss import RssConfig

    with pytest.raises(TypeError):
        GoogleNewsAdapter(client).fetch(RssConfig(url="https://example.com/feed"), ANCIENT)
