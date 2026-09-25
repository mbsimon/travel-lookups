"""Flight QA against real captures (2026-09-25): Fora BNA-NAP economy at 2
adults and Google Flights for the same search, including Delta's fare ladder
from the booking options on both routings. Fora commission fields scrubbed.
"""
from __future__ import annotations

import copy
import gzip
import json
from datetime import date
from pathlib import Path

import pytest

from travel_lookups import flight_qa as q
from travel_lookups import flights as g

FIX = Path(__file__).parent / "fixtures" / "qa"
TODAY = date(2026, 9, 25)


def load(name):
    return json.loads(gzip.decompress((FIX / f"{name}.gz").read_bytes()))


POOL = load("fora_bna_nap_ow.json")
S1, S2, S3, S9 = (load(f"{n}.json") for n in ("s1_bna_nap_ow", "s2_booking_dl2294",
                                                 "s3_bna_nap_ow_1adult", "s9_booking_dl4664"))


def token_of(resp, flights):
    return next(o for o in q._options(resp)
                if "+".join(f["flight_number"].replace(" ", "") for f in o["flights"]) == flights)


class Fake:
    """A SerpApi stand-in: first search, chained legs by token, booking options."""

    def __init__(self, first, by_token=None, booking=None):
        self.first, self.by_token, self.booking = first, by_token or {}, booking or {}
        self.calls, self.spent = [], 0

    def __call__(self, params):
        self.calls.append(params)
        self.spent += 1
        if "booking_token" in params:
            return copy.deepcopy(self.booking.get(params["booking_token"], {}))
        if "departure_token" in params:
            return copy.deepcopy(self.by_token.get(params["departure_token"], {}))
        return copy.deepcopy(self.first)


def one_way(booking_for=None, **kw):
    b = booking_for or {token_of(S1, "DL2294+DL278")["booking_token"]: S2,
                        token_of(S1, "DL4664+DL232")["booking_token"]: S9}
    return Fake(S1, booking=b)


def run(flights, fetch=None, **kw):
    return q.check(POOL, flights, cabins=["Y"], adults=2, children=0,
                   fetch=fetch or one_way(), today=TODAY, **kw)


# ─── Units (Phase 0) ──────────────────────────────────────────────────────────

def test_google_price_is_the_party_total():
    two = {"+".join(l["flight"].replace(" ", "") for l in i["legs"]): i
           for i in (g._itinerary(o, 2) for o in q._options(S1))}
    one = {"+".join(l["flight"].replace(" ", "") for l in i["legs"]): i
           for i in (g._itinerary(o, 1) for o in q._options(S3))}
    assert two["DL2294+DL278"]["party_total_usd"] == 2092
    assert two["DL2294+DL278"]["per_person_usd"] == one["DL2294+DL278"]["per_person_usd"] == 1046
    for k in set(two) & set(one):     # Google rounds each total to the dollar
        assert abs(two[k]["per_person_usd"] - one[k]["per_person_usd"]) <= 1, k


def test_every_public_fare_carries_both_units():
    for opt in q._g_ladder(S2, 2):
        assert opt["per_person_usd"] * 2 == opt["party_total_usd"]


# ─── Verdicts ─────────────────────────────────────────────────────────────────

def test_atlanta_routing_is_at_parity_brand_for_brand():
    out = run(["DL2294", "DL278"])
    assert out["verdict"] == "MATCH" and out["confidence"] == "high"
    assert out["public"]["compared_fare"]["option_title"] == "Delta Main Classic"
    assert out["delta"]["per_person_usd"] == 0
    rows = {r["brand"]: r["delta_per_person_usd"] for r in out["fare_comparison"]}
    assert rows["DELTA MAIN CLASSIC"] == 0 and rows["DELTA MAIN EXTRA"] == 0
    assert out["client_line"] == ""


def test_basic_is_never_the_comparison():
    """Google's headline for these flights is Main Basic at $1,046/pp."""
    out = run(["DL2294", "DL278"])
    assert out["public"]["headline_party_total_usd"] == 2092
    assert out["public"]["compared_fare"]["family"] != "basic"


def test_jfk_routing_is_above_retail():
    out = run(["DL4664", "DL232"])
    assert out["verdict"] == "FORA_ABOVE_RETAIL" and out["risk"]["above_retail"]
    assert out["delta"]["per_person_usd"] == 537
    assert out["delta"]["party_total_usd"] == 1074
    assert "ABOVE" in out["manager_line"] and "2026-09-26" in out["manager_line"]
    assert out["client_line"] == ""


def _cheaper_public(title="Delta Main Classic", price=2600):
    b = copy.deepcopy(S2)
    for o in b["booking_options"]:
        if (o.get("together") or {}).get("option_title") == title:
            o["together"]["price"] = price
    return b


def test_fora_lower_earns_a_client_line_and_only_when_fresh():
    tok = token_of(S1, "DL2294+DL278")["booking_token"]
    out = run(["DL2294", "DL278"], fetch=Fake(S1, booking={tok: _cheaper_public()}))
    assert out["verdict"] == "FORA_LOWER"
    line = out["client_line"]
    assert "$2,332 for the two of you" in line and "about $268 less" in line
    for bad in ("Google", "commission", "IATA"):
        assert bad not in line
    stale = run(["DL2294", "DL278"], fetch=Fake(S1, booking={tok: _cheaper_public()}),
                pool_age_minutes=90)
    assert stale["verdict"] == "FORA_LOWER" and stale["client_line"] == ""


def test_only_a_basic_public_fare_gets_no_verdict():
    b = copy.deepcopy(S2)
    b["booking_options"] = [o for o in b["booking_options"]
                            if (o.get("together") or {}).get("option_title") == "Delta Main Basic"]
    tok = token_of(S1, "DL2294+DL278")["booking_token"]
    out = run(["DL2294", "DL278"], fetch=Fake(S1, booking={tok: b}))
    assert out["verdict"] == "NO_COMPARABLE_PUBLIC_FARE" and "delta" not in out


def test_a_different_party_is_refused():
    b = copy.deepcopy(S2)
    b["search_parameters"]["adults"] = 1
    tok = token_of(S1, "DL2294+DL278")["booking_token"]
    out = run(["DL2294", "DL278"], fetch=Fake(S1, booking={tok: b}))
    assert out["verdict"] == "NO_DATA" and "party" in out["reason"]


def test_an_expired_fare_is_refused():
    out = q.check(POOL, ["DL2294", "DL278"], cabins=["Y"], adults=2, children=0,
                  fetch=one_way(), today=date(2026, 9, 30))
    assert out["verdict"] == "NO_DATA" and "ticketed by 2026-09-26" in out["reason"]


def test_mixed_cabins_have_no_public_equivalent():
    r = q.fora_routing(POOL, ["DL2294", "DL278"], ["Y"])
    assert "mixed-cabin" in q.plan({**r, "segments": r["segments"] * 2}, ["C", "Y"], 2, 0)["error"]


def test_flights_not_in_the_fora_search():
    out = run(["DL1", "DL2"])
    assert out["verdict"] == "NO_DATA"


def test_one_way_spends_two_searches():
    f = one_way()
    run(["DL2294", "DL278"], fetch=f)
    assert len(f.calls) == 2 and "booking_token" in f.calls[1]


def test_public_alternatives_say_where_they_sit_on_fora():
    """On the JFK routing, the faster ATL connection is on Google and in Fora's pool."""
    out = run(["DL4664", "DL232"])
    alt = {"+".join(a["flights"]): a for a in out["public_alternatives"]}
    assert "DL2294+DL278" in alt
    a = alt["DL2294+DL278"]
    assert a["faster_by_minutes"] > 0 and a["on_fora"].startswith("in Fora's pool, $1,166")
    assert "not compared" in a["price_note"]


# ─── Multi-leg chaining ───────────────────────────────────────────────────────

def test_multi_city_walks_every_leg_before_pricing():
    pool = load("fora_multi.json")
    s6, s7, s8 = load("s6_multi_leg1.json"), load("s7_multi_leg2.json"), load("s8_multi_leg3.json")
    fl = ["AA3487", "AA180", "IB1836", "IB411", "AA8666", "AA4933"]
    first = token_of(s6, "AA3487+AA180")["departure_token"]
    second = token_of(s7, "IB1836+IB411")["departure_token"]
    f = Fake(s6, by_token={first: s7, second: s8})
    out = q.check(pool, fl, cabins=["Y", "Y", "Y"], adults=2, children=0, fetch=f, today=TODAY)
    assert [("departure_token" in c, "booking_token" in c) for c in f.calls] == \
        [(False, False), (True, False), (True, False)]
    # Fable's capture chose DL63+DL2275 on leg 3, so Google's leg-3 list does
    # not carry AA8666+AA4933: the walk must stop there and say which leg.
    assert out["verdict"] == "NO_PUBLIC_MATCH" and "leg 3" in out["reason"]
