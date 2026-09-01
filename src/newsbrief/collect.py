"""Collector workload: fetch every enabled source and store new items.

Placeholder. The real implementation lands in build phase 2, alongside the rss
and google_news adapters.
"""

from __future__ import annotations

from newsbrief.config import Settings


def run(settings: Settings) -> int:
    """Fetch all enabled sources. Returns the process exit code."""
    raise NotImplementedError("the collector arrives in build phase 2")
