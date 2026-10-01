"""Tests for entrypoint dispatch."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from newsbrief import db
from newsbrief.__main__ import COMMANDS, main
from newsbrief.config import Settings, get_settings

AMBIENT_VARS = (
    "ANTHROPIC_API_KEY",
    "RSSHUB_BASE_URL",
    "SMTP_HOST",
    "SMTP_FROM",
    "SMTP_TO",
    "BASIC_AUTH_PASSWORD",
)


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the developer's real environment out of the test run."""
    for name in AMBIENT_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "newsbrief.db"))
    # Point the seed path at nothing. Left at its default it resolves to the
    # real seed/topics.yaml in the repo, and `main(["collect"])` would then
    # bootstrap three topics and fetch seven live feeds. No test touches the
    # network, and a default that quietly does is worse than a loud one.
    monkeypatch.setenv("SEED_PATH", str(tmp_path / "no-such-seed.yaml"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _do_not_actually_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    """The web command blocks forever by design, which a test cannot allow."""
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)


def test_settings_read_the_database_path_from_the_environment(tmp_path: Path) -> None:
    assert get_settings().db_path == tmp_path / "newsbrief.db"


def test_no_arguments_prints_usage_and_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: python -m newsbrief" in capsys.readouterr().out


def test_help_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help"]) == 0
    assert "collect" in capsys.readouterr().out


def test_unknown_command_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().err


@pytest.mark.parametrize("command", COMMANDS)
def test_known_commands_run_migrations_first(command: str, tmp_path: Path) -> None:
    """Each command runs migrations before dispatching, even while stubbed."""
    main([command])
    assert (tmp_path / "newsbrief.db").exists()


def test_collect_succeeds_with_no_sources_configured() -> None:
    """An empty database is not an error, it is just a quiet run."""
    assert main(["collect"]) == 0


def test_compose_succeeds_with_an_empty_database(capsys: pytest.CaptureFixture[str]) -> None:
    """No items is a quiet day, not a failure. A brief is still produced."""
    assert main(["compose"]) == 0
    assert "Nothing to report today." in capsys.readouterr().out


def test_the_web_command_serves() -> None:
    """With uvicorn stubbed this only proves the app builds and dispatch works."""
    assert main(["web"]) == 0


def test_a_broken_database_path_exits_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("DB_PATH", str(blocker / "newsbrief.db"))
    get_settings.cache_clear()
    assert main(["collect"]) == 1


def test_optional_integrations_default_to_unconfigured() -> None:
    settings = Settings()
    assert settings.llm_configured is False
    assert settings.smtp_configured is False


# --- bootstrapping and export -----------------------------------------------


def test_an_empty_database_is_seeded_on_first_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whichever workload starts first does it, so every command bootstraps."""
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text(
        "topics:\n  - name: Rust\n    kind: tech\n    relevance_hint: toolchain\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SEED_PATH", str(seed_file))
    get_settings.cache_clear()

    assert main(["collect"]) == 0

    conn = db.connect(tmp_path / "newsbrief.db")
    try:
        assert [r["name"] for r in conn.execute("SELECT name FROM topics")] == ["Rust"]
    finally:
        conn.close()


def test_a_broken_seed_file_does_not_stop_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UI can still be used to enter the configuration by hand."""
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("topics: [oops\n  - broken", encoding="utf-8")
    monkeypatch.setenv("SEED_PATH", str(seed_file))
    get_settings.cache_clear()

    assert main(["collect"]) == 0


def add_a_topic(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "newsbrief.db")
    try:
        db.migrate(conn)
        with db.write_transaction(conn):
            conn.execute(
                "INSERT INTO topics (name, slug, kind, relevance_hint)"
                " VALUES ('Rust', 'rust', 'tech', 'toolchain releases')"
            )
    finally:
        conn.close()


def test_export_topics_writes_the_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_a_topic(tmp_path)

    assert main(["export-topics"]) == 0

    out = capsys.readouterr().out
    assert "# Exported by" in out
    assert "name: Rust" in out


def test_export_topics_stdout_is_nothing_but_yaml(tmp_path: Path) -> None:
    """Its stdout is the file somebody is redirecting, so a log line in the
    middle of it would corrupt what they saved.

    Run in a subprocess on purpose. structlog caches a logger on first use, so
    inside one pytest process an earlier command has already fixed the stream
    and the routing cannot be observed. A real invocation is the only honest
    test of it, and it is also exactly how this command gets used.
    """
    add_a_topic(tmp_path)

    finished = subprocess.run(
        [sys.executable, "-m", "newsbrief", "export-topics"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DB_PATH": str(tmp_path / "newsbrief.db"),
            "SEED_PATH": str(tmp_path / "no-such-seed.yaml"),
        },
        check=True,
        timeout=60,
    )

    assert finished.stdout.startswith("# Exported by")
    assert '"event"' not in finished.stdout
    assert '"event"' in finished.stderr

    parsed = yaml.safe_load(finished.stdout)
    assert [topic["name"] for topic in parsed["topics"]] == ["Rust"]


def test_export_topics_does_not_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A read command that wrote three topics into an empty database and then
    printed them back would be lying about what is configured."""
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text(
        "topics:\n  - name: Rust\n    kind: tech\n    relevance_hint: toolchain\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SEED_PATH", str(seed_file))
    get_settings.cache_clear()

    assert main(["export-topics"]) == 0

    assert "name: Rust" not in capsys.readouterr().out
    conn = db.connect(tmp_path / "newsbrief.db")
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM topics").fetchone()["n"] == 0
    finally:
        conn.close()


def test_export_topics_is_listed_in_the_usage(capsys: pytest.CaptureFixture[str]) -> None:
    main(["help"])

    assert "export-topics" in capsys.readouterr().out
