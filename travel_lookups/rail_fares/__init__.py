"""Live rail fares, per train, read directly from each operator. Read-only.

Trainline and SNCF Connect sit behind DataDome and cannot be read headless
(see trains.py), so this goes operator by operator, each through the API its
own website or app calls:

    Spain        Renfe, Iryo, Ouigo España
    Italy        Trenitalia, Italo
    France       Ouigo France (SNCF's own TGV inOui fares are not covered)
    UK/BE/NL/DE  Eurostar
    Germany      DB (which also prices many cross-border trips)
    Switzerland  SBB
    Austria      ÖBB

rail_fares() places both stations with Transitous (trains.station) to learn
their countries, picks the operators that serve that pair, and asks them all
at once on a thread pool. Each operator resolves the names to its own station
ids and fails on its own: a blocked, throttled or broken operator becomes one
line in `operators`, and the rest still answer.

Every price is a live public fare for the whole party, as the operator quoted
it at that moment. Nothing is held or booked; fares move with demand.
"""

from __future__ import annotations

import concurrent.futures as futures
import importlib.util
import time
from dataclasses import dataclass
from datetime import date as _date, datetime
from typing import Callable

from .. import trains
from . import _base, db, eurostar, iryo, italo, oebb, ouigo, renfe, sbb, trenitalia
from ._base import FareError, Query

# The whole call answers within this, whatever the operators do. An operator
# still running at the deadline is reported as "timeout" and its thread is
# left to finish on its own HTTP timeout.
OVERALL_BUDGET_SECONDS = 35

# Successful answers are reused for this long. A person comparing trains
# re-asks the same route within minutes, and the operators' fares do not move
# that fast. Renfe keeps its own cache as well, because of its throttle.
RESULT_TTL_SECONDS = 10 * 60

MAX_ADULTS = 9


class RailFaresError(trains.TrainsError):
    """Raised when the question itself cannot be asked (bad date, unplaceable
    station). Operator failures never raise; they are reported per operator."""


@dataclass(frozen=True)
class Operator:
    key: str
    name: str
    search: Callable[[Query], dict]
    needs_curl_cffi: bool


OPERATORS: dict[str, Operator] = {op.key: op for op in (
    Operator("renfe", "Renfe", renfe.search, False),
    Operator("iryo", "Iryo", iryo.search, True),
    Operator("ouigo_es", "Ouigo España", ouigo.search_es, False),
    Operator("ouigo_fr", "Ouigo France", ouigo.search_fr, False),
    Operator("trenitalia", "Trenitalia", trenitalia.search, False),
    Operator("italo", "Italo", italo.search, True),
    Operator("eurostar", "Eurostar", eurostar.search, True),
    Operator("db", "DB", db.search, True),
    Operator("sbb", "SBB", sbb.search, False),
    Operator("oebb", "ÖBB", oebb.search, True),
)}

# Countries are the ISO codes Transitous reports for a station.
DOMESTIC = {
    "ES": ["renfe", "iryo", "ouigo_es"],
    "IT": ["trenitalia", "italo"],
    "FR": ["ouigo_fr"],
    "DE": ["db"],
    "CH": ["sbb"],
    "AT": ["oebb"],
}
# Eurostar's network: London, Paris, Lille, Brussels, Amsterdam, Rotterdam and
# the Cologne/Dortmund line.
EUROSTAR_COUNTRIES = {"GB", "FR", "BE", "NL", "DE"}
# National operators whose own shop sells trips across their border.
CROSS_BORDER = {"DE": "db", "CH": "sbb", "AT": "oebb", "IT": "trenitalia", "ES": "renfe"}
# Countries DB prices international trips between.
DB_REACH = {"DE", "AT", "CH", "NL", "BE", "FR", "DK", "PL", "CZ", "LU", "IT", "HU"}

_results = _base.TTLCache(RESULT_TTL_SECONDS)


def curl_cffi_installed() -> bool:
    return importlib.util.find_spec("curl_cffi") is not None


def select_operators(origin_country: str | None, destination_country: str | None) -> list[str]:
    """Operators to ask for a trip between two countries, most local first."""
    a, b = (origin_country or "").upper(), (destination_country or "").upper()
    if not a or not b:
        return []
    if a == b:
        return list(DOMESTIC.get(a, []))
    chosen = []
    if a in EUROSTAR_COUNTRIES and b in EUROSTAR_COUNTRIES:
        chosen.append("eurostar")
    for country in (a, b):
        key = CROSS_BORDER.get(country)
        if key and key not in chosen:
            chosen.append(key)
    if a in DB_REACH and b in DB_REACH and "db" not in chosen:
        chosen.append("db")
    return chosen


def status() -> dict:
    """What can run here, for a health check. Makes no network call."""
    has_cffi = curl_cffi_installed()
    return {
        "curl_cffi": has_cffi,
        "operators": {k: ("ready" if has_cffi or not op.needs_curl_cffi
                          else "unavailable: curl_cffi not installed")
                      for k, op in OPERATORS.items()},
        "renfe_cooldown_seconds": max(0, int(renfe._cooldown_until - time.monotonic())),
    }


def _validate(date: str, adults: int, depart_after: str | None) -> None:
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise RailFaresError(f"date must be YYYY-MM-DD, got {date!r}") from exc
    if day < _date.today():
        raise RailFaresError(f"{date} is in the past")
    if not 1 <= int(adults) <= MAX_ADULTS:
        raise RailFaresError(f"adults must be 1-{MAX_ADULTS}, got {adults}")
    if depart_after is not None:
        try:
            datetime.strptime(depart_after, "%H:%M")
        except ValueError as exc:
            raise RailFaresError(f"depart_after must be HH:MM, got {depart_after!r}") from exc


def _place(name: str) -> tuple[dict | None, str | None]:
    """Where a station or city is, for choosing operators.

    A rail stop from trains.station() when there is one. A bare city name
    ("Paris", "Madrid") stopped resolving to a stop upstream in August 2026,
    but the same geocoder still ranks the city itself first, and its country
    is all operator selection needs.
    """
    try:
        return trains.station(name), None
    except trains.TrainsError as exc:
        problem = str(exc)
    try:
        results = trains._get("/geocode", {"text": name.strip()})
    except trains.TrainsError:
        return None, problem
    if isinstance(results, dict):
        results = results.get("features") or []
    for r in results:
        if r.get("country"):
            return {"name": r.get("name"), "country": r.get("country"),
                    "latitude": r.get("lat"), "longitude": r.get("lon"),
                    "timezone": r.get("tz"), "placed_by": "geocoder city match"}, None
    return None, problem


def _run(op: Operator, q: Query) -> dict:
    """One operator's search, as a status line plus its rows. Never raises."""
    started = time.monotonic()
    line = {"operator": op.name, "key": op.key}
    if op.needs_curl_cffi and not curl_cffi_installed():
        return {**line, "status": "unavailable",
                "message": "curl_cffi not installed (pip install 'travel-lookups[rail]')",
                "trains": 0, "rows": []}
    key = (op.key, _base.normalize(q.origin), _base.normalize(q.destination),
           q.date, q.adults, q.depart_after)
    result = _results.get(key)
    line["cached"] = result is not None
    try:
        if result is None:
            result = op.search(q)
            _results.put(key, result)
    except FareError as exc:
        return {**line, "status": exc.status, "message": str(exc), "trains": 0, "rows": [],
                "seconds": round(time.monotonic() - started, 1)}
    except Exception as exc:  # a parser meeting a changed response, most likely
        return {**line, "status": "error", "message": f"{type(exc).__name__}: {exc}"[:300],
                "trains": 0, "rows": [], "seconds": round(time.monotonic() - started, 1)}
    rows = result.get("trains") or []
    if q.depart_after:
        rows = [r for r in rows if r["date"] > q.date or r["departs"] >= q.depart_after]
    line.update({"origin": result.get("origin"), "destination": result.get("destination"),
                 "trains": len(rows), "rows": rows,
                 "seconds": round(time.monotonic() - started, 1)})
    if rows:
        line["status"] = "ok"
    else:
        line["status"] = "no_service"
        line["message"] = (f"{op.name} returned no trains for this pair on {q.date}"
                           + (f" after {q.depart_after}" if q.depart_after else ""))
    return line


def rail_fares(origin: str, destination: str, date: str, adults: int = 1,
               depart_after: str | None = None, operators: list[str] | None = None,
               max_results: int | None = None) -> dict:
    """Live per-train fares between two stations from every operator that serves them.

    `origin` and `destination` are station or city names in local spelling
    ("Madrid", "Barcelona Sants", "Milano Centrale", "Zürich HB"). `date` and
    `depart_after` (HH:MM) are local to the origin. `operators` narrows or
    overrides the choice with keys from OPERATORS. `max_results` caps the
    merged list after sorting by departure. `cheapest` has one entry per
    currency among the trains returned.
    """
    _validate(date, adults, depart_after)
    unknown = [k for k in operators or [] if k not in OPERATORS]
    if unknown:
        raise RailFaresError(f"unknown operator(s) {unknown}; known: {sorted(OPERATORS)}")
    origin_hint, origin_problem = _place(origin)
    destination_hint, destination_problem = _place(destination)

    if operators:
        chosen = list(dict.fromkeys(operators))
    else:
        if origin_hint is None or destination_hint is None:
            raise RailFaresError(
                (origin_problem or destination_problem or "station not found")
                + " Without a placed station there is no way to pick operators; "
                  "pass operators=[...] to ask specific ones.")
        chosen = select_operators(origin_hint.get("country"), destination_hint.get("country"))

    q = Query(origin=origin, destination=destination, date=date, adults=int(adults),
              depart_after=depart_after, origin_hint=origin_hint,
              destination_hint=destination_hint)
    lines = []
    if chosen:
        pool = futures.ThreadPoolExecutor(max_workers=len(chosen), thread_name_prefix="rail-fares")
        running = {pool.submit(_run, OPERATORS[k], q): k for k in chosen}
        done, _ = futures.wait(running, timeout=OVERALL_BUDGET_SECONDS)
        by_key = {running[f]: f.result() for f in done}
        pool.shutdown(wait=False, cancel_futures=True)
        for k in chosen:
            lines.append(by_key.get(k) or {
                "operator": OPERATORS[k].name, "key": k, "status": "timeout",
                "message": f"no answer within {OVERALL_BUDGET_SECONDS}s", "trains": 0,
                "rows": []})

    rows = sorted((r for line in lines for r in line.pop("rows")),
                  key=lambda r: (r["date"], r["departs"], r["operator"]))
    total = len(rows)
    if max_results is not None and total > max_results:
        rows = rows[:max_results]
    # One cheapest train per currency: a CHF SBB fare and a EUR Trenitalia
    # fare on Zürich-Milano are not comparable without a conversion rate.
    cheapest: dict[str, dict] = {}
    for r in rows:
        if r["cheapest"]:
            cur = r["cheapest"]["currency"]
            if cur not in cheapest or r["cheapest"]["price"] < cheapest[cur]["cheapest"]["price"]:
                cheapest[cur] = r

    result = {
        "origin": origin, "destination": destination, "date": date, "adults": int(adults),
        "depart_after": depart_after,
        "countries": [(origin_hint or {}).get("country"), (destination_hint or {}).get("country")],
        "trains": rows, "count": len(rows),
        "cheapest": [{k: r[k] for k in ("operator", "train", "departs", "arrives")}
                     | {"fare": r["cheapest"]} for r in cheapest.values()],
        "operators": lines,
        "note": ("Live public fares read from each operator's own site at the time of "
                 "this call. They change with demand and nothing is held or booked. "
                 "'price' is for the whole party; 'per_person' divides it. Currencies "
                 "differ by operator (EUR, GBP, CHF). An operator marked blocked, "
                 "throttled, unavailable, timeout or error is a gap in this tool on this "
                 "call and says nothing about whether it runs the route."),
    }
    if total > len(rows):
        result["truncated"] = f"{total - len(rows)} later trains not shown; narrow with depart_after"
    if not chosen:
        result["note"] = ("No operator covered here serves this country pair "
                          f"({result['countries'][0]} to {result['countries'][1]}). That is "
                          "a gap in this tool, not a fact about the route; rail_journeys "
                          "still shows what runs.")
    return result


__all__ = ["rail_fares", "select_operators", "status", "OPERATORS", "RailFaresError"]
