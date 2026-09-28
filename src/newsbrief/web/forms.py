"""Turning form posts into validated source configs, and back again.

The source editor renders fields specific to the chosen type. Rather than keep
a hand written list of those fields per type, they are read off the adapter's
own Pydantic config model. Adding an adapter in a later phase then gives the UI
its form for free, and the form can never drift from what the adapter accepts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from newsbrief.sources import CONFIG_MODELS

BOOKKEEPING_FIELDS = frozenset({"etag", "last_modified"})
"""Written by the collector after a fetch. Cache state, not user settings."""

# What a field is called on screen, where the attribute name is not enough.
LABELS: Mapping[str, str] = {
    "url": "Feed URL",
    "query": "Search query",
    "window": "Time window",
    "route": "RSSHub route",
    "handle": "Bluesky handle",
    "subreddit": "Subreddit",
    "listing": "Listing",
    "sport": "Sport",
    "league": "League",
    "team_id": "Team id",
}

HINTS: Mapping[str, str] = {
    "query": 'Quoted phrases and negative terms both work: "Midlakes United" -realty',
    "window": "How far back to search, such as 12h or 7d",
    "url": "The feed itself, not the site. Use discovery if you only have a site URL.",
}


@dataclass(slots=True)
class FieldSpec:
    """One input on the source editor."""

    name: str
    label: str
    hint: str
    required: bool
    default: str

    @property
    def placeholder(self) -> str:
        return self.default or ""


def editable_fields(source_type: str) -> list[FieldSpec]:
    """The inputs to render for a source type, read from its config model."""
    model = CONFIG_MODELS.get(source_type)
    if model is None:
        return []

    specs: list[FieldSpec] = []
    for name, field in model.model_fields.items():
        if name in BOOKKEEPING_FIELDS:
            continue
        default = "" if field.is_required() else str(field.default or "")
        specs.append(
            FieldSpec(
                name=name,
                label=LABELS.get(name, name.replace("_", " ").capitalize()),
                hint=HINTS.get(name, ""),
                required=field.is_required(),
                default=default,
            )
        )
    return specs


def config_from_form(source_type: str, form: Mapping[str, Any]) -> dict[str, object]:
    """Pull just this type's fields out of a form post.

    An empty optional field is dropped rather than sent as an empty string, so
    the config model applies its own default instead of failing validation.
    """
    config: dict[str, object] = {}
    for spec in editable_fields(source_type):
        raw = form.get(spec.name)
        value = "" if raw is None else str(raw).strip()
        if value or spec.required:
            config[spec.name] = value
    return config


def validate(source_type: str, config: Mapping[str, object]) -> tuple[BaseModel | None, str | None]:
    """Validate a config against its adapter's model.

    Returns the parsed model or a readable error. The editor shows the error in
    place, which is how a typo is caught before the next collector run rather
    than in tomorrow's brief.
    """
    model = CONFIG_MODELS.get(source_type)
    if model is None:
        return None, f"no adapter is registered for source type {source_type!r}"
    try:
        return model.model_validate(dict(config)), None
    except ValidationError as exc:
        return None, "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'config'}: {error['msg']}"
            for error in exc.errors()
        )


def merge_bookkeeping(
    existing: Mapping[str, object], config: Mapping[str, object]
) -> dict[str, object]:
    """Keep the collector's cache state across an edit of the user's fields.

    An edited feed URL should drop the old validators, but an edit that leaves
    the URL alone should not throw away a working ETag.
    """
    merged = dict(config)
    if existing.get("url") and existing.get("url") == config.get("url"):
        for name in BOOKKEEPING_FIELDS:
            if name in existing:
                merged[name] = existing[name]
    return merged
