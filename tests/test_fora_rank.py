"""The ranked answer, checked against a real Fora pool.

Fixture: BNA-NAP economy, 2027-05-27, 2 adults, captured live 2026-09-24 with
commission and contract fields removed (this repo is public). 405 fares.
Michael found DL2294+DL278 via ATL and UA2848+UA966 via EWR on Google; the
old tool buried the first around rank 150 and quoted the second as economy.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from travel_lookups import fora_rank as fr
from travel_lookups.fora_flights import FlightSearchError, fare_matches, sells_cabins

POOL = json.loads(gzip.decompress(
    (Path(__file__).parent / "fixtures" / "fora_bna_nap_20270527_economy.json.gz").read_bytes()))


def _build(**kw):
    return fr.build(POOL, fr.Options(cabins=["Y"], adults=2, **kw))


def _flights(row):
    return "+".join(row["flights"])


def test_the_connection_michael_would_pick_ranks_first():
    out = _build()
    assert _flights(out["shortlist"][0]) == "DL2294+DL278"


def test_no_airport_change_reaches_the_shortlist():
    out = _build(max_results=50)
    for row in out["shortlist"]:
        for leg in row["legs"]:
            assert not any(l.get("airport_change") for l in leg["layovers"]), _flights(row)
    assert any(e["reason"] == "airport_change" for e in out["excluded"])


def test_ua_via_newark_is_not_sold_in_economy():
    """Every fare on UA2848+UA966 is Premium Economy or better across the Atlantic."""
    ua = [it for it in POOL if "UA2848" in it["key"] and "UA966" in it["key"]]
    assert ua and not any(sells_cabins(it, ["Y"]) for it in ua)
    out = _build(flights=["UA2848", "UA966"])
    assert out["shortlist"] == [] and "cabin" in out["note"]


def test_a_mixed_cabin_fare_does_not_pass_as_economy():
    it = {"legs": [{"segmentKeys": ["a", "b"]}]}
    assert fare_matches(it, {"cabinClass": ["Y", "Y"]}, ["Y"])
    assert not fare_matches(it, {"cabinClass": ["Y", "S"]}, ["Y"])
    assert fare_matches(it, {"cabinClass": ["C", "F"]}, ["C"]), "domestic first on business"


def test_codeshare_twins_collapse():
    """DL4664+DL232 is sold under five numbers; it must be one row."""
    out = _build(max_results=50)
    assert out["pool"]["physical_routings"] < out["pool"]["fares"]
    sigs = [tuple((l["origin"], l["departs_at"], l["arrives_at"]) for l in r["legs"])
            for r in out["shortlist"]]
    assert len(sigs) == len(set(sigs))
    jfk = next(r for r in out["shortlist"] if _flights(r) == "DL4664+DL232")
    assert len(jfk["also_sold_as"]) >= 3


def test_cheapest_refundable_prefers_a_ranked_routing():
    a = _build()["anchors"]["cheapest_refundable"]
    assert not a["status"].startswith("excluded")


def test_feeders_into_the_same_long_haul_fold():
    out = _build()
    top = out["shortlist"][0]
    assert top["other_departures"], "other ATL feeders into DL278 fold under the top row"
    assert all(d["flights"].endswith("DL278") for d in top["other_departures"])


def test_every_exclusion_is_counted_with_its_lift():
    out = _build()
    assert out["excluded"]
    for e in out["excluded"]:
        assert e["routings"] > 0 and e["lift_with"]
    removed = sum(e["routings"] for e in out["excluded"])
    assert removed + out["pool"]["ranked"] == out["pool"]["physical_routings"]


def test_anchors_always_say_where_they_landed():
    out = _build()
    a = out["anchors"]
    assert {"cheapest", "fastest", "cheapest_fewest_stops"} <= set(a)
    for v in a.values():
        assert v["status"]
    assert a["cheapest"]["status"].startswith("excluded"), "the 31-hour Aer Lingus slog"


def test_rank_reasons_explain_the_order():
    out = _build()
    assert out["shortlist"][0]["rank_reasons"]
    assert any("sit in" in r or "vs fastest" in r
               for row in out["shortlist"][1:] for r in row["rank_reasons"])


def test_price_sort_still_available_and_different():
    best = [_flights(r) for r in _build()["shortlist"]]
    cheap = _build(sort="price")["shortlist"]
    prices = [r["price_per_person_usd"] for r in cheap]
    assert prices == sorted(prices) and [_flights(r) for r in cheap] != best


def test_pinned_flights_bypass_comfort_filters_but_not_cabin():
    out = _build(flights=["DL4664", "DL232"])
    assert out["shortlist"] and _flights(out["shortlist"][0]) == "DL4664+DL232"


def test_flights_not_sold_together_say_so():
    out = _build(flights=["DL2294", "UA966"])
    assert out["shortlist"] == [] and "does not sell those flights together" in out["note"]


def test_fare_ladder_shows_the_refundable_step():
    top = _build()["shortlist"][0]
    assert len(top["fare_options"]) > 1
    assert any(f["refundable"] for f in top["fare_options"])


def test_refundable_only_prices_on_the_refundable_fare():
    out = _build(refundable_only=True)
    assert out["shortlist"] and all(r["refundable"] for r in out["shortlist"])


def test_arrive_before_and_depart_after_filter():
    out = _build(depart_after=["10:00"], max_results=50)
    for r in out["shortlist"]:
        assert r["legs"][0]["departs_at"][11:16] >= "10:00"
    assert any(e["reason"] == "departs_too_early" for e in out["excluded"])


def test_bad_inputs_are_errors():
    with pytest.raises(FlightSearchError):
        fr.Options(cabins=["Y"], sort="cheapest")
    with pytest.raises(FlightSearchError):
        fr.Options(cabins=["Y"], depart_after=["25:00"])


def test_an_airport_change_is_never_offered_unless_asked():
    """Michael: LGA to JFK "should not be permitted unless specifically asked for"."""
    change = [it for it in POOL if any(
        len(t.get("locations") or []) > 1 for lg in it["legs"] for t in lg["timeline"]
        if t.get("type") == "layover")]
    assert change, "the fixture holds LGA/JFK routings"
    flights = [tok[14:] for tok in change[0]["key"].split("|")]
    pinned = _build(flights=flights)
    assert pinned["shortlist"] == [] and "changes airports" in pinned["note"]
    out = _build(max_results=200)
    for a in out["anchors"].values():
        assert "/" not in a["connections"], a
    allowed = _build(flights=flights, allow_airport_change=True)
    assert allowed["shortlist"]


def test_by_routing_is_capped_and_best_first():
    out = _build()
    assert 0 < len(out["by_routing"]) <= fr.BY_ROUTING_MAX
    assert out["by_routing"][0]["routing"].startswith("DL via ATL")


def _two_leg(fares):
    legs = [{"origin": "BNA", "destination": "NAP", "segmentKeys": ["a"], "timeline": [
                {"type": "air", "locations": ["BNA", "NAP"], "startsAt": "2027-05-27T12:00:00-05:00"}],
             "departsAt": "2027-05-27T12:00:00-05:00", "arrivesAt": "2027-05-28T08:00:00+02:00",
             "elapsedTime": 600},
            {"origin": "NAP", "destination": "BCN", "segmentKeys": ["b"], "timeline": [
                {"type": "air", "locations": ["NAP", "BCN"], "startsAt": "2027-06-01T09:00:00+02:00"}],
             "departsAt": "2027-06-01T09:00:00+02:00", "arrivesAt": "2027-06-01T11:00:00+02:00",
             "elapsedTime": 120}]
    return [{"key": "20270527BNANAPXX1|20270601NAPBCNXX2", "legs": legs,
             "itineraryFares": [{"rawFare": p, "cabinClass": c, "baggage": 1,
                                 "totalFare": {"totalPrice": p}} for p, c in fares]}]


def test_a_leg_without_the_cabin_says_so_and_names_what_is_sold():
    """NAP-BCN has no premium economy. Relaxing strict_cabin for the whole trip
    would let the transatlantic come back in economy; change that leg instead."""
    pool = _two_leg([(900, ["Y", "Y"]), (1500, ["S", "Y"]), (3000, ["S", "C"])])
    out = fr.build(pool, fr.Options(cabins=["S", "S"]))
    assert out["shortlist"] == []
    leg2 = out["cabins_sold"][1]
    assert leg2["sold"] == ["economy", "business"] and not leg2["requested_is_sold"]
    assert "leg 2" in out["note"] and "strict_cabin" in out["note"]
    fixed = fr.build(pool, fr.Options(cabins=["S", "Y"]))
    assert fixed["shortlist"][0]["price_per_person_usd"] == 1500


def test_domestic_first_on_a_business_leg_counts_as_business_sold():
    two = _two_leg([(5000, ["C", "C"])])
    two[0]["legs"][0]["segmentKeys"] = ["a1", "a2"]
    two[0]["itineraryFares"][0]["cabinClass"] = ["F", "C", "C"]
    assert "business" in fr.build(two, fr.Options(cabins=["C", "C"]))["cabins_sold"][0]["sold"]


def test_the_default_layover_cap_is_michaels_three_hours():
    assert fr.Options(cabins=["Y"]).max_layover_minutes == 180
    out = _build(max_results=50)
    for r in out["shortlist"]:
        for leg in r["legs"]:
            assert all(l["minutes"] <= 180 for l in leg["layovers"]), _flights(r)


def test_premium_economy_counts_with_an_economy_feeder():
    """Delta sells BNA-JFK-NAP Premium Select as Y/S: no PE cabin on the regional jet."""
    out = fr.build(POOL, fr.Options(cabins=["S"], adults=2))
    assert "premium_economy" in out["cabins_sold"][0]["sold"]
    assert out["shortlist"], "real premium economy itineraries exist on this route"
    top = out["shortlist"][0]
    assert "S" in top["cabins"][0]


def test_the_feeder_allowance_is_premium_economy_only():
    """Economy still means economy everywhere: UA966 in PE is not economy."""
    from travel_lookups.fora_flights import leg_cabin_ok
    assert leg_cabin_ok(["Y", "S"], [143, 540], "S")
    assert not leg_cabin_ok(["S", "Y"], [143, 540], "S"), "PE on the short hop only"
    assert not leg_cabin_ok(["Y", "S"], [143, 540], "Y")
    assert not leg_cabin_ok(["Y", "C"], [143, 540], "C"), "business needs every segment"
