"""The adapter registry.

Adding a source type means writing the adapter and adding one line here. The
collector and the web UI both go through `get_adapter`, so a type that is not
registered degrades to a skipped source rather than a crash.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from newsbrief.sources.base import NotModifiedError, SourceAdapter, SourceConfig, build_client
from newsbrief.sources.google_news import GoogleNewsAdapter, GoogleNewsConfig
from newsbrief.sources.rss import RssAdapter, RssConfig

AdapterFactory = Callable[[httpx.Client], SourceAdapter]

ADAPTERS: dict[str, AdapterFactory] = {
    RssAdapter.type: RssAdapter,
    GoogleNewsAdapter.type: GoogleNewsAdapter,
}

CONFIG_MODELS: dict[str, type[SourceConfig]] = {
    RssAdapter.type: RssConfig,
    GoogleNewsAdapter.type: GoogleNewsConfig,
}


def get_adapter(source_type: str, client: httpx.Client) -> SourceAdapter | None:
    """Build the adapter for a source type, or None when it is not registered."""
    factory = ADAPTERS.get(source_type)
    return None if factory is None else factory(client)


__all__ = [
    "ADAPTERS",
    "CONFIG_MODELS",
    "GoogleNewsAdapter",
    "GoogleNewsConfig",
    "NotModifiedError",
    "RssAdapter",
    "RssConfig",
    "SourceAdapter",
    "SourceConfig",
    "build_client",
    "get_adapter",
]
