"""Google Flights (via SerpApi) for the flight QA step: fetch, cache, budget, audit.

Everything here is I/O. The comparison logic lives in `flight_qa.py`, which
takes a `fetch(params) -> dict` callable so it runs the same against a live
SerpApi or against captured fixtures.

Budget: SerpApi's own account endpoint is the truth about what is left (it is
free and not counted as a search). The per-host ledger in `flights.py` drifted
from it on every host, so it is only the fallback when the endpoint is down.
Repeated identical requests are served from a 6-hour cache, and SerpApi does
not count cached or failed searches either.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import requests

from . import flights as _flights

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
            c.execute("INSERT INTO audit (at, fora_key, verdict, delta_pp, searches, body) "
                      "VALUES (?,?,?,?,?,?)",
                      (time.time(), (result.get("fora") or {}).get("key"),
                       result.get("verdict"), (result.get("delta") or {}).get("per_person_usd"),
                       (result.get("public") or {}).get("searches_spent"),
                       json.dumps(result, default=str)))
    except Exception:
        pass                                        # an audit failure never blocks a check
