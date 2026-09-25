"""QA a Fora flight quote against what the client will see on Google Flights.

Before a fare goes to a client: is the same itinerary priced the same
publicly, is Fora above retail, and does the public list hold something
better that Fora's shortlist lacks?

The rule that governs every number here (Michael, 2026-09-25): compare
apples to apples. Fora never quotes basic economy, so its fare is compared
with the SAME brand on Google (Delta Main Classic to Delta Main Classic), read
from Google's booking options, where each brand has its own price. Google's
headline price is often basic, and its `exclude_basic` filter did not remove
Delta's Main Basic on an international route. A comparison against a basic
fare is never a verdict.

Brand for brand on BNA-NAP 2027-05-27 the two sources matched to the dollar
on DL2294+DL278 (Main Classic $1,166/pp, Main Extra $1,276, Comfort $1,916),
and on DL4664+DL232 Fora's Main Classic was $1,391/pp against $854 public:
a real above-retail case.

Units: SerpApi's `price` is the PARTY total. Every money field below says
`per_person_usd` or `party_total_usd`; there is no bare price.

Pure logic. Network access comes in through `fetch(params) -> dict`
(`public_fares.Fetcher` live, a fixture reader in tests).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

from . import fora_rank as fr
from .fora_flights import _is_basic_economy, flight_numbers, normalize_flight

MATCH_PCT = 0.02            # within 2% is the same price (both sides move by the minute)
ABOVE_RETAIL_MIN_PP = 25.0  # and a gap must be worth a client's attention
MAX_FRESH_MINUTES = 60      # a client line needs both prices fresher than this
TRAVEL_CLASS = {"Y": 1, "S": 2, "W": 2, "C": 3, "J": 3, "F": 4, "P": 4}
_BASIC_WORDS = ("basic", "light", "lite", "saver")
_NUMBERS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
            7: "seven", 8: "eight", 9: "nine"}


# ─── The Fora side ────────────────────────────────────────────────────────────

def _local(ts) -> str:
    """"2027-05-27T12:15:00-05:00" -> "2027-05-27 12:15" (Google's format)."""
    s = str(ts or "")
    return f"{s[:10]} {s[11:16]}" if len(s) >= 16 else s


def _segments(it: dict) -> list[list[tuple]]:
    out = []
    for leg in it.get("legs") or []:
        out.append([((t.get("locations") or [""])[0], (t.get("locations") or [""])[-1],
                     _local(t.get("startsAt")))
                    for t in leg.get("timeline") or [] if t.get("type") == "air"])
    return out


def _norm_brand(b) -> str:
    return re.sub(r"[^a-z0-9]", "", str(b or "").lower())


def _family(fa: dict) -> str:
    if _is_basic_economy(fa):
        return "basic"
    return "flexible" if fa.get("refundableBefore") else "standard"


def fora_routing(pool: list[dict], flights: list[str], cabins: list[str]) -> dict | None:
    """The routing in the Fora pool with these flights, its codeshare twins,
    and the whole fare ladder across them (each brand at its cheapest)."""
    want = [normalize_flight(f) for f in flights]
    rep = next((it for it in pool if [normalize_flight(x) for x in flight_numbers(it)] == want),
               None)
    if rep is None:
        return None
    sig = _segments(rep)
    twins = [it for it in pool if _segments(it) == sig]
    ladder = {}
    for it in twins:
        for fa in it.get("itineraryFares") or []:
            brands = [b.get("brandName") for b in fa.get("branding") or [] if b.get("brandName")]
            key = tuple(dict.fromkeys(brands)) or ("",)
            price = fa.get("rawFare")
            if price is None:
                continue
            if key not in ladder or price < ladder[key]["per_person_usd"]:
                ladder[key] = {"brands": list(key), "per_person_usd": price,
                               "party_total_usd": (fa.get("totalFare") or {}).get("totalPrice"),
                               "family": _family(fa), "checked_bags": fa.get("baggage"),
                               "cabins": fa.get("cabinClass"),
                               "last_ticket_date": fa.get("lastTicketDate"),
                               "matches_cabin": fr._cabin_ok(fr._fare(fa, fr._legs(it)), cabins),
                               "flights": flight_numbers(it), **fr._terms(fa)}
    fares = sorted(ladder.values(), key=lambda f: f["per_person_usd"])
    quoted = next((f for f in fares if f["matches_cabin"] and f["family"] != "basic"), None)
    return {"key": rep.get("key"), "flights": flight_numbers(rep),
            "also_sold_as": sorted({"+".join(flight_numbers(t)) for t in twins} -
                                   {"+".join(flight_numbers(rep))}),
            "segments": sig, "legs": fr._legs(rep), "ladder": fares, "quoted": quoted,
            "elapsed": sum(int(l.get("elapsedTime") or 0) for l in rep.get("legs") or []),
            "stops": sum(len(l.get("stopLocation") or []) for l in rep.get("legs") or [])}


# ─── The Google side ──────────────────────────────────────────────────────────

def _g_segments(option: dict) -> list[tuple]:
    return [((f.get("departure_airport") or {}).get("id"),
             (f.get("arrival_airport") or {}).get("id"),
             (f.get("departure_airport") or {}).get("time"))
            for f in option.get("flights") or []]


def _options(resp: dict) -> list[dict]:
    return (resp.get("best_flights") or []) + (resp.get("other_flights") or [])


def _find(resp: dict, segments: list[tuple]) -> dict | None:
    return next((o for o in _options(resp) if _g_segments(o) == segments), None)


def _g_family(t: dict) -> str:
    title = str(t.get("option_title") or "").lower()
    ext = " ".join(t.get("extensions") or []).lower()
    bags = " ".join(t.get("baggage_prices") or []).lower()
    if any(w in title for w in _BASIC_WORDS):
        return "basic"
    if "no ticket changes" in ext and "checked bag free" not in bags:
        return "basic"
    if "full refund" in ext:
        return "flexible"
    if title or "checked bag free" in bags or "free change" in ext:
        return "standard"
    return "unknown"


def _g_ladder(booking: dict, tickets: int) -> list[dict]:
    out = []
    for opt in booking.get("booking_options") or []:
        t = opt.get("together") or {}
        if t.get("price") is None:
            continue
        out.append({"seller": t.get("book_with"), "airline_direct": bool(t.get("airline")),
                    "option_title": t.get("option_title"), "family": _g_family(t),
                    "party_total_usd": t.get("price"),
                    "per_person_usd": round(t["price"] / tickets, 2),
                    "terms": t.get("extensions") or [], "baggage": t.get("baggage_prices") or []})
    return out


# ─── Planning the Google search ──────────────────────────────────────────────

def plan(routing: dict, cabins: list[str], adults: int, children: int) -> dict:
    """The SerpApi requests that list this exact itinerary, leg by leg."""
    classes = {TRAVEL_CLASS.get(c) for c in cabins}
    if len(classes) != 1 or None in classes:
        return {"error": "Google Flights prices one cabin per search, so a mixed-cabin "
                         "ticket has no public equivalent to compare with"}
    legs = routing["segments"]
    ends = [(l[0][0], l[-1][1], l[0][2][:10]) for l in legs]
    stops = min(max(len(l) - 1 for l in legs) + 1, 3)
    base = {"travel_class": classes.pop(), "adults": adults, "children": children,
            "stops": stops}
    if len(legs) == 1:
        return {"type": 2, "steps": 1, "first": {**base, "type": 2, "departure_id": ends[0][0],
                                                  "arrival_id": ends[0][1],
                                                  "outbound_date": ends[0][2]}}
    if len(legs) == 2 and ends[0][0] == ends[1][1] and ends[0][1] == ends[1][0]:
        return {"type": 1, "steps": 2, "first": {**base, "type": 1, "departure_id": ends[0][0],
                                                  "arrival_id": ends[0][1],
                                                  "outbound_date": ends[0][2],
                                                  "return_date": ends[1][2]}}
    mc = [{"departure_id": o, "arrival_id": d, "date": dt} for o, d, dt in ends]
    return {"type": 3, "steps": len(legs),
            "first": {**base, "type": 3, "multi_city_json": json.dumps(mc)}}


# ─── The check ───────────────────────────────────────────────────────────────

def check(pool: list[dict], flights: list[str], *, cabins: list[str], adults: int,
          children: int, fetch, pool_age_minutes: float = 0, carriers_for_retry=None,
          today: date | None = None, options: "fr.Options | None" = None) -> dict:
    """Compare one Fora routing with Google Flights. See the module docstring."""
    today = today or datetime.now(timezone.utc).date()
    tickets = adults + children
    routing = fora_routing(pool, flights, cabins)
    if routing is None:
        return _result("NO_DATA", reason=f"{'+'.join(flights)} is not in this Fora search")
    q = routing["quoted"]
    fora = {"key": routing["key"], "flights": routing["flights"],
            "also_sold_as": routing["also_sold_as"], "tickets": tickets,
            "quoted_fare": q, "ladder": routing["ladder"],
            "pool_age_minutes": round(pool_age_minutes)}
    if q is None:
        return _result("NO_DATA", fora=fora,
                       reason="no non-basic Fora fare in the requested cabin on these flights")
    if q.get("last_ticket_date") and q["last_ticket_date"] < today.isoformat():
        return _result("NO_DATA", fora=fora,
                       reason=f"the Fora fare had to be ticketed by {q['last_ticket_date']}; "
                              "search again")
    p = plan(routing, cabins, adults, children)
    if p.get("error"):
        return _result("NO_DATA", fora=fora, reason=p["error"])

    # Walk Google's legs: each step lists one leg's options; only the last
    # carries a booking_token, and only that price describes the trip.
    resp = fetch(p["first"])
    if resp.get("error"):
        return _result("NO_DATA", fora=fora, reason=f"Google Flights: {resp['error']}")
    first_list = resp
    ages = [resp.get("_cached_age_minutes", 0)]
    chosen = []
    for k, seg in enumerate(routing["segments"]):
        opt = _find(resp, seg)
        if opt is None and k == 0 and carriers_for_retry:
            resp = fetch({**p["first"], "include_airlines": ",".join(carriers_for_retry)})
            ages.append(resp.get("_cached_age_minutes", 0))
            opt = _find(resp, seg)
        if opt is None:
            out = _result("NO_PUBLIC_MATCH", fora=fora,
                          reason=f"Google Flights did not list leg {k + 1} "
                                 f"({'+'.join(routing['flights'])}) on this search")
            out["public_alternatives"] = _alternatives(first_list, routing, pool, options,
                                                       tickets, one_way=p["type"] == 2)
            return out
        chosen.append(opt)
        if k < len(routing["segments"]) - 1:
            tok = opt.get("departure_token")
            if not tok:
                return _result("NO_DATA", fora=fora, reason="Google gave no token for the next leg")
            resp = fetch({**p["first"], "departure_token": tok})
            ages.append(resp.get("_cached_age_minutes", 0))
            if resp.get("error"):
                return _result("NO_DATA", fora=fora, reason=f"Google Flights: {resp['error']}")
    last = chosen[-1]
    if not last.get("booking_token"):
        return _result("NO_DATA", fora=fora, reason="Google's last leg carried no booking token")
    booking = fetch({**p["first"], "booking_token": last["booking_token"]})
    ages.append(booking.get("_cached_age_minutes", 0))
    params = booking.get("search_parameters") or {}
    if params.get("adults") not in (None, adults) or params.get("currency") not in (None, "USD"):
        return _result("NO_DATA", fora=fora,
                       reason="Google answered for a different party or currency")
    g_ladder = _g_ladder(booking, tickets)
    public = {"source": "google_flights", "searches_spent": getattr(fetch, "spent", None),
              "age_minutes": max(ages), "tickets": tickets,
              "headline_party_total_usd": last.get("price"),
              "ladder": g_ladder}

    # The comparison: same brand first, then same family by terms, never basic.
    want = _norm_brand(q["brands"][0]) if len(q["brands"]) == 1 else None
    exact = [g for g in g_ladder if want and _norm_brand(g["option_title"]) == want]
    by_terms = [g for g in g_ladder if g["family"] == q["family"]]
    if exact:
        comp, how = min(exact, key=lambda g: g["per_person_usd"]), "same fare brand"
    elif by_terms:
        comp, how = min(by_terms, key=lambda g: g["per_person_usd"]), "same fare terms"
    else:
        only_basic = g_ladder and all(g["family"] == "basic" for g in g_ladder)
        out = _result("NO_COMPARABLE_PUBLIC_FARE" if only_basic else "TERMS_UNKNOWN",
                      fora=fora, public=public,
                      reason=("Google shows only a basic fare on these flights, which is "
                              "never compared" if only_basic else
                              "Google did not name the fare, so no like-for-like price"))
        out["fare_comparison"] = _ladder_rows(routing["ladder"], g_ladder)
        return out
    public["compared_fare"] = comp
    public["matched_by"] = how
    delta = round(q["per_person_usd"] - comp["per_person_usd"], 2)
    pct = delta / comp["per_person_usd"] if comp["per_person_usd"] else 0.0
    if abs(pct) <= MATCH_PCT or (0 < delta <= ABOVE_RETAIL_MIN_PP):
        verdict = "MATCH"
    elif delta < 0:
        verdict = "FORA_LOWER"
    else:
        verdict = "FORA_ABOVE_RETAIL"
    out = _result(verdict, fora=fora, public=public)
    out["delta"] = {"per_person_usd": delta, "party_total_usd": round(delta * tickets, 2),
                    "pct": round(pct, 4)}
    out["confidence"] = "high" if how == "same fare brand" else "medium"
    out["risk"] = {"above_retail": verdict == "FORA_ABOVE_RETAIL"}
    out["fare_comparison"] = _ladder_rows(routing["ladder"], g_ladder)
    out["public_alternatives"] = _alternatives(first_list, routing, pool, options, tickets,
                                               one_way=p["type"] == 2)
    out["manager_line"] = _manager_line(out, q, comp, how, tickets)
    out["client_line"] = _client_line(out, q, tickets, pool_age_minutes, max(ages), today)
    return out


def _result(verdict: str, **kw) -> dict:
    return {"verdict": verdict, "checked_at": datetime.now(timezone.utc).isoformat(
        timespec="seconds"), **kw, "client_line": kw.get("client_line", "")}


def _ladder_rows(fora_ladder: list[dict], g_ladder: list[dict]) -> list[dict]:
    """Every Fora brand with a same-named public brand, side by side."""
    rows = []
    for f in fora_ladder:
        if len(f["brands"]) != 1 or f["family"] == "basic":
            continue
        g = [x for x in g_ladder if _norm_brand(x["option_title"]) == _norm_brand(f["brands"][0])]
        if g:
            gp = min(x["per_person_usd"] for x in g)
            rows.append({"brand": f["brands"][0], "fora_per_person_usd": f["per_person_usd"],
                         "public_per_person_usd": gp,
                         "delta_per_person_usd": round(f["per_person_usd"] - gp, 2)})
    return rows


def _money(x: float) -> str:
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def _manager_line(out: dict, q: dict, comp: dict, how: str, tickets: int) -> str:
    d = out["delta"]["per_person_usd"]
    head = (f"Same flights, {how} ({comp['option_title'] or comp['family']}): "
            f"Fora {_money(q['per_person_usd'])}/pp, public {_money(comp['per_person_usd'])}/pp "
            f"({comp['seller'] or 'seller not named'}).")
    tail = {"MATCH": " At parity.",
            "FORA_LOWER": f" Fora is {_money(-d)}/pp under public "
                          f"({_money(-d * tickets)} for the party).",
            "FORA_ABOVE_RETAIL": f" Fora is {_money(d)}/pp ABOVE public "
                                 f"({out['delta']['pct']:.0%}); check another fare or routing "
                                 "before quoting."}[out["verdict"]]
    exp = f" Fora fare must be ticketed by {q['last_ticket_date']}." if q.get(
        "last_ticket_date") else ""
    return head + tail + exp


def _client_line(out, q, tickets, pool_age, public_age, today) -> str:
    """Only when Fora is verifiably lower on the same brand, both prices fresh,
    and the fare not expired. Never names Google, commission or the IATA."""
    if out["verdict"] != "FORA_LOWER" or out.get("confidence") != "high":
        return ""
    if pool_age > MAX_FRESH_MINUTES or public_age > MAX_FRESH_MINUTES:
        return ""
    if q.get("last_ticket_date") and q["last_ticket_date"] < today.isoformat():
        return ""
    who = _NUMBERS.get(tickets, str(tickets))
    party = "you" if tickets == 1 else f"the {who} of you"
    line = (f"Booking these flights with me comes to {_money(q['party_total_usd'])} for {party}, "
            f"about {_money(-out['delta']['party_total_usd'])} less than the lowest price the "
            "airline is showing publicly for the same flights and fare.")
    for bad in ("commission", "iata", "33520476", "google"):
        assert bad not in line.lower()
    return line


# ─── Public options Fora's shortlist lacks ──────────────────────────────────

def _alternatives(resp, routing, pool, options, tickets, one_way: bool) -> list[dict]:
    """Google itineraries that meet Michael's rules and are faster or have
    fewer stops than the quoted routing, with where each sits on Fora.

    Their public price is the lowest fare Google shows, which is often basic,
    so it is labeled as such and never compared with Fora's fare. On a round
    trip or multi-city the first list's prices are running totals for a
    completion Google chose, so they are omitted.
    """
    o = options or fr.Options(cabins=["Y"] * len(routing["segments"]))
    # Fora itineraries by their first leg's flights (airports and local times).
    fora_by_sig = {}
    for it in pool:
        sig = _segments(it)
        if sig and sig[0]:
            fora_by_sig.setdefault(tuple(sig[0]), it)
    out = []
    for g in _options(resp):
        segs = _g_segments(g)
        if len(routing["segments"]) == 1 and segs == routing["segments"][0]:
            continue
        lays = g.get("layovers") or []
        change = any(a[1] != b[0] for a, b in zip(segs, segs[1:]))
        mins = [int(l.get("duration") or 0) for l in lays]
        if change or any(l.get("overnight") for l in lays):
            continue
        if o.max_layover_minutes is not None and any(m > o.max_layover_minutes for m in mins):
            continue
        if o.min_layover_minutes is not None and any(m < o.min_layover_minutes for m in mins):
            continue
        stops = len(segs) - 1
        leg0 = routing["legs"][0]
        faster = (g.get("total_duration") or 10**6) < leg0["elapsed"] - 15
        fewer = stops < leg0["stops"]
        if not (faster or fewer):
            continue
        fl = [normalize_flight(f.get("flight_number", "").replace(" ", "")) for f in
              g.get("flights") or [] if f.get("flight_number")]
        match = fora_by_sig.get(tuple(segs))
        if match is None:
            on_fora = "not in Fora's results for this search"
        else:
            r = fr._read(match, o)
            on_fora = ("excluded on Fora: " + r["correct"][0][1] if r["correct"] else
                       "excluded on Fora: " + r["comfort"][0][1] if r["comfort"] else
                       "in Fora's pool, " + _money(r["fare"]["price_per_person_usd"]) + "/pp")
        item = {"flights": fl, "elapsed_minutes": g.get("total_duration"), "stops": stops,
                "faster_by_minutes": max(0, leg0["elapsed"] - (g.get("total_duration") or 0)),
                "fewer_stops": fewer, "on_fora": on_fora}
        if one_way and g.get("price") is not None:
            item["lowest_public_per_person_usd"] = round(g["price"] / tickets, 2)
            item["price_note"] = "lowest fare Google shows, often basic; not compared"
        out.append(item)
    return sorted(out, key=lambda x: (x["stops"], x["elapsed_minutes"] or 0))[:5]
