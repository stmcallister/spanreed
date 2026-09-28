"""End to end composer tests, with the model stubbed and no network.

Covers the happy path and every degraded path, because the degraded paths are
the ones that run on the morning something is broken.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from newsbrief import db
from newsbrief import llm as llm_module
from newsbrief.compose import pipeline
from newsbrief.config import Settings
from tests.conftest import NOW, StubLlm, add_source, add_topic
from tests.test_cluster import insert_item, make_item


@pytest.fixture
def populated(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Two topics, a handful of fresh items, as the collector would have left it."""
    team = add_topic(conn, "Midlakes United", kind="team", max_items=3)
    tech = add_topic(conn, "Rust", kind="tech", slug="rust", max_items=3)
    team_source = add_source(conn, team)
    tech_source = add_source(conn, tech)

    for index, (topic_id, source_id, title) in enumerate(
        [
            (team, team_source, "Midlakes sign a goalkeeper from Denmark"),
            (team, team_source, "Final: Midlakes 2, Rovers 1"),
            (tech, tech_source, "Rust 1.90 lands with faster incremental builds"),
            (tech, tech_source, "Cargo gains a workspace wide lockfile option"),
        ],
        start=1,
    ):
        insert_item(
            conn,
            make_item(
                index,
                title,
                f"https://a.example/{index}",
                topic_id=topic_id,
                source_id=source_id,
                published_at=db.to_iso_utc(NOW - timedelta(hours=index)),
                summary=f"Summary for {title}.",
            ),
        )
    return conn


@pytest.fixture
def offline(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Deep read is off by default in these tests, so nothing wants the network."""
    return settings.model_copy(update={"deep_read_items": 0})


def stub_llm(monkeypatch: pytest.MonkeyPatch, llm: StubLlm | None) -> None:
    monkeypatch.setattr(llm_module, "build", lambda _settings: llm)


# --- the happy path ---------------------------------------------------------


def test_a_brief_is_written_and_archived(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = StubLlm(prose="## Midlakes United\n\n- [A story](https://a.example/1) It happened.")
    stub_llm(monkeypatch, llm)

    markdown, brief_id = pipeline.compose(populated, offline, NOW)

    row = populated.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["degraded"] == 0
    assert row["model"] == "stub-write-model"
    assert row["item_count"] == 4
    assert "It happened." in markdown
    assert row["notes"] is None


def test_token_usage_reaches_the_brief_row(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_llm(monkeypatch, StubLlm())

    _, brief_id = pipeline.compose(populated, offline, NOW)

    row = populated.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["input_tokens"] > 0
    assert row["output_tokens"] > 0


def test_the_footer_is_on_every_brief(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_llm(monkeypatch, StubLlm())

    markdown, _ = pipeline.compose(populated, offline, NOW)

    assert "All sources fetched cleanly in the last 24 hours." in markdown


def test_a_failed_source_is_named_in_the_footer(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole observability story, and it is sufficient."""
    populated.execute(
        "INSERT INTO fetch_log (source_id, run_at, status, error) VALUES (1, ?, 'error', 'boom')",
        (db.to_iso_utc(NOW - timedelta(hours=2)),),
    )
    stub_llm(monkeypatch, StubLlm())

    markdown, _ = pipeline.compose(populated, offline, NOW)

    assert "Midlakes United (rss)" in markdown


def test_an_older_failure_is_not_named(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    populated.execute(
        "INSERT INTO fetch_log (source_id, run_at, status, error) VALUES (1, ?, 'error', 'boom')",
        (db.to_iso_utc(NOW - timedelta(hours=40)),),
    )
    stub_llm(monkeypatch, StubLlm())

    markdown, _ = pipeline.compose(populated, offline, NOW)

    assert "All sources fetched cleanly" in markdown


def test_the_item_cap_is_enforced_in_code(
    conn: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not left to a prompt. A brief that grows unbounded stops being read."""
    topic = add_topic(conn, "Busy", max_items=99)
    source = add_source(conn, topic)
    for index in range(1, 41):
        insert_item(
            conn,
            make_item(
                index,
                f"A completely distinct story about subject {index}",
                f"https://a.example/{index}",
                topic_id=topic,
                source_id=source,
                published_at=db.to_iso_utc(NOW - timedelta(minutes=index)),
            ),
        )
    stub_llm(monkeypatch, StubLlm())

    _, brief_id = pipeline.compose(conn, offline, NOW)

    row = conn.execute("SELECT item_count FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["item_count"] == offline.max_brief_items == 15


@respx.mock
def test_the_top_items_are_deep_read_and_reach_the_writer(
    populated: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This stage is why the leading items read like something a person wrote."""
    body = (
        "The club confirmed the signing on Monday after a medical in Copenhagen. "
        "The fee was not disclosed and the contract runs for three seasons, with "
        "the manager saying the squad badly needed the extra depth in goal."
    )
    for index in range(1, 5):
        respx.get(f"https://a.example/{index}").mock(
            return_value=httpx.Response(
                200, text=f"<html><body><article><p>{body}</p></article></body></html>"
            ),
        )
    llm = StubLlm()
    stub_llm(monkeypatch, llm)

    pipeline.compose(populated, settings, NOW)

    prompt = llm.write_prompts[0]
    assert prompt.count("article text:") == settings.deep_read_items == 3
    assert "feed summary:" in prompt


@respx.mock
def test_a_dead_article_falls_back_to_the_feed_summary(
    populated: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(url__regex=r"https://a\.example/\d+").mock(side_effect=httpx.ConnectError("no route"))
    llm = StubLlm()
    stub_llm(monkeypatch, llm)

    _, brief_id = pipeline.compose(populated, settings, NOW)

    assert "article text:" not in llm.write_prompts[0]
    row = populated.execute("SELECT degraded FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["degraded"] == 0


# --- the degraded paths -----------------------------------------------------


def test_no_model_configured_still_produces_a_brief(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_llm(monkeypatch, None)

    markdown, brief_id = pipeline.compose(populated, offline, NOW)

    row = populated.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["degraded"] == 1
    assert "no model configured" in row["notes"]
    assert row["item_count"] > 0
    assert "Midlakes sign a goalkeeper" in markdown


def test_a_failed_filter_falls_back_to_source_and_recency(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_llm(monkeypatch, StubLlm(classify_error=RuntimeError("overloaded")))

    markdown, brief_id = pipeline.compose(populated, offline, NOW)

    row = populated.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["degraded"] == 1
    assert "relevance filter unavailable" in row["notes"]
    assert row["item_count"] > 0
    assert "Note: relevance filter unavailable" in markdown


def test_a_failed_write_emits_a_plain_list(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded brief is always better than no brief."""
    stub_llm(monkeypatch, StubLlm(write_error=RuntimeError("overloaded")))

    markdown, brief_id = pipeline.compose(populated, offline, NOW)

    row = populated.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["degraded"] == 1
    assert "items listed as found" in row["notes"]
    assert "[Midlakes sign a goalkeeper from Denmark](https://a.example/1)" in markdown


def test_an_empty_database_still_produces_a_brief(
    conn: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_llm(monkeypatch, StubLlm())

    markdown, brief_id = pipeline.compose(conn, offline, NOW)

    assert "Nothing to report today." in markdown
    row = conn.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["item_count"] == 0
    assert row["degraded"] == 0


# --- housekeeping -----------------------------------------------------------


def test_the_brief_date_is_the_readers_date_not_the_utc_one() -> None:
    """06:00 Pacific is already the next day in UTC."""
    early = datetime(2026, 9, 2, 13, 0, tzinfo=UTC)

    assert pipeline.brief_date_for(early, "America/Los_Angeles") == "2026-09-02"


def test_an_unknown_timezone_falls_back_to_utc() -> None:
    assert pipeline.brief_date_for(NOW, "Mars/Olympus_Mons") == "2026-09-01"


def test_old_items_are_purged(populated: sqlite3.Connection, offline: Settings) -> None:
    topic = add_topic(populated, "Old", slug="old")
    source = add_source(populated, topic)
    insert_item(
        populated,
        make_item(
            99,
            "An item from long ago",
            "https://a.example/99",
            topic_id=topic,
            source_id=source,
        ),
    )
    populated.execute(
        "UPDATE items SET fetched_at = ? WHERE id = 99", (db.to_iso_utc(NOW - timedelta(days=90)),)
    )

    deleted = pipeline.purge_old_items(populated, NOW, offline.item_retention_days)

    assert deleted == 1
    assert populated.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 4


def test_purging_keeps_the_briefs(populated: sqlite3.Connection, offline: Settings) -> None:
    """Briefs are kept forever. They are small."""
    stub = populated
    pipeline.purge_old_items(stub, NOW + timedelta(days=365), offline.item_retention_days)

    assert stub.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


# --- delivery ---------------------------------------------------------------


def test_the_brief_is_emailed_after_it_is_archived(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from newsbrief.deliver import email as email_module

    sent: list[dict[str, object]] = []

    def record(settings: Settings, **kwargs: object) -> email_module.DeliveryResult:
        # The brief must already be in the database by the time this runs.
        stored = populated.execute("SELECT COUNT(*) AS n FROM briefs").fetchone()["n"]
        sent.append({**kwargs, "briefs_already_archived": stored})
        return email_module.DeliveryResult(status="sent")

    monkeypatch.setattr(email_module, "send", record)
    stub_llm(monkeypatch, StubLlm())

    pipeline.compose(populated, offline, NOW)

    assert len(sent) == 1
    assert sent[0]["briefs_already_archived"] == 1
    assert sent[0]["brief_date"] == "2026-09-01"
    assert sent[0]["degraded"] is False
    assert "<h2>" in str(sent[0]["html"])


def test_a_failed_delivery_does_not_lose_the_brief(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archive first, then email. That ordering is the whole point."""
    from newsbrief.deliver import email as email_module

    def explode(settings: Settings, **kwargs: object) -> email_module.DeliveryResult:
        return email_module.DeliveryResult(status="failed", error="connection refused")

    monkeypatch.setattr(email_module, "send", explode)
    stub_llm(monkeypatch, StubLlm())

    markdown, brief_id = pipeline.compose(populated, offline, NOW)

    assert markdown
    row = populated.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    assert row["item_count"] > 0


def test_a_degraded_brief_is_flagged_to_the_mailer(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from newsbrief.deliver import email as email_module

    seen: list[object] = []

    def record(settings: Settings, **kwargs: object) -> email_module.DeliveryResult:
        seen.append(kwargs["degraded"])
        return email_module.DeliveryResult(status="skipped")

    monkeypatch.setattr(email_module, "send", record)
    stub_llm(monkeypatch, None)

    pipeline.compose(populated, offline, NOW)

    assert seen == [True]


def test_an_unconfigured_mailer_is_simply_skipped(
    populated: sqlite3.Connection, offline: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No SMTP settings in the test fixture, so this exercises the real path."""
    stub_llm(monkeypatch, StubLlm())

    assert offline.smtp_configured is False
    markdown, _ = pipeline.compose(populated, offline, NOW)

    assert markdown
