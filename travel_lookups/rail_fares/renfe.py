"""Renfe fares (AVE, Avlo, Alvia, Euromed, Avant...), per train, from
venta.renfe.com's own booking flow.

The protocol is the one the site's search page runs (ported from
github.com/drg2/renfecli):

  1. GET  /vol/inicio.do                 session cookies (JSESSIONID, Akamai)
  2. POST /vol/dwr/.../__System.generateId  DWR script-session id, as the page does
  3. POST /vol/buscarTren.do             puts the search in the server session
  4. POST /vol/dwr/.../getTrainsList.dwr the trains, every fare, as a JS literal

RENFE THROTTLES. After about six searches in quick succession it answers with
DWR error U014 ("session dropped") for several minutes. So searches are paced
module-wide (MIN_SECONDS_BETWEEN_SEARCHES), results are cached for ten
minutes, and a U014 starts a cooldown during which calls return "throttled"
at once without touching Renfe. Nothing here retries in a loop; a retry loop
against a throttle only extends it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from . import _base
from ._base import Query

OPERATOR = "Renfe"
BASE = "https://venta.renfe.com"
SEARCH_PAGE = "/vol/buscarTrenEnlaces.do"
# Renfe's own station list, the one its search box autocompletes from.
STATIONS_URL = ("https://www.renfe.com/content/dam/renfe/es/General/buscadores/"
                "javascript/estacionesEstaticas.js")

MIN_SECONDS_BETWEEN_SEARCHES = 10
# If pacing would hold a call longer than this, say "throttled" instead of
# spending the tool's whole time budget waiting.
MAX_PACING_WAIT_SECONDS = 12
COOLDOWN_AFTER_U014_SECONDS = 10 * 60
RESULT_TTL_SECONDS = 10 * 60

_stations = _base.TTLCache(_base.STATION_LIST_TTL_SECONDS)
_results = _base.TTLCache(RESULT_TTL_SECONDS)
_pace_lock = threading.Lock()
_last_search_at = 0.0
_cooldown_until = 0.0


def _session():
    try:
        session = _base.http_session(impersonate=True)
    except _base.Unavailable:
        # Renfe answers plain requests too; curl_cffi only makes it look more
        # like the browser that normally calls it.
        session = _base.http_session()
    session.headers.update({"user-agent": _base.BROWSER_UA,
                            "accept-language": "es-ES,es;q=0.9,en;q=0.8",
                            "origin": BASE, "referer": BASE + "/vol/inicio.do"})
    return session


# ─── stations ────────────────────────────────────────────────────────────────

def parse_station_list(js: str) -> list[dict]:
    """estacionesEstaticas.js opens with `var estacionesEstatico=[...];` and
    goes on to define other lists; only the first array is read."""
    start = js.find("[")
    try:
        stations, _ = json.JSONDecoder().raw_decode(js, start)
    except (ValueError, IndexError) as exc:
        raise _base.FareError("Renfe's station list is not in the expected format") from exc
    return stations


def station_candidates(raw: list[dict]) -> list[dict]:
    """Renfe station records as candidates. "MADRID (TODAS)" is the city code
    the site itself uses when someone types just "Madrid"."""
    out = []
    for s in raw:
        name = s.get("desgEstacion") or s.get("desgEstacionPlano") or ""
        group = "(TODAS)" in name.upper()
        aliases = [s.get("desgEstacionPlano") or ""]
        if group:
            aliases.append(re.sub(r"\s*\(TODAS\)\s*", "", name, flags=re.I))
        out.append({"id": s["cdgoEstacion"], "name": name, "aliases": aliases,
                    "group": group, "lat": None, "lon": None,
                    "rank": s.get("nmroPrioridad") or 9999})
    return out


# ─── the DWR call ────────────────────────────────────────────────────────────

def _dwr_body(params: list[tuple[str, str]], dwr_session: str | None) -> str:
    lines = ["callCount=1", "windowName=",
             "c0-scriptName=trainEnlacesManager", "c0-methodName=getTrainsList", "c0-id=0"]
    refs = []
    for i, (key, value) in enumerate(params, 1):
        lines.append(f"c0-e{i}=string:{quote(value, safe='')}")
        refs.append(f"{key}:reference:c0-e{i}")
    lines.append("c0-param0=Object_Object:{" + ", ".join(refs) + "}")
    script_session = (dwr_session or "0123456789ABCDEF0123456789ABCDEF") + "/travellookups"
    lines += ["batchId=0", "instanceId=0", f"page={quote(SEARCH_PAGE, safe='')}",
              f"scriptSessionId={script_session}"]
    return "\n".join(lines) + "\n"


def _akamai_key(params: dict) -> str:
    """listaTrenes.js sends the filter values in this fixed order as an
    Akamai-Key header, with true/false written as 1/0."""
    order = ["origen", "destino", "fechaSalida", "fechaVuelta", "trayecto", "idaVuelta",
             "adultos", "ninos", "ninosMenores", "sinEnlace", "conMascota", "conBicicleta",
             "plazaH", "atendo", "tipoFranjaI", "horaFranjaIda", "tipoFranjaV",
             "horaFranjaVuelta", "codPromo"]
    return ", ".join({"true": "1", "false": "0"}.get(params[k], params[k]) for k in order)


def extract_payload(script: str) -> dict:
    """The JS object literal inside a DWR reply, as a dict."""
    if "r.handleException(" in script:
        code = re.search(r'cdgoError:"(\w+)"', script)
        code = code.group(1) if code else "?"
        if code == "U014":
            raise _base.Throttled("Renfe is throttling searches (U014); try again in "
                                  "about ten minutes")
        raise _base.FareError(f"Renfe returned DWR error {code}")
    match = re.search(r'r\.handleCallback\("\d+","\d+",', script)
    if not match:
        if "queue-it" in script.lower():
            raise _base.Blocked("Renfe put the search in its queue-it waiting room")
        raise _base.FareError("Renfe's reply was not a DWR callback: " + script[:160])
    depth, in_string, escaped = 0, False, False
    start = match.end()
    for j in range(start, len(script)):
        c = script[j]
        if escaped:
            escaped = False
        elif in_string and c == "\\":
            escaped = True
        elif c == '"':
            in_string = not in_string
        elif in_string:
            pass
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                literal = script[start:j + 1]
                # DWR writes bare keys; quote them to make it JSON.
                return json.loads(re.sub(r'([{,]\s*)([A-Za-z_$][\w$]*)\s*:', r'\1"\2":', literal))
    raise _base.FareError("Renfe's DWR reply was truncated")


def _price(text: str | None) -> float | None:
    text = (text or "").strip().replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def parse(payload: dict, adults: int) -> list[dict]:
    """A getTrainsList payload as rows.

    precioTarifa is per person: on 2026-09-28 the same train priced 37.35 for
    one adult and 37.35 again for two. It is multiplied by `adults` here so
    every operator's `price` is the party total.
    """
    rows = []
    for leg in payload.get("listadoTrenes") or []:
        if not leg.get("viajeIda"):
            continue
        if leg.get("hayError"):
            raise _base.NoService("Renfe: " + (leg.get("mensajeListaTrenVacia")
                                               or leg.get("cdgoError") or "no trains"))
        for t in leg.get("listviajeViewEnlaceBean") or []:
            legs = t.get("trayectos") or []
            numbers = [str(x.get("cdgoTren") or "").lstrip("0") for x in legs]
            kinds = [t.get("tipoTrenUno"), t.get("tipoTrenDos")]
            train = " + ".join(f"{kinds[i] if i < 2 and kinds[i] else ''} {n}".strip()
                               for i, n in enumerate(numbers)) or (t.get("tipoTrenUno") or "Renfe")
            day = datetime.strptime(t["fecha"], "%Y-%m-%d")
            departs = datetime.combine(day, datetime.strptime(t["horaSalida"][:5], "%H:%M").time())
            arrives = datetime.combine(day, datetime.strptime(t["horaLlegada"][:5], "%H:%M").time())
            if arrives < departs:
                arrives += timedelta(days=1)
            sold_out = bool(t.get("completo"))
            fares = [_base.fare(f.get("titulo") or f.get("codigoTarifa"),
                                _party(_price(f.get("precioTarifa")), adults), "EUR",
                                adults, sold_out=sold_out)
                     for f in t.get("tarifasDisponibles") or []]
            fares.sort(key=lambda f: f["price"] if f["price"] is not None else 1e9)
            if not fares:
                fares = [_base.fare("sold out" if sold_out else "no price returned", None,
                                    "EUR", adults, sold_out=sold_out)]
            rows.append(_base.row(
                OPERATOR, train, departs, arrives,
                _title(t.get("descripcionEstacionOrigen")),
                _title(t.get("descripcionEstacionDestino")),
                fares, changes=max(len(legs) - 1, 0),
                duration_minutes=t.get("duracionViajeTotalEnMinutos")))
    return rows


def _party(per_person: float | None, adults: int) -> float | None:
    return None if per_person is None else per_person * adults


def _title(name: str | None) -> str | None:
    return name.title() if name else None


# ─── pacing ──────────────────────────────────────────────────────────────────

def _wait_for_turn() -> None:
    global _last_search_at
    with _pace_lock:
        now = time.monotonic()
        if now < _cooldown_until:
            raise _base.Throttled(f"Renfe is cooling down after a throttle; try again in "
                                  f"{int(_cooldown_until - now) // 60 + 1} min")
        wait = _last_search_at + MIN_SECONDS_BETWEEN_SEARCHES - now
        if wait > MAX_PACING_WAIT_SECONDS:
            raise _base.Throttled("Renfe searches are paced; try again in a few seconds")
        if wait > 0:
            time.sleep(wait)
        _last_search_at = time.monotonic()


def _start_cooldown() -> None:
    global _cooldown_until
    with _pace_lock:
        _cooldown_until = time.monotonic() + COOLDOWN_AFTER_U014_SECONDS


def reset_pacing() -> None:
    """For tests: forget pacing, cooldown and cached results."""
    global _last_search_at, _cooldown_until
    with _pace_lock:
        _last_search_at = _cooldown_until = 0.0
    _results.clear()


# ─── search ──────────────────────────────────────────────────────────────────

def _fetch(origin_id: str, destination_id: str, date: str, adults: int) -> dict:
    _wait_for_turn()
    session = _session()
    _base.call(session, "GET", BASE + "/vol/inicio.do", operator=OPERATOR,
               headers={"accept": "text/html,*/*;q=0.8"})
    generate_id = ("callCount=1\nc0-scriptName=__System\nc0-methodName=generateId\n"
                   "c0-id=0\nbatchId=0\ninstanceId=0\npage=%2Fvol%2Finicio.do\n"
                   "scriptSessionId=\nwindowName=\n")
    response = _base.call(session, "POST", BASE + "/vol/dwr/call/plaincall/__System.generateId.dwr",
                          operator=OPERATOR, data=generate_id,
                          headers={"content-type": "text/plain"})
    match = re.search(r'handleCallback\("0","0","([^"]+)"\)', response.text)
    if match:
        session.cookies.set("DWRSESSIONID", match.group(1), domain="venta.renfe.com", path="/")
    day = datetime.strptime(date, "%Y-%m-%d").strftime("%d/%m/%Y")
    form = {"tipoBusqueda": "autocomplete", "currenLocation": "menuBusqueda",
            "vengoderenfecom": "SI", "desOrigen": "", "desDestino": "",
            "cdgoOrigen": origin_id, "cdgoDestino": destination_id, "idiomaBusqueda": "ES",
            "FechaIdaSel": day, "FechaVueltaSel": "", "_fechaIdaVisual": day,
            "_fechaVueltaVisual": "", "adultos_": str(adults), "ninos_": "0",
            "ninosMenores": "0", "codPromocional": "", "plazaH": "false",
            "sinEnlace": "false", "conMascota": "false", "conBicicleta": "false",
            "asistencia": "false", "franjaHoraI": "", "franjaHoraV": "",
            "Idioma": "es", "Pais": "ES"}
    _base.call(session, "POST", BASE + "/vol/buscarTren.do", operator=OPERATOR, data=form,
               headers={"accept": "text/html,*/*;q=0.8"})
    params = [("atendo", "false"), ("sinEnlace", "false"), ("plazaH", "false"),
              ("tipoFranjaI", ""), ("tipoFranjaV", ""), ("horaFranjaIda", ""),
              ("horaFranjaVuelta", ""), ("fechaSalida", day), ("fechaVuelta", ""),
              ("adultos", str(adults)), ("ninos", "0"), ("ninosMenores", "0"),
              ("trayecto", "I"), ("idaVuelta", ""), ("conMascota", "false"),
              ("conBicicleta", "false"), ("origen", origin_id),
              ("destino", destination_id), ("codPromo", "")]
    response = _base.call(
        session, "POST", BASE + "/vol/dwr/call/plaincall/trainEnlacesManager.getTrainsList.dwr",
        operator=OPERATOR, data=_dwr_body(params, session.cookies.get("DWRSESSIONID")),
        headers={"content-type": "text/plain", "accept": "*/*",
                 "referer": BASE + SEARCH_PAGE, "Akamai-Key": _akamai_key(dict(params))})
    try:
        return extract_payload(response.text)
    except _base.Throttled:
        _start_cooldown()
        raise


def _load_stations() -> list[dict]:
    session = _base.http_session()
    response = _base.call(session, "GET", STATIONS_URL, operator=OPERATOR)
    return station_candidates(parse_station_list(response.text))


def search(q: Query) -> dict:
    candidates = _stations.get_or_load("stations", _load_stations)
    origin = _base.match_station(q.origin, candidates, OPERATOR, q.origin_hint)
    destination = _base.match_station(q.destination, candidates, OPERATOR, q.destination_hint)
    key = (origin["id"], destination["id"], q.date, q.adults)
    payload = _results.get(key)
    if payload is None:
        payload = _fetch(origin["id"], destination["id"], q.date, q.adults)
        _results.put(key, payload)
    return {"origin": _title(origin["name"]), "destination": _title(destination["name"]),
            "trains": parse(payload, q.adults)}
