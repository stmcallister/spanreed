"""Shared test fixtures. No test in this suite touches the network."""

from __future__ import annotations

import json
import socket
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from spanreed import db
from spanreed.config import Settings
from spanreed.llm import Usage
from spanreed.models import Topic
from spanreed.sources.base import build_client

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _host_of(address: Any) -> str:
    return str(address[0]) if isinstance(address, tuple) and address else str(address)


def _refuse(target: str, kind: str) -> AssertionError:
    return AssertionError(f"this test tried to {kind} {target}. Record a fixture instead.")


@pytest.fixture(autouse=True, scope="session")
def _no_network() -> Iterator[None]:
    """Make a real outbound connection fail the test that attempts it.

    No test in this suite touches the network, and the fixtures exist so that
    none has to. Trusting that is not the same as enforcing it: adding
    `seed/topics.yaml` quietly turned the entry point tests into something that
    bootstrapped three topics and fetched seven live feeds, and nothing failed.
    A suite that silently depends on the network is one that breaks on a train,
    and a recorded fixture that has stopped matching reality is a bug this
    would otherwise hide.

    These wrappers are typed loosely on purpose. They stand in front of C
    functions whose real signatures they only pass through.

    Loopback stays open, because `TestClient` and the SMTP stub are local.
    """

    def guard_connect(self: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
        host = _host_of(address)
        if host not in _LOCAL_HOSTS:
            raise _refuse(host, "reach")
        return _real_connect(self, address, *args, **kwargs)

    def guard_connect_ex(self: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
        host = _host_of(address)
        if host not in _LOCAL_HOSTS:
            raise _refuse(host, "reach")
        return _real_connect_ex(self, address, *args, **kwargs)

    def guard_dns(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and str(host) not in _LOCAL_HOSTS:
            raise _refuse(repr(host), "resolve")
        return _real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = guard_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guard_connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = guard_dns
    try:
        yield
    finally:
        socket.socket.connect = _real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _real_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = _real_getaddrinfo


FIXTURES = Path(__file__).parent / "fixtures"

# The recorded feeds carry dates around here, so tests anchor to a fixed clock
# rather than to the wall clock.
NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=tmp_path / "spanreed.db", anthropic_api_key=None)


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    connection = db.connect(settings.db_path)
    db.migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(settings: Settings) -> Iterator[httpx.Client]:
    with build_client(settings) as http_client:
        yield http_client


def add_topic(
    conn: sqlite3.Connection,
    name: str = "Midlakes United",
    *,
    slug: str | None = None,
    kind: str = "team",
    enabled: bool = True,
    relevance_hint: str = "anything about the club",
    max_items: int = 3,
    priority: int = 50,
) -> int:
    cursor = conn.execute(
        "INSERT INTO topics (name, slug, kind, enabled, relevance_hint, max_items, priority)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            slug or name.lower().replace(" ", "-"),
            kind,
            int(enabled),
            relevance_hint,
            max_items,
            priority,
        ),
    )
    return int(cursor.lastrowid or 0)


def make_topic(
    topic_id: int = 1,
    name: str = "Midlakes United",
    *,
    kind: str = "team",
    max_items: int = 3,
    priority: int = 50,
    relevance_hint: str = "anything about the club",
) -> Topic:
    """A Topic without a database behind it, for the stages that only read one."""
    return Topic(
        id=topic_id,
        name=name,
        slug=name.lower().replace(" ", "-"),
        kind="team" if kind == "team" else "tech",
        enabled=True,
        relevance_hint=relevance_hint,
        max_items=max_items,
        priority=priority,
        created_at=db.to_iso_utc(NOW),
    )


class StubLlm:
    """Stands in for the SDK wrapper. Records what it was asked and answers.

    Every composer test uses this. Nothing in the suite constructs a real
    client, so nothing in the suite can reach the network or need a key.
    """

    def __init__(
        self,
        *,
        verdicts: dict[int, tuple[bool, float]] | None = None,
        prose: str = "## Midlakes United\n\n- [A story](https://a.example/1) It happened.",
        classify_error: Exception | None = None,
        write_error: Exception | None = None,
    ) -> None:
        self.usage = Usage()
        self.verdicts = verdicts or {}
        self.prose = prose
        self.classify_error = classify_error
        self.write_error = write_error
        self.classify_prompts: list[str] = []
        self.write_prompts: list[str] = []

    @property
    def filter_model(self) -> str:
        return "stub-filter-model"

    @property
    def write_model(self) -> str:
        return "stub-write-model"

    def classify(self, system: str, prompt: str, schema: type[Any], max_tokens: int) -> Any:
        self.classify_prompts.append(prompt)
        if self.classify_error is not None:
            raise self.classify_error
        self.usage.record(100, 50)
        ids = [
            int(line.removeprefix("id: "))
            for line in prompt.splitlines()
            if line.startswith("id: ")
        ]
        return schema(
            verdicts=[
                {
                    "id": item_id,
                    "keep": self.verdicts.get(item_id, (True, 0.9))[0],
                    "score": self.verdicts.get(item_id, (True, 0.9))[1],
                    "reason": "stub verdict",
                }
                for item_id in ids
            ]
        )

    def write(self, system: str, prompt: str, max_tokens: int) -> str:
        self.write_prompts.append(prompt)
        if self.write_error is not None:
            raise self.write_error
        self.usage.record(200, 300)
        return self.prose


def add_source(
    conn: sqlite3.Connection,
    topic_id: int,
    source_type: str = "rss",
    config: dict[str, object] | None = None,
    *,
    enabled: bool = True,
    consecutive_failures: int = 0,
    last_fetch_at: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO sources (topic_id, type, config, enabled, consecutive_failures, last_fetch_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            topic_id,
            source_type,
            json.dumps(config or {"url": "https://arstechnica.com/feed/"}),
            int(enabled),
            consecutive_failures,
            last_fetch_at,
        ),
    )
    return int(cursor.lastrowid or 0)
