"""Generic RSS and Atom feeds.

Conditional requests are the whole point of the extra config fields. Most feeds
honour `ETag` or `Last-Modified`, and a 304 costs nothing to make and nothing to
parse.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

import httpx
from pydantic import BaseModel, field_validator

from newsbrief.models import RawItem
from newsbrief.sources.base import (
    ConfigModelType,
    NotModifiedError,
    SourceConfig,
    drop_older_than,
    parse_feed,
)


class RssConfig(SourceConfig):
    """Config for an `rss` source.

    `etag` and `last_modified` are written back by the collector after every
    successful fetch. They are cache bookkeeping, not user settings, and the
    source editor leaves them alone.
    """

    url: str
    etag: str | None = None
    last_modified: str | None = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return cleaned


class RssAdapter:
    """Fetch and parse one feed URL."""

    type: ClassVar[str] = "rss"
    ConfigModel: ClassVar[ConfigModelType] = RssConfig

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(self, config: BaseModel, since: datetime) -> list[RawItem]:
        if not isinstance(config, RssConfig):
            raise TypeError(f"rss needs an RssConfig, got {type(config).__name__}")

        headers: dict[str, str] = {}
        if config.etag:
            headers["If-None-Match"] = config.etag
        if config.last_modified:
            headers["If-Modified-Since"] = config.last_modified

        response = self._client.get(config.url, headers=headers)
        if response.status_code == httpx.codes.NOT_MODIFIED:
            raise NotModifiedError(config.url)
        response.raise_for_status()

        # Overwrite unconditionally: a server that stopped sending validators
        # should not leave us replaying a stale one forever.
        config.etag = response.headers.get("etag")
        config.last_modified = response.headers.get("last-modified")

        return drop_older_than(parse_feed(response.content), since)
