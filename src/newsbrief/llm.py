"""The single wrapper around the Anthropic SDK.

Nothing else in the codebase imports `anthropic`. Two calls exist: a cheap
structured classification for the relevance filter and one prose call for the
brief itself. Everything else the composer does is plain code.

Retries are the SDK's own. `max_retries=2` means three attempts in total, and
the SDK retries 429, 5xx, and connection errors while never retrying a 400,
which is exactly the policy this project wants. Writing a second retry loop on
top would multiply the attempts rather than cap them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar

import anthropic
import structlog
from pydantic import BaseModel

from newsbrief.config import Settings

log = structlog.get_logger(__name__)

MAX_ATTEMPTS = 3
"""Three attempts total, expressed to the SDK as two retries."""

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LlmUnavailableError(RuntimeError):
    """No API key is configured, so no model call can be attempted.

    Raised instead of returning None so that a stage which needs the model
    fails into its degraded path rather than silently producing nothing.
    """


@dataclass(slots=True)
class Usage:
    """Running token total for one composer run.

    Recorded on the brief row. The token record is how you notice when a few
    cents a day stops being a few cents a day.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1


class LlmClient(Protocol):
    """What the composer stages require of the model.

    A Protocol so the pipeline tests can pass a stub without touching the SDK
    or the network.
    """

    usage: Usage

    @property
    def filter_model(self) -> str: ...

    @property
    def write_model(self) -> str: ...

    def classify(
        self, system: str, prompt: str, schema: type[SchemaT], max_tokens: int
    ) -> SchemaT: ...

    def write(self, system: str, prompt: str, max_tokens: int) -> str: ...


class Llm:
    """The real client. Constructed once per composer run."""

    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        if not settings.llm_configured:
            raise LlmUnavailableError("ANTHROPIC_API_KEY is not set")
        self._settings = settings
        self._client = client or anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            max_retries=MAX_ATTEMPTS - 1,
        )
        self.usage = Usage()

    @property
    def filter_model(self) -> str:
        return self._settings.filter_model

    @property
    def write_model(self) -> str:
        return self._settings.write_model

    def classify(self, system: str, prompt: str, schema: type[SchemaT], max_tokens: int) -> SchemaT:
        """Structured output call for the relevance filter.

        Structured output rather than JSON in a free text response, so there is
        no prose to parse and no regex anywhere near a model response.
        """
        response = self._client.messages.parse(
            model=self.filter_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        self._record(response.usage, self.filter_model)

        parsed = response.parsed_output
        if parsed is None:
            raise LlmUnavailableError(
                f"model returned no parsable output (stop_reason={response.stop_reason})"
            )
        return parsed

    def write(self, system: str, prompt: str, max_tokens: int) -> str:
        """Prose call for the brief. Returns markdown."""
        response = self._client.messages.create(
            model=self.write_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        self._record(response.usage, self.write_model)

        text = "\n".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise LlmUnavailableError(
                f"model returned no text (stop_reason={response.stop_reason})"
            )
        return text.strip()

    def _record(self, usage: anthropic.types.Usage, model: str) -> None:
        self.usage.record(usage.input_tokens, usage.output_tokens)
        log.info(
            "llm call",
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )


def build(settings: Settings) -> Llm | None:
    """The client, or None when no key is configured.

    Returning None lets the composer take its degraded path without the caller
    having to catch anything.
    """
    try:
        return Llm(settings)
    except LlmUnavailableError as exc:
        log.warning("llm is not configured, the brief will be degraded", detail=str(exc))
        return None
