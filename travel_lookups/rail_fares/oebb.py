"""ÖBB fares, per connection, from the JSON API behind shop.oebbtickets.at.

The shop hands any visitor an anonymous token; with it the timetable and the
price endpoint answer without a login. Cloudflare rejects python-requests by
its TLS fingerprint, so this needs curl_cffi. ÖBB returns one price per
connection, the cheapest it offers for the party.
"""

from __future__ import annotations

from datetime import datetime

from . import _base
from ._base import Query

OPERATOR = "ÖBB"
BASE = "https://shop.oebbtickets.at"
MAX_CONNECTIONS = 8

_stations = _base.TTLCache(_base.STATION_LIST_TTL_SECONDS)


def _session():
    session = _base.http_session(impersonate=True)
    session.headers.update({"Accept": "application/json", "Channel": "inet"})
    token = _base.json_of(_base.call(session, "GET", f"{BASE}/api/domain/v1/anonymousToken",
                                     operator=OPERATOR), OPERATOR).get("access_token")
    if not token:
        raise _base.Blocked("ÖBB issued no anonymous token")
    session.headers["accesstoken"] = token
    _base.call(session, "POST", f"{BASE}/api/domain/v1/initUserData", operator=OPERATOR, json={})
    return session


def _resolve(session, query: str, hint: dict | None) -> dict:
    def load(text):
        return _base.json_of(_base.call(session, "GET", f"{BASE}/api/hafas/v1/stations",
                                        operator=OPERATOR, params={"name": text, "count": 3}),
                             OPERATOR) or []
    found = _stations.get_or_load(_base.normalize(query), lambda: load(query))
    if not found and hint and hint.get("name"):
        found = _stations.get_or_load(_base.normalize(hint["name"]), lambda: load(hint["name"]))
    found = [s for s in found if s.get("number")]
    if not found:
        raise _base.NoService(f"ÖBB has no station matching {query!r}")
    # A city-wide meta station ("Wien") has an empty name and the city in
    # `meta`; the timetable call refuses an empty name.
    return {**found[0], "name": found[0].get("name") or found[0].get("meta")}


def parse(connections: list[dict], prices: dict[str, dict], adults: int) -> list[dict]:
    """Timetable connections plus {connectionId: offer} as rows. The offer's
    price is for all the passengers the search named."""
    rows = []
    for connection in connections:
        sections = [s for s in connection.get("sections") or [] if s.get("category")]
        names = [f"{s['category'].get('displayName') or s['category'].get('name') or ''} "
                 f"{s['category'].get('number') or ''}".strip() for s in sections]
        offer = prices.get(connection.get("id")) or {}
        fare_ok = offer.get("price") is not None and not offer.get("offerError")
        fares = [_base.fare("cheapest available" if fare_ok else "no price returned",
                            offer.get("price") if fare_ok else None, "EUR", adults,
                            cls="1st" if offer.get("firstClass") else "2nd")]
        origin, destination = connection.get("from") or {}, connection.get("to") or {}
        rows.append(_base.row(
            OPERATOR, " + ".join(names) or "ÖBB",
            _base.parse_local(origin["departure"]), _base.parse_local(destination["arrival"]),
            origin.get("name"), destination.get("name"), fares,
            changes=max(len(sections) - 1, 0)))
    return rows


def search(q: Query) -> dict:
    session = _session()
    origin = _resolve(session, q.origin, q.origin_hint)
    destination = _resolve(session, q.destination, q.destination_hint)
    when = datetime.strptime(f"{q.date} {q.depart_after or '06:00'}", "%Y-%m-%d %H:%M")
    passengers = [{"me": i == 0, "remembered": False, "type": "ADULT", "id": i + 1,
                   "cards": [], "relations": [], "isSelected": True}
                  for i in range(q.adults)]
    body = {"reverse": False, "datetimeDeparture": when.strftime("%Y-%m-%dT%H:%M:00.000"),
            "filter": {"trains": True}, "passengers": passengers, "count": MAX_CONNECTIONS,
            "from": {"name": origin["name"], "number": origin["number"]},
            "to": {"name": destination["name"], "number": destination["number"]},
            "timeout": {}}
    timetable = _base.json_of(_base.call(session, "POST", f"{BASE}/api/hafas/v4/timetable",
                                         operator=OPERATOR, json=body), OPERATOR)
    connections = timetable.get("connections") or []
    prices = {}
    if connections:
        response = _base.call(session, "GET", f"{BASE}/api/offer/v1/prices", operator=OPERATOR,
                              params=[("connectionIds[]", c["id"]) for c in connections])
        prices = {o["connectionId"]: o
                  for o in _base.json_of(response, OPERATOR).get("offers") or []}
    return {"origin": origin["name"], "destination": destination["name"],
            "trains": parse(connections, prices, q.adults)}
