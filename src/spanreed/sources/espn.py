"""Scores and fixtures from ESPN's undocumented site API.

No key, no docs, and no promises. These endpoints will change shape without
warning, so every read here is a `.get()` chain and every event is parsed
inside its own try block. A restructured response yields fewer items. It never
raises.

Two shapes already differ between the two endpoints this adapter calls. On the
schedule, `competitor.score` is an object carrying `displayValue`. On the
scoreboard it is a bare string. Both are handled, and that is the whole reason
`_score` exists.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
import structlog
from pydantic import BaseModel, field_validator

from spanreed.models import RawItem
from spanreed.sources.base import ConfigModelType, SourceConfig, drop_older_than

log = structlog.get_logger(__name__)

API_ROOT = "https://site.api.espn.com/apis/site/v2/sports"

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# A match in progress is deliberately skipped. Its score changes between every
# collector run, so each fetch would hash to a new row and the same game would
# arrive in the brief three times with three different scorelines.
COMPLETED_STATE = "post"
UPCOMING_STATE = "pre"


class EspnConfig(SourceConfig):
    """Config for an `espn` source.

    With a `team_id` this follows one team: its own schedule, plus anything on
    today's league scoreboard involving it. Without one it follows the whole
    league scoreboard.
    """

    sport: str
    league: str
    team_id: str | None = None

    @field_validator("sport", "league")
    @classmethod
    def _validate_segment(cls, value: str) -> str:
        cleaned = value.strip().strip("/")
        if not _SEGMENT.match(cleaned):
            raise ValueError("must be a path segment such as soccer or usa.1")
        return cleaned

    @field_validator("team_id")
    @classmethod
    def _validate_team_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not _SEGMENT.match(cleaned):
            raise ValueError("team_id must be the numeric id ESPN uses, such as 9726")
        return cleaned


def schedule_url(config: EspnConfig) -> str:
    return f"{API_ROOT}/{config.sport}/{config.league}/teams/{config.team_id}/schedule"


def scoreboard_url(config: EspnConfig) -> str:
    return f"{API_ROOT}/{config.sport}/{config.league}/scoreboard"


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _competition(event: Any) -> dict[str, Any]:
    competitions = event.get("competitions") if isinstance(event, dict) else None
    if isinstance(competitions, list) and competitions and isinstance(competitions[0], dict):
        return competitions[0]
    return {}


def _status_type(event: dict[str, Any], competition: dict[str, Any]) -> dict[str, Any]:
    """The status block, which lives on the competition and sometimes the event."""
    for holder in (competition, event):
        status = holder.get("status")
        if isinstance(status, dict):
            kind = status.get("type")
            if isinstance(kind, dict):
                return kind
    return {}


def _score(competitor: dict[str, Any]) -> str | None:
    """The score, which is an object on one endpoint and a string on the other."""
    raw = competitor.get("score")
    if isinstance(raw, dict):
        display = _text(raw.get("displayValue"))
        if display:
            return display
        value = raw.get("value")
        return str(int(value)) if isinstance(value, (int, float)) else None
    if isinstance(raw, str):
        return _text(raw)
    if isinstance(raw, (int, float)):
        return str(int(raw))
    return None


def _team_name(competitor: dict[str, Any]) -> str | None:
    team = competitor.get("team")
    if not isinstance(team, dict):
        return None
    for key in ("displayName", "shortDisplayName", "name", "abbreviation"):
        name = _text(team.get(key))
        if name:
            return name
    return None


def _team_id(competitor: dict[str, Any]) -> str | None:
    team = competitor.get("team")
    return _text(str(team.get("id"))) if isinstance(team, dict) and team.get("id") else None


def _sides(competition: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Home and away, in that order. Falls back to list order when unlabelled."""
    competitors = competition.get("competitors")
    if not isinstance(competitors, list) or len(competitors) < 2:
        return None
    entries = [c for c in competitors if isinstance(c, dict)]
    if len(entries) < 2:
        return None

    home = next((c for c in entries if c.get("homeAway") == "home"), None)
    away = next((c for c in entries if c.get("homeAway") == "away"), None)
    if home is None or away is None:
        home, away = entries[0], entries[1]
    return home, away


def _event_url(event: dict[str, Any]) -> str | None:
    """The gamecast link.

    `rel` is a list, and some entries are app deep links on a scheme no browser
    will open, so only http links count and a desktop one is preferred.
    """
    links = event.get("links")
    if not isinstance(links, list):
        return None

    fallback: str | None = None
    for link in links:
        if not isinstance(link, dict):
            continue
        href = _text(link.get("href"))
        if not href or not href.startswith(("http://", "https://")):
            continue
        rel = link.get("rel")
        rels = set(rel) if isinstance(rel, list) else set()
        if "desktop" in rels:
            return href
        fallback = fallback or href
    return fallback


def _event_date(event: dict[str, Any], competition: dict[str, Any]) -> datetime | None:
    """The event's own date. Never fetch time, which would be wrong for a fixture."""
    for raw in (event.get("date"), competition.get("date"), competition.get("startDate")):
        text = _text(raw)
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _involves(competition: dict[str, Any], team_id: str | None) -> bool:
    if team_id is None:
        return True
    competitors = competition.get("competitors")
    if not isinstance(competitors, list):
        return False
    return any(isinstance(c, dict) and _team_id(c) == team_id for c in competitors)


def event_to_item(event: Any, source_name: str) -> RawItem | None:
    """One event as a brief item, or None when it is not one we report.

    Returns None rather than raising for anything unexpected, because the shape
    of these responses is not a promise anybody made.
    """
    if not isinstance(event, dict):
        return None

    competition = _competition(event)
    state = _text(_status_type(event, competition).get("state"))
    if state not in (COMPLETED_STATE, UPCOMING_STATE):
        return None

    sides = _sides(competition)
    if sides is None:
        return None
    home, away = sides
    home_name, away_name = _team_name(home), _team_name(away)
    if not home_name or not away_name:
        return None

    url = _event_url(event)
    if not url:
        return None

    if state == COMPLETED_STATE:
        home_score, away_score = _score(home), _score(away)
        if home_score is None or away_score is None:
            return None
        title = f"Final: {home_name} {home_score}, {away_name} {away_score}"
    else:
        title = f"Upcoming: {away_name} at {home_name}"

    return RawItem(
        url=url,
        title=title,
        summary=_text(_status_type(event, competition).get("detail")) or _text(event.get("name")),
        source_name=source_name,
        published_at=_event_date(event, competition),
    )


def parse_events(payload: Any, team_id: str | None, source_name: str) -> list[RawItem]:
    """Read every event we can, skipping every one we cannot."""
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        log.debug("espn response carried no events list")
        return []

    items: list[RawItem] = []
    for event in events:
        try:
            if not _involves(_competition(event), team_id):
                continue
            item = event_to_item(event, source_name)
        except Exception:
            log.warning("skipping an espn event that could not be read", exc_info=True)
            continue
        if item is not None:
            items.append(item)
    return items


class EspnAdapter:
    """Fetch one team's schedule, or one league's scoreboard, or both."""

    type: ClassVar[str] = "espn"
    ConfigModel: ClassVar[ConfigModelType] = EspnConfig

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(self, config: BaseModel, since: datetime) -> list[RawItem]:
        if not isinstance(config, EspnConfig):
            raise TypeError(f"espn needs an EspnConfig, got {type(config).__name__}")

        source_name = f"ESPN {config.league}"
        items: list[RawItem] = []

        urls = [scoreboard_url(config)]
        if config.team_id:
            urls.insert(0, schedule_url(config))

        for url in urls:
            # One endpoint failing must not lose the other's events. The
            # scoreboard in particular is empty on days with no fixtures, and
            # that is not a failure worth reporting.
            try:
                response = self._client.get(url)
                response.raise_for_status()
                payload = response.json()
            except Exception:
                log.warning("espn endpoint failed", url=url, exc_info=True)
                continue
            items.extend(parse_events(payload, config.team_id, source_name))

        return drop_older_than(_dedupe(items), since)


def _dedupe(items: list[RawItem]) -> list[RawItem]:
    """The same game appears on both endpoints. Keep the first copy."""
    seen: set[str] = set()
    unique: list[RawItem] = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        unique.append(item)
    return unique
