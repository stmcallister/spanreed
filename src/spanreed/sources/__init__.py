"""The adapter registry.

Adding a source type means writing the adapter and adding two lines here. The
collector and the web UI both go through `get_adapter`, so a type that is not
registered degrades to a skipped source rather than a crash.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from spanreed.sources.base import NotModifiedError, SourceAdapter, SourceConfig, build_client
from spanreed.sources.bluesky import BlueskyAdapter, BlueskyConfig
from spanreed.sources.discovery import FeedCandidate, discover
from spanreed.sources.espn import EspnAdapter, EspnConfig
from spanreed.sources.google_news import GoogleNewsAdapter, GoogleNewsConfig
from spanreed.sources.reddit import RedditAdapter, RedditConfig
from spanreed.sources.rss import RssAdapter, RssConfig
from spanreed.sources.rsshub import RsshubAdapter, RsshubConfig, RsshubUnconfiguredError

AdapterFactory = Callable[[httpx.Client], SourceAdapter]

ADAPTERS: dict[str, AdapterFactory] = {
    RssAdapter.type: RssAdapter,
    GoogleNewsAdapter.type: GoogleNewsAdapter,
    EspnAdapter.type: EspnAdapter,
    RedditAdapter.type: RedditAdapter,
    BlueskyAdapter.type: BlueskyAdapter,
    RsshubAdapter.type: RsshubAdapter,
}

CONFIG_MODELS: dict[str, type[SourceConfig]] = {
    RssAdapter.type: RssConfig,
    GoogleNewsAdapter.type: GoogleNewsConfig,
    EspnAdapter.type: EspnConfig,
    RedditAdapter.type: RedditConfig,
    BlueskyAdapter.type: BlueskyConfig,
    RsshubAdapter.type: RsshubConfig,
}


def get_adapter(source_type: str, client: httpx.Client) -> SourceAdapter | None:
    """Build the adapter for a source type, or None when it is not registered."""
    factory = ADAPTERS.get(source_type)
    return None if factory is None else factory(client)


__all__ = [
    "ADAPTERS",
    "CONFIG_MODELS",
    "BlueskyAdapter",
    "BlueskyConfig",
    "EspnAdapter",
    "EspnConfig",
    "FeedCandidate",
    "GoogleNewsAdapter",
    "GoogleNewsConfig",
    "NotModifiedError",
    "RedditAdapter",
    "RedditConfig",
    "RssAdapter",
    "RssConfig",
    "RsshubAdapter",
    "RsshubConfig",
    "RsshubUnconfiguredError",
    "SourceAdapter",
    "SourceConfig",
    "build_client",
    "discover",
    "get_adapter",
]
