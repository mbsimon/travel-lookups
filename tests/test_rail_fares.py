"""Operator-direct rail fares: each operator's parser against a captured
response, station matching, and the dispatcher's fail-soft behavior.

Fixtures in fixtures/rail/ are real responses captured on 2026-09-28 for
2026-10-15, trimmed to a few trains. No test reaches the network: every HTTP
session and every Transitous call is replaced, and a stray one fails the test.
"""
from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import pytest

from travel_lookups import rail_fares as rf
from travel_lookups import trains
from travel_lookups.rail_fares import (_base, db, eurostar, iryo, italo, oebb, ouigo, renfe,
                                       sbb, trenitalia)

FIXTURES = Path(__file__).parent / "fixtures" / "rail"
FUTURE = "2099-10-15"


def load(name: str):
    return json.loads(gzip.decompress((FIXTURES / f"{name}.json.gz").read_bytes()))


def renfe_payload() -> dict:
    return renfe.extract_payload(
        gzip.decompress((FIXTURES / "renfe_trains.dwr.gz").read_bytes()).decode())


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Any HTTP session or Transitous call is a hard failure, and every cache
    starts empty so one test's answer cannot leak into another."""
    def forbidden(*_a, **_k):
        raise AssertionError("a rail_fares test must not reach the network")
    monkeypatch.setattr(_base, "http_session", forbidden)
    monkeypatch.setattr(trains, "_get", forbidden)
    monkeypatch.setattr(trains, "station", forbidden)
    rf._results.clear()
    renfe.reset_pacing()


# ─── parsers ─────────────────────────────────────────────────────────────────

def test_ouigo_packages_become_fares_priced_for_the_party():
    rows = ouigo.parse(load("ouigo_es_journeys"), 1, "Ouigo España")
    first = rows[0]
    assert first["train"] == "OUIGO 06471"
    assert (first["departs"], first["arrives"]) == ("06:22", "09:45")
    assert [f["fare"] for f in first["fares"]] == ["OUIGO Esencial", "OUIGO Plus", "Ouigo FULL"]
    assert first["cheapest"]["price"] == 33.0
    assert first["duration"] == "3h 23m"


def test_a_full_ouigo_train_is_sold_out_and_never_the_cheapest():
    rows = ouigo.parse(load("ouigo_fr_journeys"), 1, "Ouigo France")
    full = [r for r in rows if r["sold_out"]]
    assert len(full) == 1 and full[0]["cheapest"] is None
    assert all(r["cheapest"] for r in rows if not r["sold_out"])


def test_per_person_divides_the_party_total():
    row = ouigo.parse(load("ouigo_es_journeys"), 2, "Ouigo España")[0]
    assert row["cheapest"]["price"] == 33.0
    assert row["cheapest"]["per_person"] == 16.5


def test_iryo_names_each_bundle_including_the_ones_outside_the_proposals():
    stations = {s["uicStationCode"]: s["name"] for s in load("iryo_stations")["data"]}
    row = iryo.parse(load("iryo_search"), 1, stations)[0]
    names = [f["fare"] for f in row["fares"]]
    # Espacio Silencio is named only in the bundle's texts entry.
    assert names[:2] == ["Inicial", "Espacio Silencio"]
    assert "Infinita Bistró" in names
    assert (row["origin"], row["destination"]) == ("Madrid-Atocha", "Barcelona-Sants")
    assert row["train"] == "iryo 06261" and row["departs"] == "06:09"


def test_renfe_quotes_per_person_and_is_multiplied_to_the_party():
    """precioTarifa came back 37.35 for one adult and for two on the same AVE."""
    one = renfe.parse(renfe_payload(), 1)
    two = renfe.parse(renfe_payload(), 2)
    assert one[1]["train"] == "AVE 3063"
    assert one[1]["cheapest"]["price"] == 37.35
    assert two[1]["cheapest"]["price"] == 74.7
    assert two[1]["cheapest"]["per_person"] == 37.35
    assert one[0]["duration"] == "3h 08m"


def test_renfe_u014_is_a_throttle_and_starts_the_cooldown(monkeypatch):
    # The framing of a real DWR reply, with the exception renfecli documents.
    u014 = ("throw 'allowScriptTagRemoting is false.';\n(function(){\n"
            "var r=window.dwr._[0];\n//#DWR-REPLY\n"
            'r.handleException("0","0",{cdgoError:"U014",message:"sesion caducada"});\n})();')
    with pytest.raises(_base.Throttled):
        renfe.extract_payload(u014)
    # A throttle inside a search starts a cooldown; the next search is
    # refused at once, without a session or a request.
    monkeypatch.setattr(renfe, "_stations", _FixedCache(_renfe_candidates()))

    class Session:
        cookies = type("C", (), {"get": lambda *_: None, "set": lambda *_a, **_k: None})()
        headers: dict = {}

        def request(self, method, url, **_):
            return type("R", (), {"status_code": 200,
                                  "text": u014 if "getTrainsList" in url else ""})()
    monkeypatch.setattr(renfe, "_session", lambda: Session())
    q = _base.Query("Madrid Atocha", "Barcelona Sants", FUTURE)
    with pytest.raises(_base.Throttled):
        renfe.search(q)
    monkeypatch.setattr(renfe, "_session", lambda: pytest.fail("cooldown must not call Renfe"))
    with pytest.raises(_base.Throttled, match="cooling down"):
        renfe.search(q)


def test_renfe_pacing_refuses_rather_than_waiting_out_the_budget(monkeypatch):
    monkeypatch.setattr(renfe, "MIN_SECONDS_BETWEEN_SEARCHES", 60)
    renfe._wait_for_turn()
    with pytest.raises(_base.Throttled, match="paced"):
        renfe._wait_for_turn()


def test_renfe_answers_repeat_questions_from_its_cache(monkeypatch):
    monkeypatch.setattr(renfe, "_stations", _FixedCache(_renfe_candidates()))
    calls = []
    monkeypatch.setattr(renfe, "_fetch", lambda *a: calls.append(a) or renfe_payload())
    q = _base.Query("Madrid Atocha", "Barcelona Sants", FUTURE)
    renfe.search(q)
    renfe.search(q)
    assert len(calls) == 1


def test_renfe_station_list_reads_only_the_first_array():
    js = ('var estacionesEstatico=[{"cdgoEstacion":"60000","desgEstacion":"MADRID-PUERTA DE '
          'ATOCHA-ALMUDENA GRANDES","desgEstacionPlano":"MADRID-PUERTA DE ATOCHA",'
          '"nmroPrioridad":2}];\nvar estacionesDestacada=[{"x":1}];')
    assert [s["cdgoEstacion"] for s in renfe.parse_station_list(js)] == ["60000"]


def test_renfe_queue_it_page_is_reported_as_blocked():
    with pytest.raises(_base.Blocked):
        renfe.extract_payload("<html>queue-it waiting room</html>")


def test_trenitalia_single_train_lists_every_service_level_and_drops_age_fares():
    row = trenitalia.parse(load("trenitalia_solutions"), 1)[0]
    assert row["train"] == "FR 9516"
    classes = {f["class"] for f in row["fares"]}
    assert {"STANDARD", "PREMIUM", "BUSINESS", "EXECUTIVE"} <= classes
    names = {f["fare"].lower() for f in row["fares"]}
    assert not any("young" in n or "senior" in n for n in names)
    assert row["cheapest"]["price"] == 54.9


def test_trenitalia_connection_without_a_grid_is_unpriced_not_sold_out():
    rows = trenitalia.parse(load("trenitalia_solutions"), 1)
    connection = rows[-1]
    assert connection["changes"] == 2
    assert connection["cheapest"] is None
    assert connection["sold_out"] is False
    assert connection["fares"][0]["fare"] == "no price returned"


def test_italo_keeps_the_cheapest_per_class_and_offer_with_seats():
    names = {k: v["name"] for k, v in load("italo_stations").items()}
    row = italo.parse(load("italo_booking"), 1, names)[0]
    assert row["train"] == "Italo 9904"
    assert (row["origin"], row["destination"]) == ("Roma Termini", "Milano Centrale")
    keys = [(f["class"], f["fare"]) for f in row["fares"]]
    assert len(keys) == len(set(keys))
    assert row["cheapest"] == {"fare": "eXtra Magic", "class": "Smart", "price": 34.9,
                               "per_person": 34.9, "currency": "EUR"}


def test_eurostar_uses_the_party_total_not_the_display_price():
    """displayPrice is per person; two adults came back 132 display, 264 total."""
    payload = load("eurostar_search")
    for journey in payload["data"]["journeySearch"]["outbound"]["journeys"]:
        for f in journey["fares"]:
            f["prices"]["total"] = f["prices"]["displayPrice"] * 2
    row = eurostar.parse(payload, 2, "GBP")[0]
    assert row["cheapest"]["price"] == 264.0
    assert row["cheapest"]["per_person"] == 132.0
    assert row["cheapest"]["currency"] == "GBP"
    assert row["duration"] == "2h 28m"          # 06:01 London to 09:29 Paris
    assert row["fares"][0]["seats_left"] == 102


def test_db_reports_its_from_price_per_connection():
    rows = db.parse(load("db_fahrplan"), 1)
    assert rows[0]["train"] == "ICE 1602"
    assert rows[0]["cheapest"]["price"] == 95.99
    assert rows[1]["changes"] == 1 and rows[1]["train"] == "ICE 600 + ICE 376"


def test_sbb_prices_arrive_in_centimes_for_one_traveler():
    data = load("sbb_trips")
    rows = sbb.parse(data["trips"], data["prices"], 2, "Zürich HB", "Genève")
    first = rows[0]
    assert first["cheapest"]["price"] == 113.6     # 56.80 CHF x 2
    assert first["cheapest"]["currency"] == "CHF"
    assert {f["class"] for f in first["fares"]} == {"2nd", "1st"}


def test_sbb_trip_without_prices_is_unpriced_not_sold_out():
    data = load("sbb_trips")
    rows = sbb.parse(data["trips"][:1], {}, 1, "Zürich HB", "Genève")
    assert rows[0]["sold_out"] is False and rows[0]["cheapest"] is None


def test_oebb_one_price_per_connection():
    data = load("oebb_timetable")
    rows = oebb.parse(data["connections"], data["prices"], 1)
    assert rows[0]["train"] == "RJX 19962"
    assert rows[0]["cheapest"]["price"] == 63.7
    assert rows[0]["fares"][0]["class"] == "2nd"


# ─── station matching ────────────────────────────────────────────────────────

@pytest.mark.parametrize("query,expected", [
    ("Madrid", "MT1"),                   # the city code, as ouigo.com searches it
    ("Madrid Atocha", "7160000"),
    ("Madrid Chamartín", "7117000"),
    ("Barcelona", "7171801"),
])
def test_ouigo_station_matching(query, expected):
    candidates = ouigo.station_candidates(load("ouigo_es_stations"))
    assert _base.match_station(query, candidates, "Ouigo")["id"] == expected


@pytest.mark.parametrize("query,expected", [
    ("Roma", "RM0"), ("Roma Termini", "RMT"), ("Milano", "MI0"),
    ("Firenze S.M.Novella", "SMN"),
])
def test_italo_station_matching_skips_bus_stops_and_disabled_codes(query, expected):
    candidates = italo.station_candidates(load("italo_stations"))
    assert all(not c["name"].endswith("BUS") for c in candidates)
    assert "OLD" not in {c["id"] for c in candidates}
    assert _base.match_station(query, candidates, "Italo")["id"] == expected


@pytest.mark.parametrize("query,expected", [
    ("London", "7015400"), ("Paris", "8727100"), ("Bruxelles-Midi", "8814001"),
    ("Köln Hbf", "8015458"), ("Amsterdam", "8400058"),
])
def test_eurostar_station_matching_accepts_local_names(query, expected):
    candidates = eurostar.station_candidates(load("eurostar_stations"))
    assert _base.match_station(query, candidates, "Eurostar")["id"] == expected


def _renfe_candidates():
    raw = [
        {"cdgoEstacion": "MADRI", "desgEstacion": "MADRID (TODAS)", "desgEstacionPlano": "MADRID (TODAS)", "nmroPrioridad": 1},
        {"cdgoEstacion": "60000", "desgEstacion": "MADRID-PUERTA DE ATOCHA-ALMUDENA GRANDES", "desgEstacionPlano": "MADRID-PUERTA DE ATOCHA-ALMUDENA GRANDES", "nmroPrioridad": 2},
        {"cdgoEstacion": "71801", "desgEstacion": "BARCELONA-SANTS", "desgEstacionPlano": "BARCELONA-SANTS", "nmroPrioridad": 4},
        {"cdgoEstacion": "18000", "desgEstacion": "MADRID-ATOCHA CERCANÍAS", "desgEstacionPlano": "MADRID-ATOCHA CERCANIAS", "nmroPrioridad": 40},
    ]
    return renfe.station_candidates(raw)


def test_renfe_prefers_its_own_ranking_between_two_atochas():
    """Both Atochas cover "Madrid Atocha"; the AVE station is Renfe's rank 2,
    the commuter platforms rank 40."""
    candidates = _renfe_candidates()
    assert _base.match_station("Madrid Atocha", candidates, "Renfe")["id"] == "60000"
    assert _base.match_station("Madrid", candidates, "Renfe")["id"] == "MADRI"
    assert _base.match_station("Madrid Atocha Cercanías", candidates, "Renfe")["id"] == "18000"


def test_an_unknown_station_is_refused_not_guessed():
    candidates = ouigo.station_candidates(load("ouigo_es_stations"))
    with pytest.raises(_base.NoService):
        _base.match_station("Bilbao", candidates, "Ouigo")


def test_the_transitous_name_breaks_a_tie():
    candidates = ouigo.station_candidates(load("ouigo_es_stations"))
    hint = {"name": "Madrid-Chamartín-Clara Campoamor"}
    assert _base.match_station("Madrid Chamartin", candidates, "Ouigo", hint)["id"] == "7117000"


# ─── dispatcher ──────────────────────────────────────────────────────────────

class _FixedCache:
    def __init__(self, value):
        self.value = value

    def get_or_load(self, *_):
        return self.value


def _row(operator, departs, price):
    return _base.row(operator, f"{operator} 1",
                     _base.parse_local(f"{FUTURE}T{departs}:00"),
                     _base.parse_local(f"{FUTURE}T{departs}:00").replace(hour=23),
                     "A", "B", [_base.fare("Standard", price, "EUR", 1)])


def _fake(name, behavior):
    """An operator whose search raises `behavior`, sleeps past the budget
    ("slow"), or returns `behavior` as its rows."""
    def search(q):
        if isinstance(behavior, Exception):
            raise behavior
        if behavior == "slow":
            time.sleep(2)
            return {"trains": []}
        return {"origin": "A", "destination": "B", "trains": behavior}
    return rf.Operator(name.lower(), name, search, False)


@pytest.fixture
def placed(monkeypatch):
    """Both stations place in Spain; the three Spanish operators are fakes."""
    monkeypatch.setattr(trains, "station", lambda q: {"name": q, "country": "ES",
                                                      "timezone": "Europe/Madrid"})

    def install(**behaviors):
        ops = {k: rf.Operator(k, k, _fake(k, b).search, False) for k, b in behaviors.items()}
        monkeypatch.setattr(rf, "OPERATORS", ops)
        monkeypatch.setattr(rf, "DOMESTIC", {"ES": list(ops)})
    return install


def test_selection_by_country():
    assert rf.select_operators("ES", "ES") == ["renfe", "iryo", "ouigo_es"]
    assert rf.select_operators("IT", "IT") == ["trenitalia", "italo"]
    assert rf.select_operators("FR", "FR") == ["ouigo_fr"]
    assert rf.select_operators("GB", "FR") == ["eurostar"]
    assert rf.select_operators("AT", "DE") == ["oebb", "db"]
    assert rf.select_operators("CH", "IT") == ["sbb", "trenitalia", "db"]
    assert rf.select_operators("BE", "NL") == ["eurostar", "db"]
    assert rf.select_operators("GB", "GB") == []
    assert rf.select_operators(None, "ES") == []


def test_one_operator_failing_never_sinks_the_others(placed):
    placed(renfe=_base.Blocked("Renfe refused the request (HTTP 403)"),
           iryo=[_row("Iryo", "09:00", 40.0)],
           ouigo_es=ValueError("a parser met a changed response"))
    result = rf.rail_fares("Madrid", "Barcelona", FUTURE)
    statuses = {l["key"]: l["status"] for l in result["operators"]}
    assert statuses == {"renfe": "blocked", "iryo": "ok", "ouigo_es": "error"}
    assert result["count"] == 1 and result["trains"][0]["operator"] == "Iryo"
    assert "ValueError" in next(l for l in result["operators"] if l["key"] == "ouigo_es")["message"]


def test_rows_from_every_operator_are_merged_and_sorted_by_departure(placed):
    placed(renfe=[_row("Renfe", "10:00", 50.0), _row("Renfe", "07:00", 60.0)],
           iryo=[_row("Iryo", "08:30", 35.0)], ouigo_es=[])
    result = rf.rail_fares("Madrid", "Barcelona", FUTURE)
    assert [r["departs"] for r in result["trains"]] == ["07:00", "08:30", "10:00"]
    assert [c["operator"] for c in result["cheapest"]] == ["Iryo"]
    assert result["cheapest"][0]["fare"]["price"] == 35.0
    ouigo_line = next(l for l in result["operators"] if l["key"] == "ouigo_es")
    assert ouigo_line["status"] == "no_service"


def test_cheapest_is_per_currency_never_across_currencies(placed):
    chf = _row("SBB", "09:00", 30.0)
    chf["cheapest"]["currency"] = "CHF"
    placed(renfe=[chf], iryo=[_row("Iryo", "08:30", 35.0)], ouigo_es=[])
    result = rf.rail_fares("Madrid", "Barcelona", FUTURE)
    assert {c["fare"]["currency"]: c["operator"] for c in result["cheapest"]} == {
        "EUR": "Iryo", "CHF": "SBB"}


def test_depart_after_filters_every_operator(placed):
    placed(renfe=[_row("Renfe", "07:00", 60.0), _row("Renfe", "15:00", 50.0)],
           iryo=[_row("Iryo", "08:30", 35.0)], ouigo_es=[])
    result = rf.rail_fares("Madrid", "Barcelona", FUTURE, depart_after="12:00")
    assert [r["departs"] for r in result["trains"]] == ["15:00"]


def test_a_slow_operator_times_out_and_the_rest_still_answer(placed, monkeypatch):
    monkeypatch.setattr(rf, "OVERALL_BUDGET_SECONDS", 0.5)
    placed(renfe="slow", iryo=[_row("Iryo", "09:00", 40.0)], ouigo_es=[])
    started = time.monotonic()
    result = rf.rail_fares("Madrid", "Barcelona", FUTURE)
    assert time.monotonic() - started < 1.5
    assert {l["key"]: l["status"] for l in result["operators"]}["renfe"] == "timeout"
    assert result["count"] == 1


def test_operators_needing_curl_cffi_say_so_when_it_is_missing(placed, monkeypatch):
    placed(renfe=[], iryo=[_row("Iryo", "09:00", 40.0)], ouigo_es=[])
    rf.OPERATORS["iryo"] = rf.Operator("iryo", "Iryo", rf.OPERATORS["iryo"].search, True)
    monkeypatch.setattr(rf, "curl_cffi_installed", lambda: False)
    result = rf.rail_fares("Madrid", "Barcelona", FUTURE)
    line = next(l for l in result["operators"] if l["key"] == "iryo")
    assert line["status"] == "unavailable" and "curl_cffi" in line["message"]


def test_a_real_curl_cffi_import_failure_is_unavailable(monkeypatch):
    monkeypatch.undo()   # the real http_session, with curl_cffi hidden
    import builtins
    real_import = builtins.__import__

    def no_cffi(name, *a, **k):
        if name.startswith("curl_cffi"):
            raise ImportError("hidden for the test")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_cffi)
    with pytest.raises(_base.Unavailable, match="curl_cffi not installed"):
        _base.http_session(impersonate=True)


def test_answers_are_reused_within_the_ttl(placed):
    calls = []

    def counting(q):
        calls.append(q)
        return {"trains": [_row("Iryo", "09:00", 40.0)]}
    placed(renfe=[], iryo=[], ouigo_es=[])
    rf.OPERATORS["iryo"] = rf.Operator("iryo", "Iryo", counting, False)
    rf.rail_fares("Madrid", "Barcelona", FUTURE)
    rf.rail_fares("Madrid", "Barcelona", FUTURE)
    assert len(calls) == 1


def test_an_uncovered_country_pair_says_it_is_a_tool_gap(monkeypatch):
    monkeypatch.setattr(trains, "station", lambda q: {"name": q, "country": "GB"})
    result = rf.rail_fares("London", "Edinburgh", FUTURE)
    assert result["trains"] == [] and result["operators"] == []
    assert "gap in this tool" in result["note"]


def test_a_bare_city_is_placed_by_the_geocoder(monkeypatch):
    """Bare city names stopped resolving to a stop in August 2026; the
    geocoder still ranks the city first, which is enough to pick operators."""
    def no_stop(q):
        raise trains.TrainsError(f"No rail station matched {q!r}.")
    monkeypatch.setattr(trains, "station", no_stop)
    monkeypatch.setattr(trains, "_get", lambda path, params: [
        {"type": "PLACE", "name": params["text"], "country": "FR", "lat": 48.8, "lon": 2.3}])
    hint, problem = rf._place("Paris")
    assert problem is None and hint["country"] == "FR"


def test_an_unplaceable_station_without_operators_is_refused(monkeypatch):
    def no_stop(q):
        raise trains.TrainsError("No rail station matched.")
    monkeypatch.setattr(trains, "station", no_stop)
    monkeypatch.setattr(trains, "_get", lambda path, params: [])
    with pytest.raises(rf.RailFaresError, match="operators="):
        rf.rail_fares("Nowhere", "Elsewhere", FUTURE)


@pytest.mark.parametrize("kwargs,match", [
    ({"date": "2020-01-01"}, "past"),
    ({"date": "15/10/2026"}, "YYYY-MM-DD"),
    ({"date": FUTURE, "adults": 0}, "adults"),
    ({"date": FUTURE, "depart_after": "9am"}, "HH:MM"),
    ({"date": FUTURE, "operators": ["trainline"]}, "unknown operator"),
])
def test_bad_questions_are_refused_before_any_call(kwargs, match):
    with pytest.raises(rf.RailFaresError, match=match):
        rf.rail_fares("Madrid", "Barcelona", **kwargs)


def test_status_makes_no_network_call():
    status = rf.status()
    assert set(status["operators"]) == set(rf.OPERATORS)
    assert isinstance(status["curl_cffi"], bool)
