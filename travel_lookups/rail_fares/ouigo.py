"""OUIGO Spain and OUIGO France fares, per train, over plain HTTPS.

Both markets run the same middleware: log in with the web app's own public
credentials for a short-lived JWT, then POST a journey search. No browser and
no captcha. Each train comes back with its three packages (Essential, Plus,
Full), priced for the whole party.
"""

from __future__ import annotations

from . import _base
from ._base import Query

# Public web-app credentials, the same for every visitor, baked into each
# site's JS bundle (ventas.ouigo.com and ventes.ouigo.com). If a login starts
# failing with 401, open the site, search once with the browser's network tab
# open, and read the body of the POST to /api/Token/login.
WEB_USER = "ouigo.web"
MARKETS = {
    "es": {"operator": "Ouigo España",
           "api": "https://mdw01.api-es.ouigo.com/api",
           "password": "SquirelWeb!2020",
           "site": "https://ventas.ouigo.com", "language": "es-ES"},
    "fr": {"operator": "Ouigo France",
           "api": "https://mdw.api-fr.ouigo.com/api",
           "password": "Prep)?BFLDgsJ]b|(98>hj^2,?0kdsdkr5",
           "site": "https://ventes.ouigo.com", "language": "fr-FR"},
}

_stations = _base.TTLCache(_base.STATION_LIST_TTL_SECONDS)


def _session(market: dict):
    session = _base.http_session()
    session.headers.update({"Origin": market["site"], "Referer": market["site"] + "/",
                            "Content-Language": market["language"],
                            "Accept": "application/json"})
    response = _base.call(session, "POST", f"{market['api']}/Token/login",
                          operator=market["operator"],
                          json={"username": WEB_USER, "password": market["password"]})
    token = (_base.json_of(response, market["operator"]) or {}).get("token")
    if not token:
        raise _base.Blocked(f"{market['operator']} login returned no token; the "
                            f"public web credentials may have rotated")
    session.headers["Authorization"] = f"Bearer {token}"
    return session


def station_candidates(raw: list[dict]) -> list[dict]:
    """Ouigo's GetStations list as match_station candidates.

    City codes (Spain's MT1, France's PT1/LY1) carry the city as a synonym and
    have no parent; France marks them is_agglomeration or names them "toutes
    gares", Spain "Todas las estaciones".
    """
    out = []
    for s in raw:
        name = s.get("name") or ""
        group = bool(s.get("is_agglomeration")) or any(
            w in name.lower() for w in ("toutes gares", "todas las estaciones"))
        geo = s.get("geo_data") or {}
        out.append({"id": s["_u_i_c_station_code"], "name": name,
                    "aliases": s.get("synonyms") or [], "group": group,
                    "lat": _float(geo.get("latitude")), "lon": _float(geo.get("longitude"))})
    return out


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse(payload: dict, adults: int, operator: str) -> list[dict]:
    """A journeysearch response as rows. Package prices are party totals."""
    rows = []
    for journey in payload.get("outbound") or []:
        dep, arr = journey["departure_station"], journey["arrival_station"]
        segments = journey.get("segments") or []
        train = "OUIGO " + "+".join(s.get("service_name", "?") for s in segments)
        fares = [
            _base.fare(p.get("name") or p.get("product_family_id"), p.get("price"),
                       "EUR", adults, seats_left=p.get("remaining_seats"),
                       sold_out=bool(p.get("is_package_full")))
            for p in sorted(journey.get("packages") or [], key=lambda p: p.get("price") or 0)
        ]
        if journey.get("full") and not fares:
            fares = [_base.fare("OUIGO", None, "EUR", adults, sold_out=True)]
        rows.append(_base.row(
            operator, train,
            _base.parse_local(dep["departure_timestamp"]),
            _base.parse_local(arr["arrival_timestamp"]),
            dep.get("name"), arr.get("name"), fares,
            changes=max(len(segments) - 1, 0)))
    return rows


def _search(market_key: str, q: Query) -> dict:
    market = MARKETS[market_key]
    operator = market["operator"]
    session = _session(market)
    candidates = _stations.get_or_load(market_key, lambda: station_candidates(
        _base.json_of(_base.call(session, "GET", f"{market['api']}/Data/GetStations",
                                 operator=operator), operator)))
    origin = _base.match_station(q.origin, candidates, operator, q.origin_hint)
    destination = _base.match_station(q.destination, candidates, operator, q.destination_hint)
    if origin["id"] == destination["id"]:
        raise _base.NoService(f"{operator}: origin and destination are the same station")
    response = _base.call(session, "POST", f"{market['api']}/Sale/journeysearch",
                          operator=operator,
                          json={"origin": origin["id"], "destination": destination["id"],
                                "outbound_date": q.date,
                                "passengers": [{"type": "A"} for _ in range(q.adults)]})
    return {"origin": origin["name"], "destination": destination["name"],
            "trains": parse(_base.json_of(response, operator), q.adults, operator)}


def search_es(q: Query) -> dict:
    return _search("es", q)


def search_fr(q: Query) -> dict:
    return _search("fr", q)
