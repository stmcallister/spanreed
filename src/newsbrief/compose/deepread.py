"""Stage 3: a bounded full text pass over the few items at the top.

This stage is why the leading items read like something a person wrote rather
than a rearranged headline. It is also the only place in the composer that
touches the network, so it is kept on a short leash: a handful of items, six
seconds each, forty kilobytes each, and a feed summary to fall back on.

Nothing here can fail the brief. A fetch that times out, a paywall, or a page
that turns out to be a video player all produce the same outcome, which is that
the writer works from the feed summary instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import structlog
from selectolax.parser import HTMLParser

from newsbrief.models import Item

log = structlog.get_logger(__name__)

FETCH_TIMEOUT_SECONDS = 6.0
MAX_BYTES = 40 * 1024
MAX_TEXT_CHARS = 6000
"""Plenty for two or three sentences, and it keeps the write call small."""

MIN_TEXT_CHARS = 200
"""Shorter than this is a cookie banner or a paywall notice, not an article."""

ARTICLE_ACCEPT = "text/html, application/xhtml+xml;q=0.9, */*;q=0.5"

# Removed before reading the text, because otherwise the extract is mostly
# navigation and consent copy.
_STRIP_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "figure",
    "iframe",
    "svg",
)

# Tried in order. The first one that yields enough text wins.
_CONTENT_SELECTORS = ("article", "main", '[role="main"]', "body")


def _read_capped(client: httpx.Client, url: str) -> str:
    """Fetch at most `MAX_BYTES`, then stop reading.

    Streaming rather than a plain get, so a page that turns out to be enormous
    costs forty kilobytes rather than all of it.
    """
    with client.stream(
        "GET",
        url,
        timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
        headers={"Accept": ARTICLE_ACCEPT},
    ) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= MAX_BYTES:
                break
        payload = b"".join(chunks)[:MAX_BYTES]
        encoding = response.encoding or "utf-8"

    try:
        return payload.decode(encoding, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_text(html: str) -> str | None:
    """Pull the readable body out of a page, or None when there is not one.

    The last forty kilobytes of a page are usually cut mid tag. selectolax
    recovers from that the same way a browser does, so a truncated document
    still yields the paragraphs that arrived.
    """
    try:
        tree = HTMLParser(html)
    except Exception:
        log.debug("could not parse article markup", exc_info=True)
        return None

    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    for selector in _CONTENT_SELECTORS:
        for node in tree.css(selector):
            text = " ".join(node.text(separator=" ").split())
            if len(text) >= MIN_TEXT_CHARS:
                return text[:MAX_TEXT_CHARS]
    return None


def read_one(client: httpx.Client, item: Item) -> str | None:
    """Fetch and extract one article. Never raises."""
    try:
        html = _read_capped(client, item.best_url)
    except Exception as exc:
        log.info(
            "deep read fetch failed, falling back to the feed summary",
            item_id=item.id,
            url=item.best_url,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None

    text = extract_text(html)
    if text is None:
        log.info("no readable article body, falling back to the feed summary", item_id=item.id)
    return text


def run(client: httpx.Client, items: Sequence[Item], limit: int) -> dict[int, str]:
    """Read the top `limit` items. Returns extracted text by item id.

    An item missing from the result simply has no deep read, and the writer
    uses its feed summary.
    """
    targets = list(items)[:limit]
    extracts: dict[int, str] = {}
    for item in targets:
        text = read_one(client, item)
        if text:
            extracts[item.id] = text

    log.info("deep read", attempted=len(targets), extracted=len(extracts))
    return extracts
