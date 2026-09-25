"""Turn a raw Fora fare pool into the shortlist an advisor would write.

A Fora search for BNA-NAP on 2027-05-27 returns about 405 fares. They are
about 296 physical routings, because codeshares repeat the same flights under
Delta, KLM and Air France numbers, and the cheapest of them are 31-hour Aer
Lingus trips with 13-hour sits in Dublin. Sorted by price and cut to 15, that
list shows an agent about four real options and hides the connection Michael
would pick (DL2294+DL278 via Atlanta, 56 minutes, 11h50) around rank 150.

This module does what the pool cannot:

- Cabin belongs to each fare. One Delta routing sells Main, Comfort, Premium
  Select and Delta One. A routing qualifies for an economy search only through
  a fare that is economy on every segment. UA2848+UA966 passed the old check
  (first segment only) at $1,923 as "economy"; every fare Fora sells on it is
  Premium Economy or business across the Atlantic.
- Codeshare twins collapse into one physical routing (same airports, same
  departure times), listed under `also_sold_as`.
- Routings are ranked on effective price: the fare plus H dollars per hour of
  inconvenience, where H is 8% of the pool's 25th-percentile fare (floor $40),
  so one rule serves a $700 economy fare and a $7,000 business fare.
- Uncomfortable routings (airport changes, 5h+ sits, overnight layovers,
  arriving two days later, three stops) are excluded by default, and every
  exclusion is counted with the parameter that lifts it, so an agent can
  never conclude Fora doesn't sell something it merely filtered.
- Feeders into the same final flight fold into one row, and four anchors
  (cheapest, fastest, cheapest with fewest stops, cheapest refundable) always
  report where they landed.

Design reviewed by Fable against the live pool, 2026-09-24.
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .fora_flights import (CABIN_ALLOWED, FlightSearchError, flight_numbers,
                           has_flights, leg_cabin_ok, normalize_flight,
                           _is_basic_economy)

H_SHARE_OF_P25 = 0.08
H_FLOOR_USD = 40.0

PENALTY_PER_EXTRA_HOUR = 1.0
PENALTY_PER_EXTRA_STOP = 4.0
TIGHT_CONNECTION_MIN = 60
PENALTY_TIGHT = 0.5
LONG_SIT_MIN = 180            # past this, each hour sitting costs an hour more
EARLY_DEPARTURE = (6, 30)
PENALTY_EARLY = 1.0
LATE_ARRIVAL_FROM, LATE_ARRIVAL_UNTIL = 21, 5
PENALTY_LATE = 2.0
OVERNIGHT_LAYOVER_MIN = 240
# Michael's rule: no layover over three hours unless he asks for one.
MAX_LAYOVER_MINUTES = 180
CABIN_NAMES = {"Y": "economy", "S": "premium_economy", "W": "premium_economy",
               "C": "business", "J": "business", "F": "first", "P": "first"}
BY_ROUTING_MAX = 15

# Exclusions an agent can lift, and how.
LIFT = {
    # Change the cabin on the leg that lacks it (see `cabins_sold`).
    # strict_cabin=false relaxes EVERY leg, so a premium economy transatlantic
    # can come back priced in economy.
    "cabin": "set that leg's cabin to one listed in cabins_sold",
    "basic_economy": "allow_basic_economy=true",
    "not_refundable": "refundable_only=false",
    "airport_change": "allow_airport_change=true",
    "short_layover": "min_layover_minutes (lower it)",
    "long_layover": "max_layover_minutes (raise it)",
    "overnight_layover": "allow_overnight_layover=true",
    "arrives_two_days_later": "max_date_shift=2",
    "too_many_stops": "max_stops (raise it, top level or on the leg)",
    "nonstop_only": "drop nonstop on that leg",
    "departs_too_early": "depart_after",
    "arrives_too_late": "arrive_before",
}
COMFORT = {"nonstop_only", "short_layover", "long_layover", "overnight_layover",
           "arrives_two_days_later", "too_many_stops", "departs_too_early",
           "arrives_too_late"}


@dataclass
class Options:
    cabins: list[str]                       # requested cabin code per leg
    adults: int = 1
    children: int = 0
    sort: str = "best"
    strict_cabin: bool = True
    allow_basic_economy: bool = False
    refundable_only: bool = False
    allow_airport_change: bool = False
    allow_overnight_layover: bool = False
    min_layover_minutes: int | None = 45
    max_layover_minutes: int | None = MAX_LAYOVER_MINUTES
    max_date_shift: int = 1
    max_stops: int | None = 2
    leg_max_stops: list[int | None] = field(default_factory=list)   # per leg; 0 = nonstop
    depart_after: list[str | None] = field(default_factory=list)   # "HH:MM" per leg
    arrive_before: list[str | None] = field(default_factory=list)
    flights: list[str] = field(default_factory=list)
    max_results: int = 10

    def __post_init__(self):
        if self.sort not in ("best", "price", "duration"):
            raise FlightSearchError(f"sort must be best, price or duration, not {self.sort!r}")
        self.flights = [normalize_flight(f) for f in self.flights]
        for name in ("depart_after", "arrive_before"):
            for v in getattr(self, name):
                if v is not None:
                    _hhmm(v)


def _hhmm(v: str) -> tuple[int, int]:
    try:
        h, m = str(v).split(":")
        h, m = int(h), int(m)
        assert 0 <= h < 24 and 0 <= m < 60
        return h, m
    except Exception:
        raise FlightSearchError(f"{v!r} is not a local time like 14:30")


def _dt(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return None


def _clock(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else "?"


def _mins(m: int) -> str:
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m}m"


# ─── One itinerary, read once ─────────────────────────────────────────────────

def _legs(it: dict) -> list[dict]:
    out = []
    for leg in it.get("legs") or []:
        segs, lays, seg_min = [], [], []
        for t in leg.get("timeline") or []:
            if t.get("type") == "air":
                locs = t.get("locations") or ["", ""]
                segs.append((locs[0], locs[-1], t.get("startsAt")))
                seg_min.append(int(t.get("elapsedTime") or 0))
            elif t.get("type") == "layover":
                locs = t.get("locations") or [""]
                start, end = _dt(t.get("startsAt")), _dt(t.get("endsAt"))
                m = int(t.get("elapsedTime") or 0)
                lays.append({"airport": "/".join(locs), "minutes": m,
                             "airport_change": len(locs) > 1,
                             "overnight": bool(start and end and start.date() != end.date()
                                               and m >= OVERNIGHT_LAYOVER_MIN)})
        out.append({
            "origin": leg.get("origin"), "destination": leg.get("destination"),
            "departs_at": leg.get("departsAt"), "arrives_at": leg.get("arrivesAt"),
            "dep": _dt(leg.get("departsAt")), "arr": _dt(leg.get("arrivesAt")),
            "elapsed": int(leg.get("elapsedTime") or 0),
            "stops": len(leg.get("stopLocation") or []),
            "date_shift": int(leg.get("dateShift") or 0),
            "segments": segs, "layovers": lays, "segment_minutes": seg_min,
            "n_segments": max(1, len(leg.get("segmentKeys") or ()) or len(segs)),
            "airline": leg.get("marketingAirline"),
            "operated_by": list(dict.fromkeys(leg.get("operatingAirlines") or [])),
            "equipment": leg.get("equipmentCodes"),
        })
    return out


def _fare(fa: dict, legs: list[dict]) -> dict:
    flat = fa.get("cabinClass") or []
    cabins, i = [], 0
    for lg in legs:
        cabins.append(flat[i:i + lg["n_segments"]])
        i += lg["n_segments"]
    brands = list(dict.fromkeys(b.get("brandName") for b in fa.get("branding") or []
                                if b.get("brandName")))
    seats = [s for s in (fa.get("remainingSeats") or []) if isinstance(s, int)]
    return {
        "brands": brands,
        "price_per_person_usd": fa.get("rawFare"),
        "price_total_usd": (fa.get("totalFare") or {}).get("totalPrice"),
        "refundable": bool(fa.get("refundableBefore")),
        "checked_bags": fa.get("baggage"),
        "basic_economy": _is_basic_economy(fa),
        "cabins": ["/".join(dict.fromkeys(c)) for c in cabins],
        "_cabins": cabins,
        "_minutes": [lg["segment_minutes"] for lg in legs],
        "seats_left": min(seats) if seats else None,
        "commission_per_ticket_usd": (fa.get("commission") or {}).get("amount"),
        **_terms(fa),
    }


def _terms(fa: dict) -> dict:
    """Change and refund rules in words, from the fare's own penalty list."""
    pens = fa.get("penalties") or []

    def pick(kind, when):
        return next((p for p in pens if p.get("type") == kind
                     and p.get("applicability") == when), None)

    ex, rf = pick("Exchange", "Before"), pick("Refund", "Before")
    if ex is None:
        changes = "change rules not returned; confirm at booking"
    elif ex.get("changeable") is False:
        changes = "no changes"
    else:
        fee = ex.get("amount")
        changes = ("changes allowed, no fee" if not fee
                   else f"changes allowed for a ${fee:,.0f} fee plus any fare difference")
    if fa.get("refundableBefore") or (rf and rf.get("refundable") and not rf.get("amount")):
        refund = "fully refundable before departure"
    elif rf and rf.get("refundable"):
        refund = f"refundable less a ${rf.get('amount', 0):,.0f} fee"
    elif rf is None:
        refund = "refund rules not returned; confirm at booking"
    else:
        refund = "non-refundable"
    return {"changes": changes, "refund": refund}


def _cabin_ok(fare: dict, wanted: list[str]) -> bool:
    if len(fare["_cabins"]) != len(wanted):
        return False
    return all(leg_cabin_ok(c, m, w)
               for c, m, w in zip(fare["_cabins"], fare["_minutes"], wanted))


def _read(it: dict, o: Options) -> dict:
    legs = _legs(it)
    fares = [_fare(fa, legs) for fa in it.get("itineraryFares") or []]
    fares = [f for f in fares if f["price_per_person_usd"] is not None]
    fares.sort(key=lambda f: f["price_per_person_usd"])
    correct = []                         # reasons the routing cannot be quoted
    usable = fares
    if o.strict_cabin:
        usable = [f for f in usable if _cabin_ok(f, o.cabins)]
        if fares and not usable:
            correct.append(("cabin", "no fare in the requested cabin on every flight"))
    if not o.allow_basic_economy:
        before = usable
        usable = [f for f in usable if not f["basic_economy"]]
        if before and not usable:
            correct.append(("basic_economy", "only basic economy is sold"))
    if o.refundable_only:
        before = usable
        usable = [f for f in usable if f["refundable"]]
        if before and not usable:
            correct.append(("not_refundable", "no refundable fare"))
    for f in fares:
        f["eligible"] = f in usable
    # Michael: an airport change "should not be permitted unless specifically
    # asked for". A hard rule, like cabin: pinning flights does not lift it,
    # and it never appears as an anchor. Bags are not through-checked and the
    # transfer (LGA to JFK is an hour in traffic) is on the traveler.
    if not o.allow_airport_change:
        for lg in legs:
            for lay in lg["layovers"]:
                if lay["airport_change"]:
                    correct.append(("airport_change", f"changes airports {lay['airport']}"))
    return {"it": it, "legs": legs, "fares": fares, "fare": usable[0] if usable else None,
            "flights": flight_numbers(it), "correct": correct,
            "comfort": _comfort(legs, o),
            "physical": tuple(s for lg in legs for s in lg["segments"]),
            "elapsed": sum(lg["elapsed"] for lg in legs),
            "stops": sum(lg["stops"] for lg in legs)}


def _comfort(legs: list[dict], o: Options) -> list[tuple[str, str]]:
    out = []
    for i, lg in enumerate(legs):
        for lay in lg["layovers"]:
            m, ap = lay["minutes"], lay["airport"]
            if o.min_layover_minutes is not None and m < o.min_layover_minutes:
                out.append(("short_layover", f"{m}m connection in {ap}"))
            if o.max_layover_minutes is not None and m > o.max_layover_minutes:
                out.append(("long_layover", f"{_mins(m)} layover in {ap}"))
            if lay["overnight"] and not o.allow_overnight_layover:
                out.append(("overnight_layover", f"overnight layover in {ap}"))
        if lg["date_shift"] > o.max_date_shift:
            out.append(("arrives_two_days_later",
                        f"arrives {lg['date_shift']} days after departure"))
        limit = o.leg_max_stops[i] if i < len(o.leg_max_stops) and \
            o.leg_max_stops[i] is not None else o.max_stops
        if limit is not None and lg["stops"] > limit:
            out.append(("nonstop_only" if limit == 0 else "too_many_stops",
                        f"{'leg ' + str(i + 1) + ': ' if len(legs) > 1 else ''}"
                        f"{lg['stops']} stop{'s' if lg['stops'] != 1 else ''}"
                        + (", nonstop asked" if limit == 0 else f", max {limit}")))
        after = o.depart_after[i] if i < len(o.depart_after) else None
        if after and lg["dep"] and (lg["dep"].hour, lg["dep"].minute) < _hhmm(after):
            out.append(("departs_too_early", f"departs {_clock(lg['dep'])}, before {after}"))
        before = o.arrive_before[i] if i < len(o.arrive_before) else None
        if before and lg["arr"] and (lg["date_shift"] > 0 or
                                     (lg["arr"].hour, lg["arr"].minute) > _hhmm(before)):
            out.append(("arrives_too_late", f"arrives {_clock(lg['arr'])}"
                        + (f" +{lg['date_shift']}" if lg["date_shift"] else "")
                        + f", after {before}"))
    return out


# ─── Scoring ──────────────────────────────────────────────────────────────────

def _penalties(r: dict, fastest: list[int], fewest: list[int]) -> tuple[float, list[str]]:
    hours, why = 0.0, []
    for i, lg in enumerate(r["legs"]):
        tag = f"leg {i + 1}: " if len(r["legs"]) > 1 else ""
        extra = max(0, lg["elapsed"] - fastest[i])
        if extra >= 15:
            hours += PENALTY_PER_EXTRA_HOUR * extra / 60
            why.append(f"{tag}+{_mins(extra)} vs fastest")
        more = max(0, lg["stops"] - fewest[i])
        if more:
            hours += PENALTY_PER_EXTRA_STOP * more
            why.append(f"{tag}{more} more stop{'s' if more > 1 else ''} than the fewest")
        for lay in lg["layovers"]:
            m = lay["minutes"]
            if m < TIGHT_CONNECTION_MIN:
                hours += PENALTY_TIGHT
                why.append(f"{tag}tight {m}m in {lay['airport']}")
            elif m > LONG_SIT_MIN:
                hours += (m - LONG_SIT_MIN) / 60
                why.append(f"{tag}{_mins(m)} sit in {lay['airport']}")
        if lg["dep"] and (lg["dep"].hour, lg["dep"].minute) < EARLY_DEPARTURE:
            hours += PENALTY_EARLY
            why.append(f"{tag}departs {_clock(lg['dep'])}")
        if lg["arr"] and (lg["arr"].hour >= LATE_ARRIVAL_FROM or lg["arr"].hour < LATE_ARRIVAL_UNTIL):
            hours += PENALTY_LATE
            why.append(f"{tag}arrives {_clock(lg['arr'])}")
    return hours, why


def _hour_value(prices: list[float]) -> float:
    if not prices:
        return H_FLOOR_USD
    q = statistics.quantiles(prices, n=4)[0] if len(prices) > 1 else prices[0]
    return max(H_FLOOR_USD, H_SHARE_OF_P25 * q)


# ─── Output ───────────────────────────────────────────────────────────────────

def _public_fare(f: dict) -> dict:
    return {k: v for k, v in f.items() if not k.startswith("_")}


def _row(r: dict, o: Options) -> dict:
    f = r["fare"] or {}
    pax = o.adults + o.children
    seats = f.get("seats_left")
    per = f.get("commission_per_ticket_usd")
    return {
        "client_quote": client_quote(r, pax),
        "commission_total_usd": round(per * pax, 2) if per is not None else None,
        "flights": r["flights"],
        "also_sold_as": r.get("twins", []),
        "price_per_person_usd": f.get("price_per_person_usd"),
        "price_total_usd": f.get("price_total_usd"),
        "effective_price_per_person_usd": round(r["effective"]) if "effective" in r else None,
        "rank_reasons": r.get("why") or ["the benchmark: fastest, fewest stops"],
        "fare_brands": f.get("brands"),
        "cabins": f.get("cabins"),
        "refundable": f.get("refundable"),
        "checked_bags": f.get("checked_bags"),
        "basic_economy": f.get("basic_economy"),
        "commission_per_ticket_usd": f.get("commission_per_ticket_usd"),
        "seats_left": seats,
        "few_seats": seats is not None and seats < pax + 2,
        "elapsed_minutes": r["elapsed"],
        "stops": r["stops"],
        "legs": [{
            "origin": lg["origin"], "destination": lg["destination"],
            "departs_at": lg["departs_at"], "arrives_at": lg["arrives_at"],
            "elapsed_minutes": lg["elapsed"], "stops": lg["stops"],
            "layovers": [{k: v for k, v in lay.items() if v or k in ("airport", "minutes")}
                         for lay in lg["layovers"]],
            "airline": lg["airline"], "operated_by": lg["operated_by"],
            "equipment": lg["equipment"],
        } for lg in r["legs"]],
        "fare_options": [_public_fare(x) for x in r["fares"]],
        "other_departures": r.get("folded", []),
        "key": r["it"].get("key"),
    }


AIRLINES = {
    "AA": "American", "AC": "Air Canada", "AF": "Air France", "AS": "Alaska",
    "AY": "Finnair", "AZ": "ITA Airways", "B6": "JetBlue", "BA": "British Airways",
    "DL": "Delta", "EI": "Aer Lingus", "EK": "Emirates", "IB": "Iberia", "KL": "KLM",
    "LH": "Lufthansa", "LX": "Swiss", "OS": "Austrian", "QR": "Qatar Airways",
    "SN": "Brussels Airlines", "TK": "Turkish Airlines", "TP": "TAP Air Portugal",
    "UA": "United", "UX": "Air Europa", "VS": "Virgin Atlantic", "WS": "WestJet",
    "9E": "Delta Connection", "YX": "Delta Connection", "OH": "American Eagle",
    "MQ": "American Eagle", "OO": "SkyWest", "G7": "United Express",
}
CABIN_WORDS = {"Y": "economy", "S": "premium economy", "W": "premium economy",
               "C": "business", "J": "business", "F": "first", "P": "first"}


def _when(dt: datetime | None) -> str:
    if not dt:
        return "?"
    h = dt.strftime("%I:%M%p").lstrip("0").lower()
    return f"{dt.strftime('%a %b')} {dt.day}, {h}"


def client_quote(r: dict, travelers: int) -> str:
    """A block Michael can paste into a client email.

    No commission, no agency identifiers, and the price is the party total,
    because a per-ticket figure or a commission line one copy-paste away from
    a client is how they leak.
    """
    f = r["fare"] or {}
    lines = []
    for i, lg in enumerate(r["legs"]):
        cab = f.get("_cabins", [[]])[i] if i < len(f.get("_cabins", [])) else []
        cabin = " / ".join(dict.fromkeys(CABIN_WORDS.get(c, c) for c in cab)) or "economy"
        via = ", ".join(f"{lay['airport']} ({_mins(lay['minutes'])})" for lay in lg["layovers"])
        airline = AIRLINES.get(lg["airline"], lg["airline"] or "")
        ops = [AIRLINES.get(x, x) for x in lg["operated_by"] if x != lg["airline"]]
        lines.append(
            f"{airline} {lg['origin']} to {lg['destination']}, {cabin}: departs "
            f"{_when(lg['dep'])}, arrives {_when(lg['arr'])}; "
            + (f"connects in {via}" if via else "nonstop")
            + (f" (operated by {', '.join(dict.fromkeys(ops))})" if ops else "") + ".")
    brands = ", ".join(f.get("brands") or [])
    bags = f.get("checked_bags")
    terms = [f"{bags} checked bag{'s' if bags != 1 else ''} per person" if bags else None,
             f.get("changes"), f.get("refund")]
    total = f.get("price_total_usd")
    lines.append(f"Fare: {brands}. " + "; ".join(t for t in terms if t) + ".")
    if total is not None:
        lines.append(f"Total for {travelers} traveler{'s' if travelers != 1 else ''}: "
                     f"${total:,.2f}, taxes included. Fares change until ticketed.")
    return "\n".join(lines)


def _brief(r: dict) -> dict:
    f = r["fare"] or {}
    return {"flights": "+".join(r["flights"]),
            "price_per_person_usd": f.get("price_per_person_usd"),
            "elapsed": _mins(r["elapsed"]), "stops": r["stops"],
            "connections": ", ".join(f"{lay['airport']} {_mins(lay['minutes'])}"
                                     for lg in r["legs"] for lay in lg["layovers"])}


def _airports(ok: list[dict], key) -> list[dict]:
    """Per leg, the airports actually flown when a city code (NYC) spans
    several, with the cheapest and best (effective price) option from each. Empty when
    every routing uses the same pair, so it only speaks when there is a choice.
    """
    out = []
    n = max((len(r["legs"]) for r in ok), default=0)
    for i in range(n):
        by = defaultdict(list)
        for r in ok:
            lg = r["legs"][i]
            by[(lg["origin"], lg["destination"])].append(r)
        if len(by) < 2:
            continue
        for (a, b), rs in sorted(by.items(), key=lambda kv: min(key(r) for r in kv[1])):
            out.append({"leg": i + 1, "from": a, "to": b, "routings": len(rs),
                        "cheapest_per_person_usd": min(r["fare"]["price_per_person_usd"]
                                                       for r in rs),
                        "best_effective_per_person_usd": round(min(
                            r.get("effective", r["fare"]["price_per_person_usd"]) for r in rs))})
    return out


def _cabins_sold(read: list[dict], n_legs: int) -> list[list[str]]:
    """Per leg, every cabin some fare sells on ALL of that leg's flights.

    NAP-BCN has no premium economy: short intra-Europe flights sell economy
    and business. Saying so per leg is what lets an agent change one leg's
    cabin instead of relaxing the cabin rule for the whole trip.
    """
    order = ["economy", "premium_economy", "business", "first"]
    code = {"economy": "Y", "premium_economy": "S", "business": "C", "first": "F"}
    out = [set() for _ in range(n_legs)]
    for r in read:
        for f in r["fares"]:
            for i, segs in enumerate(f["_cabins"][:n_legs]):
                if not segs:
                    continue
                # The same rule the cabin match uses, so "sold" and
                # "would match" can never disagree.
                for name in order:
                    if leg_cabin_ok(segs, f["_minutes"][i], code[name]) and not (
                            name == "business" and all(c in ("F", "P") for c in segs)):
                        out[i].add(name)
    return [sorted(s, key=order.index) for s in out]


def build(itineraries: list[dict], o: Options) -> dict:
    """The whole answer for one search. See the module docstring."""
    read = [_read(it, o) for it in itineraries]
    if o.flights:
        pinned = [r for r in read if has_flights(r["it"], o.flights)]
    else:
        pinned = None

    # Collapse codeshare twins into physical routings, cheapest usable first.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in (pinned if pinned is not None else read):
        groups[r["physical"] or (r["it"].get("key"),)].append(r)
    routings = []
    for twins in groups.values():
        twins.sort(key=lambda r: (r["fare"] is None,
                                  (r["fare"] or {}).get("price_per_person_usd") or 9e9))
        rep = dict(twins[0])
        rep["twins"] = ["+".join(t["flights"]) for t in twins[1:]]
        routings.append(rep)

    excluded = Counter()
    ok, correct_ok = [], []
    for r in routings:
        if r["correct"]:
            excluded[r["correct"][0][0]] += 1
            continue
        correct_ok.append(r)
        if r["comfort"] and not o.flights:
            excluded[r["comfort"][0][0]] += 1
            continue
        ok.append(r)

    n_legs = len(o.cabins)
    fastest = [min((r["legs"][i]["elapsed"] for r in ok), default=0) for i in range(n_legs)]
    fewest = [min((r["legs"][i]["stops"] for r in ok), default=0) for i in range(n_legs)]
    H = _hour_value([r["fare"]["price_per_person_usd"] for r in ok])
    for r in ok:
        hours, why = _penalties(r, fastest, fewest)
        r["effective"] = r["fare"]["price_per_person_usd"] + H * hours
        r["why"] = why

    key = {"best": lambda r: r["effective"],
           "price": lambda r: r["fare"]["price_per_person_usd"],
           "duration": lambda r: (r["elapsed"], r["fare"]["price_per_person_usd"])}[o.sort]
    ok.sort(key=key)

    # Feeders into the same final flight(s) fold under the best of them.
    rows, where, fold_at = [], {}, {}
    for pos, r in enumerate(ok):
        fk = tuple(lg["segments"][-1] if lg["segments"] else None for lg in r["legs"])
        if fk in fold_at and not o.flights:
            host = rows[fold_at[fk]]
            host.setdefault("folded", []).append({
                "flights": "+".join(r["flights"]),
                "departs_at": r["legs"][0]["departs_at"],
                "connections": ", ".join(f"{lay['airport']} {_mins(lay['minutes'])}"
                                         for lg in r["legs"] for lay in lg["layovers"]),
                "price_per_person_usd": r["fare"]["price_per_person_usd"],
                "effective_price_per_person_usd": round(r["effective"])})
            where[id(r)] = f"folded under #{fold_at[fk] + 1}"
            continue
        fold_at[fk] = len(rows)
        rows.append(r)
        where[id(r)] = f"#{len(rows)}"
    shortlist = rows[:max(1, o.max_results)]

    def status(r):
        if r["correct"]:
            return f"excluded: {r['correct'][0][1]}"
        if r["comfort"] and not o.flights:
            return f"excluded: {r['comfort'][0][1]}"
        s = where.get(id(r), "")
        if s.startswith("#") and int(s[1:]) > len(shortlist):
            return f"ranked {s}, below the shortlist"
        return f"shortlist {s}" if s.startswith("#") else s

    anchors = {}
    price = lambda r: r["fare"]["price_per_person_usd"]
    if correct_ok:
        for name, pick in (
                ("cheapest", min(correct_ok, key=price)),
                ("fastest", min(correct_ok, key=lambda r: (r["elapsed"], price(r)))),
                ("cheapest_fewest_stops",
                 min(correct_ok, key=lambda r: (r["stops"], price(r))))):
            anchors[name] = {**_brief(pick), "status": status(pick)}
    def refundable_fares(pool):
        return [(r, f) for r in pool for f in r["fares"]
                if f["refundable"] and (not o.strict_cabin or _cabin_ok(f, o.cabins))
                and (o.allow_basic_economy or not f["basic_economy"])]
    # A ranked routing's refundable fare first: the cheapest refundable fare
    # overall was a LaGuardia-to-JFK airport change, which answers nothing.
    refund = refundable_fares(ok) or refundable_fares(
        [r for r in routings if not r["correct"] or r["correct"][0][0] == "not_refundable"])
    if refund:
        r, f = min(refund, key=lambda rf: rf[1]["price_per_person_usd"])
        anchors["cheapest_refundable"] = {**_brief(r), "price_per_person_usd": f["price_per_person_usd"],
                                          "fare_brands": f["brands"], "status": status(r)}

    by_routing = defaultdict(lambda: {"routings": 0, "cheapest_per_person_usd": None,
                                      "fastest": None, "_best": None})
    for r in ok:
        carrier = r["legs"][0]["airline"] or "?"
        via = " / ".join(",".join(l["airport"] for l in lg["layovers"]) or "nonstop"
                         for lg in r["legs"])
        b = by_routing[f"{carrier} via {via}"]
        b["routings"] += 1
        p = price(r)
        if b["cheapest_per_person_usd"] is None or p < b["cheapest_per_person_usd"]:
            b["cheapest_per_person_usd"] = p
        if b["fastest"] is None or r["elapsed"] < b["_e"]:
            b["fastest"], b["_e"] = _mins(r["elapsed"]), r["elapsed"]
        if b["_best"] is None or key(r) < b["_best"]:
            b["_best"] = key(r)
    # The best of each routing first, capped: a round trip produced 48 lines,
    # which is a list to wade through, not a map.
    by_routing_out = [{"routing": k, **{x: y for x, y in v.items() if not x.startswith("_")}}
                      for k, v in sorted(by_routing.items(), key=lambda kv: kv[1]["_best"])
                      ][:BY_ROUTING_MAX]

    sold = _cabins_sold(read, len(o.cabins))
    # Whether ANY fare in the pool flies this leg nonstop in a usable cabin,
    # so "nonstop to Naples" can be answered "none is sold" instead of empty.
    nonstop = [any(len(r["legs"]) > i and r["legs"][i]["stops"] == 0 and not r["correct"]
                   for r in read) for i in range(len(o.cabins))]
    out = {
        "cabins_sold": [{"leg": i + 1, "requested": CABIN_NAMES.get(o.cabins[i], o.cabins[i]),
                         "sold": sold[i],
                         "requested_is_sold": CABIN_NAMES.get(o.cabins[i]) in sold[i],
                         "nonstop_sold": nonstop[i]}
                        for i in range(len(o.cabins))],
        "shortlist": [{"rank": i + 1, **_row(r, o)} for i, r in enumerate(shortlist)],
        "anchors": anchors,
        "by_routing": by_routing_out,
        "airports": _airports(ok, key),
        "excluded": [{"reason": k, "routings": v, "lift_with": LIFT[k]}
                     for k, v in excluded.most_common()],
        "pool": {"fares": len(itineraries), "physical_routings": len(routings),
                 "ranked": len(ok), "hour_value_usd": round(H),
                 "sort": o.sort},
    }
    missing = [c for c in out["cabins_sold"] if not c["requested_is_sold"] and c["sold"]]
    no_nonstop = [i + 1 for i, lim in enumerate(o.leg_max_stops)
                  if lim == 0 and i < len(nonstop) and not nonstop[i]]
    if o.strict_cabin and missing:
        out["note"] = " ".join(
            f"No {c['requested'].replace('_', ' ')} is sold on leg {c['leg']} on any routing "
            f"(sold: {', '.join(c['sold'])})." for c in missing) + (
            " Set that leg's cabin to one of those and search again; leave strict_cabin on, "
            "or the other legs can come back in the wrong cabin too.")
    elif no_nonstop:
        out["note"] = ("No nonstop is sold on leg" + ("s " if len(no_nonstop) > 1 else " ")
                       + ", ".join(map(str, no_nonstop))
                       + " in this cabin on this date. Drop `nonstop` on that leg or set "
                       "its max_stops to 1.")
    elif o.flights and not routings:
        out["note"] = (f"None of the {len(itineraries)} fares Fora returned contain all of "
                       f"{', '.join(o.flights)}. Fora does not sell those flights together "
                       "as one fare on this date.")
    elif o.flights and not ok:
        out["note"] = ("Fora sells those flights together, but not as asked: "
                       + "; ".join(sorted({r['correct'][0][1] for r in routings if r['correct']}))
                       + ". See `excluded` for the parameter that lifts it.")
    elif not ok:
        out["note"] = ("Nothing survived the filters. See `excluded` for what was removed "
                       "and the parameter that lifts each reason.")
    return out
