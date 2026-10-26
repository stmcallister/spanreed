"""Tests for the rsshub adapter, which is optional and skips when unconfigured."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import ValidationError

from spanreed.sources.rsshub import (
    RsshubAdapter,
    RsshubConfig,
    RsshubUnconfiguredError,
    build_url,
)
from tests.conftest import fixture_bytes

BASE = "https://rsshub.example"
ROUTE = "/github/issue/stmcallister/spanreed"
ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)
CONFIG = RsshubConfig(route=ROUTE)


# --- config -----------------------------------------------------------------


@pytest.mark.parametrize("value", [ROUTE, ROUTE.lstrip("/"), f"  {ROUTE}  "])
def test_a_route_is_normalized_to_a_leading_slash(value: str) -> None:
    assert RsshubConfig(route=value).route == ROUTE


def test_a_full_url_is_rejected() -> None:
    """The route is a path. The host comes from RSSHUB_BASE_URL."""
    with pytest.raises(ValidationError):
        RsshubConfig(route="https://rsshub.example/github/issue/a/b")


def test_an_empty_route_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RsshubConfig(route="   ")


def test_the_url_joins_base_and_route() -> None:
    assert build_url(CONFIG, BASE) == f"{BASE}{ROUTE}"


def test_a_trailing_slash_on_the_base_does_not_double_up() -> None:
    assert build_url(CONFIG, f"{BASE}/") == f"{BASE}{ROUTE}"


# --- fetching ---------------------------------------------------------------


@respx.mock
def test_fetch_parses_the_route(client: httpx.Client) -> None:
    respx.get(f"{BASE}{ROUTE}").mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_basic.xml"))
    )

    items = RsshubAdapter(client, base_url=BASE).fetch(CONFIG, ANCIENT)

    assert len(items) == 3


def test_an_unconfigured_base_url_raises_the_skip_signal(client: httpx.Client) -> None:
    """Skipped rather than failed, so an integration nobody turned on does not
    burn down the failure counter and auto disable the source."""
    with pytest.raises(RsshubUnconfiguredError):
        RsshubAdapter(client, base_url="").fetch(CONFIG, ANCIENT)


def test_the_base_url_falls_back_to_settings(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spanreed.config import get_settings

    monkeypatch.setenv("RSSHUB_BASE_URL", BASE)
    get_settings.cache_clear()
    try:
        assert RsshubAdapter(client).base_url == BASE
    finally:
        get_settings.cache_clear()


@respx.mock
def test_a_route_that_does_not_exist_raises_for_the_collector(client: httpx.Client) -> None:
    respx.get(f"{BASE}{ROUTE}").mock(return_value=httpx.Response(404))

    with pytest.raises(httpx.HTTPStatusError):
        RsshubAdapter(client, base_url=BASE).fetch(CONFIG, ANCIENT)


@respx.mock
def test_a_truncated_response_yields_fewer_items(client: httpx.Client) -> None:
    respx.get(f"{BASE}{ROUTE}").mock(
        return_value=httpx.Response(200, content=fixture_bytes("rss_truncated.xml"))
    )

    assert len(RsshubAdapter(client, base_url=BASE).fetch(CONFIG, ANCIENT)) < 3


def test_the_wrong_config_type_is_a_bug(client: httpx.Client) -> None:
    from spanreed.sources.rss import RssConfig

    with pytest.raises(TypeError):
        RsshubAdapter(client, base_url=BASE).fetch(RssConfig(url="https://a.example/feed"), ANCIENT)
