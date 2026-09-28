"""Google Flights for the flight QA step: fetch, cache, budget, audit.

Two sources, one order. Google Flights' own pages, read directly by
`google_flights` (free), answer first. SerpApi (250 searches a month) is the
fallback when a page cannot be read: a timeout, zero rows, a parse error, a
block page, or no browser in this process. Every result says which source
answered (`source`), and the audit log keeps it.

Everything here is I/O. The comparison logic lives in `flight_qa.py`, which
takes a `fetch(params) -> dict` callable (SerpApi) or a `direct` object
(Google's pages) so it runs the same against live sources or fixtures.

Budget: SerpApi's own account endpoint is the truth about what is left (it is
free and not counted as a search). The per-host ledger in `flights.py` drifted
from it on every host, so it is only the fallback when the endpoint is down.
Repeated identical requests are served from a 6-hour cache, and SerpApi does
not count cached or failed searches either.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import requests

from . import flight_qa as _qa
from . import flights as _flights
from . import google_flights as _gf

log = logging.getLogger("travel_lookups.public_fares")

SERPAPI_ACCOUNT = "https://serpapi.com/account.json"
CACHE_TTL_SECONDS = 6 * 3600        # fares move; a day-old public price is not evidence
RESERVE_SEARCHES = 40               # never spend the account below this for QA
DAILY_CAP = 30                      # QA searches per day, counted from the audit log
TIMEOUT_SECONDS = 60


class BudgetHeld(RuntimeError):
    """Spending would take the account under the reserve or past the daily cap."""


def _db() -> sqlite3.Connection:
    base = Path(os.getenv("FLIGHTS_CACHE_DIR", str(Path.home() / ".cache/flight-research")))
    base.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(base / "flight_qa.sqlite")
    c.execute("CREATE TABLE IF NOT EXISTS serp_cache (k TEXT PRIMARY KEY, at REAL, body TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS spend (at REAL)")
    c.execute("""CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at REAL,
                 fora_key TEXT, verdict TEXT, delta_pp REAL, searches INTEGER, body TEXT)""")
    cols = {r[1] for r in c.execute("PRAGMA table_info(audit)")}
    if "source" not in cols:                   # added with the direct source, 2026-09-28
        c.execute("ALTER TABLE audit ADD COLUMN source TEXT")
    return c


def _key(params: dict) -> str:
    clean = {k: v for k, v in sorted(params.items()) if k != "api_key"}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode()).hexdigest()


def searches_left() -> int | None:
    """What SerpApi says is left on the account, or None if it can't be read."""
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return None
    try:
        r = requests.get(SERPAPI_ACCOUNT, params={"api_key": key}, timeout=20)
        return int(r.json().get("total_searches_left"))
    except Exception:
        return None


def spent_today() -> int:
    with _db() as c:
        r = c.execute("SELECT count(*) FROM spend WHERE at > ?", (time.time() - 86400,)).fetchone()
    return int(r[0] or 0)


class Fetcher:
    """`fetch(params)` for flight_qa: cached, budgeted, counted.

    `spent` counts searches actually sent to SerpApi on this check; cached
    answers cost nothing and are marked with `_cached_age_minutes`.
    """

    def __init__(self, reserve: int = RESERVE_SEARCHES, daily_cap: int = DAILY_CAP):
        self.reserve, self.daily_cap, self.spent = reserve, daily_cap, 0
        self._left = None

    def check_budget(self, needed: int) -> None:
        if not os.getenv("SERPAPI_KEY"):
            raise BudgetHeld("SERPAPI_KEY is not set")
        self._left = searches_left()
        if self._left is None:
            b = _flights.serpapi_budget()          # fallback ledger
            self._left = b["limit"] - b["used"]
        if self._left - needed < self.reserve:
            raise BudgetHeld(f"{self._left} SerpApi searches left; QA keeps {self.reserve} "
                             f"in reserve and this check needs up to {needed}")
        if spent_today() + needed > self.daily_cap:
            raise BudgetHeld(f"daily QA cap of {self.daily_cap} searches reached")

    def __call__(self, params: dict) -> dict:
        k = _key(params)
        with _db() as c:
            row = c.execute("SELECT at, body FROM serp_cache WHERE k=?", (k,)).fetchone()
        if row and time.time() - row[0] < CACHE_TTL_SECONDS:
            body = json.loads(row[1])
            body["_cached_age_minutes"] = round((time.time() - row[0]) / 60)
            return body
        full = {"engine": "google_flights", "api_key": os.getenv("SERPAPI_KEY"),
                "currency": "USD", "hl": "en", "gl": "us", **params}
        r = requests.get(_flights.SERPAPI_BASE, params=full, timeout=TIMEOUT_SECONDS)
        self.spent += 1
        _flights._serpapi_spend()
        with _db() as c:
            c.execute("INSERT INTO spend (at) VALUES (?)", (time.time(),))
        body = r.json()
        if r.status_code >= 400 or body.get("error"):
            return {"error": body.get("error") or f"HTTP {r.status_code}"}
        body.pop("search_metadata", None)          # carries SerpApi URLs; not needed
        with _db() as c:
            c.execute("INSERT OR REPLACE INTO serp_cache (k, at, body) VALUES (?,?,?)",
                      (k, time.time(), json.dumps(body)))
        body["_cached_age_minutes"] = 0
        return body


def audit(result: dict) -> None:
    """Keep what each verdict was built from, so a disputed number can be re-read."""
    try:
        with _db() as c:
            c.execute("INSERT INTO audit (at, fora_key, verdict, delta_pp, searches, body, "
                      "source) VALUES (?,?,?,?,?,?,?)",
                      (time.time(), (result.get("fora") or {}).get("key"),
                       result.get("verdict"), (result.get("delta") or {}).get("per_person_usd"),
                       (result.get("public") or {}).get("searches_spent"),
                       json.dumps(result, default=str), result.get("source")))
    except Exception:
        pass                                        # an audit failure never blocks a check


# ─── Google Flights' own pages (primary) ─────────────────────────────────────

class Direct:
    """`booking()` and `search()` for flight_qa.check_direct, cached like SerpApi.

    Answers are kept CACHE_TTL_SECONDS under the page URL, so the rows of one
    Fora search share a result list and a repeat check is instant. "Not sold
    together" is an answer and is cached too; an unreadable page is not.
    """

    def __init__(self, use_cache: bool = True):
        self.use_cache, self.pages = use_cache, 0

    def _cached(self, url: str):
        if not self.use_cache:
            return None
        with _db() as c:
            row = c.execute("SELECT at, body FROM serp_cache WHERE k=?",
                            ("direct:" + url,)).fetchone()
        if row and time.time() - row[0] < CACHE_TTL_SECONDS:
            body = json.loads(row[1])
            if body.get("_unavailable"):
                raise _gf.ItineraryUnavailable(body["_unavailable"])
            body["_cached_age_minutes"] = round((time.time() - row[0]) / 60)
            return body
        return None

    def _keep(self, url: str, body: dict) -> None:
        if self.use_cache:
            with _db() as c:
                c.execute("INSERT OR REPLACE INTO serp_cache (k, at, body) VALUES (?,?,?)",
                          ("direct:" + url, time.time(), json.dumps(body, default=str)))

    def _get(self, url: str, fetch):
        hit = self._cached(url)
        if hit is not None:
            return hit
        self.pages += 1
        try:
            body = fetch()
        except _gf.ItineraryUnavailable as exc:
            self._keep(url, {"_unavailable": exc.detail})
            raise
        self._keep(url, body)
        body["_cached_age_minutes"] = 0
        return body

    def booking(self, legs, *, adults: int, children: int = 0, cabin=1) -> dict:
        url = _gf.booking_url(legs, adults=adults, children=children, cabin=cabin)
        return self._get(url, lambda: _gf.booking(legs, adults=adults, children=children,
                                                  cabin=cabin))

    def search(self, legs, *, adults: int, children: int = 0, cabin=1) -> dict:
        url = _gf.search_url(legs, adults=adults, children=children, cabin=cabin)
        return self._get(url, lambda: _gf.search(legs, adults=adults, children=children,
                                                 cabin=cabin))


def qa_check(pool: list[dict], flights: list[str], *, cabins: list[str], adults: int,
             children: int = 0, pool_age_minutes: float = 0, options=None,
             carriers_for_retry=None, direct: Direct | None = None,
             serp: "Fetcher | None" = None, alternatives: bool = True,
             allow_fallback: bool = True) -> dict:
    """flight_qa for one Fora routing: Google's own pages first, SerpApi after.

    The fallback spends SerpApi searches under the existing budget rules
    (account reserve, daily cap, 6-hour cache). `allow_fallback=False` returns
    NO_DATA with the direct failure instead, for rows that must not spend.
    The result says which source answered (`source`) and, after a fallback,
    why the direct read failed (`direct_error`). Every result is audited.
    """
    direct = direct or Direct()
    try:
        out = _qa.check_direct(pool, flights, cabins=cabins, adults=adults, children=children,
                               direct=direct, pool_age_minutes=pool_age_minutes,
                               options=options, alternatives=alternatives)
        out["searches_spent"] = 0
    except _gf.DirectError as exc:
        why = str(exc)
        log.warning("Google Flights direct read failed for %s (%s); %s", "+".join(flights),
                    why, "falling back to SerpApi" if allow_fallback else "no fallback")
        if not allow_fallback:
            out = _qa._stamp(_qa._result("NO_DATA", reason=f"Google Flights page could not be "
                                                           f"read ({why}); SerpApi fallback "
                                                           "not used for this row"), None)
        else:
            serp = serp or Fetcher()
            try:
                serp.check_budget(len(cabins) + 2)
            except BudgetHeld as held:
                out = _qa._stamp(_qa._result(
                    "NO_DATA", reason=f"Google Flights page could not be read ({why}) and "
                                      f"the SerpApi budget is held: {held}"), None)
            else:
                out = _qa.check(pool, flights, cabins=cabins, adults=adults, children=children,
                                fetch=serp, pool_age_minutes=pool_age_minutes,
                                carriers_for_retry=carriers_for_retry, options=options)
                out["searches_spent"] = serp.spent
                if out.get("public"):
                    out["public"]["searches_spent"] = serp.spent
        out["direct_error"] = why
    audit(out)
    return out
