"""Tests for the bounded full text pass. Every response here is recorded."""

from __future__ import annotations

import httpx
import respx

from newsbrief.compose import deepread
from tests.test_cluster import make_item

ARTICLE_URL = "https://a.example/1"

ARTICLE = """
<html><head><title>T</title><style>body{color:red}</style></head>
<body>
  <nav>Home Sport Culture Subscribe</nav>
  <article><p>{body}</p></article>
  <footer>Copyright somebody</footer>
</body></html>
"""


def page(body: str) -> str:
    return ARTICLE.replace("{body}", body)


BODY = (
    "The club confirmed the transfer on Monday morning after a medical in Copenhagen. "
    "The fee was not disclosed, and the deal runs until the end of the season with an "
    "option to make the move permanent. The manager said the squad needed the depth."
)


# --- extraction -------------------------------------------------------------


def test_the_article_body_is_extracted() -> None:
    text = deepread.extract_text(page(BODY))

    assert text is not None
    assert "confirmed the transfer" in text


def test_navigation_and_styling_are_stripped() -> None:
    """Otherwise the extract is mostly menus and consent copy."""
    text = deepread.extract_text(page(BODY))

    assert text is not None
    assert "Subscribe" not in text
    assert "color:red" not in text
    assert "Copyright" not in text


def test_a_page_with_no_real_body_yields_nothing() -> None:
    """A cookie banner is not an article, and the feed summary is better."""
    assert deepread.extract_text(page("Accept cookies")) is None


def test_extraction_is_capped() -> None:
    text = deepread.extract_text(page("word " * 40000))

    assert text is not None
    assert len(text) <= deepread.MAX_TEXT_CHARS


def test_a_truncated_document_still_yields_its_paragraphs() -> None:
    """The last kilobyte of a capped read is always cut mid tag."""
    truncated = page(BODY)[: page(BODY).index("</article>") + 4]

    text = deepread.extract_text(truncated)

    assert text is not None
    assert "confirmed the transfer" in text


def test_unparsable_input_is_not_an_error() -> None:
    assert deepread.extract_text("") is None


# --- fetching ---------------------------------------------------------------


@respx.mock
def test_read_one_fetches_and_extracts(client: httpx.Client) -> None:
    respx.get(ARTICLE_URL).mock(return_value=httpx.Response(200, text=page(BODY)))

    text = deepread.read_one(client, make_item(1, "A story", ARTICLE_URL))

    assert text is not None
    assert "Copenhagen" in text


@respx.mock
def test_only_the_first_forty_kilobytes_are_read(client: httpx.Client) -> None:
    """A page that turns out to be enormous costs 40KB, not all of it."""
    huge = page(BODY) + "<p>" + ("padding " * 200000) + "</p>"
    respx.get(ARTICLE_URL).mock(return_value=httpx.Response(200, text=huge))

    text = deepread.read_one(client, make_item(1, "A story", ARTICLE_URL))

    assert text is not None
    assert len(text) <= deepread.MAX_TEXT_CHARS


@respx.mock
def test_a_paywall_falls_back_to_the_summary(client: httpx.Client) -> None:
    respx.get(ARTICLE_URL).mock(return_value=httpx.Response(402, text="Subscribe"))

    assert deepread.read_one(client, make_item(1, "A story", ARTICLE_URL)) is None


@respx.mock
def test_a_timeout_falls_back_to_the_summary(client: httpx.Client) -> None:
    respx.get(ARTICLE_URL).mock(side_effect=httpx.ReadTimeout("too slow"))

    assert deepread.read_one(client, make_item(1, "A story", ARTICLE_URL)) is None


@respx.mock
def test_a_resolved_google_link_is_fetched_not_the_redirect(client: httpx.Client) -> None:
    publisher = "https://publisher.example/story"
    respx.get(publisher).mock(return_value=httpx.Response(200, text=page(BODY)))
    item = make_item(
        1, "A story", "https://news.google.com/rss/articles/abc", canonical_url=publisher
    )

    assert deepread.read_one(client, item) is not None


# --- the stage as a whole ---------------------------------------------------


@respx.mock
def test_run_reads_only_the_top_items(client: httpx.Client) -> None:
    """Three items, not fifteen. This stage is deliberately on a short leash."""
    for index in range(1, 6):
        respx.get(f"https://a.example/{index}").mock(
            return_value=httpx.Response(200, text=page(BODY))
        )
    items = [make_item(i, f"Story {i}", f"https://a.example/{i}") for i in range(1, 6)]

    extracts = deepread.run(client, items, limit=3)

    assert sorted(extracts) == [1, 2, 3]


@respx.mock
def test_one_failed_fetch_does_not_stop_the_others(client: httpx.Client) -> None:
    respx.get("https://a.example/1").mock(side_effect=httpx.ConnectError("no route"))
    respx.get("https://a.example/2").mock(return_value=httpx.Response(200, text=page(BODY)))
    items = [make_item(i, f"Story {i}", f"https://a.example/{i}") for i in (1, 2)]

    assert sorted(deepread.run(client, items, limit=3)) == [2]


@respx.mock
def test_a_limit_of_zero_reads_nothing(client: httpx.Client) -> None:
    assert deepread.run(client, [make_item(1, "s", ARTICLE_URL)], limit=0) == {}
