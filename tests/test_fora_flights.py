"""fora_flights — the React Flight ref resolver and the leg/cabin plumbing.

The one thing that would show up as real damage: a `$N:path` back-reference
left unresolved crashes every caller downstream with
`AttributeError: 'str' object has no attribute 'get'` on a live search — this
already happened once building the module (see its docstring). No network;
these use captured/synthetic RSC-shaped fixtures.
"""
from datetime import date

from travel_lookups import fora_flights as ff


def test_resolve_refs_inlines_self_reference():
    # Shape seen live: itineraryFares[0].cabinClass is a back-ref into the
    # itinerary's own cabinCodes list.
    chunks = {
        "1": {
            "ok": True,
            "data": {
                "itineraries": [
                    {"cabinCodes": ["C", "Y"],
                     "itineraryFares": [{"cabinClass": "$1:data:itineraries:0:cabinCodes"}]}
                ]
            },
        }
    }
    resolved = ff._resolve_refs(chunks["1"], chunks)
    fares = resolved["data"]["itineraries"][0]["itineraryFares"]
    assert fares[0]["cabinClass"] == ["C", "Y"]


def test_resolve_refs_leaves_non_ref_strings_alone():
    chunks = {"1": {"ok": True, "data": {"airlineCodes": ["AA"], "alliance": "oneworld"}}}
    resolved = ff._resolve_refs(chunks["1"], chunks)
    assert resolved["data"]["alliance"] == "oneworld"


def test_parse_rsc_extracts_chunk_one():
    text = '0:{"a":"$@1"}\n1:{"ok":true,"data":{"itineraries":[{"minFareAmount":586.53}]}}\n'
    data = ff._parse_rsc(text)
    assert data["itineraries"][0]["minFareAmount"] == 586.53


def test_parse_rsc_raises_on_missing_chunk_one():
    # The shape a stale Next-Action hash produces: a 404 body with no numbered lines.
    import pytest
    with pytest.raises(ff.FlightSearchError):
        ff._parse_rsc("<html>not found</html>")


def test_leg_cabin_code_mapping():
    assert ff.Leg("MAD", "JFK", date(2026, 10, 15), "business").code == "C"
    assert ff.Leg("MAD", "JFK", date(2026, 10, 15), "economy").code == "Y"
    assert ff.Leg("MAD", "JFK", date(2026, 10, 15), "C").code == "C"


def test_leg_path_and_body():
    leg = ff.Leg("MAD", "JFK", date(2026, 10, 15), "business")
    assert leg._path() == "MAD-JFK/2026-10-15+C"
    assert leg._body() == {
        "origin": "MAD", "destination": "JFK",
        "departs": "2026-10-15", "timeMode": "departs", "cabin": "C",
    }


def test_search_legs_rejects_empty():
    import pytest
    with pytest.raises(ff.FlightSearchError):
        ff.search_legs([])


def test_summarize_reads_per_leg_cabin_and_commission():
    itinerary = {
        "minFareAmount": 1726.63,
        "marketingAirlineCodes": ["UX"],
        "alliance": None,
        "totalStops": 0,
        "elapsedTime": 900,
        "key": "abc",
        "legs": [
            {"origin": "MAD", "destination": "JFK", "departsAt": "2026-10-15T12:00:00+02:00",
             "arrivesAt": "2026-10-15T14:00:00-04:00", "marketingAirline": "UX",
             "equipmentCodes": ["772"], "redeye": False, "stopLocation": []},
            {"origin": "JFK", "destination": "MAD", "departsAt": "2026-10-22T16:00:00-04:00",
             "arrivesAt": "2026-10-23T05:00:00+02:00", "marketingAirline": "UX",
             "equipmentCodes": ["772"], "redeye": True, "stopLocation": []},
        ],
        "itineraryFares": [{"cabinClass": ["C", "Y"], "commission": {"amount": 98.65}}],
    }
    s = ff.summarize(itinerary)
    assert [leg["cabin"] for leg in s["legs"]] == ["C", "Y"]
    assert s["commission_usd"] == 98.65
