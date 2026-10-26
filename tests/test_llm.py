"""Tests for the SDK wrapper. Nothing here reaches the network."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from spanreed import llm
from spanreed.config import Settings


class Answer(BaseModel):
    verdict: str


class FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeBlock:
    def __init__(self, text: str, block_type: str = "text") -> None:
        self.text = text
        self.type = block_type


class FakeResponse:
    def __init__(
        self,
        *,
        parsed: Any = None,
        content: list[FakeBlock] | None = None,
        stop_reason: str = "end_turn",
        usage: FakeUsage | None = None,
    ) -> None:
        self.parsed_output = parsed
        self.content = content or []
        self.stop_reason = stop_reason
        self.usage = usage or FakeUsage(10, 20)


class FakeMessages:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def _next(self, kwargs: dict[str, Any]) -> FakeResponse:
        self.calls.append(kwargs)
        return self._responses.pop(0)

    def parse(self, **kwargs: Any) -> FakeResponse:
        return self._next(kwargs)

    def create(self, **kwargs: Any) -> FakeResponse:
        return self._next(kwargs)


class FakeAnthropic:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.messages = FakeMessages(responses)


def build(responses: list[FakeResponse]) -> tuple[llm.Llm, FakeAnthropic]:
    settings = Settings(anthropic_api_key="test-key")
    fake = FakeAnthropic(responses)
    return llm.Llm(settings, client=fake), fake  # type: ignore[arg-type]


# --- configuration ----------------------------------------------------------


def test_an_unconfigured_key_refuses_to_build_a_client() -> None:
    with pytest.raises(llm.LlmUnavailableError):
        llm.Llm(Settings(anthropic_api_key=None))


def test_build_returns_none_when_unconfigured() -> None:
    """The composer checks for None rather than catching, so it can degrade."""
    assert llm.build(Settings(anthropic_api_key=None)) is None


def test_build_returns_a_client_when_a_key_is_present() -> None:
    assert llm.build(Settings(anthropic_api_key="test-key")) is not None


def test_retries_are_delegated_to_the_sdk_as_three_total_attempts() -> None:
    """Three attempts maximum, expressed as the SDK's two retries."""
    client = llm.Llm(Settings(anthropic_api_key="test-key"))
    assert client._client.max_retries == 2
    assert llm.MAX_ATTEMPTS == 3


def test_models_come_from_settings() -> None:
    client, _ = build([])
    assert client.filter_model == "claude-haiku-4-5-20251001"
    assert client.write_model == "claude-sonnet-5"


# --- classify ---------------------------------------------------------------


def test_classify_returns_the_parsed_schema() -> None:
    client, fake = build([FakeResponse(parsed=Answer(verdict="keep"))])

    result = client.classify("system", "prompt", Answer, max_tokens=256)

    assert result.verdict == "keep"
    assert fake.messages.calls[0]["output_format"] is Answer
    assert fake.messages.calls[0]["max_tokens"] == 256


def test_classify_raises_when_nothing_could_be_parsed() -> None:
    client, _ = build([FakeResponse(parsed=None, stop_reason="max_tokens")])

    with pytest.raises(llm.LlmUnavailableError, match="max_tokens"):
        client.classify("system", "prompt", Answer, max_tokens=256)


# --- write ------------------------------------------------------------------


def test_write_joins_the_text_blocks() -> None:
    client, _ = build(
        [FakeResponse(content=[FakeBlock("## Topic"), FakeBlock("- an item"), FakeBlock("", "x")])]
    )

    assert client.write("system", "prompt", max_tokens=100) == "## Topic\n- an item"


def test_write_raises_when_the_response_has_no_text() -> None:
    client, _ = build([FakeResponse(content=[], stop_reason="refusal")])

    with pytest.raises(llm.LlmUnavailableError, match="refusal"):
        client.write("system", "prompt", max_tokens=100)


# --- usage ------------------------------------------------------------------


def test_token_usage_accumulates_across_every_call() -> None:
    """The brief records the total, which is how a cost change gets noticed."""
    client, _ = build(
        [
            FakeResponse(parsed=Answer(verdict="keep"), usage=FakeUsage(100, 20)),
            FakeResponse(content=[FakeBlock("text")], usage=FakeUsage(400, 900)),
        ]
    )

    client.classify("system", "prompt", Answer, max_tokens=256)
    client.write("system", "prompt", max_tokens=100)

    assert client.usage.input_tokens == 500
    assert client.usage.output_tokens == 920
    assert client.usage.calls == 2


def test_max_tokens_is_set_on_every_call() -> None:
    """CLAUDE.md requires it explicitly, so no call may omit it."""
    client, fake = build(
        [FakeResponse(parsed=Answer(verdict="keep")), FakeResponse(content=[FakeBlock("t")])]
    )
    client.classify("system", "prompt", Answer, max_tokens=256)
    client.write("system", "prompt", max_tokens=100)

    assert all("max_tokens" in call for call in fake.messages.calls)
