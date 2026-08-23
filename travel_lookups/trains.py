"""Rail routing and availability. Read-only — nothing here books or holds.

The same split as flights.py, for the same reason:

  Transitous — what actually runs between two stations: operator, train number,
               local times, duration, changes. Free, no key, open GTFS.
There is deliberately no fare source. One existed — per-operator Apify actors
behind a $5/month ceiling — and it was deleted on 2026-08-17: the budget was
exhausted, coverage was per-operator and patchy, and Spain, the home market,
had none at all. A tool whose usual answer is "budget spent, and Renfe is not
covered anyway" teaches everyone to distrust the whole surface. Sketching an
itinerary needs times and changes; a price is confirmed at booking regardless.

WHY NOT THE OBVIOUS ONES. Scraping is closed: form-filler/adapters/train.py
probed thetrainline.com and sncf-connect.com on 2026-07-18 across headless
chromium, real Chrome and Firefox, and both hard-403 the journey search behind
a DataDome captcha. bahn.de answers OPS_BLOCKED to anything that isn't a
browser. Trainline's own Global API and Rail Europe both exist and both need a
commercial agreement. Transitous needs none of that.

This module is also the implementation behind the `trains` tools on the Mac's
travel-research MCP server, which imports it rather than keeping its own copy.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TRANSITOUS_BASE = "https://api.transitous.org/api/v1"

# Transitous is a volunteer-run public service. Identify ourselves and do not
# hammer it — this is the courtesy that keeps free infrastructure free.
USER_AGENT = "fora-itinerary-research/1.0 (michael.simon@fora.travel)"
TIMEOUT_SECONDS = 45

# Rail only. Without this the planner happily routes Madrid-Barcelona by bus,
# which is a true answer to a question nobody asked.
RAIL_MODES = "RAIL,HIGHSPEED_RAIL,LONG_DISTANCE,NIGHT_RAIL,REGIONAL_RAIL"

DEFAULT_RESULTS = 5
MAX_RESULTS = 12

# Station metadata is static; cache it so repeated sketching costs one call.
    # Directory name predates the split into travel-lookups and is deliberately
    # unchanged: renaming it would orphan the warm airport/station caches on both
    # the Mac and the travel-mcp container's /state volume, and the VPS one is
    # seeded from the Mac's.
STATION_CACHE_FILE = Path(
    os.getenv("TRAINS_CACHE_DIR", str(Path.home() / ".cache/flight-research"))
) / "stations.json"

class TrainsError(RuntimeError):
    """Raised when a rail source declines or cannot answer."""


def _load_station_cache() -> dict[str, dict]:
    try:
        return json.loads(STATION_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


_station_cache: dict[str, dict] = _load_station_cache()


def _save_station_cache() -> None:
    try:
        STATION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATION_CACHE_FILE.write_text(json.dumps(_station_cache, indent=1))
    except OSError:
        pass  # A cache write failure must never break a lookup.


def _get(path: str, params: dict) -> object:
    try:
        response = requests.get(f"{TRANSITOUS_BASE}{path}", params=params,
                                headers={"User-Agent": USER_AGENT,
                                         "Accept": "application/json"},
                                timeout=TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise TrainsError(f"Could not reach Transitous: {exc}") from exc
    if response.status_code == 429:
        raise TrainsError("Transitous rate limit hit — wait a moment and retry.")
    if response.status_code >= 400:
        raise TrainsError(f"Transitous error {response.status_code}: {response.text[:200]}")
    return response.json()


def station(query: str) -> dict:
    """Resolve a place name to a rail station, with its timezone.

    Prefers an actual STOP that rail calls at. A search for "Barcelona Sants"
    also returns a bar called Luna Sants and a metro entrance; picking by
    importance alone would sometimes route a client from a cafe.
    """
    key = query.strip().lower()
    if key in _station_cache:
        return _station_cache[key]

    results = _get("/geocode", {"text": query.strip()})
    if isinstance(results, dict):
        results = results.get("features") or []
    if not results:
        raise TrainsError(f"No station found for {query!r}.")

    rail = {"RAIL", "HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_RAIL"}
    stops = [r for r in results
             if r.get("type") == "STOP" and rail.intersection(r.get("modes") or [])]

    # NO fallback to "any stop". A bare city name returns mostly PLACEs and bus
    # stops, and relaxing the filter picked "Seville Street" — a bus stop in the
    # United Kingdom — as the origin for a Madrid-Seville train. Wrong and
    # confident is the worst answer available here.
    #
    # It usually means the English name was used: "Seville" surfaces no rail
    # stop at all, while "Sevilla" returns Sevilla-Santa Justa with an
    # importance two orders of magnitude above the noise. So say that.
    if not stops:
        raise TrainsError(
            f"No rail station matched {query!r}. Rail data is indexed under local "
            f"names — try the local spelling or the station itself (Sevilla rather "
            f"than Seville, 'Madrid Atocha', 'Milano Centrale').")

    # Importance separates a main station from a halt of the same name by orders
    # of magnitude; score is the geocoder's own relevance, used only to break ties.
    best = max(stops, key=lambda r: (r.get("importance") or 0, r.get("score") or 0))
    record = {"id": best.get("id"), "name": best.get("name"),
              "timezone": best.get("tz"), "country": best.get("country"),
              "latitude": best.get("lat"), "longitude": best.get("lon"),
              "modes": best.get("modes") or [], "query": query.strip()}
    if not record["timezone"]:
        raise TrainsError(f"Station {record['name']!r} has no timezone; refusing to guess times.")
    _station_cache[key] = record
    _save_station_cache()
    return record


def _local(utc_iso: str | None, tz_name: str | None) -> dict:
    """Transitous returns genuine UTC. Convert, never display the raw value.

    Verified 2026-08-16: asking for 05:00+02:00 returns an EARLIER departure
    than asking for 05:00Z, so offsets are honoured and the Z is real. A
    Madrid departure reported as 08:27Z is 10:27 on the platform — printing
    the raw figure would put a two-hour error in a client's itinerary.
    """
    if not utc_iso:
        return {"utc": None, "local": None, "local_date": None}
    moment = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    out = {"utc": moment.strftime("%Y-%m-%d %H:%MZ"), "local": None, "local_date": None}
    if tz_name:
        try:
            here = moment.astimezone(ZoneInfo(tz_name))
            out["local"] = here.strftime("%H:%M")
            out["local_date"] = here.strftime("%Y-%m-%d")
        except Exception:
            pass
    return out


def _leg(leg: dict) -> dict:
    origin, destination = leg.get("from") or {}, leg.get("to") or {}
    return {
        "operator": leg.get("agencyName"),
        "service": leg.get("tripShortName") or leg.get("routeShortName"),
        "kind": leg.get("mode"),
        "from": origin.get("name"),
        "to": destination.get("name"),
        "departs": _local(origin.get("departure"), origin.get("tz")),
        "arrives": _local(destination.get("arrival"), destination.get("tz")),
        "duration": _hhmm(leg.get("duration")),
        "reservation_required": leg.get("reservation"),
        "cancelled": leg.get("cancelled", False),
        "live": leg.get("realTime", False),
    }


def _hhmm(seconds: int | None) -> str | None:
    if not seconds or seconds <= 0:
        return None
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def journeys(origin: str, destination: str, date: str,
             depart_after: str = "07:00", max_results: int = DEFAULT_RESULTS) -> dict:
    """Rail services between two stations on a date, walking legs stripped.

    `date` and `depart_after` are LOCAL to the origin station — which is the
    only interpretation a traveller ever means.
    """
    max_results = max(1, min(int(max_results), MAX_RESULTS))
    origin_station, destination_station = station(origin), station(destination)

    try:
        naive = datetime.strptime(f"{date} {depart_after}", "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise TrainsError(
            f"date must be YYYY-MM-DD and depart_after HH:MM, got {date!r} {depart_after!r}"
        ) from exc
    departure = naive.replace(tzinfo=ZoneInfo(origin_station["timezone"]))

    payload = _get("/plan", {
        "fromPlace": origin_station["id"], "toPlace": destination_station["id"],
        "time": departure.isoformat(), "numItineraries": max_results,
        "transitModes": RAIL_MODES})

    options = []
    for itinerary in (payload.get("itineraries") or [])[:max_results]:
        legs = [_leg(leg) for leg in itinerary.get("legs") or []
                if leg.get("mode") != "WALK"]
        if not legs:
            continue  # a walk-only "journey" between two stations is noise
        options.append({
            "legs": legs,
            "changes": max(len(legs) - 1, 0),
            "duration": _hhmm(itinerary.get("duration")),
            "departs": legs[0]["departs"],
            "arrives": legs[-1]["arrives"],
            "operators": sorted({l["operator"] for l in legs if l["operator"]}),
        })

    return {
        "origin": origin_station, "destination": destination_station,
        "date": date, "depart_after": depart_after,
        "journeys": options, "count": len(options),
        "note": ("Scheduled rail service, local times at each station. No fares "
                 "and no seat availability — zero here means no rail service "
                 "found, not missing data."),
    }
