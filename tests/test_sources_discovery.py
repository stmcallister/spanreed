"""Tests for feed discovery, which the source editor calls on a pasted URL."""

from __future__ import annotations

import httpx
import pytest
import respx

from newsbrief.sources import discovery
from newsbrief.sources.discovery import declared_feeds, discover, normalize_site_url, probe_paths
from tests.conftest import fixture_bytes

SITE = "https://example.test/"
FEED = "https://example.test/feed/"
COMMENTS = "https://example.test/comments/atom.xml"


def rss() -> bytes:
    return fixture_bytes("rss_basic.xml")


# --- normalizing what a person pastes ---------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("example.test", "https://example.test/"),
        ("https://example.test", "https://example.test/"),
        ("  http://example.test/blog  ", "http://example.test/blog"),
        ("https://example.test/blog?tag=x", "https://example.test/blog?tag=x"),
        ("https://example.test/blog#top", "https://example.test/blog"),
    ],
)
def test_a_pasted_url_is_normalized(value: str, expected: str) -> None:
    assert normalize_site_url(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "ftp://example.test", "not a url at all"])
def test_something_that_is_not_a_url_is_refused(value: str) -> None:
    assert normalize_site_url(value) is None


# --- reading the document head ----------------------------------------------


def test_declared_feeds_are_found_and_resolved() -> None:
    """The correct method, and the one that usually works."""
    found = declared_feeds(fixture_bytes("site_with_feed.html").decode(), SITE)

    assert found == [FEED, COMMENTS]


def test_a_stylesheet_is_not_a_feed() -> None:
    found = declared_feeds(fixture_bytes("site_with_feed.html").decode(), SITE)

    assert not any("style" in url for url in found)


def test_a_json_feed_is_not_offered() -> None:
    """feedparser cannot read one, so offering it would be a broken source."""
    found = declared_feeds(fixture_bytes("site_with_feed.html").decode(), SITE)

    assert not any(url.endswith(".json") for url in found)


def test_a_page_declaring_nothing_yields_nothing() -> None:
    assert declared_feeds(fixture_bytes("site_without_feed.html").decode(), SITE) == []


def test_unparsable_markup_is_not_an_error() -> None:
    assert declared_feeds("", SITE) == []


def test_probe_paths_are_rooted_at_the_host() -> None:
    paths = probe_paths("https://example.test/blog/post/1")

    assert paths[0] == "https://example.test/feed/"
    assert all(p.startswith("https://example.test/") for p in paths)


# --- the whole helper -------------------------------------------------------


@respx.mock
def test_a_declared_feed_is_found_and_confirmed(client: httpx.Client) -> None:
    respx.get(SITE).mock(
        return_value=httpx.Response(200, content=fixture_bytes("site_with_feed.html"))
    )
    respx.get(FEED).mock(return_value=httpx.Response(200, content=rss()))
    respx.get(COMMENTS).mock(return_value=httpx.Response(404))

    found = discover(client, "example.test")

    assert len(found) == 1
    assert found[0].url == FEED
    assert found[0].item_count == 3
    assert found[0].title == "Ars Technica"
    assert found[0].latest_title is not None


@respx.mock
def test_a_candidate_that_is_not_really_a_feed_is_dropped(client: httpx.Client) -> None:
    """A site that answers 200 with its own 404 page is the common case."""
    respx.get(SITE).mock(
        return_value=httpx.Response(200, content=fixture_bytes("site_with_feed.html"))
    )
    respx.get(FEED).mock(return_value=httpx.Response(200, html="<h1>Not found</h1>"))
    respx.get(COMMENTS).mock(return_value=httpx.Response(404))

    assert discover(client, SITE) == []


@respx.mock
def test_probing_finds_a_feed_the_page_never_advertised(client: httpx.Client) -> None:
    respx.get(SITE).mock(
        return_value=httpx.Response(200, content=fixture_bytes("site_without_feed.html"))
    )
    respx.get("https://example.test/feed/").mock(return_value=httpx.Response(404))
    respx.get("https://example.test/rss").mock(return_value=httpx.Response(404))
    respx.get("https://example.test/feed.xml").mock(return_value=httpx.Response(200, content=rss()))
    for path in ("/atom.xml", "/index.xml", "/blog/feed/"):
        respx.get(f"https://example.test{path}").mock(return_value=httpx.Response(404))

    found = discover(client, SITE)

    assert [f.url for f in found] == ["https://example.test/feed.xml"]


@respx.mock
def test_a_pasted_feed_url_is_answered_with_itself(client: httpx.Client) -> None:
    """Somebody who pastes a feed URL means that feed."""
    respx.get(FEED).mock(return_value=httpx.Response(200, content=rss()))

    found = discover(client, FEED)

    assert [f.url for f in found] == [FEED]


@respx.mock
def test_a_site_that_is_down_yields_nothing_rather_than_raising(
    client: httpx.Client,
) -> None:
    respx.get(SITE).mock(side_effect=httpx.ConnectError("no route"))
    for path in discovery.PROBE_PATHS:
        respx.get(f"https://example.test{path}").mock(side_effect=httpx.ConnectError("no route"))

    assert discover(client, SITE) == []


def test_something_that_is_not_a_url_yields_nothing(client: httpx.Client) -> None:
    assert discover(client, "   ") == []


@respx.mock
def test_a_site_declaring_many_feeds_is_capped(client: httpx.Client) -> None:
    links = "".join(
        f'<link rel="alternate" type="application/rss+xml" href="/feed{i}.xml">' for i in range(30)
    )
    respx.get(SITE).mock(
        return_value=httpx.Response(200, html=f"<html><head>{links}</head><body></body></html>")
    )
    for i in range(30):
        respx.get(f"https://example.test/feed{i}.xml").mock(
            return_value=httpx.Response(200, content=rss())
        )

    assert len(discover(client, SITE)) == discovery.MAX_CANDIDATES


@respx.mock
def test_the_same_feed_declared_twice_appears_once(client: httpx.Client) -> None:
    html = (
        "<html><head>"
        '<link rel="alternate" type="application/rss+xml" href="/feed/">'
        '<link rel="alternate" type="application/rss+xml" href="https://example.test/feed/">'
        "</head><body></body></html>"
    )
    respx.get(SITE).mock(return_value=httpx.Response(200, html=html))
    respx.get(FEED).mock(return_value=httpx.Response(200, content=rss()))

    assert len(discover(client, SITE)) == 1
