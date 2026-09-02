"""Google News search feeds.

The workhorse for teams and subjects that mainstream outlets ignore. Quoted
phrases and negative terms both work in `query`, so `"Midlakes United" -realty`
is valid and is usually how a topic named after a common word gets tamed.

Item links are Google redirects. We store the redirect as `url` and make one
attempt at resolving the publisher URL. Failure is the normal case for newer
Google links, so there is no retry loop and no error path here.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import ClassVar
from urllib.parse import quote, urlsplit

import httpx
import structlog
from pydantic import BaseModel, field_validator

from newsbrief.models import RawItem
from newsbrief.sources.base import ConfigModelType, SourceConfig, drop_older_than, parse_feed

log = structlog.get_logger(__name__)

SEARCH_URL = "https://news.google.com/rss/search"
GOOGLE_HOST_SUFFIX = "news.google.com"

_WINDOW = re.compile(r"^[1-9][0-9]{0,2}[hd]$")

# One HEAD per item, with a short timeout, and we stop trying once a run has
# shown that resolution is not working. Without the circuit breaker a feed of
# twenty unresolvable links costs twenty timeouts.
RESOLVE_TIMEOUT_SECONDS = 3.0
RESOLVE_GIVE_UP_AFTER = 3


class GoogleNewsConfig(SourceConfig):
    """Config for a `google_news` source."""

    query: str
    window: str = "1d"

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("query must not be empty")
        return cleaned

    @field_validator("window")
    @classmethod
    def _validate_window(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not _WINDOW.match(cleaned):
            raise ValueError("window must look like 12h or 7d")
        return cleaned


def build_url(config: GoogleNewsConfig) -> str:
    """The exact URL that will be fetched.

    The source editor renders this live as the query is typed, so that quoting
    and negative terms are visible before the source is saved.
    """
    return (
        f"{SEARCH_URL}?q={quote(config.query, safe='')}+when:{config.window}"
        "&hl=en-US&gl=US&ceid=US:en"
    )


class GoogleNewsAdapter:
    """Fetch one Google News search feed."""

    type: ClassVar[str] = "google_news"
    ConfigModel: ClassVar[ConfigModelType] = GoogleNewsConfig

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(self, config: BaseModel, since: datetime) -> list[RawItem]:
        if not isinstance(config, GoogleNewsConfig):
            raise TypeError(f"google_news needs a GoogleNewsConfig, got {type(config).__name__}")

        response = self._client.get(build_url(config))
        response.raise_for_status()
        items = drop_older_than(parse_feed(response.content), since)
        self._resolve_links(items)
        return items

    def _resolve_links(self, items: list[RawItem]) -> None:
        failures = 0
        for item in items:
            if failures >= RESOLVE_GIVE_UP_AFTER:
                log.debug("giving up on link resolution for this fetch", resolved=0)
                return
            resolved = self._resolve(item.url)
            if resolved is None:
                failures += 1
                continue
            failures = 0
            item.canonical_url = resolved

    def _resolve(self, url: str) -> str | None:
        """One HEAD, following redirects. Any failure is normal, so return None."""
        try:
            response = self._client.head(
                url, timeout=RESOLVE_TIMEOUT_SECONDS, follow_redirects=True
            )
        except httpx.HTTPError:
            return None
        except Exception:
            log.debug("link resolution failed unexpectedly", url=url, exc_info=True)
            return None

        final = str(response.url)
        host = urlsplit(final).netloc.lower()
        if final == url or host.endswith(GOOGLE_HOST_SUFFIX):
            return None
        return final
