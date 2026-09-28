"""Eurostar fares, per train, from the GraphQL gateway eurostar.com itself calls.

Covers London, Paris, Brussels, Lille, Amsterdam, Rotterdam and Cologne (the
former Thalys network is Eurostar now). Needs curl_cffi: the gateway sits
behind AWS WAF, which turns away python-requests by its TLS fingerprint.
Each train lists Standard, Plus and Premier with the seats left at that price.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from . import _base
from ._base import Query

OPERATOR = "Eurostar"
GATEWAY = "https://site-api.eurostar.com/gateway"
STATIONS_API = "https://site-api.eurostar.com/search/stations/graphql"

# The public API key eurostar.com sends to its station search, the same for
# every visitor. If the station list starts failing with 403, open
# eurostar.com with the network tab open and read the x-api-key header on the
# POST to /search/stations/graphql.
STATIONS_API_KEY = "NGktEpCX5R2jYamA9WejQ5b5ryxxUhq51pg7iNXm"

# Local names for the cities Eurostar lists in English, so a query in the
# station's own language finds it.
LOCAL_NAMES = {"Brussels": ["Bruxelles", "Brussel"], "Cologne": ["Köln", "Koeln"],
               "London": ["Londres", "Londra"]}

STATIONS_QUERY = """query getStations($withDefaultConnections: Boolean) {
  stations(withDefaultConnections: $withDefaultConnections) {
    uic name country countryCode city isEurostarDirect isErDirect
  }
}"""

# Trimmed from the NewBookingSearch query the site sends; only the fields read
# below are requested.
SEARCH_QUERY = """query NewBookingSearch($origin: String!, $destination: String!,
  $outbound: String!, $currency: Currency!, $adult: Int, $productFamilies: [String] = ["PUB"],
  $contractCode: String = "EIL_ALL", $filteredClassesOfService: [ClassOfServiceEnum],
  $multipleFlexibility: Boolean = true, $prioritiseShortHaulODTrains: Boolean = true,
  $hideExternalCarrierTrains: Boolean = true, $hideDirectExternalCarrierTrains: Boolean = true) {
  journeySearch(outboundDate: $outbound, origin: $origin, destination: $destination,
    adults: $adult, productFamilies: $productFamilies, contractCode: $contractCode,
    currency: $currency, multipleFlexibility: $multipleFlexibility,
    prioritiseShortHaulODTrains: $prioritiseShortHaulODTrains) {
    outbound {
      origin { name uic }
      destination { name uic }
      journeys(hideIndirectTrainsWhenDisruptedAndCancelled: false, hideDepartedTrains: true,
               hideExternalCarrierTrains: $hideExternalCarrierTrains,
               hideDirectExternalCarrierTrains: $hideDirectExternalCarrierTrains) {
        timing { date departureTime: departs arrivalTime: arrives duration }
        fares(filteredClassesOfService: $filteredClassesOfService) {
          classOfService { name code }
          prices { displayPrice total }
          seats
          legs { serviceName serviceType { name code } }
        }
      }
    }
  }
}"""

_stations = _base.TTLCache(_base.STATION_LIST_TTL_SECONDS)


def station_candidates(raw: list[dict]) -> list[dict]:
    """Stations Eurostar runs to itself. The full list also carries hundreds of
    connecting stations sold with a partner leg; those are left out."""
    out = []
    for s in raw:
        if not (s.get("isEurostarDirect") or s.get("isErDirect")):
            continue
        city = s.get("city") or ""
        out.append({"id": s["uic"], "name": s["name"],
                    "aliases": [city, *LOCAL_NAMES.get(city, [])],
                    "group": False, "lat": None, "lon": None,
                    "country": s.get("countryCode")})
    return out


def parse(payload: dict, adults: int, currency_code: str) -> list[dict]:
    """A NewBookingSearch response as rows.

    `total` is the party total; displayPrice is per person (on 2026-09-28 two
    adults came back displayPrice 132, total 264).
    """
    bound = ((payload.get("data") or {}).get("journeySearch") or {}).get("outbound") or {}
    origin = (bound.get("origin") or {}).get("name")
    destination = (bound.get("destination") or {}).get("name")
    rows = []
    for journey in bound.get("journeys") or []:
        timing = journey.get("timing") or {}
        day = datetime.strptime(timing["date"], "%Y-%m-%d")
        departs = datetime.combine(day, datetime.strptime(timing["departureTime"], "%H:%M").time())
        arrives = datetime.combine(day, datetime.strptime(timing["arrivalTime"], "%H:%M").time())
        if arrives < departs:
            arrives += timedelta(days=1)
        fares, legs = [], []
        for f in journey.get("fares") or []:
            legs = legs or f.get("legs") or []
            prices = f.get("prices") or {}
            seats = f.get("seats")
            fares.append(_base.fare(
                (f.get("classOfService") or {}).get("name"), prices.get("total"),
                currency_code, adults, seats_left=seats,
                sold_out=not prices or seats == 0))
        fares.sort(key=lambda f: (f["sold_out"], f["price"] if f["price"] is not None else 1e9))
        if not fares:
            fares = [_base.fare("Eurostar", None, currency_code, adults, sold_out=True)]
        train = " + ".join(f"{(l.get('serviceType') or {}).get('name') or 'Eurostar'} "
                           f"{l.get('serviceName') or ''}".strip() for l in legs) or "Eurostar"
        rows.append(_base.row(OPERATOR, train, departs, arrives, origin, destination, fares,
                              changes=max(len(legs) - 1, 0),
                              duration_minutes=timing.get("duration")))
    return rows


def _load_stations(session) -> list[dict]:
    response = _base.call(session, "POST", STATIONS_API, operator=OPERATOR,
                          json={"operationName": "getStations",
                                "variables": {"withDefaultConnections": False},
                                "query": STATIONS_QUERY},
                          headers={"x-api-key": STATIONS_API_KEY,
                                   "Origin": "https://www.eurostar.com",
                                   "Referer": "https://www.eurostar.com/"})
    body = _base.json_of(response, OPERATOR)
    return ((body.get("data") or {}).get("stations")) or []


def search(q: Query) -> dict:
    session = _base.http_session(impersonate=True)
    candidates = station_candidates(_stations.get_or_load("stations",
                                                          lambda: _load_stations(session)))
    origin = _base.match_station(q.origin, candidates, OPERATOR, q.origin_hint)
    destination = _base.match_station(q.destination, candidates, OPERATOR, q.destination_hint)
    if origin["id"] == destination["id"]:
        raise _base.NoService("Eurostar: origin and destination are the same station")
    # Priced in the currency of the country the trip starts in, as the site does.
    currency_code = "GBP" if origin.get("country") == "GBR" else "EUR"
    variables = {"origin": origin["id"], "destination": destination["id"],
                 "outbound": q.date, "currency": currency_code, "adult": q.adults,
                 "filteredClassesOfService": ["STANDARD", "PLUS", "PREMIER"]}
    headers = {"x-platform": "web", "x-market-code": "uk" if currency_code == "GBP" else "fr",
               "x-source-url": "search-app/", "cid": "SRCH-" + uuid.uuid4().hex[:22],
               "Origin": "https://www.eurostar.com", "Referer": "https://www.eurostar.com/",
               "Accept-Language": "en-GB"}
    response = _base.call(session, "POST", GATEWAY, operator=OPERATOR, headers=headers,
                          json={"operationName": "NewBookingSearch", "variables": variables,
                                "query": SEARCH_QUERY})
    payload = _base.json_of(response, OPERATOR)
    if payload.get("errors") and not (payload.get("data") or {}).get("journeySearch"):
        raise _base.FareError("Eurostar: " + str(payload["errors"][0].get("message"))[:200])
    return {"origin": origin["name"], "destination": destination["name"],
            "trains": parse(payload, q.adults, currency_code)}
