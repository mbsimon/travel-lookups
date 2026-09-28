"""Shared plumbing for the operator modules: errors, HTTP, station matching,
caches and the normalized row shape.

Every operator module raises one of the FareError subclasses below. The
dispatcher turns each into a status line for that operator, so one operator
failing never sinks the others.
"""

from __future__ import annotations

import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import requests

# Seconds for one HTTP request. The dispatcher also caps each operator's whole
# search (OPERATOR_BUDGET_SECONDS in __init__), so a slow multi-call operator
# cannot hold the tool open.
HTTP_TIMEOUT_SECONDS = 20

# A current desktop Chrome. Operators' web APIs serve their own site, and a
# python-requests user agent is the first thing a bot filter looks at.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Station lists change a few times a year. Half a day keeps one warm process
# from refetching them on every search.
STATION_LIST_TTL_SECONDS = 12 * 3600


@dataclass
class Query:
    """One search as every operator receives it.

    `date` and `depart_after` are local to the origin. The hints are the
    Transitous station records the dispatcher resolved (name, country,
    coordinates), or None when it could not; operators use them only to break
    ties between their own stations.
    """
    origin: str
    destination: str
    date: str
    adults: int = 1
    depart_after: str | None = None
    origin_hint: dict | None = None
    destination_hint: dict | None = None


# ─── errors ──────────────────────────────────────────────────────────────────

class FareError(RuntimeError):
    """An operator could not answer. `status` is what the dispatcher reports."""
    status = "error"


class NoService(FareError):
    """The operator does not serve this station or pair."""
    status = "no_service"


class Blocked(FareError):
    """The operator's bot filter refused the request."""
    status = "blocked"


class Throttled(FareError):
    """The operator asked us to slow down, or our own pacing held the call."""
    status = "throttled"


class Unavailable(FareError):
    """A dependency this operator needs is not installed here."""
    status = "unavailable"


class OperatorTimeout(FareError):
    status = "timeout"


# ─── HTTP ────────────────────────────────────────────────────────────────────

def http_session(impersonate: bool = False) -> object:
    """A requests.Session, or a curl_cffi one presenting Chrome's TLS fingerprint.

    Iryo, Italo, Eurostar, DB and OeBB sit behind Cloudflare or Akamai, which
    reject python-requests by its TLS handshake before reading a header.
    curl_cffi is an optional extra (travel-lookups[rail]) and is imported only
    here, so the package still imports without it.
    """
    if not impersonate:
        session = requests.Session()
        session.headers["User-Agent"] = BROWSER_UA
        return session
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as exc:
        raise Unavailable("curl_cffi not installed (pip install "
                          "'travel-lookups[rail]')") from exc
    return cffi_requests.Session(impersonate="chrome")


def call(session, method: str, url: str, *, operator: str,
         timeout: float = HTTP_TIMEOUT_SECONDS, **kwargs):
    """One request, with transport and HTTP failures mapped to FareErrors."""
    try:
        response = session.request(method, url, timeout=timeout, **kwargs)
    except Exception as exc:  # requests and curl_cffi raise unrelated classes
        text = f"{type(exc).__name__}: {exc}"
        if "timeout" in text.lower() or "timed out" in text.lower():
            raise OperatorTimeout(f"{operator} did not answer within {timeout:.0f}s") from exc
        raise FareError(f"could not reach {operator}: {text[:200]}") from exc
    code = response.status_code
    if code in (401, 403):
        raise Blocked(f"{operator} refused the request (HTTP {code}); its bot "
                      f"filter may have changed")
    if code == 429:
        raise Throttled(f"{operator} rate-limited the request (HTTP 429)")
    if code >= 400:
        raise FareError(f"{operator} returned HTTP {code}: {response.text[:200]}")
    return response


def json_of(response, operator: str):
    try:
        return response.json()
    except ValueError as exc:
        raise FareError(f"{operator} answered with something other than JSON: "
                        f"{response.text[:160]!r}") from exc


# ─── caches ──────────────────────────────────────────────────────────────────

class TTLCache:
    """A small thread-safe in-process cache. Nothing is written to disk."""

    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._items: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._items.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1]
            self._items.pop(key, None)
            return None

    def put(self, key, value) -> None:
        with self._lock:
            self._items[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def get_or_load(self, key, loader: Callable[[], object]):
        value = self.get(key)
        if value is None:
            value = loader()
            self.put(key, value)
        return value


# ─── station matching ────────────────────────────────────────────────────────

# Words that carry no identity in a station name, in the languages we meet.
_STOPWORDS = {"de", "del", "la", "las", "los", "el", "di", "da", "des", "du",
              "le", "les", "the", "y", "e", "et", "und", "and"}


def normalize(text: str) -> str:
    """Lowercase, accents stripped, punctuation to spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("ß", "ss").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def tokens(text: str) -> set[str]:
    return {t for t in normalize(text).split() if t not in _STOPWORDS}


def _km(a: dict, b: dict) -> float:
    try:
        lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    except (KeyError, TypeError, ValueError):
        return math.inf
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


def match_station(query: str, candidates: list[dict], operator: str,
                  hint: dict | None = None) -> dict:
    """Pick the operator's own station for a place name.

    Each candidate is {"id", "name", "aliases": [...], "group": bool, "lat",
    "lon"} and optionally "rank" (the operator's own priority, lower first). A group is an operator's "all stations in this city" code (Renfe's
    MADRID (TODAS), Ouigo's MT1); it wins when the query is just the city,
    which is how the operator's own site searches. Otherwise the candidate
    covering the most words of the query wins, then the one covering the most
    of the Transitous name in `hint`, then the operator's own rank (Renfe
    ranks Madrid-Puerta de Atocha above the Atocha commuter platforms), then
    the fewest extra words, then the nearest to the hint's coordinates.

    Refuses rather than guesses: a query that shares no word with any of the
    operator's stations raises NoService, because that operator does not call
    there.
    """
    query_tokens = tokens(query)
    query_norm = normalize(query)
    hint = hint or {}
    hint_tokens = tokens(hint.get("name") or "")
    hint_point = {"lat": hint.get("latitude"), "lon": hint.get("longitude")}

    best, best_key = None, None
    for index, cand in enumerate(candidates):
        names = [cand["name"], *(cand.get("aliases") or [])]
        words = set().union(*(tokens(n) for n in names))
        cover = len(query_tokens & words) / len(query_tokens) if query_tokens else 0.0
        hint_cover = len(hint_tokens & words) / len(hint_tokens) if hint_tokens else 0.0
        if cover < 0.5 and hint_cover < 0.6:
            continue
        exact = query_norm in {normalize(n) for n in names}
        key = (round(cover, 3), exact, exact and bool(cand.get("group")),
               round(hint_cover, 3), -(cand.get("rank") or 0),
               -len(tokens(cand["name"]) - query_tokens),
               -_km(hint_point, cand), -index)
        if best_key is None or key > best_key:
            best, best_key = cand, key
    if best is None:
        raise NoService(f"{operator} has no station matching {query!r}")
    return best


# ─── rows ────────────────────────────────────────────────────────────────────

_CURRENCY = {"€": "EUR", "£": "GBP", "CHF": "CHF", "EUR": "EUR", "GBP": "GBP"}


def currency(code: str | None) -> str | None:
    if not code:
        return None
    return _CURRENCY.get(code.strip(), code.strip().upper())


def fare(name: str, price: float | None, currency_code: str | None, adults: int,
         cls: str | None = None, seats_left: int | None = None,
         sold_out: bool = False) -> dict:
    """One fare for the whole party. `price` is the party total as the
    operator quoted it for `adults` adults; per_person divides it. A fare with
    no price that is not sold out is one the operator listed without pricing
    it; it is kept, and never counts as the cheapest."""
    if price is not None:
        price = round(float(price), 2)
    return {"fare": name, "class": cls, "price": price,
            "per_person": round(price / adults, 2) if price is not None else None,
            "currency": currency(currency_code),
            "seats_left": seats_left, "sold_out": bool(sold_out)}


def hhmm(minutes: int | None) -> str | None:
    if minutes is None or minutes <= 0:
        return None
    return f"{minutes // 60}h {minutes % 60:02d}m"


def parse_local(value: str) -> datetime:
    """An operator timestamp as a datetime, keeping its offset when it has one.

    Accepts '2026-10-15T08:05:00.000+02:00', '2026-10-15T06:09:00+0200' and
    naive '2026-10-15T05:35:00'. Operators quote local station time; the
    wall-clock part is what the row shows.
    """
    value = value.strip().replace("Z", "+00:00")
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
    return datetime.fromisoformat(value)


def row(operator: str, train: str, departs: datetime, arrives: datetime,
        origin: str | None, destination: str | None, fares: list[dict],
        changes: int | None = None, duration_minutes: int | None = None) -> dict:
    """The normalized shape every operator returns, one per train or connection."""
    if duration_minutes is None:
        if (departs.tzinfo is None) == (arrives.tzinfo is None):
            duration_minutes = int((arrives - departs).total_seconds() // 60)
    available = [f for f in fares if not f["sold_out"] and f["price"] is not None]
    cheapest = min(available, key=lambda f: f["price"]) if available else None
    return {
        "operator": operator,
        "train": train,
        "date": departs.strftime("%Y-%m-%d"),
        "departs": departs.strftime("%H:%M"),
        "arrives": arrives.strftime("%H:%M"),
        "arrives_next_day": arrives.date() > departs.date(),
        "origin": origin,
        "destination": destination,
        "duration": hhmm(duration_minutes),
        "changes": changes,
        "fares": fares,
        "cheapest": ({"fare": cheapest["fare"], "class": cheapest["class"],
                      "price": cheapest["price"], "per_person": cheapest["per_person"],
                      "currency": cheapest["currency"]} if cheapest else None),
        "sold_out": bool(fares) and all(f["sold_out"] for f in fares),
    }
