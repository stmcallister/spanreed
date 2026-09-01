"""Tests for entrypoint dispatch."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
def test_known_commands_migrate_then_report_the_missing_phase(command: str, tmp_path: Path) -> None:
    """Each command runs migrations before dispatching, even while stubbed."""
    assert main([command]) == 1
    assert (tmp_path / "newsbrief.db").exists()


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
