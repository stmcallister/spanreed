"""Tests for the management UI. No network, and the model is never called."""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from spanreed import db
from spanreed.config import Settings
from spanreed.web import repo
from spanreed.web.app import create_app
from tests.conftest import NOW, add_source, add_topic, fixture_bytes

FEED_URL = "https://arstechnica.com/feed/"


@pytest.fixture
def client(settings: Settings, conn: sqlite3.Connection) -> Iterator[TestClient]:
    """An app on the same temp database the `conn` fixture already migrated."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def secured(settings: Settings, conn: sqlite3.Connection) -> Iterator[TestClient]:
    guarded = settings.model_copy(update={"basic_auth_password": "hunter2"})
    with TestClient(create_app(guarded)) as test_client:
        yield test_client


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --- auth -------------------------------------------------------------------


def test_an_unauthenticated_request_is_challenged(secured: TestClient) -> None:
    response = secured.get("/")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_the_right_credential_gets_in(secured: TestClient) -> None:
    assert secured.get("/", headers=basic("spanreed", "hunter2")).status_code == 200


def test_a_wrong_password_is_rejected(secured: TestClient) -> None:
    assert secured.get("/", headers=basic("spanreed", "wrong")).status_code == 401


def test_a_wrong_user_is_rejected(secured: TestClient) -> None:
    assert secured.get("/", headers=basic("someone", "hunter2")).status_code == 401


def test_a_malformed_header_is_rejected(secured: TestClient) -> None:
    assert secured.get("/", headers={"Authorization": "Basic !!!not-base64"}).status_code == 401


def test_the_probe_is_exempt_from_auth(secured: TestClient) -> None:
    """Kubernetes cannot present credentials, and a probe that always fails
    is a pod that never becomes ready."""
    assert secured.get("/healthz").status_code == 200


def test_auth_is_skipped_when_no_password_is_configured(client: TestClient) -> None:
    assert client.get("/").status_code == 200


# --- the probe --------------------------------------------------------------


def test_healthz_reports_ok(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_healthz_reports_unready_without_a_schema(settings: Settings) -> None:
    """A database that has not been migrated is not ready to serve."""
    with TestClient(create_app(settings)) as bare:
        response = bare.get("/healthz")

    assert response.status_code == 503
    assert response.json()["status"] == "unready"


# --- topics -----------------------------------------------------------------


def test_the_topics_list_is_empty_to_begin_with(client: TestClient) -> None:
    assert "No topics yet" in client.get("/").text


def test_a_topic_can_be_created(client: TestClient, conn: sqlite3.Connection) -> None:
    response = client.post(
        "/topics",
        data={
            "name": "Midlakes United",
            "kind": "team",
            "relevance_hint": "transfers and results",
            "max_items": "4",
            "priority": "70",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    topic = repo.get_topic(conn, 1)
    assert topic is not None
    assert (topic.name, topic.slug, topic.max_items, topic.priority) == (
        "Midlakes United",
        "midlakes-united",
        4,
        70,
    )


def test_a_duplicate_topic_name_is_rejected(client: TestClient, conn: sqlite3.Connection) -> None:
    add_topic(conn, "Midlakes United")
    form = {"name": "Midlakes United", "kind": "team", "relevance_hint": "x"}

    assert client.post("/topics", data=form).status_code == 409


def test_an_unknown_kind_is_rejected(client: TestClient) -> None:
    form = {"name": "Something", "kind": "weather", "relevance_hint": "x"}

    assert client.post("/topics", data=form).status_code == 400


def test_the_numbers_are_clamped(client: TestClient, conn: sqlite3.Connection) -> None:
    """The form uses number inputs, so out of range only arrives by hand."""
    client.post(
        "/topics",
        data={
            "name": "Greedy",
            "kind": "tech",
            "relevance_hint": "x",
            "max_items": "900",
            "priority": "0",
        },
    )
    topic = repo.get_topic(conn, 1)

    assert topic is not None
    assert (topic.max_items, topic.priority) == (15, 1)


def test_the_editor_shows_the_worked_example(client: TestClient) -> None:
    """The relevance hint is the most important input in the application."""
    page = client.get("/topics/new").text

    assert "real estate company with a similar name" in page
    assert "tells the relevance filter what counts" in page


def test_a_topic_can_be_edited(client: TestClient, conn: sqlite3.Connection) -> None:
    topic_id = add_topic(conn, "Old Name")

    client.post(
        f"/topics/{topic_id}",
        data={
            "name": "New Name",
            "kind": "tech",
            "relevance_hint": "a better hint",
            "max_items": "2",
            "priority": "30",
        },
    )

    topic = repo.get_topic(conn, topic_id)
    assert topic is not None
    assert (topic.name, topic.slug, topic.kind) == ("New Name", "new-name", "tech")
    assert topic.relevance_hint == "a better hint"


def test_editing_a_topic_that_is_gone_is_a_404(client: TestClient) -> None:
    form = {"name": "x", "kind": "team", "relevance_hint": "x"}

    assert client.post("/topics/999", data=form).status_code == 404


def test_the_toggle_returns_just_the_row(client: TestClient, conn: sqlite3.Connection) -> None:
    """HTMX swaps this row in place rather than reloading the page."""
    topic_id = add_topic(conn)

    response = client.post(f"/topics/{topic_id}/enabled", data={"enabled": "false"})

    assert response.status_code == 200
    assert response.text.strip().startswith("<tr")
    assert "<html" not in response.text
    topic = repo.get_topic(conn, topic_id)
    assert topic is not None and topic.enabled is False


def test_deleting_a_topic_takes_its_sources(client: TestClient, conn: sqlite3.Connection) -> None:
    topic_id = add_topic(conn)
    add_source(conn, topic_id)

    client.post(f"/topics/{topic_id}/delete")

    assert repo.get_topic(conn, topic_id) is None
    assert repo.sources_for_topic(conn, topic_id) == []


def test_the_topics_list_counts_sources_and_items(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    topic_id = add_topic(conn, "Counted")
    add_source(conn, topic_id)
    add_source(conn, topic_id, enabled=False)

    summaries = repo.topic_summaries(conn, NOW)

    assert summaries[0].source_count == 2
    assert summaries[0].enabled_source_count == 1
    assert summaries[0].has_disabled_sources is True


def test_only_recent_items_are_counted(client: TestClient, conn: sqlite3.Connection) -> None:
    """A topic that stopped producing should look like it stopped producing."""
    from tests.test_cluster import insert_item, make_item

    topic_id = add_topic(conn, "Quiet")
    source_id = add_source(conn, topic_id)
    old = make_item(1, "Ancient", "https://a.example/1", topic_id=topic_id, source_id=source_id)
    insert_item(conn, old)
    conn.execute(
        "UPDATE items SET fetched_at = ? WHERE id = 1", (db.to_iso_utc(NOW - timedelta(days=30)),)
    )

    assert repo.topic_summaries(conn, NOW)[0].recent_item_count == 0


# --- sources ----------------------------------------------------------------


def test_a_source_can_be_added(client: TestClient, conn: sqlite3.Connection) -> None:
    topic_id = add_topic(conn)

    response = client.post(
        f"/topics/{topic_id}/sources",
        data={"type": "rss", "url": FEED_URL},
        follow_redirects=False,
    )

    assert response.status_code == 303
    sources = repo.sources_for_topic(conn, topic_id)
    assert [(s.type, s.config) for s in sources] == [("rss", {"url": FEED_URL})]


def test_an_invalid_source_config_is_refused(client: TestClient, conn: sqlite3.Connection) -> None:
    """A typo is caught here rather than in tomorrow's brief."""
    topic_id = add_topic(conn)

    response = client.post(
        f"/topics/{topic_id}/sources", data={"type": "rss", "url": "arstechnica.com"}
    )

    assert response.status_code == 400
    assert repo.sources_for_topic(conn, topic_id) == []


def test_an_unknown_source_type_is_refused(client: TestClient, conn: sqlite3.Connection) -> None:
    topic_id = add_topic(conn)

    response = client.post(f"/topics/{topic_id}/sources", data={"type": "carrier-pigeon"})

    assert response.status_code == 400


def test_an_optional_field_left_blank_uses_its_default(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    topic_id = add_topic(conn)

    client.post(
        f"/topics/{topic_id}/sources",
        data={"type": "google_news", "query": "midlakes united", "window": ""},
    )

    source = repo.sources_for_topic(conn, topic_id)[0]
    assert source.config == {"query": "midlakes united", "window": "1d"}


def test_editing_a_source_clears_its_failure_history(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The failures were usually caused by the setting just corrected."""
    topic_id = add_topic(conn)
    source_id = add_source(conn, topic_id, consecutive_failures=12)

    client.post(f"/sources/{source_id}", data={"type": "rss", "url": FEED_URL})

    source = repo.get_source(conn, source_id)
    assert source is not None and source.consecutive_failures == 0


def test_an_unchanged_url_keeps_its_cache_validators(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Throwing away a working ETag would refetch a whole feed for nothing."""
    topic_id = add_topic(conn)
    source_id = add_source(
        conn, topic_id, config={"url": FEED_URL, "etag": 'W/"abc"', "last_modified": "Mon"}
    )

    client.post(f"/sources/{source_id}", data={"type": "rss", "url": FEED_URL})

    source = repo.get_source(conn, source_id)
    assert source is not None and source.config["etag"] == 'W/"abc"'


def test_a_changed_url_drops_the_cache_validators(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    topic_id = add_topic(conn)
    source_id = add_source(conn, topic_id, config={"url": FEED_URL, "etag": 'W/"abc"'})

    client.post(f"/sources/{source_id}", data={"type": "rss", "url": "https://other.example/feed"})

    source = repo.get_source(conn, source_id)
    assert source is not None and "etag" not in source.config


def test_a_source_can_be_disabled_and_re_enabled(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    topic_id = add_topic(conn)
    source_id = add_source(conn, topic_id, consecutive_failures=20, enabled=False)

    client.post(f"/sources/{source_id}/enabled", data={"enabled": "true"})

    source = repo.get_source(conn, source_id)
    assert source is not None
    assert source.enabled is True
    assert source.consecutive_failures == 0


def test_a_source_can_be_deleted(client: TestClient, conn: sqlite3.Connection) -> None:
    topic_id = add_topic(conn)
    source_id = add_source(conn, topic_id)

    client.post(f"/sources/{source_id}/delete")

    assert repo.get_source(conn, source_id) is None


# --- the source editor form -------------------------------------------------


def test_the_fields_for_a_type_come_from_its_config_model(client: TestClient) -> None:
    """Adding an adapter in a later phase gives the UI its form for free."""
    page = client.get("/sources/fields", params={"type": "google_news"}).text

    assert 'name="query"' in page
    assert 'name="window"' in page


def test_cache_bookkeeping_is_not_shown_as_a_setting(client: TestClient) -> None:
    page = client.get("/sources/fields", params={"type": "rss"}).text

    assert 'name="url"' in page
    assert 'name="etag"' not in page


def test_an_unknown_type_has_no_fields(client: TestClient) -> None:
    assert client.get("/sources/fields", params={"type": "nope"}).status_code == 404


def test_the_google_news_url_is_shown_as_it_is_typed(client: TestClient) -> None:
    """Seeing the quoting before saving is the whole point."""
    response = client.get(
        "/sources/google-news-url", params={"query": '"Midlakes United" -realty', "window": "7d"}
    )

    assert "%22Midlakes%20United%22%20-realty" in response.text
    assert "when:7d" in response.text


def test_a_bad_window_is_reported_in_place(client: TestClient) -> None:
    response = client.get("/sources/google-news-url", params={"query": "x", "window": "soon"})

    assert "12h or 7d" in response.text


# --- the test-source feature ------------------------------------------------


@respx.mock
def test_testing_a_source_shows_what_came_back(client: TestClient) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_basic.xml"))
    )

    response = client.post("/sources/test", data={"type": "rss", "url": FEED_URL})

    assert response.status_code == 200
    assert "Rust 1.90 lands" in response.text
    assert "Nothing was saved" in response.text


@respx.mock
def test_testing_a_source_writes_nothing(client: TestClient, conn: sqlite3.Connection) -> None:
    """The feature exists to refine a query before anything is stored."""
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_basic.xml"))
    )

    client.post("/sources/test", data={"type": "rss", "url": FEED_URL})

    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"] == 0


@respx.mock
def test_only_the_first_five_items_are_shown(client: TestClient) -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_basic.xml"))
    )

    response = client.post("/sources/test", data={"type": "rss", "url": FEED_URL})

    assert response.text.count("<li>") <= 5


@respx.mock
def test_a_dead_source_reports_the_error_in_place(client: TestClient) -> None:
    respx.get(FEED_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    response = client.post("/sources/test", data={"type": "rss", "url": FEED_URL})

    assert response.status_code == 200
    assert "Test failed" in response.text
    assert "no route to host" in response.text


def test_testing_an_invalid_config_reports_the_validation_error(client: TestClient) -> None:
    response = client.post("/sources/test", data={"type": "rss", "url": "arstechnica.com"})

    assert "Test failed" in response.text


@respx.mock
def test_a_feed_with_nothing_recent_says_so(client: TestClient) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=b"<rss></rss>"))

    response = client.post("/sources/test", data={"type": "rss", "url": FEED_URL})

    assert "No items came back" in response.text


# --- source health ----------------------------------------------------------


def test_auto_disabled_sources_are_pinned_to_the_top(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    topic_id = add_topic(conn)
    healthy = add_source(conn, topic_id)
    dead = add_source(
        conn, topic_id, "google_news", {"query": "x"}, enabled=False, consecutive_failures=20
    )

    rows = repo.source_health(conn)

    assert rows[0].source.id == dead
    assert rows[0].auto_disabled is True
    assert [row.source.id for row in rows if row.source.id == healthy]


def test_a_manually_disabled_source_is_not_auto_disabled(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Off because a person turned it off is a different thing to report."""
    topic_id = add_topic(conn)
    add_source(conn, topic_id, enabled=False, consecutive_failures=0)

    assert repo.source_health(conn)[0].auto_disabled is False


def test_the_health_page_names_the_last_error(client: TestClient, conn: sqlite3.Connection) -> None:
    topic_id = add_topic(conn, "Broken")
    source_id = add_source(conn, topic_id, consecutive_failures=20, enabled=False)
    conn.execute(
        "UPDATE sources SET last_error = 'HTTPStatusError: 404', last_status = 'error'"
        " WHERE id = ?",
        (source_id,),
    )

    page = client.get("/health").text

    assert "auto disabled" in page
    assert "HTTPStatusError: 404" in page
    assert "disabled automatically" in page


def test_a_dead_source_can_be_revived(client: TestClient, conn: sqlite3.Connection) -> None:
    topic_id = add_topic(conn)
    source_id = add_source(conn, topic_id, enabled=False, consecutive_failures=20)

    client.post(f"/sources/{source_id}/revive")

    source = repo.get_source(conn, source_id)
    assert source is not None
    assert source.enabled is True
    assert source.consecutive_failures == 0


def test_the_last_successful_fetch_is_shown(client: TestClient, conn: sqlite3.Connection) -> None:
    topic_id = add_topic(conn)
    source_id = add_source(conn, topic_id)
    conn.execute(
        "INSERT INTO fetch_log (source_id, run_at, status) VALUES (?, ?, 'ok')",
        (source_id, db.to_iso_utc(NOW)),
    )

    assert repo.source_health(conn)[0].last_ok_at is not None


# --- the brief archive ------------------------------------------------------


def test_the_archive_is_empty_to_begin_with(client: TestClient) -> None:
    assert "No briefs yet" in client.get("/briefs").text


def test_a_brief_renders_from_its_stored_html(client: TestClient, conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO briefs (brief_date, markdown, html, item_count, model)"
        " VALUES ('2026-09-01', '## Topic', '<h2>Topic</h2>', 2, 'claude-sonnet-5')"
    )

    page = client.get("/briefs/2026-09-01").text

    assert "<h2>Topic</h2>" in page
    assert "claude-sonnet-5" in page


def test_a_degraded_brief_says_why(client: TestClient, conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO briefs (brief_date, markdown, html, item_count, degraded, notes)"
        " VALUES ('2026-09-01', 'x', '<p>x</p>', 1, 1, 'relevance filter unavailable')"
    )

    page = client.get("/briefs/2026-09-01").text

    assert "Degraded" in page
    assert "relevance filter unavailable" in page


def test_a_missing_brief_is_a_readable_404(client: TestClient) -> None:
    response = client.get("/briefs/1999-01-01")

    assert response.status_code == 404
    assert "no brief for 1999-01-01" in response.text
    assert "Back to topics" in response.text


def test_generate_now_runs_the_composer(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same code path the CronJob takes, degraded paths included."""
    from spanreed import llm as llm_module

    monkeypatch.setattr(llm_module, "build", lambda _settings: None)
    add_topic(conn)

    response = client.post("/briefs/generate", follow_redirects=False)

    assert response.status_code == 303
    assert conn.execute("SELECT COUNT(*) AS n FROM briefs").fetchone()["n"] == 1


# --- feed discovery ---------------------------------------------------------


@respx.mock
def test_discovery_finds_a_feed_on_a_pasted_site(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    topic_id = add_topic(conn)
    respx.get("https://example.test/").mock(
        return_value=httpx.Response(200, content=fixture_bytes("site_with_feed.html"))
    )
    respx.get("https://example.test/feed/").mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_basic.xml"))
    )
    respx.get("https://example.test/comments/atom.xml").mock(return_value=httpx.Response(404))

    page = client.get(f"/topics/{topic_id}/discover", params={"url": "example.test"}).text

    assert "https://example.test/feed/" in page
    assert "Add as a source" in page


def test_discovery_with_no_url_asks_for_nothing(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """The input fires on keyup, so an empty box must not start fetching."""
    topic_id = add_topic(conn)

    response = client.get(f"/topics/{topic_id}/discover", params={"url": ""})

    assert response.status_code == 200
    assert response.text.strip() == ""


@respx.mock
def test_discovery_says_so_when_it_finds_nothing(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    topic_id = add_topic(conn)
    respx.get("https://example.test/").mock(
        return_value=httpx.Response(200, content=fixture_bytes("site_without_feed.html"))
    )
    for path in ("/feed/", "/rss", "/feed.xml", "/atom.xml", "/index.xml", "/blog/feed/"):
        respx.get(f"https://example.test{path}").mock(return_value=httpx.Response(404))

    page = client.get(f"/topics/{topic_id}/discover", params={"url": "example.test"}).text

    assert "No feed found" in page


def test_discovery_on_a_topic_that_is_gone_is_a_404(client: TestClient) -> None:
    assert client.get("/topics/999/discover", params={"url": "x.test"}).status_code == 404


# --- the new source types ---------------------------------------------------


def test_every_registered_adapter_has_an_editor_form(client: TestClient) -> None:
    """The form is read off each adapter's config model, so a new adapter gets
    its fields without anybody editing a template."""
    from spanreed.sources import CONFIG_MODELS

    for source_type in CONFIG_MODELS:
        page = client.get("/sources/fields", params={"type": source_type}).text
        assert 'name="' in page, source_type


def test_the_espn_form_offers_its_three_fields(client: TestClient) -> None:
    page = client.get("/sources/fields", params={"type": "espn"}).text

    for field in ("sport", "league", "team_id"):
        assert f'name="{field}"' in page


def test_a_reddit_source_can_be_added_through_the_ui(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    topic_id = add_topic(conn)

    client.post(
        f"/topics/{topic_id}/sources",
        data={"type": "reddit", "subreddit": "r/rust", "listing": "top", "window": "week"},
    )

    source = repo.sources_for_topic(conn, topic_id)[0]
    assert source.config == {"subreddit": "rust", "listing": "top", "window": "week"}


def test_an_invalid_reddit_listing_is_refused(client: TestClient, conn: sqlite3.Connection) -> None:
    topic_id = add_topic(conn)

    response = client.post(
        f"/topics/{topic_id}/sources",
        data={"type": "reddit", "subreddit": "rust", "listing": "popular"},
    )

    assert response.status_code == 400
    assert repo.sources_for_topic(conn, topic_id) == []
