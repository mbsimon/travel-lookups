"""Flight lookup — the cost gate and the codeshare collapse.

Two things here would show up as real damage. Spending a SerpApi search on a
question AeroAPI answers free burns a 250/month budget on schedules, and it
does it silently. Listing codeshares as separate options offers a client a
choice between four flight numbers that are one aircraft.

No network.
"""
import sys
from pathlib import Path

import pytest


from travel_lookups import flights  # noqa: E402

# One physical departure, sold under its operator and three partner numbers —
# the shape AeroAPI actually returns for MAD-JFK.
CODESHARE_ROWS = [
    {"ident": "IBE4001", "ident_iata": "IB4001", "actual_ident": "AAL95",
     "actual_ident_iata": "AA95", "aircraft_type": "B772",
     "scheduled_out": "2026-09-15T08:25:00Z", "scheduled_in": "2026-09-15T16:25:00Z",
     "seats_cabin_first": 0, "seats_cabin_business": 37, "seats_cabin_coach": 236},
    {"ident": "BAW1567", "ident_iata": "BA1567", "actual_ident": "AAL95",
     "actual_ident_iata": "AA95", "aircraft_type": "B772",
     "scheduled_out": "2026-09-15T08:25:00Z", "scheduled_in": "2026-09-15T16:25:00Z",
     "seats_cabin_first": 0, "seats_cabin_business": 37, "seats_cabin_coach": 236},
    {"ident": "AAL95", "ident_iata": "AA95", "actual_ident": None,
     "aircraft_type": "B772",
     "scheduled_out": "2026-09-15T08:25:00Z", "scheduled_in": "2026-09-15T16:25:00Z",
     "seats_cabin_first": 0, "seats_cabin_business": 37, "seats_cabin_coach": 236},
]

AIRPORTS = {
    "MAD": {"iata": "MAD", "icao": "LEMD", "name": "Adolfo Suárez Madrid-Barajas",
            "city": "Madrid", "timezone": "Europe/Madrid"},
    "JFK": {"iata": "JFK", "icao": "KJFK", "name": "John F. Kennedy Intl",
            "city": "New York", "timezone": "America/New_York"},
}


@pytest.fixture
def offline(monkeypatch):
    """AeroAPI answers from fixtures; any SerpApi call is a hard failure."""
    monkeypatch.setattr(flights, "airport", lambda code: AIRPORTS[code.upper()])
    monkeypatch.setattr(flights, "_aero_all_pages",
                        lambda path, params, collection: (CODESHARE_ROWS, False))

    def forbidden(*a, **k):
        raise AssertionError("a schedule lookup must not reach the network")
    monkeypatch.setattr(flights.requests, "get", forbidden)


def test_codeshares_collapse_to_one_aircraft(offline):
    """Four marketing numbers, one departure — one row, not four."""
    out = flights.nonstop_service("MAD", "JFK", "2026-09-15")
    assert out["count"] == 1
    flight = out["nonstop_flights"][0]
    assert flight["operated_by"] == "AA95"
    assert set(flight["sold_also_as"]) == {"IB4001", "BA1567"}
    # The operating row itself is not its own codeshare.
    assert "AA95" not in flight["sold_also_as"]


def test_times_are_local_to_each_airport(offline):
    """08:25Z out of Madrid is 10:25 there; 16:25Z into JFK is 12:25 there."""
    flight = flights.nonstop_service("MAD", "JFK", "2026-09-15")["nonstop_flights"][0]
    assert flight["departs"]["local"] == "10:25"
    assert flight["arrives"]["local"] == "12:25"
    assert flight["duration"] == "8h 00m"


def test_cabin_layout_survives(offline):
    flight = flights.nonstop_service("MAD", "JFK", "2026-09-15")["nonstop_flights"][0]
    assert flight["cabins"] == {"first": 0, "business": 37, "economy": 236}


def test_schedule_lookup_never_spends_a_serpapi_search(offline):
    """The whole point of the split: schedules are free, fares are not.

    `offline` makes any outbound HTTP call raise, so this passes only while
    nonstop_service stays on the AeroAPI path.
    """
    flights.nonstop_service("MAD", "JFK", "2026-09-15")


def test_schedule_window_is_capped(offline):
    """A month-long schedule request is a page-budget hole, not an answer."""
    out = flights.nonstop_service("MAD", "JFK", "2026-09-15", days=30)
    assert out["window"]["days"] == flights.MAX_SCHEDULE_DAYS


def test_bad_date_is_refused_before_any_call(offline):
    with pytest.raises(flights.FlightsError):
        flights.nonstop_service("MAD", "JFK", "next tuesday")


def test_fares_report_unconfigured_rather_than_failing(monkeypatch):
    """Without a key the fare path must degrade, not throw — schedules still work."""
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    out = flights.itineraries("MAD", "HND", "2026-09-20")
    assert out["configured"] is False
    assert "SERPAPI_KEY" in out["reason"]


def test_iata_collision_is_not_trusted(monkeypatch):
    """`HND` asked of AeroAPI's airport lookup returns Henderson, Nevada.

    Left unchecked, its America/Los_Angeles timezone converts a Tokyo arrival
    into Pacific time — a wrong number on an itinerary. The record must come
    back marked unverified so no caller names the place.
    """
    monkeypatch.setattr(flights, "_airport_cache", {})
    monkeypatch.setattr(flights, "_save_airport_cache", lambda: None)
    monkeypatch.setattr(flights, "_aero", lambda path, params=None: {
        "code_iata": None, "code_icao": "KHND", "name": "Henderson Exec",
        "city": "Las Vegas", "timezone": "America/Los_Angeles"})
    record = flights.airport("HND")
    assert record["iata_verified"] is False
    assert record["requested"] == "HND"


def test_schedule_data_overrides_a_bad_airport_lookup(monkeypatch):
    """The schedules endpoint resolves HND correctly, so believe it over the
    airport lookup and re-resolve from the ICAO the flights report."""
    monkeypatch.setattr(flights, "_aero_all_pages", lambda p, q, collection: ([{
        "ident": "BAW7", "ident_iata": "BA7", "actual_ident": None,
        "aircraft_type": "A35K", "origin_icao": "EGLL", "destination_icao": "RJTT",
        "scheduled_out": "2026-09-20T08:25:00Z",
        "scheduled_in": "2026-09-20T22:00:00Z"}], False))

    resolved = {"EGLL": {"iata": "LHR", "icao": "EGLL", "city": "London",
                         "timezone": "Europe/London", "iata_verified": True},
                "RJTT": {"iata": "HND", "icao": "RJTT", "city": "Ota",
                         "timezone": "Asia/Tokyo", "iata_verified": True},
                "LHR": {"iata": "LHR", "icao": "EGLL", "city": "London",
                        "timezone": "Europe/London", "iata_verified": True},
                "HND": {"iata": None, "icao": "KHND", "city": "Las Vegas",
                        "timezone": "America/Los_Angeles", "iata_verified": False}}
    monkeypatch.setattr(flights, "airport", lambda code: resolved[code.upper()])

    out = flights.nonstop_service("LHR", "HND", "2026-09-20")
    assert out["destination"]["city"] == "Ota"
    assert out["destination"]["timezone"] == "Asia/Tokyo"
    # 22:00Z lands 07:00 Tokyo the NEXT day, not 15:00 in Las Vegas.
    assert out["nonstop_flights"][0]["arrives"]["local"] == "07:00"
    assert out["nonstop_flights"][0]["arrives"]["local_date"] == "2026-09-21"


def test_route_profile_counts_real_departures(offline):
    """Built on schedules, so a served route can never report zero.

    It used to read AeroAPI's /airports/{id}/routes endpoint, which returns
    filed ATC route strings. That answered Venice-Rome and Barcelona-Heathrow
    with zero rows while both fly several times a day — and answered
    Madrid-JFK with plausible numbers, which is why it survived testing.
    """
    out = flights.route_profile("MAD", "JFK", "2026-09-15", days=1)
    assert out["departures"] == 1          # the three fixture rows are one aircraft
    assert out["per_day"] == 1.0
    assert out["carriers"] == [{"code": "AA", "flights": 1}]
    assert out["aircraft_mix"] == [{"type": "B772", "flights": 1}]
    assert "no scheduled nonstop, not missing data" in out["note"]


def test_paged_requests_are_charged_their_real_rate_cost():
    """A max_pages=3 request spends three of the tier's ~10 queries a minute.

    Charging it as one let a schedule sweep sail past the limit and fail on a
    429, which reads to the caller as a broken route rather than a busy client.
    """
    flights._call_times.clear()
    flights._throttle(flights.AEROAPI_MAX_PAGES)
    assert len(flights._call_times) == flights.AEROAPI_MAX_PAGES
    flights._call_times.clear()


def test_module_cannot_book(monkeypatch):
    """Read-only by construction. No verb here writes to a supplier."""
    # Callables only: a CONSTANT may legitimately contain one of these words —
    # SERPAPI_RESERVE is a budget held back, not a way to reserve a seat. What
    # must not exist is a function that books.
    exported = [n for n in dir(flights) if not n.startswith("_")
                and callable(getattr(flights, n))]
    for forbidden in ("book", "reserve", "hold", "purchase", "order", "ticket"):
        assert not any(forbidden in name.lower() for name in exported), forbidden


def test_the_serpapi_budget_is_enforced_not_requested(monkeypatch, tmp_path):
    """250 searches ran out mid-month while every docstring said not to spend
    one on a schedule question. Asking is not a control; the reserve is."""
    monkeypatch.setattr(flights, "SERPAPI_LEDGER", tmp_path / "usage.json")
    monkeypatch.setenv("SERPAPI_KEY", "x")

    assert flights.serpapi_budget()["spendable"] == (
        flights.SERPAPI_MONTHLY - flights.SERPAPI_RESERVE)

    # Burn everything down to the reserve.
    import json as _json
    from datetime import date as _date
    (tmp_path / "usage.json").write_text(_json.dumps(
        {_date.today().strftime("%Y-%m"): flights.SERPAPI_MONTHLY - flights.SERPAPI_RESERVE}))

    def forbidden(*a, **k):
        raise AssertionError("must not call SerpApi once the budget is held")
    monkeypatch.setattr(flights.requests, "get", forbidden)

    held = flights.itineraries("MAD", "JFK", "2026-09-20")
    assert "budget held" in held["error"]
    assert held["budget"]["spendable"] == 0


def test_the_reserve_can_be_spent_deliberately(monkeypatch, tmp_path):
    """A hold that cannot be overridden is an outage. The reserve exists so a
    genuinely needed fare search still works late in the month."""
    import json as _json
    from datetime import date as _date
    monkeypatch.setattr(flights, "SERPAPI_LEDGER", tmp_path / "usage.json")
    monkeypatch.setenv("SERPAPI_KEY", "x")
    (tmp_path / "usage.json").write_text(_json.dumps(
        {_date.today().strftime("%Y-%m"): flights.SERPAPI_MONTHLY}))

    class Resp:
        status_code = 200
        text = ""
        @staticmethod
        def json():
            return {"best_flights": [], "other_flights": []}
    monkeypatch.setattr(flights.requests, "get", lambda *a, **k: Resp)

    out = flights.itineraries("MAD", "JFK", "2026-09-20", override_budget=True)
    assert out["configured"] is True and "error" not in out


def test_us_route_prices_in_dollars(monkeypatch, tmp_path):
    """A client comparing our fare to what they'd pay themselves shops in
    dollars when either end touches the US — anything else keeps the
    EUR/Spain default. Found live: a persisted airport cache from before the
    `country` field existed silently defeated this and always served EUR."""
    monkeypatch.setattr(flights, "SERPAPI_LEDGER", tmp_path / "usage.json")
    monkeypatch.setenv("SERPAPI_KEY", "x")
    countries = {"MAD": "ES", "JFK": "US", "BCN": "ES", "LHR": "GB"}
    monkeypatch.setattr(flights, "airport", lambda code: {"country": countries[code.upper()]})

    captured = {}

    class Resp:
        status_code = 200
        text = ""
        @staticmethod
        def json():
            return {"best_flights": [], "other_flights": []}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return Resp
    monkeypatch.setattr(flights.requests, "get", fake_get)

    out = flights.itineraries("MAD", "JFK", "2026-09-20")
    assert captured["params"]["currency"] == "USD"
    assert captured["params"]["gl"] == "us"
    assert out["currency"] == "USD"

    out2 = flights.itineraries("BCN", "LHR", "2026-09-20")
    assert captured["params"]["currency"] == "EUR"
    assert captured["params"]["gl"] == "es"
    assert out2["currency"] == "EUR"


def test_airport_cache_without_country_field_is_refreshed(monkeypatch):
    """A record cached before `country` existed must not be trusted forever —
    that's exactly how the US-currency check went silently dead live."""
    monkeypatch.setattr(flights, "_airport_cache", {"JFK": {"iata": "JFK", "city": "New York"}})
    monkeypatch.setattr(flights, "_save_airport_cache", lambda: None)
    monkeypatch.setattr(flights, "_aero", lambda path, params=None: {
        "code_iata": "JFK", "code_icao": "KJFK", "name": "JFK", "city": "New York",
        "country_code": "US", "timezone": "America/New_York"})
    record = flights.airport("JFK")
    assert record["country"] == "US"
