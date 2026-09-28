"""Deutsche Bahn fares, per connection, from the DB Navigator app's API.

This is the API the Android app calls (the "dbnav" profile of
db-vendo-client), with no login. bahn.de's own web API answers OPS_BLOCKED to
anything that is not a browser; the app API does not, given Chrome's TLS
fingerprint, so this needs curl_cffi. It prices many international legs too
(Austria, Switzerland, the Benelux, France), which is why the dispatcher also
asks it about cross-border trips.

DB returns one "from" price per connection: the cheapest second-class fare
it has for the whole party.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from . import _base
from ._base import Query

OPERATOR = "DB"
BASE = "https://app.services-bahn.de/mob"
SEARCH_TYPE = "application/x.db.vendo.mob.verbindungssuche.v9+json"
LOCATION_TYPE = "application/x.db.vendo.mob.location.v3+json"
# The app identifies itself this way; the API expects an app, not a browser.
APP_UA = "DBNavigator/25.35 (Android)"
DEFAULT_ZONE = "Europe/Berlin"

_locations = _base.TTLCache(_base.STATION_LIST_TTL_SECONDS)


def _headers(content_type: str) -> dict:
    return {"X-Correlation-ID": f"{uuid.uuid4()}_{uuid.uuid4()}", "Accept": content_type,
            "Content-Type": content_type, "User-Agent": APP_UA}


def _locate(session, query: str) -> list[dict]:
    def load():
        response = _base.call(session, "POST", f"{BASE}/location/search", operator=OPERATOR,
                              headers=_headers(LOCATION_TYPE),
                              json={"locationTypes": ["ST"], "searchTerm": query,
                                    "maxResults": 5})
        return _base.json_of(response, OPERATOR) or []
    return _locations.get_or_load(_base.normalize(query), load)


def _resolve(session, query: str, hint: dict | None) -> dict:
    """DB's own search ranks its results well (München finds the city's meta
    station first), so its top station is taken as is."""
    found = [s for s in _locate(session, query) if s.get("locationId")]
    if not found and hint and hint.get("name"):
        found = [s for s in _locate(session, hint["name"]) if s.get("locationId")]
    if not found:
        raise _base.NoService(f"DB has no station matching {query!r}")
    return found[0]


def parse(payload: dict, adults: int) -> list[dict]:
    """A fahrplan response as rows. The price is the party total (checked
    2026-09-28: 95.99 for one adult, 191.98 for two on the same ICE)."""
    rows = []
    for item in payload.get("verbindungen") or []:
        connection = item.get("verbindung") or {}
        sections = [s for s in connection.get("verbindungsAbschnitte") or []
                    if s.get("typ") == "FAHRZEUG"]
        if not sections:
            continue
        prices = (item.get("angebote") or {}).get("preise") or {}
        cheapest = (prices.get("gesamt") or {}).get("ab") or {}
        name = "cheapest, 2nd class" if cheapest.get("betrag") is not None else "no price returned"
        if prices.get("istTeilpreis"):
            name += " (covers only part of the trip)"
        fares = [_base.fare(name, cheapest.get("betrag"), cheapest.get("waehrung") or "EUR",
                            adults, cls="2nd")]
        first, last = sections[0], sections[-1]
        rows.append(_base.row(
            OPERATOR,
            " + ".join(s.get("mitteltext") or s.get("langtext") or s.get("kurztext") or "?"
                       for s in sections),
            _base.parse_local(first["abgangsDatum"]), _base.parse_local(last["ankunftsDatum"]),
            (first.get("abgangsOrt") or {}).get("name"),
            (last.get("ankunftsOrt") or {}).get("name"),
            fares, changes=connection.get("umstiegeAnzahl"),
            duration_minutes=(connection.get("reiseDauer") or 0) // 60 or None))
    return rows


def search(q: Query) -> dict:
    session = _base.http_session(impersonate=True)
    origin = _resolve(session, q.origin, q.origin_hint)
    destination = _resolve(session, q.destination, q.destination_hint)
    zone = ZoneInfo((q.origin_hint or {}).get("timezone") or DEFAULT_ZONE)
    when = datetime.strptime(f"{q.date} {q.depart_after or '06:00'}",
                             "%Y-%m-%d %H:%M").replace(tzinfo=zone)
    body = {"autonomeReservierung": False, "einstiegsTypList": ["STANDARD"],
            "fahrverguenstigungen": {"deutschlandTicketVorhanden": False,
                                     "nurDeutschlandTicketVerbindungen": False},
            "klasse": "KLASSE_2",
            "reisendenProfil": {"reisende": [
                {"ermaessigungen": ["KEINE_ERMAESSIGUNG KLASSENLOS"],
                 "reisendenTyp": "ERWACHSENER"} for _ in range(q.adults)]},
            "reservierungsKontingenteVorhanden": False,
            "reiseHin": {"wunsch": {
                "abgangsLocationId": origin["locationId"],
                "zielLocationId": destination["locationId"],
                "verkehrsmittel": ["ALL"], "alternativeHalteBerechnung": True,
                "zeitWunsch": {"reiseDatum": when.isoformat(), "zeitPunktArt": "ABFAHRT"}}}}
    response = _base.call(session, "POST", f"{BASE}/angebote/fahrplan", operator=OPERATOR,
                          headers=_headers(SEARCH_TYPE), json=body)
    return {"origin": origin.get("name"), "destination": destination.get("name"),
            "trains": parse(_base.json_of(response, OPERATOR), q.adults)}
