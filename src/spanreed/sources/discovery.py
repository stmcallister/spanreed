"""Find the feeds on a site, given only the site.

Not a fetch adapter. The source editor calls this when someone pastes a URL,
and it answers with every feed it can find so they can pick one.

Three steps, in order of how likely they are to be right. The declared link in
the document head is the correct method and usually works. Probing common
paths catches the sites that publish a feed without advertising it. Anything
found either way is then fetched and parsed, because a candidate that turns out
to be a 404 page is worse than no candidate at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import structlog
from selectolax.lexbor import LexborHTMLParser

from spanreed.sources.base import parse_feed

log = structlog.get_logger(__name__)

FEED_TYPES = (
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
)

# Tried only when the page declares nothing. Ordered by how common they are.
PROBE_PATHS = (
    "/feed/",
    "/rss",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
    "/blog/feed/",
)

# A host, optionally with a port. Prose gets a scheme prefixed onto it just as
# readily as a domain does, so the result has to be checked for being a host.
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")

MAX_CANDIDATES = 8
"""A page that declares thirty feeds is a page we do not need all of."""

FETCH_TIMEOUT_SECONDS = 8.0
MAX_BYTES = 512 * 1024


@dataclass(slots=True)
class FeedCandidate:
    """One feed the user can pick, with enough detail to pick by."""

    url: str
    title: str | None
    item_count: int
    latest_title: str | None = None


def normalize_site_url(value: str) -> str | None:
    """Accept what a person actually pastes, including a bare domain."""
    text = value.strip()
    if not text:
        return None
    if "://" not in text:
        text = f"https://{text}"
    split = urlsplit(text)
    if split.scheme not in ("http", "https") or not split.netloc:
        return None
    # `hostname` rather than `netloc`, which would still carry any userinfo and
    # port. A bare name with no dot is not a site somebody meant to paste.
    host = split.hostname or ""
    if not _HOST.match(host) or ("." not in host and host != "localhost"):
        return None
    return urlunsplit((split.scheme, split.netloc, split.path or "/", split.query, ""))


def _get(client: httpx.Client, url: str) -> httpx.Response | None:
    """One GET, capped. Any failure is an answer of None, never an exception."""
    try:
        response = client.get(url, timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS))
        response.raise_for_status()
    except Exception as exc:
        log.debug("discovery request failed", url=url, error=f"{type(exc).__name__}: {exc}")
        return None
    return response


def declared_feeds(html: str, base_url: str) -> list[str]:
    """Feed URLs the document head declares. The correct method, when it works."""
    try:
        tree = LexborHTMLParser(html)
    except Exception:
        log.debug("could not parse the page markup", url=base_url, exc_info=True)
        return []

    found: list[str] = []
    for node in tree.css('link[rel~="alternate"]'):
        attrs = node.attributes
        kind = (attrs.get("type") or "").strip().lower()
        href = (attrs.get("href") or "").strip()
        if not href or kind not in FEED_TYPES:
            continue
        resolved = urljoin(base_url, href)
        if resolved not in found:
            found.append(resolved)
    return found


def probe_paths(base_url: str) -> list[str]:
    """The usual places, for a site that publishes a feed without saying so."""
    split = urlsplit(base_url)
    root = urlunsplit((split.scheme, split.netloc, "", "", ""))
    return [f"{root}{path}" for path in PROBE_PATHS]


def inspect(client: httpx.Client, url: str) -> FeedCandidate | None:
    """Fetch a candidate and read it. None when it is not actually a feed."""
    response = _get(client, url)
    if response is None:
        return None

    payload = response.content[:MAX_BYTES]
    items = parse_feed(payload, synthesize_title=True)
    if not items:
        return None

    return FeedCandidate(
        url=str(response.url),
        title=items[0].source_name,
        item_count=len(items),
        latest_title=items[0].title,
    )


def discover(client: httpx.Client, site_url: str) -> list[FeedCandidate]:
    """Every feed found for a site, each one confirmed by fetching it.

    A pasted feed URL is answered with itself, because that is what somebody
    who pastes a feed URL means.
    """
    normalized = normalize_site_url(site_url)
    if normalized is None:
        log.info("discovery was given something that is not a URL", value=site_url[:120])
        return []

    response = _get(client, normalized)
    candidates: list[str] = []

    if response is not None:
        payload = response.content[:MAX_BYTES]
        if parse_feed(payload, synthesize_title=True):
            # What was pasted is already a feed.
            candidates.append(str(response.url))
        else:
            candidates.extend(declared_feeds(payload.decode("utf-8", errors="replace"), normalized))

    if not candidates:
        candidates.extend(probe_paths(normalized))

    found: list[FeedCandidate] = []
    seen: set[str] = set()
    for url in candidates[: MAX_CANDIDATES * 2]:
        if url in seen:
            continue
        seen.add(url)
        candidate = inspect(client, url)
        if candidate is None or candidate.url in {f.url for f in found}:
            continue
        found.append(candidate)
        if len(found) >= MAX_CANDIDATES:
            break

    log.info("feed discovery finished", site=normalized, found=len(found))
    return found
