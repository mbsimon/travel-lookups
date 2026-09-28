"""SBB fares, per connection, from the GraphQL endpoint behind sbb.ch's web shop.

Plain HTTPS, no login. Three queries, as the timetable page runs them:
Places (name to station id), Trips (connections), then TripPrices once per
class for all trips at once.

Two traps, both verified on 2026-09-28:

  * TripPrices without an explicit reduction prices the traveler as a
    Half-Fare card holder, which is roughly half the real price. Every call
    sends reductions ["NONE"].
  * TripPrices is the web shop's price hint and prices ONE traveler; sent two
    passengers it answers with no prices at all. The per-person price is
    multiplied by `adults`, which matches how SBB sells point-to-point tickets.

Prices arrive in centimes.
"""

from __future__ import annotations

import uuid

from . import _base
from ._base import Query

OPERATOR = "SBB"
ENDPOINT = "https://graphql.www.sbb.ch/"
# The web shop's Apollo client identity, sent by sbb.ch on every query.
HEADERS = {"apollographql-client-name": "sbb-webshop-home",
           "apollographql-client-version": "18.2.2",
           "apollographql-client-origin": "https://www.sbb.ch",
           "Origin": "https://www.sbb.ch",
           "Accept": "application/graphql-response+json"}

PLACES = ("query Places($input: PlaceInput, $language: LanguageEnum!) { "
          "places(input: $input, language: $language) { id name } }")
TRIPS = """query Trips($input: TripInput!, $pagingCursor: String, $language: LanguageEnum!) {
  trips(tripInput: $input, pagingCursor: $pagingCursor, language: $language) {
    trips { id summary { departure { time } arrival { time } }
      legs { ... on PTRideLeg { serviceJourney { serviceProducts { name number } } } } } } }"""
PRICES = ("query TripPrices($processId: ID!, $input: TripPricesQueryInput!) { "
          "tripPrices(processId: $processId, input: $input) { tripId "
          "tripPrices { price { amount currency } travelClass afterSaleFlexibility } } }")
TRANSPORT_MODES = ["HIGH_SPEED_TRAIN", "INTERCITY", "INTERREGIO", "REGIO",
                   "URBAN_TRAIN", "SPECIAL_TRAIN"]
CLASSES = {"SECOND": "2nd", "FIRST": "1st"}


_places = _base.TTLCache(_base.STATION_LIST_TTL_SECONDS)


def _flexibility(code: str | None) -> str:
    """SBB's afterSaleFlexibility code as words: FULL_FLEXIBLE -> fully flexible."""
    return (code or "SBB").replace("_", " ").lower().replace("full flexible", "fully flexible")


def _gql(session, query: str, variables: dict) -> dict:
    response = _base.call(session, "POST", ENDPOINT, operator=OPERATOR, headers=HEADERS,
                          json={"query": query, "variables": variables})
    body = _base.json_of(response, OPERATOR)
    if body.get("errors"):
        raise _base.FareError("SBB: " + str(body["errors"][0].get("message"))[:200])
    return body.get("data") or {}


def _resolve(session, query: str, hint: dict | None) -> dict:
    """SBB's own place search; its first hit is the station (Zürich HB before
    the tram stop Zürich, Bahnhofplatz/HB)."""
    def load(text):
        return _gql(session, PLACES, {"input": {"type": "NAME", "value": text},
                                      "language": "EN"}).get("places") or []
    found = _places.get_or_load(_base.normalize(query), lambda: load(query))
    if not found and hint and hint.get("name"):
        found = _places.get_or_load(_base.normalize(hint["name"]), lambda: load(hint["name"]))
    if not found:
        raise _base.NoService(f"SBB has no station matching {query!r}")
    return found[0]


def parse(trips: list[dict], prices: dict[str, dict], adults: int,
          origin: str, destination: str) -> list[dict]:
    """Trips plus {trip_id: {class: [tripPrices]}} as rows."""
    rows = []
    for trip in trips:
        legs = [l for l in trip.get("legs") or [] if l and l.get("serviceJourney")]
        names = [p.get("name") for l in legs
                 for p in (l["serviceJourney"].get("serviceProducts") or [])[:1]]
        fares = []
        for travel_class, options in (prices.get(trip["id"]) or {}).items():
            for option in options or []:
                amount = (option.get("price") or {}).get("amount")
                fares.append(_base.fare(
                    _flexibility(option.get("afterSaleFlexibility")),
                    None if amount is None else amount / 100 * adults,
                    (option.get("price") or {}).get("currency"), adults,
                    cls=CLASSES.get(travel_class, travel_class)))
        fares.sort(key=lambda f: f["price"] if f["price"] is not None else 1e9)
        if not fares:
            # No price is what SBB's web shop shows for trips it does not
            # sell online; it does not mean the train is full.
            fares = [_base.fare("no price returned", None, "CHF", adults)]
        summary = trip.get("summary") or {}
        rows.append(_base.row(
            OPERATOR, " + ".join(n for n in names if n) or "SBB",
            _base.parse_local(summary["departure"]["time"]),
            _base.parse_local(summary["arrival"]["time"]),
            origin, destination, fares, changes=max(len(legs) - 1, 0)))
    return rows


def search(q: Query) -> dict:
    session = _base.http_session()
    origin = _resolve(session, q.origin, q.origin_hint)
    destination = _resolve(session, q.destination, q.destination_hint)
    trips = _gql(session, TRIPS, {"input": {
        "directConnection": False, "occupancy": "ALL", "walkSpeed": 100,
        "includeTransportModes": TRANSPORT_MODES,
        "places": [{"type": "ID", "value": origin["id"]},
                   {"type": "ID", "value": destination["id"]}],
        "time": {"date": q.date, "time": q.depart_after or "06:00"}},
        "language": "EN"}).get("trips", {}).get("trips") or []
    prices: dict[str, dict] = {}
    if trips:
        process = str(uuid.uuid4())
        for travel_class in CLASSES:
            data = _gql(session, PRICES, {"processId": process, "input": {
                "tripIds": [t["id"] for t in trips], "travelClass": travel_class,
                "passengers": [{"reductions": ["NONE"]}]}})
            for entry in data.get("tripPrices") or []:
                prices.setdefault(entry["tripId"], {})[travel_class] = entry.get("tripPrices")
    return {"origin": origin["name"], "destination": destination["name"],
            "trains": parse(trips, prices, q.adults, origin["name"], destination["name"])}
