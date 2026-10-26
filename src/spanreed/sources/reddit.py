"""Subreddit listings, via the .rss endpoint every listing has.

Two things break this adapter, and both are covered here. Reddit blocks
requests that do not carry a descriptive User-Agent, which `build_client`
supplies for the whole run. Reddit also rate limits, and the limit applies to
the client rather than to the feed, so the throttle below is shared by every
Reddit source in the process rather than held per adapter instance.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import ClassVar

import httpx
import structlog
from pydantic import BaseModel, field_validator

from spanreed.models import RawItem
from spanreed.sources.base import ConfigModelType, SourceConfig, drop_older_than, parse_feed

log = structlog.get_logger(__name__)

LISTINGS = ("top", "hot", "new", "rising", "controversial")
WINDOWS = ("hour", "day", "week", "month", "year", "all")

_SUBREDDIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{1,20}$")

RATE_LIMIT_SECONDS = 2.0
"""One request every two seconds, across every Reddit source in the process."""


class _Throttle:
    """Shared rate limit.

    Module level rather than per instance because the collector builds a new
    adapter for every source, so per instance state would not limit anything.
    The collector is single threaded and works through sources in order, so a
    plain sleep is the whole mechanism.
    """

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._last: float | None = None

    def wait(self) -> float:
        now = time.monotonic()
        slept = 0.0
        if self._last is not None:
            remaining = self.interval - (now - self._last)
            if remaining > 0:
                time.sleep(remaining)
                slept = remaining
        self._last = time.monotonic()
        return slept

    def reset(self) -> None:
        self._last = None


throttle = _Throttle(RATE_LIMIT_SECONDS)


class RedditConfig(SourceConfig):
    """Config for a `reddit` source."""

    subreddit: str
    listing: str = "top"
    window: str = "day"

    @field_validator("subreddit")
    @classmethod
    def _validate_subreddit(cls, value: str) -> str:
        cleaned = value.strip().lstrip("/").removeprefix("r/").strip("/")
        if not _SUBREDDIT.match(cleaned):
            raise ValueError("subreddit must be a plain name such as rust or SeattleSounders")
        return cleaned

    @field_validator("listing")
    @classmethod
    def _validate_listing(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in LISTINGS:
            raise ValueError(f"listing must be one of {', '.join(LISTINGS)}")
        return cleaned

    @field_validator("window")
    @classmethod
    def _validate_window(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if cleaned not in WINDOWS:
            raise ValueError(f"window must be one of {', '.join(WINDOWS)}")
        return cleaned


def build_url(config: RedditConfig) -> str:
    """The listing feed. `t` is ignored by listings that do not take a window."""
    return f"https://reddit.com/r/{config.subreddit}/{config.listing}/.rss?t={config.window}"


class RedditAdapter:
    """Fetch one subreddit listing."""

    type: ClassVar[str] = "reddit"
    ConfigModel: ClassVar[ConfigModelType] = RedditConfig

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(self, config: BaseModel, since: datetime) -> list[RawItem]:
        if not isinstance(config, RedditConfig):
            raise TypeError(f"reddit needs a RedditConfig, got {type(config).__name__}")

        slept = throttle.wait()
        if slept:
            log.debug("throttled before a reddit request", waited_seconds=round(slept, 2))

        # reddit.com redirects to www.reddit.com, which the shared client
        # follows. A client built without redirects would see only the 301.
        response = self._client.get(build_url(config))
        response.raise_for_status()
        return drop_older_than(parse_feed(response.content), since)
