"""Tests for the espn adapter, against recorded responses.

These endpoints are undocumented and will change shape. Most of what is below
is about what happens when they do.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import ValidationError

from spanreed.sources.espn import EspnAdapter, EspnConfig, parse_events
from tests.conftest import fixture_bytes

SPORT, LEAGUE, TEAM = "soccer", "usa.1", "9726"
ROOT = "https://site.api.espn.com/apis/site/v2/sports"
SCHEDULE = f"{ROOT}/{SPORT}/{LEAGUE}/teams/{TEAM}/schedule"
SCOREBOARD = f"{ROOT}/{SPORT}/{LEAGUE}/scoreboard"

ANCIENT = datetime(2000, 1, 1, tzinfo=UTC)
CONFIG = EspnConfig(sport=SPORT, league=LEAGUE, team_id=TEAM)


def fixture_json(name: str) -> object:
    return json.loads(fixture_bytes(name))


# --- config -----------------------------------------------------------------


def test_a_path_segment_with_a_slash_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EspnConfig(sport="soccer/usa.1", league="usa.1")


def test_a_dotted_league_is_accepted() -> None:
    """usa.1 and eng.1 are how ESPN names leagues."""
    assert EspnConfig(sport="soccer", league="usa.1").league == "usa.1"


def test_team_id_is_optional() -> None:
    """Without one this follows a whole league rather than one team."""
    assert EspnConfig(sport="soccer", league="usa.1").team_id is None


def test_a_blank_team_id_is_treated_as_absent() -> None:
    assert EspnConfig(sport="soccer", league="usa.1", team_id="  ").team_id is None


def test_unknown_config_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        EspnConfig(sport="soccer", league="usa.1", teamId="9726")  # type: ignore[call-arg]


# --- parsing the real shapes ------------------------------------------------


def test_a_completed_match_becomes_a_scoreline() -> None:
    items = parse_events(fixture_json("espn_schedule.json"), TEAM, "ESPN usa.1")

    assert items[0].title == "Final: Real Salt Lake 2, Seattle Sounders FC 1"


def test_the_date_comes_from_the_event_not_the_fetch() -> None:
    items = parse_events(fixture_json("espn_schedule.json"), TEAM, "ESPN usa.1")

    assert items[0].published_at == datetime(2026, 3, 1, 0, 30, tzinfo=UTC)


def test_an_upcoming_fixture_is_emitted() -> None:
    items = parse_events(fixture_json("espn_scoreboard.json"), None, "ESPN usa.1")

    assert items[0].title == "Upcoming: St. Louis CITY SC at Red Bull New York"
    assert items[0].published_at == datetime(2026, 9, 30, 23, 30, tzinfo=UTC)


def test_the_score_is_read_from_both_shapes() -> None:
    """The schedule sends an object with displayValue. The scoreboard sends a
    bare string. Neither endpoint documents that it differs from the other."""
    schedule = fixture_json("espn_schedule.json")
    scoreboard = fixture_json("espn_scoreboard.json")

    assert parse_events(schedule, None, "ESPN")
    assert parse_events(scoreboard, None, "ESPN")


def test_the_gamecast_link_is_preferred_over_the_app_deep_link() -> None:
    """Some links use a scheme no browser will open."""
    items = parse_events(fixture_json("espn_schedule.json"), TEAM, "ESPN")

    assert items[0].url.startswith("https://www.espn.com/")


def test_events_are_filtered_to_the_team() -> None:
    """A league scoreboard is noise on a topic that follows one club."""
    scoreboard = fixture_json("espn_scoreboard.json")

    assert parse_events(scoreboard, TEAM, "ESPN") == []
    assert len(parse_events(scoreboard, None, "ESPN")) == 1


# --- degrading --------------------------------------------------------------


def test_a_restructured_response_yields_fewer_items_and_does_not_raise() -> None:
    """Nine events, eight of them broken in a different way, one survivor."""
    items = parse_events(fixture_json("espn_restructured.json"), None, "ESPN")

    assert len(items) == 1
    assert items[0].title.startswith("Final:")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"events": None},
        {"events": "not a list"},
        {"events": []},
        [],
        None,
        "a string",
    ],
)
def test_a_payload_with_no_usable_events_is_empty_not_an_error(payload: object) -> None:
    assert parse_events(payload, None, "ESPN") == []


def test_a_match_in_progress_is_skipped() -> None:
    """Its score changes between runs, so every fetch would store a new row."""
    event = {
        "date": "2026-09-01T00:00Z",
        "links": [{"rel": ["desktop"], "href": "https://www.espn.com/x"}],
        "competitions": [
            {
                "status": {"type": {"state": "in", "completed": False}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "A"}, "score": "1"},
                    {"homeAway": "away", "team": {"displayName": "B"}, "score": "0"},
                ],
            }
        ],
    }

    assert parse_events({"events": [event]}, None, "ESPN") == []


def test_a_completed_match_with_no_score_is_skipped() -> None:
    event = {
        "date": "2026-09-01T00:00Z",
        "links": [{"rel": ["desktop"], "href": "https://www.espn.com/x"}],
        "competitions": [
            {
                "status": {"type": {"state": "post", "completed": True}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "A"}},
                    {"homeAway": "away", "team": {"displayName": "B"}},
                ],
            }
        ],
    }

    assert parse_events({"events": [event]}, None, "ESPN") == []


def test_unlabelled_sides_fall_back_to_list_order() -> None:
    event = {
        "date": "2026-09-01T00:00Z",
        "links": [{"rel": ["desktop"], "href": "https://www.espn.com/x"}],
        "competitions": [
            {
                "status": {"type": {"state": "post", "completed": True}},
                "competitors": [
                    {"team": {"displayName": "A"}, "score": "2"},
                    {"team": {"displayName": "B"}, "score": "1"},
                ],
            }
        ],
    }

    assert parse_events({"events": [event]}, None, "ESPN")[0].title == "Final: A 2, B 1"


def test_an_unparsable_date_does_not_lose_the_item() -> None:
    """A missing date is normal and collect falls back to fetch time."""
    event = {
        "date": "the third of never",
        "links": [{"rel": ["desktop"], "href": "https://www.espn.com/x"}],
        "competitions": [
            {
                "status": {"type": {"state": "post", "completed": True}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "A"}, "score": "2"},
                    {"homeAway": "away", "team": {"displayName": "B"}, "score": "1"},
                ],
            }
        ],
    }
    items = parse_events({"events": [event]}, None, "ESPN")

    assert len(items) == 1
    assert items[0].published_at is None


# --- fetching ---------------------------------------------------------------


@respx.mock
def test_fetch_reads_both_endpoints(client: httpx.Client) -> None:
    respx.get(SCHEDULE).mock(
        return_value=httpx.Response(200, content=fixture_bytes("espn_schedule.json"))
    )
    respx.get(SCOREBOARD).mock(
        return_value=httpx.Response(200, content=fixture_bytes("espn_scoreboard.json"))
    )

    items = EspnAdapter(client).fetch(CONFIG, ANCIENT)

    assert len(items) == 2
    assert all(i.source_name == "ESPN usa.1" for i in items)


@respx.mock
def test_without_a_team_id_only_the_scoreboard_is_read(client: httpx.Client) -> None:
    schedule = respx.get(SCHEDULE).mock(return_value=httpx.Response(200, json={}))
    respx.get(SCOREBOARD).mock(
        return_value=httpx.Response(200, content=fixture_bytes("espn_scoreboard.json"))
    )

    items = EspnAdapter(client).fetch(EspnConfig(sport=SPORT, league=LEAGUE), ANCIENT)

    assert not schedule.called
    assert len(items) == 1


@respx.mock
def test_one_dead_endpoint_does_not_lose_the_other(client: httpx.Client) -> None:
    respx.get(SCHEDULE).mock(
        return_value=httpx.Response(200, content=fixture_bytes("espn_schedule.json"))
    )
    respx.get(SCOREBOARD).mock(return_value=httpx.Response(503))

    assert len(EspnAdapter(client).fetch(CONFIG, ANCIENT)) == 2


@respx.mock
def test_a_truncated_response_is_not_an_error(client: httpx.Client) -> None:
    """Cut off mid document, so it is not valid JSON at all."""
    respx.get(SCHEDULE).mock(
        return_value=httpx.Response(200, content=fixture_bytes("espn_schedule_truncated.json"))
    )
    respx.get(SCOREBOARD).mock(return_value=httpx.Response(200, json={"events": []}))

    assert EspnAdapter(client).fetch(CONFIG, ANCIENT) == []


@respx.mock
def test_both_endpoints_failing_yields_nothing_rather_than_raising(
    client: httpx.Client,
) -> None:
    respx.get(SCHEDULE).mock(side_effect=httpx.ConnectError("no route"))
    respx.get(SCOREBOARD).mock(side_effect=httpx.ConnectError("no route"))

    assert EspnAdapter(client).fetch(CONFIG, ANCIENT) == []


@respx.mock
def test_the_same_game_on_both_endpoints_survives_once(client: httpx.Client) -> None:
    respx.get(SCHEDULE).mock(
        return_value=httpx.Response(200, content=fixture_bytes("espn_schedule.json"))
    )
    respx.get(SCOREBOARD).mock(
        return_value=httpx.Response(200, content=fixture_bytes("espn_schedule.json"))
    )

    items = EspnAdapter(client).fetch(CONFIG, ANCIENT)

    assert len(items) == len({item.url for item in items}) == 2


@respx.mock
def test_older_events_are_dropped(client: httpx.Client) -> None:
    respx.get(SCHEDULE).mock(
        return_value=httpx.Response(200, content=fixture_bytes("espn_schedule.json"))
    )
    respx.get(SCOREBOARD).mock(return_value=httpx.Response(200, json={"events": []}))

    items = EspnAdapter(client).fetch(CONFIG, datetime(2026, 2, 25, tzinfo=UTC))

    assert [i.title for i in items] == ["Final: Real Salt Lake 2, Seattle Sounders FC 1"]


def test_the_wrong_config_type_is_a_bug_not_a_degradation(client: httpx.Client) -> None:
    from spanreed.sources.rss import RssConfig

    with pytest.raises(TypeError):
        EspnAdapter(client).fetch(RssConfig(url="https://a.example/feed"), ANCIENT)
