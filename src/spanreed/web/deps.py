"""Shared plumbing for the route modules: the connection and the templates."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates

from spanreed import db
from spanreed.config import Settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection per request, closed when the request ends.

    WAL and a busy timeout make this safe alongside the two CronJobs writing to
    the same file, and a per request connection keeps any write this UI makes
    inside a single short transaction.
    """
    conn = db.connect(request.app.state.settings.db_path, same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Config = Annotated[Settings, Depends(get_settings)]


def local_time(value: str | None, timezone: str) -> str:
    """Render a stored UTC timestamp in the reader's timezone.

    Storage is UTC everywhere. This is the only place that is allowed to care
    about America/Los_Angeles.
    """
    parsed = db.parse_timestamp(value)
    if parsed is None:
        return "never"
    try:
        zone = ZoneInfo(timezone)
    except Exception:
        return parsed.strftime("%Y-%m-%d %H:%M UTC")
    return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def relative_time(value: str | None, now: datetime | None = None) -> str:
    """How long ago, in the roughest terms that are still useful."""
    parsed = db.parse_timestamp(value)
    if parsed is None:
        return "never"
    delta = (now or datetime.now(UTC)) - parsed
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
