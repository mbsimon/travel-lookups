"""Iryo fares, per train, from the b2c API behind iryo.eu.

iryo.eu and api.iryo.eu sit behind Cloudflare, which rejects python-requests
by its TLS fingerprint (403), so this needs curl_cffi. The flow is the site's
own: read a cfgToken from the sales-channel config, then POST an availability
search. Each train comes back with one bundle per fare (Inicial, Singular,
Infinita...), priced for the whole party.
"""

from __future__ import annotations

import uuid

from . import _base
from ._base import Query

OPERATOR = "Iryo"
API = "https://api.iryo.eu/b2c"

# The public API-management key and client version the iryo.eu web app sends
# on every request, the same for every visitor. If requests start failing with
# 401, open iryo.eu, search once with the network tab open, and read the
# ocp-apim-subscription-key and x-client-version request headers.
SUBSCRIPTION_KEY = "7c9b9b1ea0fe4f0c9d1739fcbf8b5438"
CLIENT_VERSION = "1.106.3"

_stations = _base.TTLCache(_base.STATION_LIST_TTL_SECONDS)


def _headers() -> dict:
    return {
        "ocp-apim-subscription-key": SUBSCRIPTION_KEY,
        "request-channel": "WEB",
        "x-client-version": CLIENT_VERSION,
        "x-pwa-sessid": str(uuid.uuid4()),
        "x-request-id": str(uuid.uuid4()),
        "no-authorization": "",
        "origin": "https://iryo.eu",
        "referer": "https://iryo.eu/",
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "accept-language": "es-ES",
    }


def station_candidates(raw: list[dict]) -> list[dict]:
    """Iryo's station list as candidates. "Madrid-Todas las estaciones" is a
    city code with childStations; its uicStationCode is X0000."""
    out = []
    for s in raw:
        geo = s.get("geoCoordinates") or {}
        out.append({"id": s["uicStationCode"], "name": s["name"],
                    "aliases": [a for a in (s.get("city"), s.get("shortCode")) if a],
                    "group": bool(s.get("childStations")),
                    "lat": geo.get("latitude"), "lon": geo.get("longitude")})
    return out


def parse(payload: dict, adults: int, station_names: dict[str, str]) -> list[dict]:
    """An availability/search response as rows. Bundle prices are party totals."""
    offer = (payload.get("data") or {}).get("offer") or {}
    texts = offer.get("texts") or []
    names = {}
    for entry in (offer.get("commercial_proposals") or {}).get("names") or []:
        for product in entry.get("products") or []:
            names[product] = entry.get("name")
    rows = []
    for travel in offer.get("travels") or []:
        for route in travel.get("routes") or []:
            legs = route.get("legs") or []
            if not legs:
                continue
            fares = []
            for bundle in sorted(route.get("bundles") or [], key=lambda b: b.get("price") or 0):
                item = (bundle.get("items") or [{}])[0]
                code = item.get("product_code")
                fares.append(_base.fare(_fare_name(code, item.get("texts_idx"), names, texts),
                                        bundle.get("price"), bundle.get("currency"), adults))
            if not fares:
                fares = [_base.fare("Iryo", None, "EUR", adults, sold_out=True)]
            dep, arr = legs[0]["departure_station"], legs[-1]["arrival_station"]
            rows.append(_base.row(
                OPERATOR, "iryo " + "+".join(l.get("service_name", "?") for l in legs),
                _base.parse_local(dep["departure_timestamp"]),
                _base.parse_local(arr["arrival_timestamp"]),
                station_names.get(dep.get("_u_i_c_station_code")),
                station_names.get(arr.get("_u_i_c_station_code")),
                fares, changes=len(legs) - 1))
    return rows


def _fare_name(code, texts_idx, names: dict, texts: list) -> str:
    """The fare's display name: the proposal names first, then the bundle's own
    texts entry (which is where Espacio Silencio lives), then the raw code."""
    if code in names:
        return names[code]
    if isinstance(texts_idx, int) and 0 <= texts_idx < len(texts):
        name = (texts[texts_idx] or {}).get("name")
        if name:
            return name
    return code or "Iryo"


def search(q: Query) -> dict:
    session = _base.http_session(impersonate=True)
    headers = _headers()
    raw = _stations.get_or_load("stations", lambda: _base.json_of(
        _base.call(session, "GET", f"{API}/support/stations", operator=OPERATOR,
                   headers=headers), OPERATOR).get("data") or [])
    candidates = station_candidates(raw)
    origin = _base.match_station(q.origin, candidates, OPERATOR, q.origin_hint)
    destination = _base.match_station(q.destination, candidates, OPERATOR, q.destination_hint)
    config = _base.json_of(_base.call(session, "GET", f"{API}/config/sales-channel",
                                      operator=OPERATOR, headers=headers), OPERATOR)
    if not config.get("cfgToken"):
        raise _base.Blocked("Iryo returned no cfgToken; the web app's API key or "
                            "client version may have rotated")
    body = {"cfgToken": config["cfgToken"], "currency": "EUR",
            "passengers": [{"id": f"passenger_{i + 1}", "type": "AD"} for i in range(q.adults)],
            "travels": [{"origin": origin["id"], "destination": destination["id"],
                         "direction": "outbound", "departure": q.date}]}
    payload = _base.json_of(_base.call(session, "POST", f"{API}/availability/search",
                                       operator=OPERATOR, headers=headers, json=body),
                            OPERATOR)
    names = {s["uicStationCode"]: s["name"] for s in raw}
    return {"origin": origin["name"], "destination": destination["name"],
            "trains": parse(payload, q.adults, names)}
