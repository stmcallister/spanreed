"""The RSS feed of briefs, served at /feed.xml.

Built with ElementTree rather than a template so that escaping is the library's
problem. A brief's HTML goes into `description` as escaped text, which is how
RSS carries markup and what every reader expects to unescape.
"""

from __future__ import annotations

from collections.abc import Sequence
from email.utils import format_datetime
from urllib.parse import urljoin
from xml.etree import ElementTree

from spanreed import db
from spanreed.models import Brief

FEED_TITLE = "spanreed"
FEED_DESCRIPTION = "A short daily brief, one per morning."
MAX_FEED_ITEMS = 30


def _rfc_822(value: str | None) -> str | None:
    """RSS dates are RFC 822, not ISO 8601, which is the one thing readers agree on."""
    parsed = db.parse_timestamp(value)
    return None if parsed is None else format_datetime(parsed)


def build(briefs: Sequence[Brief], base_url: str) -> bytes:
    """Render the feed.

    Links are absolute because a feed is read somewhere else by definition, so
    a relative link has nothing to resolve against.
    """
    root = ElementTree.Element("rss", {"version": "2.0"})
    channel = ElementTree.SubElement(root, "channel")

    ElementTree.SubElement(channel, "title").text = FEED_TITLE
    ElementTree.SubElement(channel, "link").text = urljoin(base_url, "/briefs")
    ElementTree.SubElement(channel, "description").text = FEED_DESCRIPTION

    latest = _rfc_822(briefs[0].generated_at) if briefs else None
    if latest:
        ElementTree.SubElement(channel, "lastBuildDate").text = latest

    for brief in briefs[:MAX_FEED_ITEMS]:
        link = urljoin(base_url, f"/briefs/{brief.brief_date}")
        item = ElementTree.SubElement(channel, "item")

        title = brief.brief_date + (" (degraded)" if brief.degraded else "")
        ElementTree.SubElement(item, "title").text = title
        ElementTree.SubElement(item, "link").text = link
        # Not a permalink in the URL sense, but stable, which is what a reader
        # uses it for.
        ElementTree.SubElement(item, "guid", {"isPermaLink": "true"}).text = link
        ElementTree.SubElement(item, "description").text = brief.html

        published = _rfc_822(brief.generated_at)
        if published:
            ElementTree.SubElement(item, "pubDate").text = published

    rendered: bytes = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    return rendered
