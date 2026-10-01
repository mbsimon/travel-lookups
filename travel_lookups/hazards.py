"""Hazards near a point right now: official warnings, storms, ash, disasters.

Built after Michael asked whether the storm watch would have caught the
nor'easter of September 25-28, 2026 or the Etna eruption of August 13, 2026.
It would have caught neither: it read only NHC's tropical storms and threw
away every NWS alert that was not tropical, and it had no source outside the
US. The Stewarts were stranded at JFK when Etna closed Catania. Sources:

  NHC          tropical storms, Atlantic and East/Central Pacific
  NWS          every US warning (coastal flood, winter storm, high wind,
               flood, heat...) plus tropical watches; advisories are dropped
  MeteoAlarm   orange and red warnings from each European national service
               (AEMET in Spain), matched to the warning region that contains
               the point using MeteoAlarm's own region boundaries
  ash SIGMETs  the volcanic-ash warnings aircraft fly by, worldwide, from
               aviationweather.gov. This is the feed that closes airports
               like Catania; GDACS listed no Etna event this summer
  GDACS        the UN/EU disaster feed: earthquakes, eruptions, floods,
               wildfires, and tropical cyclones in basins NHC does not cover

Every hazard has a stable `key` so a watcher can post it once per stay and
not again when the source reissues the same warning with a new identifier.
All sources are free and keyless. Read-only.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import weather as _w
from .weather import WeatherError, _haversine_km, STORM_KIND


def _get(*a, **k):
    # Through the module so tests that fake weather._get fake this too.
    return _w._get(*a, **k)

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
METEOALARM_FEED = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{slug}"
METEOALARM_GEO_TREE = ("https://gitlab.com/api/v4/projects/meteoalarm-pm-group%2Fdocuments"
                       "/repository/tree?per_page=100")
METEOALARM_GEO_RAW = ("https://gitlab.com/meteoalarm-pm-group/documents/-/raw/master/{name}")
ASH_SIGMET_URL = "https://aviationweather.gov/api/data/isigmet"
GDACS_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
GDACS_EVENT_URL = "https://www.gdacs.org/report.aspx?eventtype={t}&eventid={i}"

CACHE_DIR = Path(os.getenv("WEATHER_CACHE_DIR",
                           str(Path.home() / ".cache/travel-weather")))
GEO_REFRESH_DAYS = 30

STORM_KM = 1500        # NHC and GDACS tropical cyclones
ASH_KM = 150           # an ash cloud this close shuts the nearest airports
METEOALARM_MIN_LEVEL = 3   # 3 = orange, 4 = red
# GDACS: (radius km, lowest alert level kept) per event type
GDACS_RULES = {"EQ": (300, "Orange"), "VO": (300, "Green"), "WF": (100, "Orange"),
               "FL": (150, "Orange"), "TC": (STORM_KM, "Green")}
GDACS_LEVELS = {"Green": 1, "Orange": 2, "Red": 3}
GDACS_RECENT_DAYS = 3
GDACS_KIND = {"EQ": "Earthquake", "VO": "Volcanic eruption", "WF": "Wildfire",
              "FL": "Flood", "TC": "Tropical cyclone"}

# NWS events whose name ends in Warning are kept. These watches are kept too.
NWS_WATCHES = ("Hurricane Watch", "Tropical Storm Watch", "Storm Surge Watch",
               "Blizzard Watch", "Winter Storm Watch")
US_CODES = ("US", "PR", "VI", "GU", "AS", "MP")

# ISO country code -> MeteoAlarm feed name.
METEOALARM_SLUG = {
    "AT": "austria", "BA": "bosnia-herzegovina", "BE": "belgium", "BG": "bulgaria",
    "CH": "switzerland", "CY": "cyprus", "CZ": "czechia", "DE": "germany",
    "DK": "denmark", "EE": "estonia", "ES": "spain", "FI": "finland",
    "FR": "france", "GB": "united-kingdom", "GR": "greece", "HR": "croatia",
    "HU": "hungary", "IE": "ireland", "IL": "israel", "IS": "iceland",
    "IT": "italy", "LT": "lithuania", "LU": "luxembourg", "LV": "latvia",
    "MD": "moldova", "ME": "montenegro", "MT": "malta", "NL": "netherlands",
    "NO": "norway", "PL": "poland", "PT": "portugal", "RO": "romania",
    "RS": "serbia", "SE": "sweden", "SI": "slovenia", "SK": "slovakia",
    "UK": "united-kingdom",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hazard(source, kind, key, title, *, severity=None, starts=None, ends=None,
            url=None, distance_km=None) -> dict:
    h = {"source": source, "kind": kind, "key": key, "title": title,
         "severity": severity, "starts": starts, "ends": ends, "url": url}
    if distance_km is not None:
        h["distance_km"] = round(distance_km)
        h["distance_mi"] = round(distance_km / 1.609)
    return {k: v for k, v in h.items() if v is not None}


# ─── geometry ────────────────────────────────────────────────────────────────

def _in_ring(lon: float, lat: float, ring) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > lat) != (y2 > lat):
            if lon < (x2 - x1) * (lat - y1) / ((y2 - y1) or 1e-12) + x1:
                inside = not inside
    return inside


def _in_geometry(lon: float, lat: float, geom: dict) -> bool:
    polys = ([geom["coordinates"]] if geom.get("type") == "Polygon"
             else geom.get("coordinates") or [])
    for poly in polys:
        if poly and _in_ring(lon, lat, poly[0]) and not any(
                _in_ring(lon, lat, hole) for hole in poly[1:]):
            return True
    return False


# ─── NHC ─────────────────────────────────────────────────────────────────────

def nhc(lat: float, lon: float) -> list[dict]:
    out = []
    for s in _w.active_storms(lat, lon, STORM_KM):
        kind = STORM_KIND.get(s.get("classification") or "", "Tropical system")
        out.append(_hazard(
            "NHC", "tropical storm", f"nhc:{s['id']}",
            f"{kind} {s['name']}, winds {s['wind_kt']} kt, moving {s['moving']}",
            url=s.get("advisory"), distance_km=s["distance_km"]))
    return out


# ─── NWS ─────────────────────────────────────────────────────────────────────

def nws_keep(event: str) -> bool:
    return bool(event) and (event.endswith("Warning") or event in NWS_WATCHES)


def nws(lat: float, lon: float) -> list[dict]:
    d = _get(NWS_ALERTS_URL, {"point": f"{lat:.4f},{lon:.4f}"}, ttl=10 * 60)
    return nws_from_features((d or {}).get("features") or [])


def nws_from_features(features: list[dict]) -> list[dict]:
    """Warnings worth a line, one per event and end date.

    NWS reissues a warning several times as it evolves (the September 2026
    nor'easter's Coastal Flood Warning for Manhattan was issued three times),
    each with a new identifier and onset. Grouping by event and end date keeps
    it to one line; the latest issue wins.
    """
    best: dict[str, dict] = {}
    for f in features:
        p = f.get("properties") or {}
        event = p.get("event") or ""
        if not nws_keep(event) or p.get("messageType") == "Cancel":
            continue
        ends = p.get("ends") or p.get("expires") or ""
        key = f"nws:{event}:{ends[:10]}"
        if key in best and (best[key]["_sent"] or "") >= (p.get("sent") or ""):
            continue
        office = p.get("senderName") or "NWS"
        best[key] = dict(_hazard("NWS", event, key, f"{event} from {office}",
                                 severity=p.get("severity"), starts=p.get("onset"),
                                 ends=ends or None, url="https://alerts.weather.gov"),
                         _sent=p.get("sent"))
    return [{k: v for k, v in h.items() if k != "_sent"} for h in best.values()]


# ─── MeteoAlarm ──────────────────────────────────────────────────────────────

_geo_lock = threading.Lock()


def _geo_dir() -> Path:
    return CACHE_DIR / "meteoalarm"


def _ensure_geocodes() -> None:
    """Split MeteoAlarm's region file into one small file per country, monthly.

    The full file is about 33 MB; loading it whole for every lookup would eat
    the travel server's memory, so only the country asked about is read.
    """
    stamp = _geo_dir() / "_version.json"
    with _geo_lock:
        try:
            meta = json.loads(stamp.read_text())
            if time.time() - meta["at"] < GEO_REFRESH_DAYS * 86400:
                return
        except (OSError, ValueError, KeyError):
            meta = {}
        try:
            tree = _get(METEOALARM_GEO_TREE)
            names = sorted(t["name"] for t in tree
                           if re.fullmatch(r"MeteoAlarm_Geocodes_[\d_]+\.json", t["name"]))
        except (WeatherError, TypeError, KeyError):
            names = []
        if not names:
            if meta:
                return  # keep the copy we have
            raise WeatherError("MeteoAlarm region boundaries unavailable")
        latest = names[-1]
        if meta.get("name") == latest:
            meta["at"] = time.time()
            stamp.write_text(json.dumps(meta))
            return
        data = _get(METEOALARM_GEO_RAW.format(name=latest))
        by_country: dict[str, list] = {}
        for f in data.get("features") or []:
            p = f.get("properties") or {}
            by_country.setdefault(p.get("country") or "?", []).append(
                {"code": p.get("code"), "name": p.get("name"), "geometry": f.get("geometry")})
        _geo_dir().mkdir(parents=True, exist_ok=True)
        for cc, feats in by_country.items():
            (_geo_dir() / f"{cc}.json").write_text(json.dumps(feats))
        stamp.write_text(json.dumps({"name": latest, "at": time.time()}))


_region_memo: dict[tuple, list[str]] = {}


def regions_at(lat: float, lon: float, country_code: str) -> list[str]:
    """MeteoAlarm region codes (EMMA_ID) whose boundary contains the point."""
    cc = "GB" if country_code == "UK" else country_code
    k = (round(lat, 3), round(lon, 3), cc)
    if k in _region_memo:
        return _region_memo[k]
    _ensure_geocodes()
    try:
        feats = json.loads((_geo_dir() / f"{cc}.json").read_text())
    except (OSError, ValueError):
        feats = []
    codes = [f["code"] for f in feats
             if f.get("geometry") and _in_geometry(lon, lat, f["geometry"])]
    _region_memo[k] = codes
    return codes


def meteoalarm(lat: float, lon: float, country_code: str | None) -> list[dict]:
    slug = METEOALARM_SLUG.get((country_code or "").upper())
    if not slug:
        return []
    codes = set(regions_at(lat, lon, country_code.upper()))
    if not codes:
        return []
    d = _get(METEOALARM_FEED.format(slug=slug), ttl=15 * 60)
    return meteoalarm_from_warnings((d or {}).get("warnings") or [], codes)


def meteoalarm_from_warnings(warnings: list[dict], codes: set[str],
                             now: datetime | None = None) -> list[dict]:
    now = now or _now()
    out, seen = [], set()
    for w in warnings:
        alert = w.get("alert") or {}
        if alert.get("msgType") == "Cancel":
            continue
        infos = alert.get("info") or []
        info = next((i for i in infos if (i.get("language") or "").startswith("en")),
                    infos[0] if infos else None)
        if not info:
            continue
        params = {p.get("valueName"): p.get("value") for p in info.get("parameter") or []}
        try:
            level = int((params.get("awareness_level") or "0").split(";")[0])
        except ValueError:
            continue
        if level < METEOALARM_MIN_LEVEL:
            continue
        areas = info.get("area") or []
        if not any(g.get("value") in codes for a in areas for g in a.get("geocode") or []):
            continue
        try:
            if datetime.fromisoformat(info["expires"]) < now:
                continue
        except (KeyError, ValueError):
            pass
        color = "red" if level >= 4 else "orange"
        what = (params.get("awareness_type") or "").split(";")[-1].strip() or info.get("event")
        onset = (info.get("onset") or info.get("effective") or "")[:10]
        key = f"meteoalarm:{','.join(sorted(codes))}:{what}:{onset}"
        if key in seen:
            continue
        seen.add(key)
        out.append(_hazard("MeteoAlarm", f"{what} warning", key,
                           f"{color.title()} {what.lower()} warning "
                           f"({info.get('senderName') or 'national service'}): "
                           f"{info.get('headline') or info.get('event')}",
                           severity=color, starts=info.get("onset"),
                           ends=info.get("expires"), url=info.get("web")))
    return out


# ─── volcanic ash ────────────────────────────────────────────────────────────

def ash(lat: float, lon: float, radius_km: float = ASH_KM) -> list[dict]:
    d = _get(ASH_SIGMET_URL, {"format": "json", "hazard": "va"}, ttl=10 * 60)
    return ash_from_sigmets(d or [], lat, lon, radius_km)


def ash_from_sigmets(sigmets: list[dict], lat: float, lon: float,
                     radius_km: float = ASH_KM) -> list[dict]:
    out, seen = [], set()
    now_ts = _now().timestamp()
    for s in sigmets:
        until = s.get("validTimeTo")
        if isinstance(until, (int, float)) and until < now_ts:
            continue  # expired; the feed keeps them for a while
        pts = [(c["lon"], c["lat"]) for c in s.get("coords") or []
               if c.get("lat") is not None]
        if not pts:
            continue
        inside = len(pts) >= 3 and _in_ring(lon, lat, pts)
        km = 0.0 if inside else min(_haversine_km(lat, lon, y, x) for x, y in pts)
        if km > radius_km:
            continue
        volcano = (s.get("qualifier") or "unnamed volcano").title()
        key = f"ash:{volcano}:{s.get('firId')}"
        if key in seen:
            continue
        seen.add(key)
        ends = (datetime.fromtimestamp(until, timezone.utc).isoformat()
                if isinstance(until, (int, float)) else None)
        out.append(_hazard("ash SIGMET", "volcanic ash", key,
                           f"Volcanic ash from {volcano} "
                           f"{'over this area' if inside else 'nearby'} "
                           f"({s.get('firName') or s.get('firId')} airspace); "
                           "flights to nearby airports may be cancelled",
                           ends=ends, distance_km=km))
    return out


# ─── GDACS ───────────────────────────────────────────────────────────────────

def gdacs(lat: float, lon: float, skip_storm_names: set[str] | None = None) -> list[dict]:
    today = _now().date()
    d = _get(GDACS_URL, {"eventlist": ";".join(GDACS_RULES),
                         "fromdate": (today - timedelta(days=GDACS_RECENT_DAYS)).isoformat(),
                         "todate": today.isoformat()}, ttl=30 * 60)
    return gdacs_from_features((d or {}).get("features") or [], lat, lon,
                               skip_storm_names or set())


def gdacs_from_features(features: list[dict], lat: float, lon: float,
                        skip_storm_names: set[str]) -> list[dict]:
    out = []
    for f in features:
        p = f.get("properties") or {}
        t = p.get("eventtype")
        rule = GDACS_RULES.get(t)
        if not rule:
            continue
        radius, floor = rule
        if GDACS_LEVELS.get(p.get("alertlevel"), 0) < GDACS_LEVELS[floor]:
            continue
        try:
            elon, elat = f["geometry"]["coordinates"][:2]
        except (KeyError, TypeError, ValueError):
            continue
        km = _haversine_km(lat, lon, elat, elon)
        if km > radius:
            continue
        name = (p.get("name") or "").strip()
        if t == "TC" and any(n in name.lower() for n in skip_storm_names):
            continue  # already reported by NHC with more detail
        out.append(_hazard("GDACS", GDACS_KIND[t].lower(),
                           f"gdacs:{t}:{p.get('eventid')}",
                           f"{p.get('alertlevel')} alert: {name}",
                           severity=p.get("alertlevel"), starts=p.get("fromdate"),
                           ends=p.get("todate"),
                           url=GDACS_EVENT_URL.format(t=t, i=p.get("eventid")),
                           distance_km=km))
    return out


# ─── all of it ───────────────────────────────────────────────────────────────

def near(lat: float, lon: float, country_code: str | None = None) -> dict:
    """Every current hazard near a point, plus which sources could not answer."""
    cc = (country_code or "").upper()
    hazards: list[dict] = []
    gaps: list[str] = []

    def take(name, fn, *args):
        try:
            got = fn(*args)
            hazards.extend(got)
            return got
        except WeatherError as e:
            gaps.append(f"{name}: {e}")
        except Exception as e:  # a parser surprise must not hide the other sources
            gaps.append(f"{name}: {type(e).__name__}")
        return []

    storms = take("NHC", nhc, lat, lon)
    if cc in US_CODES:
        take("NWS", nws, lat, lon)
    if cc in METEOALARM_SLUG:
        take("MeteoAlarm", meteoalarm, lat, lon, cc)
    take("ash SIGMETs", ash, lat, lon)
    names = {h["title"].split(",")[0].split()[-1].lower() for h in storms}
    take("GDACS", gdacs, lat, lon, names)
    return {"hazards": hazards, "gaps": gaps}


def line(h: dict) -> str:
    where = f", {h['distance_mi']:,} mi away" if h.get("distance_mi") else ""
    until = ""
    if h.get("ends"):
        try:
            until = f", until {datetime.fromisoformat(h['ends']):%b %-d %H:%M}"
        except ValueError:
            pass
    url = f" {h['url']}" if h.get("url") else ""
    return f"{h['title']}{where}{until}.{url}"
