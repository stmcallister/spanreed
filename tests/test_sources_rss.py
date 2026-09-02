"""Tests for the rss adapter, against recorded feeds."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import ValidationError

from newsbrief.sources.base import NotModifiedError
from newsbrief.sources.rss import RssAdapter, RssConfig
from tests.conftest import fixture_bytes

FEED_URL = "https://arstechnica.com/feed/"
SINCE = datetime(2026, 8, 25, tzinfo=UTC)
ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)


def test_config_rejects_a_url_without_a_scheme() -> None:
    with pytest.raises(ValidationError):
        RssConfig(url="arstechnica.com/feed/")


def test_config_rejects_unknown_keys() -> None:
    """A typo in the source editor should fail loudly, not do nothing."""
    with pytest.raises(ValidationError):
        RssConfig(url=FEED_URL, urls="oops")  # type: ignore[call-arg]


@respx.mock
def test_fetch_parses_a_recorded_feed(client: httpx.Client) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_basic.xml"))
    )
    items = RssAdapter(client).fetch(RssConfig(url=FEED_URL), ANCIENT)

    assert len(items) == 3
    first = items[0]
    assert first.title == "Rust 1.90 lands with faster incremental builds - Ars Technica"
    assert first.url == "https://arstechnica.com/2026/08/rust-190/"
    assert first.summary == "The release cuts rebuild times."
    assert first.author == "Dana Vance"
    assert first.source_name == "Ars Technica"
    assert first.published_at == datetime(2026, 8, 30, 17, 30, tzinfo=UTC)
    assert first.canonical_url is None


@respx.mock
def test_fetch_drops_old_items_but_keeps_undated_ones(client: httpx.Client) -> None:
    """A missing date is common and is not evidence that an item is old."""
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_basic.xml"))
    )
    items = RssAdapter(client).fetch(RssConfig(url=FEED_URL), SINCE)

    titles = [item.title for item in items]
    assert "An article from years ago" not in titles
    assert "Chip maker posts a record quarter" in titles


@respx.mock
def test_fetch_parses_atom(client: httpx.Client) -> None:
    url = "https://midlakesunited.example/feed.xml"
    respx.get(url).mock(return_value=httpx.Response(200, content=fixture_bytes("atom_basic.xml")))
    items = RssAdapter(client).fetch(RssConfig(url=url), ANCIENT)

    assert [item.title for item in items] == [
        "Squad news ahead of Saturday",
        "Ticket ballot opens Monday",
    ]
    assert items[0].url == "https://midlakesunited.example/news/squad-news"
    assert items[0].summary == "Two players return to training."
    assert items[0].author == "Club Press Office"
    assert items[0].source_name == "Midlakes United"


@respx.mock
def test_fetch_stores_the_validators_it_was_given(client: httpx.Client) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            content=fixture_bytes("rss_basic.xml"),
            headers={"ETag": '"abc123"', "Last-Modified": "Sat, 30 Aug 2026 18:00:00 GMT"},
        )
    )
    config = RssConfig(url=FEED_URL)
    RssAdapter(client).fetch(config, ANCIENT)

    assert config.etag == '"abc123"'
    assert config.last_modified == "Sat, 30 Aug 2026 18:00:00 GMT"


@respx.mock
def test_fetch_sends_the_conditional_headers(client: httpx.Client) -> None:
    route = respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_basic.xml"))
    )
    config = RssConfig(url=FEED_URL, etag='"abc123"', last_modified="Sat, 30 Aug 2026 18:00:00 GMT")
    RssAdapter(client).fetch(config, ANCIENT)

    request = route.calls.last.request
    assert request.headers["If-None-Match"] == '"abc123"'
    assert request.headers["If-Modified-Since"] == "Sat, 30 Aug 2026 18:00:00 GMT"


@respx.mock
def test_a_304_raises_not_modified(client: httpx.Client) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(304))
    config = RssConfig(url=FEED_URL, etag='"abc123"')

    with pytest.raises(NotModifiedError):
        RssAdapter(client).fetch(config, ANCIENT)


@respx.mock
def test_validators_are_cleared_when_the_server_stops_sending_them(client: httpx.Client) -> None:
    """Otherwise we replay a stale ETag forever and the feed goes quiet."""
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_basic.xml"))
    )
    config = RssConfig(url=FEED_URL, etag='"stale"', last_modified="Sat, 30 Aug 2026 18:00:00 GMT")
    RssAdapter(client).fetch(config, ANCIENT)

    assert config.etag is None
    assert config.last_modified is None


@respx.mock
def test_a_truncated_feed_yields_fewer_items_and_does_not_raise(client: httpx.Client) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_truncated.xml"))
    )
    items = RssAdapter(client).fetch(RssConfig(url=FEED_URL), ANCIENT)

    assert len(items) == 1
    assert items[0].url == "https://arstechnica.com/2026/08/rust-190/"


@respx.mock
@pytest.mark.parametrize("payload", [b"", b"not xml at all", b"<html><body>404</body></html>"])
def test_a_response_that_is_not_a_feed_yields_nothing(client: httpx.Client, payload: bytes) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=payload))
    assert RssAdapter(client).fetch(RssConfig(url=FEED_URL), ANCIENT) == []


@respx.mock
def test_an_http_error_propagates_for_the_collector_to_record(client: httpx.Client) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        RssAdapter(client).fetch(RssConfig(url=FEED_URL), ANCIENT)


def test_fetch_rejects_the_wrong_config_type(client: httpx.Client) -> None:
    from newsbrief.sources.google_news import GoogleNewsConfig

    with pytest.raises(TypeError):
        RssAdapter(client).fetch(GoogleNewsConfig(query="anything"), ANCIENT)
