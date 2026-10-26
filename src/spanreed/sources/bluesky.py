"""A Bluesky profile's posts, via the RSS feed every profile publishes.

Often the only machine readable output a small club puts out.

Bluesky posts have no title. The feed ships `<title></title>` on every item,
so the generic parser drops the whole feed unless titles are synthesized from
the post text, which is what this adapter asks for.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import ClassVar

import httpx
from pydantic import BaseModel, field_validator

from spanreed.models import RawItem
from spanreed.sources.base import ConfigModelType, SourceConfig, drop_older_than, parse_feed

# A handle is a domain name: letters, digits, hyphens, at least one dot.
_HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(\.[A-Za-z0-9][A-Za-z0-9-]*)+$")


class BlueskyConfig(SourceConfig):
    """Config for a `bluesky` source."""

    handle: str

    @field_validator("handle")
    @classmethod
    def _validate_handle(cls, value: str) -> str:
        cleaned = value.strip().lstrip("@").strip("/")
        cleaned = cleaned.removeprefix("https://bsky.app/profile/")
        if not _HANDLE.match(cleaned):
            raise ValueError("handle must look like sounders.bsky.social")
        return cleaned


def build_url(config: BlueskyConfig) -> str:
    """The profile feed. Bluesky redirects the handle to its DID."""
    return f"https://bsky.app/profile/{config.handle}/rss"


class BlueskyAdapter:
    """Fetch one Bluesky profile's posts."""

    type: ClassVar[str] = "bluesky"
    ConfigModel: ClassVar[ConfigModelType] = BlueskyConfig

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(self, config: BaseModel, since: datetime) -> list[RawItem]:
        if not isinstance(config, BlueskyConfig):
            raise TypeError(f"bluesky needs a BlueskyConfig, got {type(config).__name__}")

        response = self._client.get(build_url(config))
        response.raise_for_status()
        items = parse_feed(response.content, synthesize_title=True)
        return drop_older_than(items, since)
