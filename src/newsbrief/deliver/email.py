"""Optional email delivery.

Second, never first. The brief is already in the database by the time anything
here runs, so a mail server that is down costs a delivery and not a brief.
Nothing in this module raises into the pipeline.

Unconfigured is a normal state, not an error. With no SMTP settings the
composer simply archives and stops, and says so once at INFO.

The module is named `email` and sits inside a package, so `import smtplib` and
`from email.message import EmailMessage` below resolve to the standard library
rather than to this file. Python 3 imports are absolute, which is what makes
that safe.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Literal

import structlog

from newsbrief.config import Settings

log = structlog.get_logger(__name__)

DeliveryStatus = Literal["sent", "skipped", "failed"]

SMTP_TIMEOUT_SECONDS = 30.0

IMPLICIT_TLS_PORT = 465
"""Port 465 speaks TLS from the first byte. Everything else starts in the clear."""


@dataclass(slots=True)
class DeliveryResult:
    """What happened, for the log line and for the tests."""

    status: DeliveryStatus
    recipients: tuple[str, ...] = ()
    error: str | None = None


def parse_recipients(value: str | None) -> tuple[str, ...]:
    """Split SMTP_TO, which may name more than one address."""
    if not value:
        return ()
    parts = (part.strip() for part in value.replace(";", ",").split(","))
    return tuple(part for part in parts if part)


def subject_for(brief_date: str, degraded: bool) -> str:
    """The subject line. A degraded brief says so before it is opened."""
    return f"newsbrief {brief_date}{' (degraded)' if degraded else ''}"


def build_message(
    settings: Settings,
    *,
    brief_date: str,
    markdown: str,
    html: str,
    degraded: bool,
) -> EmailMessage:
    """A multipart message carrying the markdown and the rendered HTML.

    The markdown is the plain text part rather than a stripped down rendering
    of the HTML, because the brief is written as markdown and reads perfectly
    well as text.
    """
    message = EmailMessage()
    message["Subject"] = subject_for(brief_date, degraded)
    message["From"] = settings.smtp_from or ""
    message["To"] = ", ".join(parse_recipients(settings.smtp_to))
    message.set_content(markdown)
    message.add_alternative(html, subtype="html")
    return message


def _connect(settings: Settings) -> smtplib.SMTP | smtplib.SMTP_SSL:
    """Open a connection, encrypted whichever way this server expects."""
    host = settings.smtp_host or ""
    port = settings.smtp_port
    context = ssl.create_default_context()

    if port == IMPLICIT_TLS_PORT:
        return smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context)

    connection = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
    try:
        connection.starttls(context=context)
    except smtplib.SMTPNotSupportedError:
        # A server on the local network that does not offer STARTTLS. Keep
        # going rather than lose the delivery, and say so.
        log.warning("smtp server does not offer starttls, continuing unencrypted", host=host)
    return connection


def send(
    settings: Settings,
    *,
    brief_date: str,
    markdown: str,
    html: str,
    degraded: bool = False,
) -> DeliveryResult:
    """Email the brief. Never raises.

    Returns a result rather than a boolean so the caller can tell a skip from
    a failure, which are different things worth different log lines.
    """
    if not settings.smtp_configured:
        log.info("email delivery is not configured, skipping")
        return DeliveryResult(status="skipped")

    recipients = parse_recipients(settings.smtp_to)
    if not recipients:
        log.info("email delivery has no recipients, skipping")
        return DeliveryResult(status="skipped")

    message = build_message(
        settings, brief_date=brief_date, markdown=markdown, html=html, degraded=degraded
    )

    try:
        connection = _connect(settings)
        try:
            if settings.smtp_user and settings.smtp_password:
                connection.login(settings.smtp_user, settings.smtp_password)
            connection.send_message(message)
        finally:
            connection.quit()
    except Exception as exc:
        # The brief is already archived. A failed send is worth reporting and
        # is not worth failing the run over.
        detail = f"{type(exc).__name__}: {exc}"
        log.error("email delivery failed", error=detail, recipients=len(recipients))
        return DeliveryResult(status="failed", recipients=recipients, error=detail)

    log.info("brief emailed", recipients=len(recipients), brief_date=brief_date)
    return DeliveryResult(status="sent", recipients=recipients)
