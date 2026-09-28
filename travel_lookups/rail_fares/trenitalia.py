"""Trenitalia fares (Frecciarossa, Intercity, regionals), per solution, from
the lefrecce.it web BFF. Plain HTTPS, no login.

Stations resolve through the site's own location search. A solution is one
train or a connection; for a single train the fare grid lists every service
level (Standard, Premium, Business, Executive) with each offer (Base,
Economy, Super Economy). For a connection only the solution's cheapest total
is reported, since per-leg grids do not add up to a bookable price.
"""

from __future__ import annotations

from datetime import datetime

from . import _base
from ._base import Query

OPERATOR = "Trenitalia"
BASE = "https://www.lefrecce.it/Channels.Website.BFF.WEB/website"
# The BFF returns at most 10 solutions a page whatever the limit asks for;
# three pages cover a morning into the afternoon.
PAGE_SIZE = 10
PAGES = 3

# Offers that need an age or a loyalty card. Quoting them as a price anyone can
# buy would be wrong, so they are left out.
RESTRICTED_OFFERS = ("young", "senior", "giovani", "carta", "over", "junior")

_locations = _base.TTLCache(_base.STATION_LIST_TTL_SECONDS)


def _headers() -> dict:
    # An explicit Content-Type header drew an Akamai 403 on 2026-09-28 where
    # the same body without it (requests sets it from json=) went through.
    return {"User-Agent": _base.BROWSER_UA, "Accept": "application/json"}


def _locate(session, query: str) -> list[dict]:
    def load():
        response = _base.call(session, "GET", f"{BASE}/locations/search", operator=OPERATOR,
                              headers=_headers(), params={"name": query, "limit": 10})
        return _base.json_of(response, OPERATOR) or []
    return _locations.get_or_load(_base.normalize(query), load)


def station_candidates(raw: list[dict]) -> list[dict]:
    out = []
    for s in raw:
        name = s.get("displayName") or s.get("name") or ""
        group = bool(s.get("multistation"))
        aliases = [name.split("(")[0].strip()] if group else []
        out.append({"id": s["id"], "name": name, "aliases": aliases, "group": group,
                    "lat": None, "lon": None})
    return out


def _resolve(session, query: str, hint: dict | None) -> dict:
    found = _locate(session, query)
    if not found and hint and hint.get("name"):
        found = _locate(session, hint["name"])
    if not found:
        raise _base.NoService(f"Trenitalia has no station matching {query!r}")
    return _base.match_station(query, station_candidates(found), OPERATOR, hint)


def parse(payload: dict, adults: int) -> list[dict]:
    """A ticket/solutions response as rows. Prices are party totals (checked
    2026-09-28: the same train was 54.90 for one adult and 109.80 for two)."""
    rows = []
    for item in payload.get("solutions") or []:
        solution = item.get("solution") or {}
        trains = solution.get("trains") or []
        name = " + ".join(f"{t.get('acronym') or t.get('trainCategory') or ''} "
                          f"{t.get('name') or t.get('description') or ''}".strip()
                          for t in trains) or "Trenitalia"
        grids = item.get("grids") or []
        fares = _grid_fares(grids[0], adults) if len(grids) == 1 else []
        if not fares:
            price = solution.get("price") or {}
            saleable = solution.get("status") == "SALEABLE"
            fares = [_base.fare("cheapest available" if price.get("amount") is not None
                                else "no price returned", price.get("amount"),
                                price.get("currency") or "EUR", adults, sold_out=not saleable)]
        rows.append(_base.row(
            OPERATOR, name,
            _base.parse_local(solution["departureTime"]),
            _base.parse_local(solution["arrivalTime"]),
            solution.get("origin"), solution.get("destination"), fares,
            changes=max(len(trains) - 1, 0)))
    return rows


def _grid_fares(grid: dict, adults: int) -> list[dict]:
    fares = []
    for service in grid.get("services") or []:
        for offer in service.get("offers") or []:
            if any(word in (offer.get("name") or "").lower() for word in RESTRICTED_OFFERS):
                continue
            price = offer.get("price") or {}
            available = offer.get("availableAmount")
            fares.append(_base.fare(
                offer.get("name"), price.get("amount"), price.get("currency"), adults,
                cls=service.get("name"),
                # 32767 is the "unlimited" sentinel on regional tickets.
                seats_left=available if isinstance(available, int) and available < 1000 else None,
                sold_out=offer.get("status") == "SOLD_OUT" or not price.get("amount")))
    fares.sort(key=lambda f: (f["sold_out"], f["price"] if f["price"] is not None else 1e9))
    return fares


def search(q: Query) -> dict:
    session = _base.http_session()
    origin = _resolve(session, q.origin, q.origin_hint)
    destination = _resolve(session, q.destination, q.destination_hint)
    when = datetime.strptime(f"{q.date} {q.depart_after or '06:00'}", "%Y-%m-%d %H:%M")
    solutions = []
    for page in range(PAGES):
        body = {"departureLocationId": origin["id"], "arrivalLocationId": destination["id"],
                "departureTime": when.strftime("%Y-%m-%dT%H:%M:00.000"),
                "adults": q.adults, "children": 0,
                "criteria": {"frecceOnly": False, "regionalOnly": False, "noChanges": False,
                             "order": "DEPARTURE_DATE", "limit": PAGE_SIZE,
                             "offset": page * PAGE_SIZE},
                "advancedSearchRequest": {"bestFare": False}}
        batch = (_base.json_of(_post(session, body), OPERATOR).get("solutions")) or []
        solutions += batch
        if len(batch) < PAGE_SIZE:
            break
    return {"origin": origin["name"], "destination": destination["name"],
            "trains": parse({"solutions": solutions}, q.adults)}


def _post(session, body: dict):
    try:
        return _base.call(session, "POST", f"{BASE}/ticket/solutions", operator=OPERATOR,
                          headers=_headers(), json=body)
    except _base.Blocked as refused:
        # Akamai sometimes refuses a request it passes a moment later; one
        # retry with Chrome's TLS fingerprint when curl_cffi is installed.
        try:
            retry = _base.http_session(impersonate=True)
        except _base.Unavailable:
            raise refused from None
        return _base.call(retry, "POST", f"{BASE}/ticket/solutions", operator=OPERATOR,
                          json=body)
