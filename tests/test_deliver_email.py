"""Tests for optional email delivery. No SMTP server is ever contacted."""

from __future__ import annotations

import smtplib
from typing import Any, ClassVar

import pytest

from newsbrief.config import Settings
from newsbrief.deliver import email
from newsbrief.deliver.email import parse_recipients, subject_for

MARKDOWN = "## Rust\n\n- [A story](https://a.example/1) It happened."
HTML = "<h2>Rust</h2>\n<ul><li>It happened.</li></ul>"


def configured(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "smtp_host": "smtp.example",
        "smtp_port": 587,
        "smtp_from": "brief@example",
        "smtp_to": "reader@example",
    }
    values.update(overrides)
    return Settings(**values)


class FakeSMTP:
    """Records what would have been sent."""

    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host: str, port: int, timeout: float = 0.0, **kwargs: Any) -> None:
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in: tuple[str, str] | None = None
        self.sent: list[Any] = []
        self.quit_called = False
        self.starttls_supported = True
        FakeSMTP.instances.append(self)

    def starttls(self, **kwargs: Any) -> None:
        if not self.starttls_supported:
            raise smtplib.SMTPNotSupportedError("no STARTTLS here")
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def send_message(self, message: Any) -> None:
        self.sent.append(message)

    def quit(self) -> None:
        self.quit_called = True


@pytest.fixture(autouse=True)
def _fake_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)


def send(settings: Settings, **overrides: Any) -> email.DeliveryResult:
    kwargs: dict[str, Any] = {
        "brief_date": "2026-09-28",
        "markdown": MARKDOWN,
        "html": HTML,
        "degraded": False,
    }
    kwargs.update(overrides)
    return email.send(settings, **kwargs)


# --- skipping cleanly -------------------------------------------------------


def test_nothing_configured_is_a_skip_not_a_failure() -> None:
    result = send(Settings())

    assert result.status == "skipped"
    assert FakeSMTP.instances == []


@pytest.mark.parametrize("missing", ["smtp_host", "smtp_from", "smtp_to"])
def test_a_half_configured_server_is_also_a_skip(missing: str) -> None:
    assert send(configured(**{missing: None})).status == "skipped"


def test_a_recipient_list_that_is_only_punctuation_is_a_skip() -> None:
    assert send(configured(smtp_to=" , ; ")).status == "skipped"


# --- recipients -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a@example", ("a@example",)),
        ("a@example,b@example", ("a@example", "b@example")),
        (" a@example , b@example ", ("a@example", "b@example")),
        ("a@example;b@example", ("a@example", "b@example")),
        ("", ()),
        (None, ()),
    ],
)
def test_recipients_are_parsed(value: str | None, expected: tuple[str, ...]) -> None:
    assert parse_recipients(value) == expected


# --- the message ------------------------------------------------------------


def test_the_subject_carries_the_date() -> None:
    assert subject_for("2026-09-28", False) == "newsbrief 2026-09-28"


def test_a_degraded_brief_says_so_before_it_is_opened() -> None:
    assert subject_for("2026-09-28", True) == "newsbrief 2026-09-28 (degraded)"


def test_the_message_carries_both_a_text_and_an_html_part() -> None:
    send(configured())
    message = FakeSMTP.instances[0].sent[0]

    assert message.get_content_type() == "multipart/alternative"
    parts = {part.get_content_type() for part in message.walk()}
    assert {"text/plain", "text/html"} <= parts


def test_the_plain_part_is_the_markdown_itself() -> None:
    """The brief is written as markdown and reads perfectly well as text."""
    send(configured())
    message = FakeSMTP.instances[0].sent[0]

    plain = next(p for p in message.walk() if p.get_content_type() == "text/plain")
    assert "A story" in plain.get_content()


def test_the_headers_name_the_sender_and_every_recipient() -> None:
    send(configured(smtp_to="a@example,b@example"))
    message = FakeSMTP.instances[0].sent[0]

    assert message["From"] == "brief@example"
    assert message["To"] == "a@example, b@example"


# --- connecting -------------------------------------------------------------


def test_the_usual_port_starts_tls() -> None:
    send(configured())

    assert FakeSMTP.instances[0].started_tls is True


def test_port_465_is_encrypted_from_the_first_byte() -> None:
    """It speaks TLS immediately, so there is no STARTTLS to negotiate."""
    send(configured(smtp_port=465))

    assert FakeSMTP.instances[0].started_tls is False


def test_a_server_without_starttls_still_delivers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A box on the local network. Losing the delivery would be worse."""

    def no_starttls(*args: Any, **kwargs: Any) -> FakeSMTP:
        instance = FakeSMTP(*args, **kwargs)
        instance.starttls_supported = False
        return instance

    monkeypatch.setattr(smtplib, "SMTP", no_starttls)
    result = send(configured())

    assert result.status == "sent"
    assert FakeSMTP.instances[0].sent


def test_credentials_are_used_when_they_are_given() -> None:
    send(configured(smtp_user="brief", smtp_password="hunter2"))

    assert FakeSMTP.instances[0].logged_in == ("brief", "hunter2")


def test_no_login_is_attempted_without_credentials() -> None:
    """Plenty of local relays accept mail from the network unauthenticated."""
    send(configured())

    assert FakeSMTP.instances[0].logged_in is None


def test_the_connection_is_always_closed() -> None:
    send(configured())

    assert FakeSMTP.instances[0].quit_called is True


# --- failing ----------------------------------------------------------------


def test_a_dead_server_is_reported_and_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The brief is already archived, so this is worth reporting and not worth
    failing the run over."""

    def refuse(*args: Any, **kwargs: Any) -> FakeSMTP:
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    result = send(configured())

    assert result.status == "failed"
    assert result.error is not None
    assert "connection refused" in result.error


def test_a_rejected_message_is_reported_and_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(self: FakeSMTP, message: Any) -> None:
        raise RuntimeError("550 mailbox unavailable")

    monkeypatch.setattr(FakeSMTP, "send_message", reject)
    result = send(configured())

    assert result.status == "failed"
    assert result.error is not None and "550" in result.error


def test_the_connection_is_closed_even_when_the_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(self: FakeSMTP, message: Any) -> None:
        raise RuntimeError("550 mailbox unavailable")

    monkeypatch.setattr(FakeSMTP, "send_message", reject)
    send(configured())

    assert FakeSMTP.instances[0].quit_called is True


def test_a_successful_send_names_its_recipients() -> None:
    result = send(configured(smtp_to="a@example,b@example"))

    assert result.status == "sent"
    assert result.recipients == ("a@example", "b@example")
