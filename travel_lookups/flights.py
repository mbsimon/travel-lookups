"""Flight routing and availability. Read-only — nothing here books or holds.

Two sources, because they answer different questions:

  AeroAPI        — what physically flies a city pair: operating carrier, local
                   times, aircraft, seat count by cabin. First-party schedules.
  Google Flights — connecting itineraries and fares. Read from Google's own
                   pages first (`google_flights`, free); SerpApi, on a
                   250-search month, only when a page cannot be read.

AeroAPI is the default. It costs a fraction of a cent against a monthly credit,
so a question about nonstops must never touch a fare source.

This module is also the implementation behind the `travel` MCP server
(`mbsimon/travel-mcp`, served at travel.michaelbsimon.com/mcp), which imports it
rather than keeping its own copy. One definition, several front doors — Agency
HQ's Slack verbs and the itinerary pages come through the same one.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

# Load our own keys rather than inheriting whatever the importer happened to
# load. This module is imported by HQ (where hq_config has already done it) and
# by the Mac MCP server (where nothing has).
load_dotenv(Path.home() / ".config/michaelsimon/secrets.env")

AEROAPI_BASE = "https://aeroapi.flightaware.com/aeroapi"
SERPAPI_BASE = "https://serpapi.com/search"

# AeroAPI bills and rate-limits per page, and one request asking for many pages
# at once trips the limiter (max_pages=10 returns 429). Fetch a few per request
# and follow the cursor. Page one alone is NOT complete — on MAD-JFK it silently
# dropped two of five operating flights.
AEROAPI_MAX_PAGES = 3
AEROAPI_PAGE_LIMIT = 15
AEROAPI_TIMEOUT_SECONDS = 45
AEROAPI_RETRY_ATTEMPTS = 4
AEROAPI_RETRY_BACKOFF_SECONDS = 3.0

# The Personal tier allows roughly 10 queries a minute; self-throttle under it
# so a burst waits rather than fails.
AEROAPI_CALLS_PER_WINDOW = 9
AEROAPI_WINDOW_SECONDS = 60.0

SERPAPI_TIMEOUT_SECONDS = 60

DEFAULT_CURRENCY = "EUR"
DEFAULT_MARKET = "es"
DEFAULT_LANGUAGE = "en"

# A schedule lookup spanning weeks is a page-budget hole, not a useful answer.
MAX_SCHEDULE_DAYS = 7

CABIN_CODES = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
STOP_CODES = {"any": 0, "nonstop": 1, "one_stop_max": 2, "two_stops_max": 3}

# Airport metadata is static, so cache it — after the first lookup a route
# search costs one AeroAPI query instead of three.
    # Directory name predates the split into travel-lookups and is deliberately
    # unchanged: renaming it would orphan the warm airport/station caches on both
    # the Mac and the travel-mcp container's /state volume, and the VPS one is
    # seeded from the Mac's.
AIRPORT_CACHE_FILE = Path(
    os.getenv("FLIGHTS_CACHE_DIR", str(Path.home() / ".cache/flight-research"))
) / "airports.json"


class FlightsError(RuntimeError):
    """Raised when a flight source declines or cannot answer."""


def _load_airport_cache() -> dict[str, dict]:
    try:
        return json.loads(AIRPORT_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


_airport_cache: dict[str, dict] = _load_airport_cache()
_call_times: deque[float] = deque()


def _save_airport_cache() -> None:
    try:
        AIRPORT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        AIRPORT_CACHE_FILE.write_text(json.dumps(_airport_cache, indent=1))
    except OSError:
        pass  # A cache write failure must never break a lookup.


def _throttle(cost: int = 1) -> None:
    """Block until `cost` more AeroAPI queries fit inside the allowed rate.

    Cost is not always one. A request carrying `max_pages=3` spends THREE
    queries of the tier's ~10/minute, so charging it as one let a schedule
    sweep sail past the limit and fail on a 429 that the caller reads as a
    broken route.
    """
    while True:
        now = time.monotonic()
        while _call_times and now - _call_times[0] > AEROAPI_WINDOW_SECONDS:
            _call_times.popleft()
        if len(_call_times) + cost <= AEROAPI_CALLS_PER_WINDOW:
            break
        wait = AEROAPI_WINDOW_SECONDS - (now - _call_times[0]) + 0.1
        time.sleep(max(wait, 0.1))
    stamp = time.monotonic()
    for _ in range(cost):
        _call_times.append(stamp)


def _aero(path: str, params: dict | None = None) -> dict:
    # A paged request spends one query per page it may return. Cursor links
    # carry max_pages in their own query string, where `params` is None.
    if params and params.get("max_pages"):
        cost = int(params["max_pages"])
    else:
        match = re.search(r"max_pages=(\d+)", path)
        cost = int(match.group(1)) if match else 1
    key = os.getenv("AEROAPI_KEY")
    if not key:
        raise FlightsError("AEROAPI_KEY is not set — cannot reach AeroAPI.")

    response = None
    for attempt in range(AEROAPI_RETRY_ATTEMPTS):
        _throttle(cost)
        try:
            response = requests.get(f"{AEROAPI_BASE}{path}", params=params,
                                    headers={"x-apikey": key},
                                    timeout=AEROAPI_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise FlightsError(f"Could not reach AeroAPI: {exc}") from exc
        if response.status_code != 429:
            break
        if attempt < AEROAPI_RETRY_ATTEMPTS - 1:
            time.sleep(AEROAPI_RETRY_BACKOFF_SECONDS * (2**attempt))

    if response.status_code == 401:
        raise FlightsError("AeroAPI rejected the key.")
    if response.status_code == 429:
        raise FlightsError("AeroAPI rate limit hit even after backing off.")
    if response.status_code == 400 and "NO_DATA" in response.text:
        raise FlightsError(
            f"AeroAPI does not recognise the airport code {path.split('/')[-1]!r}. "
            "Use IATA (MAD) or ICAO (LEMD).")
    if response.status_code == 404:
        raise FlightsError(f"AeroAPI has no record for {path}.")
    if response.status_code >= 400:
        raise FlightsError(f"AeroAPI error {response.status_code}: {response.text[:200]}")
    return response.json()


def _aero_all_pages(path: str, params: dict, collection: str) -> tuple[list, bool]:
    """Follow AeroAPI's cursor until exhausted or capped. Returns (items, truncated)."""
    items: list = []
    pages_used = 0
    next_path: str | None = None
    while pages_used < AEROAPI_PAGE_LIMIT:
        payload = _aero(path, params) if next_path is None else _aero(next_path)
        items.extend(payload.get(collection) or [])
        pages_used += AEROAPI_MAX_PAGES
        next_path = (payload.get("links") or {}).get("next")
        if not next_path:
            return items, False
    return items, True


def airport(code: str) -> dict:
    """Resolve an airport code to name, city, timezone and coordinates.

    AeroAPI's airport lookup does NOT prefer IATA. Asked for `HND` it returns
    Henderson Executive in Las Vegas (KHND), not Tokyo Haneda (RJTT) — and the
    timezone that comes with it would have converted a Tokyo arrival into
    Pacific time. So a three-letter request that comes back carrying a
    different IATA code (or none) is marked `iata_verified: False` rather than
    trusted: the caller decides whether to name the place or just the code.
    """
    code = code.strip().upper()
    # "country" in the cached record, not just presence, guards against a
    # persisted cache from before that field existed — a pre-"country" entry
    # would otherwise serve forever and silently fail the US-currency check
    # in itineraries() (found live: MAD/JFK were already cached without it).
    if code in _airport_cache and "country" in _airport_cache[code]:
        return _airport_cache[code]
    data = _aero(f"/airports/{code}")
    verified = not (len(code) == 3 and (data.get("code_iata") or "").upper() != code)
    record = {"iata": data.get("code_iata"), "icao": data.get("code_icao"),
              "name": data.get("name"), "city": data.get("city"),
              "country": data.get("country_code"),
              "timezone": data.get("timezone"), "latitude": data.get("latitude"),
              "longitude": data.get("longitude"),
              "iata_verified": verified, "requested": code}
    _airport_cache[code] = record
    _save_airport_cache()
    return record


def _to_local(utc_iso: str | None, tz_name: str | None) -> dict:
    if not utc_iso:
        return {"utc": None, "local": None, "local_date": None}
    moment = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    out = {"utc": moment.strftime("%Y-%m-%d %H:%MZ"), "local": None, "local_date": None}
    if tz_name:
        try:
            local = moment.astimezone(ZoneInfo(tz_name))
            out["local"] = local.strftime("%H:%M")
            out["local_date"] = local.strftime("%Y-%m-%d")
        except Exception:
            pass
    return out


def _duration(depart_utc: str | None, arrive_utc: str | None) -> str | None:
    if not depart_utc or not arrive_utc:
        return None
    out = datetime.fromisoformat(depart_utc.replace("Z", "+00:00"))
    inn = datetime.fromisoformat(arrive_utc.replace("Z", "+00:00"))
    minutes = int((inn - out).total_seconds() // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes > 0 else None


def nonstop_service(origin: str, destination: str, start_date: str,
                    days: int = 1) -> dict:
    """Scheduled nonstops for a city pair, one entry per PHYSICAL flight.

    Codeshares are folded into `sold_also_as`. A client quoted "BA1567" and a
    client quoted "AA95" are on the same aircraft; listing both as options is
    how an itinerary ends up offering someone a choice between one flight.
    """
    days = max(1, min(int(days), MAX_SCHEDULE_DAYS))
    try:
        begin = date.fromisoformat(start_date)
    except ValueError as exc:
        raise FlightsError(f"start_date must be YYYY-MM-DD, got {start_date!r}") from exc
    end = begin + timedelta(days=days)

    origin_info, destination_info = airport(origin), airport(destination)
    scheduled, truncated = _aero_all_pages(
        f"/schedules/{begin.isoformat()}/{end.isoformat()}",
        {"origin": origin.strip().upper(), "destination": destination.strip().upper(),
         "max_pages": AEROAPI_MAX_PAGES},
        collection="scheduled")

    # The schedules endpoint DOES resolve IATA correctly — it answers HND with
    # RJTT rows. So when the airport lookup disagreed, believe the flight data
    # and re-resolve from the ICAO it reports. This is what keeps a Tokyo
    # arrival on Tokyo time.
    if scheduled:
        if not origin_info.get("iata_verified") and scheduled[0].get("origin_icao"):
            origin_info = airport(scheduled[0]["origin_icao"])
        if (not destination_info.get("iata_verified")
                and scheduled[0].get("destination_icao")):
            destination_info = airport(scheduled[0]["destination_icao"])

    groups: dict[tuple, dict] = {}
    for entry in scheduled:
        operating = entry.get("actual_ident") or entry.get("ident")
        depart_utc = entry.get("scheduled_out")
        signature = (operating, depart_utc)
        if signature not in groups:
            groups[signature] = {
                "operated_by": (entry.get("actual_ident_iata") or entry.get("actual_ident")
                                or entry.get("ident_iata") or entry.get("ident")),
                "departs": _to_local(depart_utc, origin_info.get("timezone")),
                "arrives": _to_local(entry.get("scheduled_in"),
                                     destination_info.get("timezone")),
                "duration": _duration(depart_utc, entry.get("scheduled_in")),
                "aircraft": entry.get("aircraft_type"),
                "cabins": {"first": entry.get("seats_cabin_first"),
                           "business": entry.get("seats_cabin_business"),
                           "economy": entry.get("seats_cabin_coach")},
                "meal_service": entry.get("meal_service"),
                "sold_also_as": []}
        if entry.get("actual_ident"):
            label = entry.get("ident_iata") or entry.get("ident_icao") or entry.get("ident")
            if label and label not in groups[signature]["sold_also_as"]:
                groups[signature]["sold_also_as"].append(label)

    flights = sorted(groups.values(),
                     key=lambda f: (f["departs"]["utc"] or "", f["operated_by"] or ""))
    return {"origin": origin_info, "destination": destination_info,
            "window": {"from": begin.isoformat(), "to": end.isoformat(), "days": days},
            "nonstop_flights": flights, "count": len(flights), "truncated": truncated,
            "note": ("Scheduled nonstop service only — no connections, and this says "
                     "nothing about seat availability or fares.")}


def route_profile(origin: str, destination: str, start_date: str | None = None,
                  days: int = 3) -> dict:
    """How well served a city pair is: frequency, carriers and aircraft mix.

    Built from published schedules, NOT from AeroAPI's /airports/{id}/routes
    endpoint. That endpoint returns filed ATC route strings — its fields are
    `route`, `filed_altitude_min/max`, `route_distance` — and its coverage is
    whatever flight plans FlightAware exposes at this tier. It answered
    Venice-Rome and Barcelona-Heathrow with ZERO rows while both are flown
    several times a day, and answered Madrid-JFK with plausible-looking
    numbers, which is exactly why the misreading survived testing. A silent
    zero that reads as "no service" on a route with four daily nonstops is the
    worst shape a wrong answer can take, so that source is gone.
    """
    if start_date is None:
        start_date = date.today().isoformat()
    schedule = nonstop_service(origin, destination, start_date, days)
    seen = schedule["nonstop_flights"]

    aircraft: Counter = Counter()
    carriers: Counter = Counter()
    for flight in seen:
        if flight.get("aircraft"):
            aircraft[flight["aircraft"]] += 1
        code = (flight.get("operated_by") or "")[:2].strip()
        if code:
            carriers[code] += 1

    window_days = schedule["window"]["days"] or 1
    return {"origin": schedule["origin"], "destination": schedule["destination"],
            "window": schedule["window"],
            "departures": len(seen),
            "per_day": round(len(seen) / window_days, 1),
            "carriers": [{"code": c, "flights": n} for c, n in carriers.most_common()],
            "aircraft_mix": [{"type": k, "flights": n} for k, n in aircraft.most_common()],
            "truncated": schedule["truncated"],
            "note": ("Published nonstop schedules over the window. Zero here means no "
                     "scheduled nonstop, not missing data.")}


def serpapi_configured() -> bool:
    return bool(os.getenv("SERPAPI_KEY"))


def _leg(flight: dict) -> dict:
    return {"flight": flight.get("flight_number"), "airline": flight.get("airline"),
            "also_sold_by": flight.get("ticket_also_sold_by") or [],
            "from": (flight.get("departure_airport") or {}).get("id"),
            "depart": (flight.get("departure_airport") or {}).get("time"),
            "to": (flight.get("arrival_airport") or {}).get("id"),
            "arrive": (flight.get("arrival_airport") or {}).get("time"),
            "aircraft": flight.get("airplane"), "cabin": flight.get("travel_class"),
            "duration_minutes": flight.get("duration")}


def _itinerary(option: dict, tickets: int = 1) -> dict:
    """One Google Flights option. `price` from SerpApi is the PARTY total.

    DL2294+DL278 read $2,092 at 2 adults and $1,046 at 1 (checked live
    2026-09-25). Read as per person, a party total made a business quote look
    twice as expensive as Fora's; read the other way it halves. Both figures are
    named here, and `price` stays one release for callers that read it.
    """
    legs = [_leg(f) for f in option.get("flights") or []]
    total = option.get("price")
    tickets = max(int(tickets or 1), 1)
    return {"legs": legs, "stops": max(len(legs) - 1, 0),
            "party_total_usd": total, "tickets": tickets,
            "per_person_usd": round(total / tickets, 2) if isinstance(total, (int, float)) else None,
            "booking_token": option.get("booking_token"),
            "departure_token": option.get("departure_token"),
            "extensions": option.get("extensions") or [],
            "layovers": [{"airport": lay.get("id"), "minutes": lay.get("duration"),
                          "overnight": lay.get("overnight", False)}
                         for lay in option.get("layovers") or []],
            "total_duration_minutes": option.get("total_duration"),
            "price": option.get("price"),
            "carriers": sorted({leg["airline"] for leg in legs if leg["airline"]})}


# SerpApi is 250 searches a month. Every docstring here says not to spend one
# on a question the free schedules answer, and the month ran out anyway —
# asking is not a control. Keep a reserve the routine path cannot touch, so a
# genuinely needed fare search still works late in the month.
SERPAPI_MONTHLY = 250
SERPAPI_RESERVE = 50
SERPAPI_LEDGER = Path(
    os.getenv("FLIGHTS_CACHE_DIR", str(Path.home() / ".cache/flight-research"))
) / "serpapi_usage.json"


def serpapi_budget() -> dict:
    """Searches used this calendar month, and what is left before the reserve."""
    month = date.today().strftime("%Y-%m")
    try:
        led = json.loads(SERPAPI_LEDGER.read_text())
    except (OSError, ValueError):
        led = {}
    used = int(led.get(month, 0))
    return {"month": month, "used": used, "limit": SERPAPI_MONTHLY,
            "reserve": SERPAPI_RESERVE,
            "spendable": max(SERPAPI_MONTHLY - SERPAPI_RESERVE - used, 0)}


def _serpapi_spend() -> None:
    month = date.today().strftime("%Y-%m")
    try:
        led = json.loads(SERPAPI_LEDGER.read_text())
    except (OSError, ValueError):
        led = {}
    led[month] = int(led.get(month, 0)) + 1
    try:
        SERPAPI_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        SERPAPI_LEDGER.write_text(json.dumps(led, indent=1))
    except OSError:
        pass  # never let bookkeeping break a lookup


# Rows whose fare-brand ladder is read from Google's booking page, in
# parallel, on a direct search. Each costs a page load (~3-6 s), no money.
BRAND_ROWS_DEFAULT = 5
NOT_CHECKED_SERPAPI = ("read through SerpApi (Google Flights page unreadable): this is Google's "
                       "headline fare and its brand is not named; a brand check costs one "
                       "booking-options search per row")


def _not_checked(reason: str) -> dict:
    return {"verdict": "NOT_CHECKED", "not_checked": reason}


def _direct_itineraries(origin, destination, outbound_date, return_date, adults, cabin,
                        max_stops, children, brand_rows) -> dict:
    """The same answer read from Google Flights' own pages. Raises DirectError."""
    from concurrent.futures import ThreadPoolExecutor
    from . import google_flights as gf
    o, d = origin.strip().upper(), destination.strip().upper()
    legs = [gf.Leg(outbound_date, o, d)] + ([gf.Leg(return_date, d, o)] if return_date else [])
    found = gf.search(legs, adults=adults, children=children,
                      cabin=CABIN_CODES.get(cabin.lower(), 1))
    limit = STOP_CODES.get(max_stops.lower(), 0)
    rows = [r for r in found["itineraries"] if not limit or r["stops"] <= limit - 1]
    one_way = not return_date

    def brands(row):
        segs = tuple(gf.Segment(x["from"], x["date"], x["to"], x["carrier"], x["number"])
                     for x in row["segments"])
        leg = gf.Leg(outbound_date, o, d, segs)
        try:
            got = gf.booking([leg], adults=adults, children=children,
                             cabin=CABIN_CODES.get(cabin.lower(), 1))
        except gf.DirectError as exc:
            return _not_checked(f"Google's booking page for these flights could not be read "
                                f"({exc.kind})")
        fares = got["fares"]
        headline = next((f for f in fares if f["airline_direct"] and f["option_title"]
                         and f["party_total_usd"] == row["party_total_usd"]), None)
        std = [f for f in fares if f["family"] in ("standard", "flexible")]
        cheapest_std = min(std, key=lambda f: f["per_person_usd"]) if std else None
        return {"verdict": "BRANDS_READ", "source": gf.SOURCE, "checked_at": got["fetched_at"],
                "headline_brand": headline["option_title"] if headline else None,
                "headline_tier": headline["family"] if headline else None,
                "cheapest_non_basic": ({"brand": cheapest_std["option_title"],
                                        "seller": cheapest_std["seller"],
                                        "per_person_usd": cheapest_std["per_person_usd"],
                                        "party_total_usd": cheapest_std["party_total_usd"]}
                                       if cheapest_std else None),
                "fares": [{"seller": f["seller"], "brand": f["option_title"],
                           "tier": f["family"], "per_person_usd": f["per_person_usd"],
                           "party_total_usd": f["party_total_usd"]} for f in fares]}

    checked = rows[:max(0, brand_rows)] if one_way else []
    if checked:
        with ThreadPoolExecutor(max_workers=max(1, gf.MAX_PAGES)) as ex:
            for row, rec in zip(checked, ex.map(brands, checked)):
                row["google_check"] = rec
    for row in rows:
        if "google_check" in row:
            continue
        if not one_way:
            row["google_check"] = _not_checked(
                "round trip: this is Google's round-trip total for the cheapest return it "
                "chose; a fare brand exists only once both flights are chosen")
        else:
            row["google_check"] = _not_checked(
                f"brand ladder read for the first {brand_rows} rows only; raise brand_rows "
                "to read more")
        row["google_check"].update(source=gf.SOURCE, checked_at=found["fetched_at"])
    for row in rows:
        row.pop("segments", None)
        row.pop("departs", None)
        row.pop("arrives", None)
    return {"configured": True, "source": gf.SOURCE, "origin": o, "destination": d,
            "outbound_date": outbound_date, "return_date": return_date, "currency": "USD",
            "tickets": adults + children, "fetched_at": found["fetched_at"],
            "google_url": found["url"], "itineraries": rows, "count": len(rows),
            "units": "party_total_usd is the whole party; per_person_usd divides it by tickets"}


def itineraries(origin: str, destination: str, outbound_date: str,
                return_date: str | None = None, adults: int = 1,
                cabin: str = "economy", max_stops: str = "any",
                override_budget: bool = False, currency: str | None = None,
                children: int = 0, brand_rows: int = BRAND_ROWS_DEFAULT) -> dict:
    """Bookable routings including connections, with fares, from Google Flights.

    Google's own page first (free, always USD); SerpApi, which spends a search,
    only when the page cannot be read or a non-USD `currency` is forced. The
    answer says which in `source`, and `direct_error` says why after a fallback.

    Each itinerary carries `party_total_usd` (the whole party), `per_person_usd`
    and `tickets`, and `google_check`: on a direct one-way search the first
    `brand_rows` rows get every fare brand Google sells on those flights
    (headline_brand names what the headline price buys; it is often basic),
    and every other row says why it was not checked.
    """
    direct_error = None
    if currency is None or currency.upper() == "USD":
        try:
            return _direct_itineraries(origin, destination, outbound_date, return_date,
                                       adults, cabin, max_stops, children, brand_rows)
        except Exception as exc:        # DirectError, or anything a page change throws
            direct_error = str(exc)
    else:
        direct_error = f"currency {currency} forced; the direct read prices in USD"
    out = _serpapi_itineraries(origin, destination, outbound_date, return_date, adults,
                               cabin, max_stops, override_budget, currency, children)
    out["source"] = "serpapi"
    out["direct_error"] = direct_error
    for row in out.get("itineraries") or []:
        row["google_check"] = _not_checked(NOT_CHECKED_SERPAPI)
    return out


def _serpapi_itineraries(origin: str, destination: str, outbound_date: str,
                         return_date: str | None = None, adults: int = 1,
                         cabin: str = "economy", max_stops: str = "any",
                         override_budget: bool = False, currency: str | None = None,
                         children: int = 0) -> dict:
    """The SerpApi path, unchanged: spends one search against the budget."""
    budget = serpapi_budget()
    if budget["spendable"] <= 0 and not override_budget:
        return {"configured": True, "budget": budget, "error": (
            f"SerpApi budget held: {budget['used']} of {budget['limit']} used this "
            f"month, keeping {budget['reserve']} in reserve. Schedules are "
            f"unaffected and free. Pass override_budget=true if this fare search "
            f"is the one worth the reserve.")}
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return {"configured": False,
                "reason": "SERPAPI_KEY is not set — connecting-itinerary search is "
                          "unavailable. Nonstop lookups still work."}

    # USD/US market whenever either end touches the US — that's the currency
    # a client comparing our fare to "what I'd pay myself" actually shops in.
    # Non-US routes keep the EUR/Spain default. Fails soft to the default on
    # an airport-lookup error; a currency mismatch is a cosmetic problem, not
    # a reason to drop the whole search.
    forced = currency
    currency, market = DEFAULT_CURRENCY, DEFAULT_MARKET
    try:
        touches_us = any(airport(code).get("country") == "US"
                         for code in (origin, destination))
        if touches_us:
            currency, market = "USD", "us"
    except Exception:
        pass
    if forced:
        currency = forced.upper()
        market = "us" if currency == "USD" else market

    params = {"engine": "google_flights", "api_key": key,
              "departure_id": origin.strip().upper(),
              "arrival_id": destination.strip().upper(),
              "outbound_date": outbound_date, "adults": adults, "children": children,
              "travel_class": CABIN_CODES.get(cabin.lower(), 1),
              "stops": STOP_CODES.get(max_stops.lower(), 0),
              "currency": currency, "hl": DEFAULT_LANGUAGE, "gl": market}
    if return_date:
        params["return_date"] = return_date
    else:
        params["type"] = 2  # one-way

    _serpapi_spend()
    try:
        response = requests.get(SERPAPI_BASE, params=params,
                                timeout=SERPAPI_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        return {"configured": True, "error": f"Could not reach SerpApi: {exc}"}
    if response.status_code >= 400:
        return {"configured": True,
                "error": f"SerpApi returned {response.status_code}: {response.text[:200]}"}
    payload = response.json()
    if payload.get("error"):
        return {"configured": True, "error": payload["error"]}

    options = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    insights = payload.get("price_insights") or {}
    return {"configured": True, "origin": origin.strip().upper(),
            "destination": destination.strip().upper(),
            "outbound_date": outbound_date, "return_date": return_date,
            "currency": currency,
            "tickets": adults + children,
            "itineraries": [_itinerary(o, adults + children) for o in options],
            "count": len(options),
            "price_context": {"lowest_party_total": insights.get("lowest_price"),
                              "level": insights.get("price_level"),
                              "typical_range_party_total": insights.get("typical_price_range"),
                              "note": "Party totals for all passengers searched."}}
