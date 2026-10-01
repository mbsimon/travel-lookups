"""Weather for trips and daily life. Read-only.

One question, answered by how far away the date is:

  days 0-7    a day-by-day forecast (Open-Meteo best_match, which picks the
              sharpest local model: AROME over Spain and France, HRRR/NBM over
              the US, ICON-D2 over Germany, ECMWF elsewhere), each day labeled
              with a confidence taken from the 51-run ECMWF ensemble
  days 8-15   ranges and chances from that ensemble, never a single-day icon,
              printed next to what is typical for those dates
  days 16-46  typical weather, plus one line when ECMWF's 46-day outlook shows
              a real lean warmer or wetter than normal
  beyond 46   typical weather only

"Typical" is computed here from 30 years of ERA5 daily data for the exact
place and dates, padded a week each side: median high and low, the range 8
years in 10 fall inside, the chance of a wet day, the best and worst year, and
whether the last 10 years ran warmer than the 30. The history for a place is
fetched once and cached, because a 30-year request is heavy on the free tier.

Research behind the cutoffs: vault `20 Projects/Weather MCP — Build Plan
(2026-09-30)`. A single-day forecast past day 10 is right about half the time,
so this module never prints one.

Risks come from official feeds: NHC's active storms (Atlantic, East and
Central Pacific), NWS alerts for US points, CAMS dust, and the known seasonal
windows for tropical storms and sargassum.

Open-Meteo is used on its free tier (Michael, 2026-09-30). It is keyless and
asks for attribution, which ATTRIBUTION carries.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
SEASONAL_URL = "https://seasonal-api.open-meteo.com/v1/seasonal"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
NHC_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
METAR_URL = "https://aviationweather.gov/api/data/metar"
TAF_URL = "https://aviationweather.gov/api/data/taf"
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
SARGASSUM_URL = "https://optics.marine.usf.edu/projects/SaWS.html"

ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0), ECMWF, NOAA."
USER_AGENT = "fora-itinerary-research/1.0 (michael.simon@fora.travel)"
TIMEOUT_SECONDS = 30

ENSEMBLE_MODEL = "ecmwf_ifs025"      # 51 members, 15 days
OUTLOOK_MODEL = "ecmwf_ec46"         # weekly anomalies to 46 days

DAY_BY_DAY_MAX = 7
RANGES_MAX = 15
OUTLOOK_MAX = 46
MAX_DAYS_LISTED = 21

CLIMATE_YEARS = 30
CLIMATE_RECENT_YEARS = 10
CLIMATE_PAD_DAYS = 7
WET_DAY_MM = 1.0                     # Weather Spark's 0.04 in
LEAN_PCT = 70                        # share of 46-day runs needed to call a lean
HEAT_C = 35.0                        # 95 F
# Grid cells at or below this elevation are coast or small island. Found live
# on 2026-09-30: every model put Nassau's lows at 81-82 F while the airport
# read 75 F, because the cell mixes in warm sea.
COASTAL_M = 15
DUST_UGM3 = 100.0
STORM_WATCH_KM = 1500
STORM_LEAD_DAYS = 14

FORECAST_TTL_S = 30 * 60
CACHE_DIR = Path(os.getenv("WEATHER_CACHE_DIR",
                           str(Path.home() / ".cache/travel-weather")))


class WeatherError(RuntimeError):
    """A source declined or could not answer. The message says which."""


class AmbiguousPlace(WeatherError):
    def __init__(self, query: str, candidates: list[dict]):
        self.candidates = candidates
        names = "; ".join(_place_label(c) for c in candidates)
        super().__init__(f"'{query}' matches places in several countries: {names}. "
                         "Retry with the country, e.g. 'Valencia, Spain'.")


# ─── http ────────────────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_memo: dict[str, tuple[float, object]] = {}
_memo_lock = threading.Lock()


RATE_LIMIT_WAIT_S = 61


def _get(url: str, params: dict | None = None, ttl: float = 0, _retry: bool = True) -> object:
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    if ttl:
        with _memo_lock:
            hit = _memo.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    try:
        r = _session.get(url, params=params, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise WeatherError(f"{_host(url)} unreachable: {type(e).__name__}") from e
    if r.status_code == 404:
        raise WeatherError(f"{_host(url)}: not found")
    try:
        data = r.json()
    except ValueError:
        raise WeatherError(f"{_host(url)} answered HTTP {r.status_code} without JSON")
    if r.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
        reason = data.get("reason") if isinstance(data, dict) else None
        # The free tier counts a 30-year history request as hundreds of calls,
        # so two new places in one minute can trip the per-minute limit. That
        # limit resets on the minute; wait once rather than fail the answer.
        if _retry and reason and "Minutely" in reason:
            time.sleep(RATE_LIMIT_WAIT_S)
            return _get(url, params, ttl, _retry=False)
        raise WeatherError(f"{_host(url)} refused: {reason or r.status_code}")
    if ttl:
        with _memo_lock:
            _memo[key] = (time.time(), data)
    return data


def _host(url: str) -> str:
    return url.split("/")[2]


# ─── places ──────────────────────────────────────────────────────────────────

_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def _place_label(p: dict) -> str:
    bits = [p.get("name"), p.get("admin1"), p.get("country")]
    return ", ".join(b for b in bits if b)


def _matches_country(result: dict, hint: str) -> bool:
    hint = hint.strip().lower()
    return hint in {(result.get("country_code") or "").lower(),
                    (result.get("country") or "").lower(),
                    (result.get("admin1") or "").lower()}


def resolve(place: str, country: str | None = None) -> dict:
    """Turn a city, 'City, Country', IATA code, hotel name or 'lat,lon' into a point.

    Ambiguous names across countries raise AmbiguousPlace with the candidates,
    so a caller never silently gets another country's weather. A country hint
    (name or ISO code) always wins over population.
    """
    place = (place or "").strip()
    if not place:
        raise WeatherError("no place given")
    m = _LATLON.match(place)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return {"name": place, "latitude": lat, "longitude": lon, "source": "coordinates"}

    if re.fullmatch(r"[A-Z]{3}", place):
        ap = _airport(place)
        if ap:
            return ap

    name, _, tail = place.partition(",")
    hint = country or (tail.split(",")[-1].strip() if tail else None)
    try:
        data = _get(GEOCODE_URL, {"name": name.strip(), "count": 10, "language": "en"},
                    ttl=86400)
        results = data.get("results") or []
    except WeatherError:
        results = []
    if hint:
        results = [r for r in results if _matches_country(r, hint)]
    if not results:
        g = _google_geocode(place)
        if g:
            return g
        raise WeatherError(f"no place found for '{place}'. Try the nearest town, "
                           "'City, Country', an airport code or 'lat,lon'.")
    results.sort(key=lambda r: r.get("population") or 0, reverse=True)
    top = results[0]
    if not hint:
        rivals = [r for r in results[1:]
                  if r.get("country_code") != top.get("country_code")
                  and (r.get("population") or 0) * 3 >= (top.get("population") or 0)
                  and (r.get("population") or 0) > 0]
        if rivals:
            cands = [_point(r) for r in [top] + rivals[:3]]
            raise AmbiguousPlace(place, cands)
    return _point(top)


def _point(r: dict) -> dict:
    return {"name": r.get("name"), "admin1": r.get("admin1"), "country": r.get("country"),
            "country_code": r.get("country_code"), "latitude": r.get("latitude"),
            "longitude": r.get("longitude"), "timezone": r.get("timezone"),
            "elevation_m": r.get("elevation"), "source": "open-meteo geocoding"}


def _airport(code: str) -> dict | None:
    try:
        from . import flights
        ap = flights.airport(code)
    except Exception:  # no AeroAPI key, network, unknown code: fall back to names
        return None
    if not ap.get("iata_verified") or ap.get("latitude") is None:
        return None
    return {"name": ap.get("name"), "country_code": ap.get("country"),
            "latitude": float(ap["latitude"]), "longitude": float(ap["longitude"]),
            "timezone": ap.get("timezone"), "icao": ap.get("icao"), "iata": ap.get("iata"),
            "source": "airport"}


def _google_geocode(place: str) -> dict | None:
    """Hotels and landmarks. Only when a Google key exists; names first otherwise."""
    key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not key:
        return None
    try:
        data = _get(GOOGLE_GEOCODE_URL, {"address": place, "key": key}, ttl=86400)
    except WeatherError:
        return None
    res = (data or {}).get("results") or []
    if not res:
        return None
    r = res[0]
    loc = r["geometry"]["location"]
    cc = next((c["short_name"] for c in r.get("address_components", [])
               if "country" in c.get("types", [])), None)
    return {"name": r.get("formatted_address"), "country_code": cc,
            "latitude": loc["lat"], "longitude": loc["lng"], "source": "google geocoding"}


# ─── units and words ─────────────────────────────────────────────────────────

WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
       45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
       55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
       67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
       77: "snow grains", 80: "showers", 81: "showers", 82: "heavy showers",
       85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
       96: "thunderstorms with hail", 99: "thunderstorms with hail"}


def _t(c: float | None, units: str) -> int | None:
    if c is None:
        return None
    return round(c * 9 / 5 + 32) if units == "F" else round(c)


def _deg(units: str) -> str:
    return "°F" if units == "F" else "°C"


def _rain_amount(mm: float | None, units: str) -> str:
    if mm is None:
        return ""
    if units == "F":
        return f"{mm / 25.4:.2f} in"
    return f"{mm:.0f} mm" if mm >= 1 else f"{mm:.1f} mm"


def _wind(kmh: float | None, units: str) -> str:
    if kmh is None:
        return ""
    return f"{round(kmh / 1.609)} mph" if units == "F" else f"{round(kmh)} km/h"


def rain_words(pct: float | None) -> str:
    """The National Weather Service's wording for a precipitation chance."""
    if pct is None:
        return "rain chance unknown"
    if pct < 20:
        return "dry"
    if pct < 30:
        return "slight chance of rain"
    if pct < 60:
        return "chance of rain"
    if pct < 80:
        return "rain likely"
    return "rain expected"


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _norm_units(units: str | None) -> str:
    u = (units or "F").strip().upper()
    return "C" if u in ("C", "METRIC", "CELSIUS") else "F"


def _parse_date(s: str | date) -> date:
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        raise WeatherError(f"bad date '{s}', use YYYY-MM-DD")


def _today(tz: str | None) -> date:
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz)).date()
        except Exception:
            pass
    return datetime.utcnow().date()


# ─── forecast (days 0-15) ────────────────────────────────────────────────────

_DAILY = ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
          "precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max,"
          "uv_index_max,sunrise,sunset")


def _forecast(lat: float, lon: float) -> dict:
    d = _get(FORECAST_URL, {"latitude": lat, "longitude": lon, "daily": _DAILY,
                            "forecast_days": 16, "timezone": "auto"}, ttl=FORECAST_TTL_S)
    return d


def _ensemble(lat: float, lon: float) -> dict[str, dict]:
    """date -> {'highs': [...51], 'lows': [...], 'rain': [...]} from the ECMWF ensemble."""
    d = _get(ENSEMBLE_URL, {"latitude": lat, "longitude": lon, "models": ENSEMBLE_MODEL,
                            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                            "forecast_days": RANGES_MAX, "timezone": "auto"},
             ttl=FORECAST_TTL_S)
    daily = d.get("daily") or {}
    out: dict[str, dict] = {}
    for i, day in enumerate(daily.get("time", [])):
        row = {"highs": [], "lows": [], "rain": []}
        for k, series in daily.items():
            if k == "time" or series[i] is None:
                continue
            if k.startswith("temperature_2m_max"):
                row["highs"].append(series[i])
            elif k.startswith("temperature_2m_min"):
                row["lows"].append(series[i])
            elif k.startswith("precipitation_sum"):
                row["rain"].append(series[i])
        out[day] = row
    return out


def _confidence(ens: dict | None, lead: int) -> str:
    """High / Moderate / Low from the ensemble spread, capped by lead time."""
    order = ["Low", "Moderate", "High"]
    if not ens or len(ens.get("highs", [])) < 10:
        label = "High" if lead <= 2 else "Moderate" if lead <= 5 else "Low"
    else:
        spread = _pct(ens["highs"], 0.9) - _pct(ens["highs"], 0.1)
        t = 2 if spread <= 3 else 1 if spread <= 6 else 0
        wet = sum(1 for r in ens["rain"] if r >= WET_DAY_MM) / max(len(ens["rain"]), 1)
        r = 2 if (wet <= 0.15 or wet >= 0.85) else 1 if (wet <= 0.3 or wet >= 0.7) else 0
        label = order[min(t, r)]
    cap = 2 if lead < 8 else 1 if lead <= 10 else 0
    return order[min(order.index(label), cap)]


# ─── typical (any date, any distance) ────────────────────────────────────────

_archive_lock = threading.Lock()


def _climate_span() -> tuple[int, int]:
    last = date.today().year - 1
    return last - CLIMATE_YEARS + 1, last


def _archive(lat: float, lon: float) -> dict[str, list]:
    """30 years of daily history for a ~10 km cell, cached on disk forever."""
    first, last = _climate_span()
    key = f"{lat:.1f}_{lon:.1f}_{first}_{last}"
    path = CACHE_DIR / "climate" / f"{key}.json"
    with _archive_lock:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        d = _get(ARCHIVE_URL, {"latitude": round(lat, 1), "longitude": round(lon, 1),
                               "start_date": f"{first}-01-01", "end_date": f"{last}-12-31",
                               "daily": "temperature_2m_max,temperature_2m_min,"
                                        "precipitation_sum,daylight_duration",
                               "timezone": "auto"})
        daily = d.get("daily") or {}
        daily["_elevation_m"] = d.get("elevation")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(daily))
        except OSError:
            pass  # a cache write failure must never break a lookup
        return daily


def _same_day(year: int, d: date) -> date:
    try:
        return d.replace(year=year)
    except ValueError:  # Feb 29
        return date(year, 2, 28)


def typical(place: dict, start: str | date, end: str | date, units: str = "F") -> dict:
    """What these dates are usually like at this place, from 30 years of data."""
    units = _norm_units(units)
    start, end = _parse_date(start), _parse_date(end)
    if end < start:
        start, end = end, start
    daily = _archive(place["latitude"], place["longitude"])
    idx = {t: i for i, t in enumerate(daily.get("time", []))}
    first, last = _climate_span()
    trip_days = (end - start).days + 1
    by_year: dict[int, dict] = {}
    for y in range(first, last + 1):
        # Trips crossing New Year take the window's year from its start date.
        a = _same_day(y, start) - timedelta(days=CLIMATE_PAD_DAYS)
        n = trip_days + 2 * CLIMATE_PAD_DAYS
        rows = {"hi": [], "lo": [], "rain": [], "light": []}
        for k in range(n):
            i = idx.get((a + timedelta(days=k)).isoformat())
            if i is None:
                continue
            for field, col in (("hi", "temperature_2m_max"), ("lo", "temperature_2m_min"),
                               ("rain", "precipitation_sum"), ("light", "daylight_duration")):
                v = (daily.get(col) or [None] * (i + 1))[i]
                if v is not None:
                    rows[field].append(v)
        if rows["hi"]:
            by_year[y] = rows
    if not by_year:
        raise WeatherError("no climate history for this place")

    his = [v for r in by_year.values() for v in r["hi"]]
    los = [v for r in by_year.values() for v in r["lo"]]
    rains = [v for r in by_year.values() for v in r["rain"]]
    lights = [v for r in by_year.values() for v in r["light"]]
    wet = sum(1 for v in rains if v >= WET_DAY_MM) / max(len(rains), 1)
    recent = [v for y, r in by_year.items() if y > last - CLIMATE_RECENT_YEARS for v in r["hi"]]
    shift_c = statistics.median(recent) - statistics.median(his) if recent else 0.0

    def yr(fn):
        return max(by_year, key=lambda y: fn(by_year[y]))

    def wet_frac(r):
        return sum(1 for v in r["rain"] if v >= WET_DAY_MM) / max(len(r["rain"]), 1)

    hottest = yr(lambda r: statistics.mean(r["hi"]))
    coolest = yr(lambda r: -statistics.mean(r["hi"]))
    wettest = yr(wet_frac)
    driest = yr(lambda r: -wet_frac(r))
    deg = _deg(units)
    med_hi, med_lo = statistics.median(his), statistics.median(los)
    expected_wet = round(wet * trip_days)
    daylight_h = statistics.median(lights) / 3600 if lights else None

    lines = [
        f"Typical high {_t(med_hi, units)}{deg}, low {_t(med_lo, units)}{deg}. "
        f"8 years in 10 the highs fall between {_t(_pct(his, .1), units)} and "
        f"{_t(_pct(his, .9), units)}{deg}.",
        f"Chance of a wet day: {round(wet * 100)}% "
        f"(about {expected_wet} of {trip_days} day{'s' if trip_days != 1 else ''})."
        if trip_days > 1 else f"Chance of a wet day: {round(wet * 100)}%.",
    ]
    if daylight_h:
        lines.append(f"Daylight about {daylight_h:.1f} hours.")
    if abs(shift_c) >= 0.5:
        word = "warmer" if shift_c > 0 else "cooler"
        diff = abs(shift_c) * (9 / 5 if units == "F" else 1)
        lines.append(f"The last {CLIMATE_RECENT_YEARS} years ran about "
                     f"{diff:.0f}{deg} {word} than the {CLIMATE_YEARS}-year figure.")
    lines.append(f"Hottest year for these dates {hottest}, coolest {coolest}; "
                 f"wettest {wettest}, driest {driest}.")
    caveats = []
    elev = daily.get("_elevation_m")
    if elev is not None and elev >= 1500:
        caveats.append("Mountain terrain: the 10 km climate grid smooths out valleys "
                       "and peaks, so local temperatures can differ a lot.")
    if abs(place["latitude"]) < 23.5:
        caveats.append("Tropics: this climate record tends to overstate rain days; "
                       "showers are often short.")
    return {
        "basis": f"typical weather for these dates ({first}-{last}, ±{CLIMATE_PAD_DAYS} days), "
                 "not a forecast",
        "units": deg,
        "median_high": _t(med_hi, units), "median_low": _t(med_lo, units),
        "high_p10": _t(_pct(his, .1), units), "high_p90": _t(_pct(his, .9), units),
        "low_p10": _t(_pct(los, .1), units), "low_p90": _t(_pct(los, .9), units),
        "wet_day_pct": round(wet * 100), "expected_wet_days": expected_wet,
        "trip_days": trip_days,
        "daylight_hours": round(daylight_h, 1) if daylight_h else None,
        "recent_shift": round(shift_c * (9 / 5 if units == "F" else 1), 1),
        "hottest_year": hottest, "coolest_year": coolest,
        "wettest_year": wettest, "driest_year": driest,
        "sea_temperature": "not available for typical weather; forecast only, within 7 days",
        "elevation_m": elev,
        "summary": lines, "caveats": caveats,
    }


# ─── 46-day outlook ──────────────────────────────────────────────────────────

def _outlook(lat: float, lon: float, start: date, end: date) -> list[str]:
    try:
        d = _get(SEASONAL_URL, {"latitude": lat, "longitude": lon, "models": OUTLOOK_MODEL,
                                "weekly": "temperature_2m_anomaly_gt0,precipitation_anomaly_gt0"},
                 ttl=6 * 3600)
    except WeatherError:
        return []
    w = d.get("weekly") or {}
    lines = []
    for i, wk in enumerate(w.get("time", [])):
        a = date.fromisoformat(wk)
        if a + timedelta(days=6) < start or a > end:
            continue
        warm = (w.get("temperature_2m_anomaly_gt0") or [None])[i]
        wet = (w.get("precipitation_anomaly_gt0") or [None])[i]
        bits = []
        if warm is not None and warm >= LEAN_PCT:
            bits.append(f"warmer than normal ({warm}% of runs)")
        elif warm is not None and warm <= 100 - LEAN_PCT:
            bits.append(f"cooler than normal ({100 - warm}% of runs)")
        if wet is not None and wet >= LEAN_PCT:
            bits.append(f"wetter than normal ({wet}% of runs; rain leans are less reliable)")
        elif wet is not None and wet <= 100 - LEAN_PCT:
            bits.append(f"drier than normal ({100 - wet}% of runs; rain leans are less reliable)")
        if bits:
            lines.append(f"Week of {a:%b} {a.day}: leans " + " and ".join(bits) + ".")
    return lines


# ─── risks ───────────────────────────────────────────────────────────────────

def _basin(lat: float, lon: float) -> tuple[str, tuple[int, int], tuple[int, int]] | None:
    """(name, season start (m,d), season end (m,d)) for the tropical-storm basin, if any."""
    # Central America between Mexico and Colombia faces both oceans; which
    # coast a point is on is not knowable from a box, so name both seasons.
    if 7 <= lat < 18 and -92 <= lon <= -77 and not _caribbean_side(lat, lon):
        return ("Atlantic and East Pacific hurricane", (5, 15), (11, 30))
    if 0 <= lat <= 45 and -100 <= lon <= -10:
        # Mexico's Pacific coast sits west of the isthmus, not in the Atlantic.
        if lon < -90 and lat < 23 and not (lon > -97.5 and lat > 18):
            return ("East Pacific hurricane", (5, 15), (11, 30))
        return ("Atlantic hurricane", (6, 1), (11, 30))
    if 0 <= lat <= 40 and -180 <= lon < -140:
        return ("Central Pacific hurricane", (6, 1), (11, 30))
    if 0 <= lat <= 35 and -140 <= lon < -90:
        return ("East Pacific hurricane", (5, 15), (11, 30))
    if 0 <= lat <= 45 and 100 <= lon <= 180:
        return ("West Pacific typhoon", (5, 1), (11, 30))
    if 0 <= lat <= 30 and 45 <= lon < 100:
        return ("North Indian cyclone", (4, 1), (12, 31))
    if -40 <= lat < 0 and (30 <= lon <= 180 or -180 <= lon <= -120):
        return ("Southern Hemisphere cyclone", (11, 1), (4, 30))
    return None


def _caribbean_side(lat: float, lon: float) -> bool:
    """Yucatan, Belize, Honduras's north coast and points east of Costa Rica."""
    return lat >= 15 or lon >= -83.5


def _in_season(d: date, a: tuple[int, int], b: tuple[int, int]) -> bool:
    md = (d.month, d.day)
    return a <= md <= b if a <= b else (md >= a or md <= b)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    h = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


def active_storms(lat: float, lon: float, radius_km: float = STORM_WATCH_KM) -> list[dict]:
    """NHC's active storms within radius_km of a point, nearest first."""
    data = _get(NHC_STORMS_URL, ttl=15 * 60)
    out = []
    for s in (data or {}).get("activeStorms") or []:
        try:
            slat, slon = float(s["latitudeNumeric"]), float(s["longitudeNumeric"])
        except (KeyError, TypeError, ValueError):
            continue
        km = _haversine_km(lat, lon, slat, slon)
        if km <= radius_km:
            out.append({"name": s.get("name"), "id": s.get("id"),
                        "classification": s.get("classification"),
                        "wind_kt": s.get("intensity"), "distance_km": round(km),
                        "distance_mi": round(km / 1.609),
                        "moving": f"{s.get('movementDir')}° at {s.get('movementSpeed')} mph",
                        "advisory": (s.get("publicAdvisory") or {}).get("url"),
                        "updated": s.get("lastUpdate")})
    return sorted(out, key=lambda s: s["distance_km"])


STORM_KIND = {"HU": "Hurricane", "TS": "Tropical Storm", "TD": "Tropical Depression",
              "PTC": "Potential Tropical Cyclone", "STS": "Subtropical Storm",
              "STD": "Subtropical Depression", "PC": "Post-tropical Cyclone"}


def storm_line(s: dict) -> str:
    kind = STORM_KIND.get(s.get("classification") or "", s.get("classification") or "Storm")
    return (f"{kind} {s['name']} is {s['distance_mi']:,} mi away, winds {s['wind_kt']} kt, "
            f"moving {s['moving']}. NHC advisory: {s['advisory']}")


def _nws_alerts(lat: float, lon: float) -> list[str]:
    try:
        d = _get(NWS_ALERTS_URL, {"point": f"{lat:.4f},{lon:.4f}"}, ttl=10 * 60)
    except WeatherError:
        return []
    out = []
    for f in (d or {}).get("features") or []:
        p = f.get("properties") or {}
        out.append(f"{p.get('event')}: {p.get('headline') or ''}".strip())
    return out


def _dust_days(lat: float, lon: float) -> list[str]:
    try:
        d = _get(AIR_URL, {"latitude": lat, "longitude": lon, "hourly": "dust",
                           "forecast_days": 5, "timezone": "auto"}, ttl=FORECAST_TTL_S)
    except WeatherError:
        return []
    h = d.get("hourly") or {}
    peak: dict[str, float] = {}
    for t, v in zip(h.get("time", []), h.get("dust", [])):
        if v is not None:
            peak[t[:10]] = max(peak.get(t[:10], 0), v)
    return [day for day, v in sorted(peak.items()) if v >= DUST_UGM3]


def risks(place: dict, start: date, end: date, highs_c: dict[str, float] | None = None,
          typical_p90_c: float | None = None) -> list[str]:
    lat, lon = place["latitude"], place["longitude"]
    today = _today(place.get("timezone"))
    lead = (start - today).days
    lines: list[str] = []
    basin = _basin(lat, lon)
    if basin:
        name, a, b = basin
        days = [start + timedelta(days=k) for k in range((end - start).days + 1)]
        if any(_in_season(d, a, b) for d in days):
            lines.append(f"These dates fall in {name} season "
                         f"({date(2000, *a):%b} {a[1]} to {date(2000, *b):%b} {b[1]}).")
    if lead <= STORM_LEAD_DAYS and end >= today:
        # Live hazards: official warnings, storms, ash and disasters near the
        # point. See hazards.py for the sources and why each is there.
        from . import hazards
        found = hazards.near(lat, lon, place.get("country_code"))
        lines += [hazards.line(h) for h in found["hazards"]]
        lines += [f"Not checked: {g}." for g in found["gaps"]]
        if lead <= 4:
            dust = _dust_days(lat, lon)
            if dust:
                lines.append("Heavy Saharan-type dust forecast on " + ", ".join(dust) +
                             " (hazy skies, poorer air).")
    if (10 <= lat <= 30 and -98 <= lon <= -58 and not (lon < -90 and lat < 23)
            and _caribbean_side(lat, lon)):
        if any(3 <= m <= 10 for m in {start.month, end.month}):
            lines.append("Sargassum season on Caribbean and Gulf coasts (spring to fall). "
                         f"Beaches vary; USF's monthly outlook: {SARGASSUM_URL}")
    hot = [d for d, c in (highs_c or {}).items() if c is not None and c >= HEAT_C]
    if hot:
        lines.append("Forecast heat: highs of 95°F (35°C) or more on " + ", ".join(hot) + ".")
    elif typical_p90_c is not None and typical_p90_c >= HEAT_C:
        lines.append("Heat risk: in the hotter years these dates reach 95°F (35°C) or more.")
    return lines


# ─── the main answer ─────────────────────────────────────────────────────────

def trip(place: str, start: str, end: str | None = None, units: str = "F",
         country: str | None = None) -> dict:
    """The right weather answer for a stay, whatever the lead time."""
    units = _norm_units(units)
    loc = resolve(place, country)
    s = _parse_date(start)
    e = _parse_date(end) if end else s
    if e < s:
        s, e = e, s
    today = _today(loc.get("timezone"))
    if e < today:
        raise WeatherError("those dates are in the past; this answers forecasts and "
                           "typical weather, not history")
    deg = _deg(units)
    days = [s + timedelta(days=k) for k in range((e - s).days + 1)][:MAX_DAYS_LISTED]
    leads = {d: (d - today).days for d in days}
    near = [d for d in days if 0 <= leads[d] <= RANGES_MAX]
    notes: list[str] = []
    fc, ens = {}, {}
    elevation = None
    if near:
        try:
            raw = _forecast(loc["latitude"], loc["longitude"])
            loc.setdefault("timezone", raw.get("timezone"))
            elevation = raw.get("elevation")
            dd = raw.get("daily") or {}
            fc = {t: {k: (dd.get(k) or [None] * (i + 1))[i] for k in dd}
                  for i, t in enumerate(dd.get("time", []))}
        except WeatherError as err:
            notes.append(f"Forecast unavailable ({err}).")
        try:
            ens = _ensemble(loc["latitude"], loc["longitude"])
        except WeatherError as err:
            notes.append(f"Ensemble unavailable, confidence is by lead time only ({err}).")

    try:
        typ = typical(loc, s, e, units)
    except WeatherError as err:
        typ = None
        notes.append(f"Typical weather unavailable ({err}).")

    day_rows = []
    highs_c: dict[str, float] = {}
    for d in near:
        key, lead = d.isoformat(), leads[d]
        f, en = fc.get(key) or {}, ens.get(key)
        if lead <= DAY_BY_DAY_MAX and f:
            hi, lo = f.get("temperature_2m_max"), f.get("temperature_2m_min")
            highs_c[key] = hi
            conf = _confidence(en, lead)
            sky = WMO.get(f.get("weather_code"), "")
            pop = f.get("precipitation_probability_max")
            rain = rain_words(pop)
            amt = f.get("precipitation_sum") or 0
            line = (f"{d:%a %b} {d.day}: {sky}, high {_t(hi, units)}{deg}, "
                    f"low {_t(lo, units)}{deg}, {rain}"
                    + ((f" ({pop}%, {_rain_amount(amt, units)})" if amt >= 0.5 else f" ({pop}%)")
                       if pop and pop >= 20 else "")
                    + f", wind to {_wind(f.get('wind_gusts_10m_max'), units)}"
                    + f". Confidence {conf.lower()}.")
            day_rows.append({"date": key, "lead_days": lead, "basis": "forecast",
                             "high": _t(hi, units), "low": _t(lo, units), "sky": sky,
                             "rain_chance": pop, "rain": _rain_amount(amt, units),
                             "uv_max": f.get("uv_index_max"), "confidence": conf,
                             "line": line})
        elif en and en.get("highs"):
            h10, h90 = _pct(en["highs"], .1), _pct(en["highs"], .9)
            l10, l90 = _pct(en["lows"], .1), _pct(en["lows"], .9)
            wet = round(100 * sum(1 for r in en["rain"] if r >= WET_DAY_MM) / len(en["rain"]))
            highs_c[key] = _pct(en["highs"], .5)
            conf = _confidence(en, lead)
            line = (f"{d:%a %b} {d.day}: highs {_t(h10, units)}-{_t(h90, units)}{deg}, "
                    f"lows {_t(l10, units)}-{_t(l90, units)}{deg}, "
                    f"rain in {wet}% of forecast runs ({rain_words(wet)}). "
                    f"Range only; confidence {conf.lower()}.")
            day_rows.append({"date": key, "lead_days": lead, "basis": "forecast range",
                             "high_range": [_t(h10, units), _t(h90, units)],
                             "low_range": [_t(l10, units), _t(l90, units)],
                             "rain_runs_pct": wet, "confidence": conf, "line": line})

    far = [d for d in days if leads[d] > RANGES_MAX]
    outlook = []
    if far and any(leads[d] <= OUTLOOK_MAX for d in far):
        outlook = _outlook(loc["latitude"], loc["longitude"], far[0], far[-1])

    sea = None
    if near and leads[near[0]] <= DAY_BY_DAY_MAX:
        sea = _sea_temp(loc["latitude"], loc["longitude"], units)

    risk = risks(loc, s, e, highs_c,
                 typical_p90_c=((typ["high_p90"] - 32) * 5 / 9 if typ and units == "F"
                                else typ["high_p90"] if typ else None))

    if not near:
        horizon = ("typical weather + 46-day outlook" if outlook or
                   (leads[days[0]] <= OUTLOOK_MAX) else "typical weather only")
    elif far:
        horizon = "forecast for the first days, typical weather for the rest"
    elif all(leads[d] <= DAY_BY_DAY_MAX for d in near):
        horizon = "day-by-day forecast"
    else:
        horizon = "forecast, with ranges past day 7"

    if elevation is None and typ is not None:
        elevation = typ.get("elevation_m")
    if elevation is not None and elevation <= COASTAL_M:
        notes.append("Coast or small island: model lows often run 3-5°F warm here "
                     "because the grid mixes in the sea; highs are more reliable.")

    summary = [r["line"] for r in day_rows]
    if typ:
        label = "Typical for these dates" if near else "Typical weather for these dates"
        summary.append(f"{label} (not a forecast): " + " ".join(typ["summary"]))
    summary += outlook
    if sea:
        summary.append(sea)
    summary += [f"Risk: {r}" for r in risk]
    summary += notes

    first_lead = leads[days[0]]
    if first_lead > OUTLOOK_MAX:
        client_note = ("This is what these dates are usually like, based on 30 years of "
                       "weather records. A real forecast starts about two weeks out.")
    elif first_lead > RANGES_MAX:
        client_note = ("This is what these dates are usually like. Day-by-day forecasts "
                       "become useful about a week before you go.")
    elif first_lead > DAY_BY_DAY_MAX:
        client_note = ("Early forecast, shown as ranges. Details firm up about a week out.")
    else:
        client_note = f"Forecast as of {today:%B} {today.day}; it can still shift."

    return {
        "place": {k: v for k, v in loc.items() if v is not None},
        "dates": {"start": s.isoformat(), "end": e.isoformat(),
                  "days_away": first_lead, "listed_days": len(days)},
        "units": deg, "horizon": horizon,
        "summary": summary, "client_note": client_note,
        "days": day_rows, "typical": typ, "outlook": outlook, "risks": risk,
        "attribution": ATTRIBUTION,
    }


def _sea_temp(lat: float, lon: float, units: str) -> str | None:
    try:
        d = _get(MARINE_URL, {"latitude": lat, "longitude": lon,
                              "daily": "sea_surface_temperature_mean", "forecast_days": 3,
                              "timezone": "auto"}, ttl=3 * 3600)
    except WeatherError:
        return None
    vals = [v for v in (d.get("daily") or {}).get("sea_surface_temperature_mean", [])
            if v is not None]
    if not vals:
        return None
    return f"Sea temperature about {_t(statistics.mean(vals), units)}{_deg(units)} now."


# ─── typical comparison ──────────────────────────────────────────────────────

def compare_typical(places: list[str], start: str, end: str | None = None,
                    units: str = "F") -> dict:
    """Typical weather for several destinations over the same dates, side by side."""
    units = _norm_units(units)
    s = _parse_date(start)
    e = _parse_date(end) if end else s
    rows, errors = [], []
    for p in places:
        try:
            loc = resolve(p)
            t = typical(loc, s, e, units)
            rows.append({"place": _place_label(loc) or p, "median_high": t["median_high"],
                         "median_low": t["median_low"],
                         "high_range": [t["high_p10"], t["high_p90"]],
                         "wet_day_pct": t["wet_day_pct"], "daylight_hours": t["daylight_hours"],
                         "line": f"{_place_label(loc) or p}: " + " ".join(t["summary"][:2]),
                         "caveats": t["caveats"]})
        except WeatherError as err:
            errors.append({"place": p, "error": str(err)})
    return {"dates": {"start": s.isoformat(), "end": e.isoformat()}, "units": _deg(units),
            "basis": "typical weather for these dates, not a forecast",
            "places": rows, "errors": errors, "attribution": ATTRIBUTION}


# ─── travel day at an airport ────────────────────────────────────────────────

FLIGHT_CAT = {"VFR": "good flying weather", "MVFR": "some low cloud or haze",
              "IFR": "low cloud or poor visibility, delays possible",
              "LIFR": "very low cloud or fog, delays likely"}


def airport_weather(code: str, units: str = "F") -> dict:
    """Current conditions (METAR) and the terminal forecast (TAF) in plain words."""
    units = _norm_units(units)
    code = code.strip().upper()
    icao = code if len(code) == 4 else None
    name = None
    if not icao:
        ap = _airport(code)
        if not ap or not ap.get("icao"):
            raise WeatherError(f"could not resolve airport '{code}' to its ICAO code")
        icao, name = ap["icao"], ap.get("name")
    metar = _get(METAR_URL, {"ids": icao, "format": "json"}, ttl=10 * 60)
    taf = _get(TAF_URL, {"ids": icao, "format": "json"}, ttl=30 * 60)
    if not metar:
        raise WeatherError(f"no current report for {icao}")
    m = metar[0]
    cat = m.get("fltCat")
    wind = m.get("wspd")
    gust = m.get("wgst")
    now = (f"{name or icao}: {_t(m.get('temp'), units)}{_deg(units)}, "
           f"wind {_wind(wind * 1.852 if wind is not None else None, units)}"
           + (f" gusting {_wind(gust * 1.852, units)}" if gust else "")
           + f", visibility {m.get('visib')} mi"
           + (f", {FLIGHT_CAT.get(cat, cat)}" if cat else "") + ".")
    return {"airport": icao, "name": name, "now": now,
            "metar": m.get("rawOb"), "taf": (taf[0].get("rawTAF") if taf else None),
            "observed": m.get("reportTime"),
            "note": "TAF is the official forecast for 5 miles around the runway, "
                    "usually 24-30 hours ahead."}


# ─── one local day, hour by hour ─────────────────────────────────────────────

def local_day(place: str, day: str | None = None, units: str = "F",
              country: str | None = None) -> dict:
    """Hour-by-hour for one day: rain timing, feels-like, UV, air quality, best outdoor window."""
    units = _norm_units(units)
    loc = resolve(place, country)
    target = _parse_date(day) if day else _today(loc.get("timezone"))
    d = _get(FORECAST_URL, {"latitude": loc["latitude"], "longitude": loc["longitude"],
                            "hourly": "temperature_2m,apparent_temperature,"
                                      "precipitation_probability,precipitation,"
                                      "weather_code,wind_gusts_10m,uv_index",
                            "start_date": target.isoformat(), "end_date": target.isoformat(),
                            "timezone": "auto"}, ttl=FORECAST_TTL_S)
    h = d.get("hourly") or {}
    hours = []
    for i, t in enumerate(h.get("time", [])):
        hours.append({"hour": int(t[11:13]),
                      "temp": _t(h["temperature_2m"][i], units),
                      "feels": _t(h["apparent_temperature"][i], units),
                      "feels_c": h["apparent_temperature"][i],
                      "rain_pct": h["precipitation_probability"][i],
                      "rain": h["precipitation"][i],
                      "sky": WMO.get(h["weather_code"][i], ""),
                      "uv": h["uv_index"][i]})
    aqi = None
    try:
        a = _get(AIR_URL, {"latitude": loc["latitude"], "longitude": loc["longitude"],
                           "hourly": "european_aqi,us_aqi", "start_date": target.isoformat(),
                           "end_date": target.isoformat(), "timezone": "auto"},
                 ttl=FORECAST_TTL_S)
        vals = [v for v in (a.get("hourly") or {}).get("us_aqi", []) if v is not None]
        aqi = max(vals) if vals else None
    except WeatherError:
        pass

    def ok(x):
        return ((x["rain_pct"] or 0) < 30 and x["feels_c"] is not None
                and 5 <= x["feels_c"] <= 30)

    best, run = [], []
    for x in [x for x in hours if 7 <= x["hour"] <= 21]:
        run = run + [x] if ok(x) else []
        if len(run) > len(best):
            best = run
    deg = _deg(units)
    wet = [x["hour"] for x in hours if (x["rain_pct"] or 0) >= 50]
    lines = []
    if hours:
        day_hours = [x for x in hours if 7 <= x["hour"] <= 21]
        lo = min(x["temp"] for x in day_hours)
        hi = max(x["temp"] for x in day_hours)
        lines.append(f"{target:%a %b} {target.day} in {_place_label(loc) or place}: "
                     f"{lo}-{hi}{deg} between 7am and 9pm.")
        lines.append("Rain likely around " + ", ".join(f"{x}:00" for x in wet) + "."
                     if wet else "No hour with a 50% rain chance or more.")
        uv = max((x["uv"] or 0) for x in hours)
        if uv >= 6:
            lines.append(f"UV peaks at {uv:.0f}; sunscreen at midday.")
    if best:
        lines.append(f"Best time outside: {best[0]['hour']}:00-{best[-1]['hour'] + 1}:00.")
    if aqi is not None:
        word = ("good" if aqi <= 50 else "moderate" if aqi <= 100 else
                "unhealthy for sensitive groups" if aqi <= 150 else "unhealthy")
        lines.append(f"Air quality {word} (US AQI up to {aqi}).")
    for x in hours:
        x.pop("feels_c", None)
    return {"place": {k: v for k, v in loc.items() if v is not None},
            "date": target.isoformat(), "units": deg, "summary": lines,
            "hours": hours, "attribution": ATTRIBUTION}
