"""Italo fares, per train, from the booking API behind biglietti.italotreno.com.

The most fragile operator here. Akamai guards it, so it needs curl_cffi, and
the search is asynchronous: an anonymous login creates a working session,
POST /booking returns an operationId, and the result has to be polled.
Polling is bounded by POLL_BUDGET_SECONDS so a slow Italo never holds the
tool open. The flow opens a booking context on Italo's side the way the
site's search does; nothing is selected, held or paid for.
"""

from __future__ import annotations

import time
import uuid

from . import _base
from ._base import Query

OPERATOR = "Italo"
SITE = "https://biglietti.italotreno.com"
API = "https://api-biglietti.italotreno.com/api/v1"
POLL_INTERVAL_SECONDS = 1.5
POLL_BUDGET_SECONDS = 18

# productClass codes as the site labels them.
CLASSES = {"S": "Smart", "S1": "Smart XL", "S2": "Smart Salotto",
           "P": "Prima", "C": "Club Executive"}

_stations = _base.TTLCache(_base.STATION_LIST_TTL_SECONDS)


def station_candidates(raw: dict) -> list[dict]:
    """Italo's station map as candidates: code -> {name, isDisabled}. It lists
    every Italian station and bus stop Italo sells to; disabled codes and bus
    stops are left out. "Roma (Tutte)" (RM0) is the all-stations code."""
    out = []
    for code, s in raw.items():
        name = (s or {}).get("name") or ""
        if s.get("isDisabled") or name.upper().endswith(" BUS"):
            continue
        group = "(tutte)" in name.lower()
        aliases = [name.split("(")[0].strip()] if group else []
        out.append({"id": code, "name": name, "aliases": aliases, "group": group,
                    "lat": None, "lon": None})
    return out


def parse(payload: dict, adults: int, station_names: dict[str, str]) -> list[dict]:
    """A finished booking-status payload as rows.

    fullPaxDiscountPrice is the price for all passengers of that type, so the
    sum over paxFares is the party total. Fares are the cheapest per
    (class, offer), and only those with seats left; seats_left is Italo's
    availableCount for that fare.
    """
    rows = []
    for trip in payload.get("trips") or []:
        for solution in trip.get("travelSolutions") or []:
            for journey in solution.get("journeys") or []:
                segments = journey.get("segments") or []
                if len(segments) != 1:
                    # Italo's own connections are rare and priced per segment;
                    # summing segment fares would invent a price the site
                    # never offers. Only direct trains are reported.
                    continue
                best: dict[tuple, dict] = {}
                for segment in segments:
                    for f in segment.get("fares") or []:
                        count = f.get("availableCount") or 0
                        if count <= 0:
                            continue
                        total = sum(p.get("fullPaxDiscountPrice") or 0 for p in f.get("paxFares") or [])
                        key = (f.get("productClass"), f.get("offerType"))
                        if key not in best or total < best[key]["price"]:
                            best[key] = {"price": total, "count": count}
                fares = [_base.fare(offer, v["price"], "EUR", adults,
                                    cls=CLASSES.get(cls, cls),
                                    seats_left=v["count"])
                         for (cls, offer), v in best.items()]
                fares.sort(key=lambda f: f["price"])
                if not fares:
                    fares = [_base.fare("Italo", None, "EUR", adults, sold_out=True)]
                first, last = segments[0], segments[-1]
                rows.append(_base.row(
                    OPERATOR,
                    " + ".join(f"Italo {s.get('trainNumber')}" for s in segments),
                    _base.parse_local(first["std"]), _base.parse_local(last["sta"]),
                    station_names.get(first.get("departureStation")),
                    station_names.get(last.get("arrivalStation")),
                    fares, changes=len(segments) - 1))
    return rows


def search(q: Query) -> dict:
    session = _base.http_session(impersonate=True)
    _base.call(session, "GET", f"{SITE}/it", operator=OPERATOR)
    working_id = str(uuid.uuid4())
    _base.call(session, "POST", f"{SITE}/api/login", operator=OPERATOR,
               json={"isAnonymous": True, "workingId": working_id})
    token = session.cookies.get("BIGSessionToken")
    if not token:
        raise _base.Blocked("Italo's anonymous login set no session token")
    headers = {"Authorization": f"Bearer {token}", "X-BIG-working-session-id": working_id,
               "Origin": SITE, "Referer": SITE + "/"}
    raw = _stations.get_or_load("stations", lambda: _base.json_of(_base.call(
        session, "POST", f"{API}/stations/list", operator=OPERATOR,
        json={"culture": "it-IT"}), OPERATOR))
    candidates = station_candidates(raw)
    origin = _base.match_station(q.origin, candidates, OPERATOR, q.origin_hint)
    destination = _base.match_station(q.destination, candidates, OPERATOR, q.destination_hint)
    body = {"isRoundTrip": False, "departureStation": origin["id"],
            "arrivalStation": destination["id"], "departureDate": q.date, "promoCode": "",
            "passengersAges": None, "seniorPassengers": 0, "adultPassengers": q.adults,
            "youngPassengers": 0, "childPassengers": 0, "culture": "it-IT",
            "referrerURL": "", "promocodeAlias": "", "hasPet": False, "showBestPrices": True,
            "showPrivateOffers": False, "employeeOffer": None, "portalType": "B2C",
            "isExclusive": False, "flowType": "Booking"}
    started = _base.json_of(_base.call(session, "POST", f"{API}/booking", operator=OPERATOR,
                                       json=body, headers=headers), OPERATOR)
    operation = started.get("operationId")
    if not operation:
        raise _base.FareError("Italo started no search operation")
    deadline = time.monotonic() + POLL_BUDGET_SECONDS
    payload = None
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        # Not _base.call: a 202 or 404 while the search runs is normal here.
        try:
            response = session.get(f"{API}/booking/status/{operation}", headers=headers,
                                   timeout=_base.HTTP_TIMEOUT_SECONDS)
        except Exception as exc:
            raise _base.FareError(f"could not reach Italo: {exc}") from exc
        if response.status_code == 200 and '"trips"' in response.text:
            payload = _base.json_of(response, OPERATOR)
            break
        if response.status_code in (401, 403):
            raise _base.Blocked(f"Italo refused the status poll (HTTP {response.status_code})")
    if payload is None:
        raise _base.OperatorTimeout(f"Italo's search did not finish within "
                                    f"{POLL_BUDGET_SECONDS}s")
    names = {code: (s or {}).get("name") for code, s in raw.items()}
    return {"origin": origin["name"], "destination": destination["name"],
            "trains": parse(payload, q.adults, names)}
