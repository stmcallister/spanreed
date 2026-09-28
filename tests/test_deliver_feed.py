"""Tests for the RSS feed of briefs."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from newsbrief.config import Settings
from newsbrief.deliver import feed
from newsbrief.models import Brief
from newsbrief.web import repo
from newsbrief.web.app import create_app

BASE = "http://newsbrief.test/"


@pytest.fixture
def web(settings: Settings, conn: sqlite3.Connection) -> Iterator[TestClient]:
    """The app on the same temp database the `conn` fixture already migrated.

    Named `web` because conftest's `client` is the httpx client the adapters
    take, and shadowing that here would be confusing rather than convenient.
    """
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def secured_web(settings: Settings, conn: sqlite3.Connection) -> Iterator[TestClient]:
    guarded = settings.model_copy(update={"basic_auth_password": "hunter2"})
    with TestClient(create_app(guarded)) as test_client:
        yield test_client


def make_brief(
    brief_date: str = "2026-09-28",
    *,
    html: str = "<h2>Rust</h2>",
    degraded: bool = False,
    generated_at: str = "2026-09-28T13:00:00Z",
) -> Brief:
    return Brief(
        id=1,
        brief_date=brief_date,
        generated_at=generated_at,
        markdown="## Rust",
        html=html,
        item_count=3,
        degraded=degraded,
    )


def parse(payload: bytes) -> ElementTree.Element:
    return ElementTree.fromstring(payload)


def channel(payload: bytes) -> ElementTree.Element:
    found = parse(payload).find("channel")
    assert found is not None
    return found


# --- shape ------------------------------------------------------------------


def test_the_feed_is_rss_two() -> None:
    root = parse(feed.build([make_brief()], BASE))

    assert root.tag == "rss"
    assert root.get("version") == "2.0"


def test_an_empty_archive_is_still_a_valid_feed() -> None:
    """A reader subscribing before the first brief should not get a parse error."""
    found = channel(feed.build([], BASE))

    assert found.findtext("title") == "newsbrief"
    assert found.findall("item") == []


def test_each_brief_becomes_an_item() -> None:
    payload = feed.build([make_brief("2026-09-28"), make_brief("2026-09-27")], BASE)

    assert len(channel(payload).findall("item")) == 2


def test_links_are_absolute() -> None:
    """A feed is read somewhere else, so a relative link resolves against nothing."""
    item = channel(feed.build([make_brief()], BASE)).find("item")

    assert item is not None
    assert item.findtext("link") == "http://newsbrief.test/briefs/2026-09-28"


def test_the_guid_is_the_permalink() -> None:
    item = channel(feed.build([make_brief()], BASE)).find("item")

    assert item is not None
    guid = item.find("guid")
    assert guid is not None
    assert guid.get("isPermaLink") == "true"
    assert guid.text == "http://newsbrief.test/briefs/2026-09-28"


def test_dates_are_rfc_822_not_iso() -> None:
    """The one thing every reader agrees on."""
    item = channel(feed.build([make_brief()], BASE)).find("item")

    assert item is not None
    assert item.findtext("pubDate") == "Mon, 28 Sep 2026 13:00:00 +0000"


def test_a_sqlite_default_timestamp_is_also_readable() -> None:
    payload = feed.build([make_brief(generated_at="2026-09-28 13:00:00")], BASE)
    item = channel(payload).find("item")

    assert item is not None
    assert item.findtext("pubDate") == "Mon, 28 Sep 2026 13:00:00 +0000"


def test_an_unreadable_timestamp_omits_the_date_rather_than_breaking() -> None:
    payload = feed.build([make_brief(generated_at="not a date")], BASE)
    item = channel(payload).find("item")

    assert item is not None
    assert item.find("pubDate") is None


def test_a_degraded_brief_is_marked_in_its_title() -> None:
    payload = feed.build([make_brief(degraded=True)], BASE)
    item = channel(payload).find("item")

    assert item is not None
    assert item.findtext("title") == "2026-09-28 (degraded)"


def test_the_brief_html_is_carried_and_escaped() -> None:
    """RSS carries markup as escaped text, which readers unescape."""
    payload = feed.build([make_brief(html="<h2>Rust & Cargo</h2>")], BASE)

    assert b"&lt;h2&gt;Rust &amp; Cargo&lt;/h2&gt;" in payload
    item = channel(payload).find("item")
    assert item is not None
    assert item.findtext("description") == "<h2>Rust & Cargo</h2>"


def test_the_feed_is_capped() -> None:
    briefs = [make_brief(f"2026-09-{day:02d}") for day in range(1, 31)] * 3
    payload = feed.build(briefs, BASE)

    assert len(channel(payload).findall("item")) == feed.MAX_FEED_ITEMS


def test_the_build_date_is_the_newest_brief() -> None:
    payload = feed.build([make_brief("2026-09-28", generated_at="2026-09-28T13:00:00Z")], BASE)

    assert channel(payload).findtext("lastBuildDate") == "Mon, 28 Sep 2026 13:00:00 +0000"


# --- the route --------------------------------------------------------------


def insert(conn: sqlite3.Connection, brief_date: str, html: str = "<h2>Rust</h2>") -> None:
    conn.execute(
        "INSERT INTO briefs (brief_date, markdown, html, item_count) VALUES (?, '## Rust', ?, 3)",
        (brief_date, html),
    )


def test_the_route_serves_rss(web: TestClient, conn: sqlite3.Connection) -> None:
    insert(conn, "2026-09-28")

    response = web.get("/feed.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    assert b"<rss" in response.content


def test_the_route_uses_the_request_host(web: TestClient, conn: sqlite3.Connection) -> None:
    insert(conn, "2026-09-28")

    payload = web.get("/feed.xml").content
    item = channel(payload).find("item")

    assert item is not None
    link = item.findtext("link")
    assert link is not None and link.endswith("/briefs/2026-09-28")
    assert link.startswith("http://")


def test_the_feed_is_newest_first(web: TestClient, conn: sqlite3.Connection) -> None:
    insert(conn, "2026-09-26")
    insert(conn, "2026-09-28")
    insert(conn, "2026-09-27")

    titles = [
        item.findtext("title") for item in channel(web.get("/feed.xml").content).findall("item")
    ]

    assert titles == ["2026-09-28", "2026-09-27", "2026-09-26"]


def test_the_feed_needs_the_same_credential_as_the_rest(secured_web: TestClient) -> None:
    """It carries the whole brief, so it is not more public than the UI."""
    assert secured_web.get("/feed.xml").status_code == 401


def test_the_feed_is_reachable_with_the_credential(secured_web: TestClient) -> None:
    import base64

    token = base64.b64encode(b"newsbrief:hunter2").decode()
    response = secured_web.get("/feed.xml", headers={"Authorization": f"Basic {token}"})

    assert response.status_code == 200


def test_the_archive_page_advertises_the_feed(web: TestClient) -> None:
    page = web.get("/briefs").text

    assert "/feed.xml" in page


def test_recent_briefs_loads_the_html(conn: sqlite3.Connection) -> None:
    """The list view skips the HTML, so the feed needs its own query."""
    insert(conn, "2026-09-28", html="<h2>Loaded</h2>")

    briefs = repo.recent_briefs(conn, 10)

    assert briefs[0].html == "<h2>Loaded</h2>"
