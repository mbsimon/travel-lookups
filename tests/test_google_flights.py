"""Reading Google Flights directly: URL encoding and the page readers.

Fixtures are real pages captured 2026-09-28 (tests/fixtures/google_direct/).
No test opens a browser.
"""
import gzip
import json
from pathlib import Path

import pytest

from travel_lookups import google_flights as g

L, S = g.Leg, g.Segment
FIX = Path(__file__).parent / "fixtures" / "google_direct"
BNA_NAP = [L("2027-05-27", "BNA", "NAP", (S("BNA", "2027-05-27", "ATL", "DL", "2294"),
                                          S("ATL", "2027-05-27", "NAP", "DL", "278")))]


def raw(name):
    with gzip.open(FIX / f"{name}.json.gz", "rt") as f:
        return json.load(f)


def by_title(fares):
    return {f["option_title"]: f for f in fares if f.get("option_title")}


# ─── URL encoding ─────────────────────────────────────────────────────────────

def test_encode_carries_legs_segments_passengers_and_trip_type():
    d = g.decode(g.encode(BNA_NAP, adults=2))
    assert d[2] == [2]                      # always 2; 1 returns an empty page
    assert d[8] == [1, 1]                   # one entry per adult
    assert d[19] == [2]                     # one-way
    leg = d[3][0]
    assert leg[13][0][2] == ["BNA"] and leg[14][0][2] == ["NAP"]
    assert [s[6] for s in leg[4]] == [["2294"], ["278"]]


def test_round_trip_is_trip_type_1():
    legs = [L("2026-11-06", "MAD", "LIS"), L("2026-11-08", "LIS", "MAD")]
    assert g.decode(g.encode(legs))[19] == [1]


def test_booking_url_uses_the_booking_path():
    assert "/travel/flights/booking?tfs=" in g.booking_url(BNA_NAP)
    assert "/travel/flights/search?tfs=" in g.search_url([L("2027-05-27", "BNA", "NAP")])


# ─── Search pages ─────────────────────────────────────────────────────────────

def test_one_way_search_reads_every_row():
    r = g.read_search(raw("search_bna_nap_ow"), [L("2027-05-27", "BNA", "NAP")], 1)
    assert len(r["itineraries"]) == 15 and r["unparsed_rows"] == 0
    first = r["itineraries"][0]
    assert first["price_kind"] == "one_way"
    assert first["party_total_usd"] == first["per_person_usd"] == 677


def test_round_trip_search_prices_are_round_trip_totals():
    legs = [L("2026-11-06", "MAD", "LIS"), L("2026-11-08", "LIS", "MAD")]
    r = g.read_search(raw("search_mad_lis_rt"), legs, 1)
    assert len(r["itineraries"]) == 46 and r["unparsed_rows"] == 0
    assert {i["price_kind"] for i in r["itineraries"]} == {"round_trip_total"}


# ─── Fare-brand pages ─────────────────────────────────────────────────────────

def test_delta_ladder_matches_the_serpapi_figures():
    """DL2294+DL278 read the same as SerpApi's booking options on 2026-09-25."""
    fares = by_title(g.read_booking(raw("booking_dl2294_dl278"), BNA_NAP, 1, use_jev=False)["fares"])
    assert fares["Delta Main Basic"]["family"] == "basic"
    assert fares["Delta Main Classic"]["per_person_usd"] == 1166
    assert fares["Delta Main Classic"]["family"] == "standard"
    assert fares["Delta Main Extra"]["per_person_usd"] == 1276
    assert fares["Delta Main Extra"]["family"] == "flexible"
    assert fares["Delta Comfort Classic"]["per_person_usd"] == 1916


def test_two_adults_are_labeled_party_total_and_per_person():
    """The per-person vs party-total confusion caused a real error before."""
    fares = by_title(g.read_booking(raw("booking_dl2294_dl278_2adults"), BNA_NAP, 2, use_jev=False)["fares"])
    classic = fares["Delta Main Classic"]
    assert classic["per_person_usd"] == 1166
    assert classic["party_total_usd"] == 2332


def test_a_page_priced_for_a_different_party_is_refused():
    with pytest.raises(g.DirectError, match="party_mismatch"):
        g.read_booking(raw("booking_dl2294_dl278"), BNA_NAP, 2, use_jev=False)


def test_round_trip_booking_page():
    fares = g.read_booking(raw("booking_dl773_dl915_rt"), [], 1, use_jev=False)["fares"]
    assert [(f["option_title"], f["family"]) for f in fares] == [
        ("Delta Main Basic", "basic"), ("Delta Main Classic", "standard"),
        ("Delta Main Extra", "flexible"), ("Delta Comfort Classic", "standard"),
        ("Delta Comfort Extra", "flexible")]


def test_united_brands_map_to_tiers_without_a_table_entry():
    fares = by_title(g.read_booking(raw("booking_united_sfo_ewr"), [], 1, use_jev=False)["fares"])
    assert fares["Basic Economy"]["family"] == "basic"
    assert fares["Economy"]["family"] == "standard"
    assert fares["Economy Fully Refundable"]["family"] == "flexible"


@pytest.mark.parametrize("label", ["", "Select flight", "Continue"])
def test_parsers_ignore_labels_that_are_not_prices(label):
    assert g.parse_row_label(label) is None
    assert g.parse_fare_label(label) is None


# ─── Codeshares: Fora's marketed numbers vs Google's operating numbers ────────

class _FakeDirect:
    """Google lists AA100 at 18:25 and a later AA flight; nothing else."""
    def search(self, legs, **k):
        return {"itineraries": [
            {"stops": 0, "flights": ["AA100"], "departs": {"time": "18:25"}},
            {"stops": 0, "flights": ["AA106"], "departs": {"time": "20:10"}},
            {"stops": 1, "flights": ["AA1", "AA2"], "departs": {"time": "18:25"}}]}


def test_codeshare_is_found_by_airports_and_departure_minute():
    from travel_lookups import flight_qa
    routing = {"segments": [[("JFK", "LHR", "2026-11-10 18:25")]]}
    assert flight_qa._google_numbers(_FakeDirect(), routing, 1, 0, 1) == ["AA100"]


def test_no_single_match_means_no_substitution():
    from travel_lookups import flight_qa
    routing = {"segments": [[("JFK", "LHR", "2026-11-10 07:00")]]}
    assert flight_qa._google_numbers(_FakeDirect(), routing, 1, 0, 1) is None
