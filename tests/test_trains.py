"""Rail lookups — local time, and the free source staying free.

The two things here that would reach a client. Transitous returns genuine UTC
while every stop carries its own timezone, so printing the raw value puts a
two-hour error on a Madrid departure. And a schedule question must never spend
the network at all — schedules are the whole product now that the fare source
is gone.

No network.
"""
import sys
from pathlib import Path

import pytest


from travel_lookups import trains  # noqa: E402

MADRID = {"id": "es-RENFE_18000", "name": "Madrid-Puerta de Atocha",
          "timezone": "Europe/Madrid", "country": "ES"}
BARCELONA = {"id": "es-RENFE_71801", "name": "Barcelona-Sants",
             "timezone": "Europe/Madrid", "country": "ES"}

# One AVE, as Transitous reports it: UTC timestamps, per-stop tz.
PLAN = {"itineraries": [{
    "duration": 11820, "transfers": 0,
    "legs": [
        {"mode": "WALK", "duration": 120},
        {"mode": "HIGHSPEED_RAIL", "duration": 11820,
         "agencyName": "RENFE OPERADORA", "tripShortName": "03083",
         "reservation": True, "realTime": False, "cancelled": False,
         "from": {"name": "Madrid-Puerta de Atocha", "tz": "Europe/Madrid",
                  "departure": "2026-09-15T06:27:00Z"},
         "to": {"name": "Barcelona-Sants", "tz": "Europe/Madrid",
                "arrival": "2026-09-15T09:44:00Z"}},
    ]}]}


@pytest.fixture
def offline(monkeypatch):
    """Stations resolve from fixtures; any HTTP call is a hard failure."""
    monkeypatch.setattr(trains, "station",
                        lambda q: MADRID if "madrid" in q.lower() else BARCELONA)
    monkeypatch.setattr(trains, "_get", lambda path, params: PLAN)

    def forbidden(*a, **k):
        raise AssertionError("a schedule lookup must not reach the network")
    monkeypatch.setattr(trains.requests, "get", forbidden)
    monkeypatch.setattr(trains.requests, "post", forbidden)


def test_times_are_local_not_the_raw_utc(offline):
    """06:27Z out of Madrid is 08:27 on the platform, not 06:27.

    Transitous honours offsets and returns real UTC — verified 2026-08-16 by
    asking for 05:00+02:00 and getting an EARLIER departure than 05:00Z.
    """
    journey = trains.journeys("Madrid Atocha", "Barcelona Sants", "2026-09-15")["journeys"][0]
    assert journey["departs"]["local"] == "08:27"
    assert journey["arrives"]["local"] == "11:44"
    assert journey["departs"]["utc"].endswith("06:27Z")


def test_walking_legs_do_not_count_as_changes(offline):
    """The planner brackets journeys with walk legs; counting them would
    report a direct AVE as a journey with two changes."""
    journey = trains.journeys("Madrid Atocha", "Barcelona Sants", "2026-09-15")["journeys"][0]
    assert journey["changes"] == 0
    assert len(journey["legs"]) == 1
    assert journey["legs"][0]["operator"] == "RENFE OPERADORA"
    assert journey["legs"][0]["service"] == "03083"


def test_a_schedule_lookup_stays_on_the_free_path(offline):
    """`offline` makes any outbound HTTP call raise, so this passes only while
    journeys() stays on Transitous — the only source there is."""
    trains.journeys("Madrid Atocha", "Barcelona Sants", "2026-09-15")


def test_bad_date_is_refused_before_any_call(offline):
    with pytest.raises(trains.TrainsError):
        trains.journeys("Madrid Atocha", "Barcelona Sants", "next tuesday")
    with pytest.raises(trains.TrainsError):
        trains.journeys("Madrid Atocha", "Barcelona Sants", "2026-09-15", depart_after="morning")


def test_results_are_capped(offline):
    out = trains.journeys("Madrid Atocha", "Barcelona Sants", "2026-09-15", max_results=99)
    assert out["count"] <= trains.MAX_RESULTS


def test_a_station_without_a_timezone_is_refused(monkeypatch):
    """Rather than guess and print a wrong platform time."""
    monkeypatch.setattr(trains, "_station_cache", {})
    monkeypatch.setattr(trains, "_save_station_cache", lambda: None)
    monkeypatch.setattr(trains, "_get", lambda p, q: [
        {"type": "STOP", "modes": ["RAIL"], "id": "x", "name": "Nowhere", "tz": None}])
    with pytest.raises(trains.TrainsError):
        trains.station("Nowhere")


def test_module_cannot_book(monkeypatch):
    # Callables only: a CONSTANT may legitimately contain one of these words —
    # SERPAPI_RESERVE is a budget held back, not a way to reserve a seat. What
    # must not exist is a function that books.
    exported = [n for n in dir(trains) if not n.startswith("_")
                and callable(getattr(trains, n))]
    for forbidden in ("book", "reserve", "hold", "purchase", "order", "ticket"):
        assert not any(forbidden in name.lower() for name in exported), forbidden


def test_a_bus_stop_is_never_accepted_as_a_station(monkeypatch):
    """"Seville" surfaced no rail stop, and relaxing the filter picked
    "Seville Street" — a bus stop in the United Kingdom — as the origin of a
    Madrid-Seville train. Fail loudly instead, and say why."""
    monkeypatch.setattr(trains, "_station_cache", {})
    monkeypatch.setattr(trains, "_save_station_cache", lambda: None)
    monkeypatch.setattr(trains, "_get", lambda p, q: [
        {"type": "PLACE", "name": "Seville", "modes": [], "tz": "Europe/Madrid"},
        {"type": "STOP", "name": "Seville Street", "modes": ["BUS"],
         "importance": 0.0008, "tz": "Europe/London", "id": "gb-bus_1"},
    ])
    with pytest.raises(trains.TrainsError) as e:
        trains.station("Seville")
    assert "local spelling" in str(e.value)


def test_the_main_station_wins_over_a_same_named_halt(monkeypatch):
    """Importance separates Sevilla-Santa Justa (0.0167) from noise (0.0001)."""
    monkeypatch.setattr(trains, "_station_cache", {})
    monkeypatch.setattr(trains, "_save_station_cache", lambda: None)
    monkeypatch.setattr(trains, "_get", lambda p, q: [
        {"type": "STOP", "name": "SEVILLA", "modes": ["REGIONAL_RAIL"],
         "importance": 0.0001, "tz": "Europe/Madrid", "id": "halt"},
        {"type": "STOP", "name": "Sevilla-Santa Justa", "id": "main",
         "modes": ["HIGHSPEED_RAIL", "LONG_DISTANCE"], "importance": 0.0167,
         "tz": "Europe/Madrid"},
    ])
    assert trains.station("Sevilla")["name"] == "Sevilla-Santa Justa"
