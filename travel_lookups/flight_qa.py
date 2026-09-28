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

def _prepare(pool, flights, cabins, adults, children, pool_age_minutes, today):
    """The Fora side of a check: (routing, quoted fare, fora summary, early result)."""
    tickets = adults + children
    routing = fora_routing(pool, flights, cabins)
    if routing is None:
        return None, None, None, _result("NO_DATA",
                                         reason=f"{'+'.join(flights)} is not in this Fora search")
    q = routing["quoted"]
    fora = {"key": routing["key"], "flights": routing["flights"],
            "also_sold_as": routing["also_sold_as"], "tickets": tickets,
            "quoted_fare": q, "ladder": routing["ladder"],
            "pool_age_minutes": round(pool_age_minutes)}
    if q is None:
        return routing, q, fora, _result(
            "NO_DATA", fora=fora,
            reason="no non-basic Fora fare in the requested cabin on these flights")
    if q.get("last_ticket_date") and q["last_ticket_date"] < today.isoformat():
        return routing, q, fora, _result(
            "NO_DATA", fora=fora, reason=f"the Fora fare had to be ticketed by "
                                         f"{q['last_ticket_date']}; search again")
    return routing, q, fora, None


def check(pool: list[dict], flights: list[str], *, cabins: list[str], adults: int,
          children: int, fetch, pool_age_minutes: float = 0, carriers_for_retry=None,
          today: date | None = None, options: "fr.Options | None" = None) -> dict:
    """Compare one Fora routing with Google Flights through SerpApi (the
    fallback source). See the module docstring."""
    today = today or datetime.now(timezone.utc).date()
    tickets = adults + children
    routing, q, fora, early = _prepare(pool, flights, cabins, adults, children,
                                       pool_age_minutes, today)
    if early:
        return _stamp(early, SOURCE_SERPAPI)
    p = plan(routing, cabins, adults, children)
    if p.get("error"):
        return _stamp(_result("NO_DATA", fora=fora, reason=p["error"]), SOURCE_SERPAPI)

    # Walk Google's legs: each step lists one leg's options; only the last
    # carries a booking_token, and only that price describes the trip.
    resp = fetch(p["first"])
    if resp.get("error"):
        return _stamp(_result("NO_DATA", fora=fora, reason=f"Google Flights: {resp['error']}"),
                      SOURCE_SERPAPI)
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
            return _stamp(out, SOURCE_SERPAPI)
        chosen.append(opt)
        if k < len(routing["segments"]) - 1:
            tok = opt.get("departure_token")
            if not tok:
                return _stamp(_result("NO_DATA", fora=fora,
                                      reason="Google gave no token for the next leg"),
                              SOURCE_SERPAPI)
            resp = fetch({**p["first"], "departure_token": tok})
            ages.append(resp.get("_cached_age_minutes", 0))
            if resp.get("error"):
                return _stamp(_result("NO_DATA", fora=fora,
                                      reason=f"Google Flights: {resp['error']}"), SOURCE_SERPAPI)
    last = chosen[-1]
    if not last.get("booking_token"):
        return _stamp(_result("NO_DATA", fora=fora,
                              reason="Google's last leg carried no booking token"), SOURCE_SERPAPI)
    booking = fetch({**p["first"], "booking_token": last["booking_token"]})
    ages.append(booking.get("_cached_age_minutes", 0))
    params = booking.get("search_parameters") or {}
    if params.get("adults") not in (None, adults) or params.get("currency") not in (None, "USD"):
        return _stamp(_result("NO_DATA", fora=fora,
                              reason="Google answered for a different party or currency"),
                      SOURCE_SERPAPI)
    g_ladder = _g_ladder(booking, tickets)
    public = {"source": SOURCE_SERPAPI, "searches_spent": getattr(fetch, "spent", None),
              "age_minutes": max(ages), "tickets": tickets,
              "headline_party_total_usd": last.get("price"),
              "ladder": g_ladder}
    alts = _alternatives(first_list, routing, pool, options, tickets, one_way=p["type"] == 2)
    return _compare(routing, q, fora, public, g_ladder, tickets, alts,
                    pool_age_minutes, max(ages), today)


def _google_numbers(direct, routing, adults, children, cabin) -> list[str] | None:
    """Each Fora flight under the number Google lists it by, or None.

    Searches Google for each flight's own airports and date and takes the one
    nonstop leaving at the same minute. None when any flight has no single
    match, so a near miss is never priced as the same itinerary.
    """
    from . import google_flights as gf
    out = []
    for leg in routing["segments"]:
        for o, d, when in leg:
            when = str(when)
            try:
                res = direct.search([gf.Leg(when[:10], o, d)], adults=adults,
                                    children=children, cabin=cabin)
            except gf.DirectError:
                return None
            numbers = {r["flights"][0] for r in res.get("itineraries") or []
                       if r.get("stops") == 0 and len(r.get("flights") or []) == 1
                       and (r.get("departs") or {}).get("time") == when[11:16]}
            if len(numbers) != 1:
                return None
            out.append(numbers.pop())
    return out


def check_direct(pool: list[dict], flights: list[str], *, cabins: list[str], adults: int,
                 children: int, direct, pool_age_minutes: float = 0,
                 today: date | None = None, options: "fr.Options | None" = None,
                 alternatives: bool = True) -> dict:
    """The same check read straight from Google Flights' own pages (primary).

    `direct` has `booking(legs, adults, children, cabin)` and
    `search(legs, adults, children, cabin)`, each returning the dicts
    `google_flights.booking()` / `.search()` return, plus
    `_cached_age_minutes`. They raise `google_flights.DirectError` when the
    page cannot be read; that propagates, so the caller can fall back to
    SerpApi. Google saying the flights are not sold together is an answer
    (NO_PUBLIC_MATCH), never a fallback.

    Costs no SerpApi search. The flights come from the Fora routing itself, so
    the booking page is asked for exactly those flights and lists every fare
    brand Google sells on them.
    """
    from . import google_flights as gf
    today = today or datetime.now(timezone.utc).date()
    tickets = adults + children
    routing, q, fora, early = _prepare(pool, flights, cabins, adults, children,
                                       pool_age_minutes, today)
    if early:
        return _stamp(early, gf.SOURCE)
    p = plan(routing, cabins, adults, children)
    if p.get("error"):
        return _stamp(_result("NO_DATA", fora=fora, reason=p["error"]), gf.SOURCE)
    cabin = p["first"]["travel_class"]
    legs = gf.segments_from_flights(routing["flights"], routing["segments"])
    search_legs = [gf.Leg(l.date, l.origin, l.destination) for l in legs]

    def alts_now():
        if not alternatives:
            return None
        try:
            found = direct.search(search_legs, adults=adults, children=children, cabin=cabin)
        except gf.DirectError:
            return []
        resp = {"best_flights": [gf.as_serpapi_option(r) for r in found["itineraries"]]}
        return _alternatives(resp, routing, pool, options, tickets, one_way=p["type"] == 2)

    got, on_google = None, routing["flights"]
    try:
        got = direct.booking(legs, adults=adults, children=children, cabin=cabin)
    except gf.ItineraryUnavailable:
        # Codeshares: Fora quotes the marketed number (IB4218) and Google lists
        # the flight under the airline that flies it (AA100). Find each flight
        # on Google by its airports and departure minute, then ask again.
        found = _google_numbers(direct, routing, adults, children, cabin)
        if found and found != routing["flights"]:
            try:
                got = direct.booking(gf.segments_from_flights(found, routing["segments"]),
                                     adults=adults, children=children, cabin=cabin)
                on_google = found
            except gf.ItineraryUnavailable:
                pass
    if got is None:
        out = _result("NO_PUBLIC_MATCH", fora=fora,
                      reason=f"Google Flights does not sell {'+'.join(routing['flights'])} "
                             "together on this date (its booking page says the itinerary "
                             "is not available)")
        a = alts_now()
        if a is not None:
            out["public_alternatives"] = a
        return _stamp(out, gf.SOURCE)
    g_ladder = got["fares"]
    age = got.get("_cached_age_minutes", 0)
    public = {"source": gf.SOURCE, "url": got.get("url"), "fetched_at": got.get("fetched_at"),
              "flights_on_google": on_google,
              "searches_spent": 0, "age_minutes": age, "tickets": tickets,
              "headline_party_total_usd": min(g["party_total_usd"] for g in g_ladder),
              "ladder": g_ladder}
    return _compare(routing, q, fora, public, g_ladder, tickets, alts_now(),
                    pool_age_minutes, age, today)


def _compare(routing, q, fora, public, g_ladder, tickets, alts, pool_age_minutes,
             public_age, today) -> dict:
    """The comparison: same brand first, then same family by terms, never basic."""
    exact = [g for g in g_ladder if len(q["brands"]) == 1
             and same_brand(q["brands"][0], g.get("option_title"), g.get("seller"))]
    by_terms = [g for g in g_ladder if g["family"] == q["family"]]
    _annotate_ladder(fora, g_ladder, public)
    if exact:
        comp, how = min(exact, key=lambda g: g["per_person_usd"]), "same fare brand"
    elif by_terms:
        comp, how = min(by_terms, key=lambda g: g["per_person_usd"]), "same fare terms"
    else:
        known = [g for g in g_ladder if g["family"] != "unknown"]
        only_basic = bool(known) and all(g["family"] == "basic" for g in known)
        out = _result("NO_COMPARABLE_PUBLIC_FARE" if only_basic else "TERMS_UNKNOWN",
                      fora=fora, public=public,
                      reason=("Google shows only a basic fare on these flights, which is "
                              "never compared" if only_basic else
                              "Google did not name the fare, so no like-for-like price"))
        out["fare_comparison"] = _ladder_rows(routing["ladder"], g_ladder)
        if alts is not None:
            out["public_alternatives"] = alts
        return _stamp(out, public["source"])
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
    if alts is not None:
        out["public_alternatives"] = alts
    out["manager_line"] = _manager_line(out, q, comp, how, tickets)
    out["client_line"] = _client_line(out, q, tickets, pool_age_minutes, public_age, today)
    return _stamp(out, public["source"])


def _result(verdict: str, **kw) -> dict:
    return {"verdict": verdict, "checked_at": datetime.now(timezone.utc).isoformat(
        timespec="seconds"), **kw, "client_line": kw.get("client_line", "")}


def same_brand(fora_brand, g_title, g_seller=None) -> bool:
    """"DELTA MAIN CLASSIC" is Google's "Delta Main Classic", and Virgin's
    "Economy Classic" matches a Fora "VIRGIN ATLANTIC ECONOMY CLASSIC": the
    seller's name may lead either side."""
    a, b, s = _norm_brand(fora_brand), _norm_brand(g_title), _norm_brand(g_seller)
    if not a or not b:
        return False
    return a == b or (bool(s) and (a == s + b or s + a == b))


def _ladder_rows(fora_ladder: list[dict], g_ladder: list[dict]) -> list[dict]:
    """Every Fora brand with a same-named public brand, side by side."""
    rows = []
    for f in fora_ladder:
        if len(f["brands"]) != 1 or f["family"] == "basic":
            continue
        g = [x for x in g_ladder if same_brand(f["brands"][0], x["option_title"], x.get("seller"))]
        if g:
            gp = min(x["per_person_usd"] for x in g)
            rows.append({"brand": f["brands"][0], "fora_per_person_usd": f["per_person_usd"],
                         "public_per_person_usd": gp,
                         "delta_per_person_usd": round(f["per_person_usd"] - gp, 2)})
    return rows


# ─── Every figure carries its check ──────────────────────────────────────────
# Michael, 2026-09-28: no flight figure is cited without a check against the
# published Google Flights fare for the same itinerary and brand. Every priced
# row a tool returns carries `google_check` (the Google figure, its brand, the
# source and when it was read) or `google_check.not_checked` with the reason.

SOURCE_SERPAPI = "serpapi"
PRICE_KEYS = frozenset({"price_per_person_usd", "price_total_usd", "per_person_usd",
                        "party_total_usd", "cheapest_per_person_usd", "total_per_person_usd",
                        "lowest_public_per_person_usd", "best_effective_per_person_usd"})
CHECK_KEYS = ("google_check", "retail")
# Subtrees that ARE the comparison (Google's own ladder, the delta) or its
# inputs, so they are not rows that need a check of their own.
_CHECK_SUBTREES = frozenset({"google_check", "retail", "public", "delta", "fare_comparison",
                             "compared_fare", "retail_summary"})


def not_checked(reason: str) -> dict:
    return {"verdict": "NOT_CHECKED", "not_checked": reason}


def check_record(res: dict) -> dict:
    """The compact Google comparison a priced row carries, from a check result."""
    pub = res.get("public") or {}
    comp = pub.get("compared_fare") or {}
    d = res.get("delta") or {}
    rec = {"verdict": res.get("verdict"), "source": res.get("source") or pub.get("source"),
           "checked_at": pub.get("fetched_at") or res.get("checked_at"),
           "public_fare": comp.get("option_title") or (comp.get("family") if comp else None),
           "public_per_person_usd": comp.get("per_person_usd"),
           "public_party_total_usd": comp.get("party_total_usd"),
           "delta_per_person_usd": d.get("per_person_usd"),
           "delta_party_total_usd": d.get("party_total_usd"),
           "confidence": res.get("confidence")}
    if not comp:
        rec["not_checked"] = res.get("reason") or f"no like-for-like public fare ({res.get('verdict')})"
    return {k: v for k, v in rec.items() if v is not None}


def _stamp(out: dict, source: str) -> dict:
    """Name the source, and give every priced row inside the result its check."""
    out["source"] = source
    pub = out.get("public") or {}
    fora = out.get("fora") or {}
    if fora.get("ladder") and not all("google_check" in r for r in fora["ladder"]):
        _annotate_ladder(fora, pub.get("ladder") or [], pub)
    q = fora.get("quoted_fare")
    if q is not None:                 # the quoted rung carries the verdict itself
        q["google_check"] = check_record(out)
    for alt in out.get("public_alternatives") or []:
        if "lowest_public_per_person_usd" in alt:
            alt["google_check"] = not_checked(
                "Google's headline price for other flights, often basic; its fare brand "
                "was not read, so it is shown for context and never compared")
    return out


def _annotate_ladder(fora: dict, g_ladder: list[dict], public: dict) -> None:
    """Each Fora fare brand next to the same brand on Google, or why not."""
    for f in fora.get("ladder") or []:
        if not g_ladder:
            f["google_check"] = not_checked("Google's fares for these flights were not read")
            continue
        if f["family"] == "basic":
            f["google_check"] = not_checked("basic fare; never quoted or compared")
            continue
        g = [x for x in g_ladder if len(f["brands"]) == 1
             and same_brand(f["brands"][0], x.get("option_title"), x.get("seller"))]
        if not g:
            f["google_check"] = not_checked(
                f"Google lists no fare named {' / '.join(f['brands']) or 'this'} on these "
                "flights")
            continue
        best = min(g, key=lambda x: x["per_person_usd"])
        f["google_check"] = {"verdict": "SAME_BRAND", "source": public.get("source"),
                             "checked_at": public.get("fetched_at"),
                             "public_fare": best["option_title"],
                             "public_per_person_usd": best["per_person_usd"],
                             "public_party_total_usd": best["party_total_usd"],
                             "delta_per_person_usd": round(f["per_person_usd"]
                                                           - best["per_person_usd"], 2)}


def unchecked_prices(obj, path: str = "") -> list[str]:
    """Paths of priced rows that carry neither a Google check nor a reason.

    A priced row is any dict with a key in PRICE_KEYS. Its check lives in
    `google_check` (or `retail`, the name fora_flight_search rows use) and
    must either name the Google figure's source and time or say `not_checked`.
    """
    bad = []
    if isinstance(obj, dict):
        if PRICE_KEYS & obj.keys():
            rec = next((obj[k] for k in CHECK_KEYS if isinstance(obj.get(k), dict)), None)
            ok = rec is not None and (bool(rec.get("not_checked")) or
                                      (rec.get("source") and rec.get("checked_at")))
            if not ok:
                bad.append(path or "<root>")
        for k, v in obj.items():
            if k not in _CHECK_SUBTREES:
                bad += unchecked_prices(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad += unchecked_prices(v, f"{path}[{i}]")
    return bad


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
