"""RSSHub routes, for the sources that publish no feed of their own.

Optional. RSSHub is a separate service, and with no base URL configured this
adapter skips cleanly rather than failing every fetch and eventually auto
disabling a source that was never misconfigured.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

import httpx
import structlog
from pydantic import BaseModel, field_validator

from newsbrief.config import get_settings
from newsbrief.models import RawItem
from newsbrief.sources.base import ConfigModelType, SourceConfig, drop_older_than, parse_feed

log = structlog.get_logger(__name__)


class RsshubConfig(SourceConfig):
    """Config for an `rsshub` source. The route is relative to the base URL."""

    route: str

    @field_validator("route")
    @classmethod
    def _validate_route(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("route must not be empty")
        if "://" in cleaned:
            raise ValueError("route is a path such as /github/issue/owner/repo, not a full URL")
        return "/" + cleaned.lstrip("/")


class RsshubUnconfiguredError(Exception):
    """No RSSHUB_BASE_URL, so this source cannot be fetched.

    The collector records this as skipped rather than failed, which is what
    keeps an unconfigured optional source out of the failure counter.
    """


def build_url(config: RsshubConfig, base_url: str) -> str:
    return f"{base_url.rstrip('/')}{config.route}"


class RsshubAdapter:
    """Fetch one RSSHub route."""

    type: ClassVar[str] = "rsshub"
    ConfigModel: ClassVar[ConfigModelType] = RsshubConfig

    def __init__(self, client: httpx.Client, base_url: str | None = None) -> None:
        self._client = client
        self._base_url = base_url

    @property
    def base_url(self) -> str | None:
        """The configured base, resolved late so tests can inject one."""
        if self._base_url is not None:
            return self._base_url
        return get_settings().rsshub_base_url

    def fetch(self, config: BaseModel, since: datetime) -> list[RawItem]:
        if not isinstance(config, RsshubConfig):
            raise TypeError(f"rsshub needs a RsshubConfig, got {type(config).__name__}")

        base = self.base_url
        if not base:
            raise RsshubUnconfiguredError("RSSHUB_BASE_URL is not set")

        response = self._client.get(build_url(config, base))
        response.raise_for_status()
        return drop_older_than(parse_feed(response.content), since)
