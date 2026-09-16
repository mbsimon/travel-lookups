"""Live flight search via flights.fora.travel, Fora's air1t-powered booking
tool. Read-only — nothing here books or holds, same rule as
`agency-hq/blacklane.py`.

Different in kind from `flights.py`: AeroAPI/SerpApi answer with free or cheap
public data over Michael's own API keys. This rides Michael's personal Fora
**advisor** login (Google SSO), so results are the same live GDS/NDC fares and
commission data the flights.fora.travel UI shows him — not a public metasearch
index. A live MAD-JFK round trip took ~7.2s and returned 100+ priced
itineraries; it's a real query every time, so don't poll it in a loop.

One definition, several front doors: the `travel` MCP server
(`mbsimon/travel-mcp`) exposes `fora_flight_search`, and Agency HQ / the
itinerary pages can import this module directly for the same result without
an MCP round trip.

## Auth chain, captured live 2026-09-16

1. Google SSO sets a NextAuth session cookie (`__Secure-authjs.session-token`)
   on flights.fora.travel, good for about a month. Captured into SESSION_FILE
   by `scripts/fora_flights_save_session.py` (interactive, Mac-only — this
   auth has no password flow to automate). Synced Mac->VPS by cron, same
   pattern as `fora_session.json` (see reference_mail_client / project CLAUDE.md).
2. The actual search is a Next.js **Server Action**, not a REST endpoint.
   air1t's real search API runs server-side inside flights.fora.travel; the
   only air1t calls the browser itself makes (`/airports/search`,
   `/logged_queries`, `/users/{id}/flight_queries`) are autocomplete and
   analytics, not the search — chasing those as "the API" is a dead end.
3. `POST /flights/{ORIG}-{DEST}/{date}+{cabin}/.../?p=...&s=...&r=...&omitbasic=true`
   with header `Next-Action: <hash>` and a JSON-array body
   `[{legs, maxResponses, maxStops, omitBasicEconomy, passengers,
      speedPriority, tripType, includeAlliances, includeAirlines,
      excludeAirlines}, {posthogSessionId}]` returns a React Flight (RSC)
   stream: lines shaped `N:{json}`. Chunk `1` is
   `{"ok": true, "data": {"itineraries": [...]}}` — the whole result set, no
   pagination call needed. `tripType` is always `"ML"` (multi-leg) — even a
   plain round trip is a 2-leg multi-leg query under the hood, which is why
   arbitrary multi-city and per-leg cabin class both just work: `legs` takes
   however many stops you give it, each with its own `cabin`.
4. `NEXT_ACTION` is tied to the current Vercel deployment (`x-deployment-id`)
   and WILL go stale whenever Fora redeploys flights.fora.travel — there is
   no discovery endpoint. `search_legs()` raises `NextActionStaleError` on the
   resulting 404; re-run
   `scripts/fora_flights_save_session.py --capture-action` to relearn it.

## The response isn't quite JSON

Body is brotli by default (`content-encoding: br`) but `requests`' default
`Accept-Encoding` (gzip/deflate/zstd, no br) makes Vercel skip brotli — no
need for the `brotli` package. React Flight also **dedups repeated sub-trees**
(e.g. a return leg's `legs` array identical to an earlier itinerary's) into a
back-reference string like `"$1:data:itineraries:0:legs"` = "chunk 1, then
that JSON path within it". `_resolve_refs` walks the whole tree and inlines
every one of these before anything downstream sees the data.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

import requests

log = logging.getLogger(__name__)

SESSION_FILE = Path(os.getenv(
    "FORA_FLIGHTS_SESSION_FILE",
    os.path.expanduser("~/.config/michaelsimon/fora_flights_session.json"),
))
BASE = "https://flights.fora.travel"

# Captured live 2026-09-16 against deployment dpl_HgFDSmhYykk3167XVBXTbYP1u7Ec.
# Re-capture with `scripts/fora_flights_save_session.py --capture-action` when stale.
NEXT_ACTION = os.getenv("FORA_FLIGHTS_NEXT_ACTION", "7f91d1bfff80247533424a95021993396a25c19735")

# Michael's org-level carrier blacklist (from the session's `organization` object) —
# used as the default `excludeAirlines` so results match what the UI shows.
CARRIER_BLACKLIST_DEFAULT = ["UL", "V7", "N0", "VY", "ID", "VT", "VZ", "SY", "4N"]

CABIN_CODES = {
    "economy": "Y", "premium_economy": "S", "premium economy": "S",
    "business": "C", "first": "F",
    "Y": "Y", "S": "S", "C": "C", "F": "F",
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

HTTP_TIMEOUT = 45
MAX_LEGS = 6  # the UI's own "Multi Leg" tab caps out here


class FlightSearchError(RuntimeError):
    """Search request failed or the response shape was unrecognized."""


class SessionExpiredError(FlightSearchError):
    """The NextAuth session cookie is missing or Fora rejected it — re-run
    scripts/fora_flights_save_session.py."""


class NextActionStaleError(FlightSearchError):
    """The hardcoded Server Action hash no longer matches Fora's deployment —
    re-run scripts/fora_flights_save_session.py --capture-action."""


@dataclass
class Leg:
    origin: str
    destination: str
    date: _date
    cabin: str = "economy"  # economy | premium_economy | business | first

    @property
    def code(self) -> str:
        code = CABIN_CODES.get(self.cabin)
        if code is None:
            raise FlightSearchError(f"Unknown cabin {self.cabin!r} — use economy/premium_economy/business/first")
        return code

    def _path(self) -> str:
        return f"{self.origin}-{self.destination}/{self.date.isoformat()}+{self.code}"

    def _body(self) -> dict:
        return {
            "origin": self.origin, "destination": self.destination,
            "departs": self.date.isoformat(), "timeMode": "departs", "cabin": self.code,
        }


def _load_cookies() -> dict:
    if not SESSION_FILE.exists():
        raise SessionExpiredError(f"No session file at {SESSION_FILE} — run scripts/fora_flights_save_session.py")
    data = json.loads(SESSION_FILE.read_text())
    cookies = {c["name"]: c["value"] for c in data.get("cookies", [])
               if c.get("domain", "").endswith("flights.fora.travel")}
    if "__Secure-authjs.session-token" not in cookies:
        raise SessionExpiredError("Session file has no NextAuth session cookie — "
                                   "run scripts/fora_flights_save_session.py")
    return cookies


_REF_RE = re.compile(r"^\$(\d+):(.*)$")


def _walk(node, path_parts):
    for part in path_parts:
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _resolve_refs(obj, chunks: dict, depth: int = 0):
    """Inline React Flight's `$N:path` dedup back-references (see module
    docstring) — depth-capped against any accidental cycle."""
    if depth > 12:
        return obj
    if isinstance(obj, str):
        m = _REF_RE.match(obj)
        if m:
            chunk_id, path = m.group(1), m.group(2)
            try:
                target = _walk(chunks[chunk_id], path.split(":")) if path else chunks[chunk_id]
            except (KeyError, IndexError, ValueError, TypeError):
                return obj
            return _resolve_refs(target, chunks, depth + 1)
        return obj
    if isinstance(obj, dict):
        return {k: _resolve_refs(v, chunks, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_refs(v, chunks, depth + 1) for v in obj]
    return obj


def _parse_rsc(text: str) -> dict:
    """Parse every numbered line of the React Flight wire format into a
    chunk dict, resolve `$N:path` back-references against it, and return the
    `{ok, data: {itineraries, ...}}` payload from chunk 1."""
    chunks = {}
    for line in text.split("\n"):
        m = re.match(r"^(\d+):(.*)$", line)
        if not m:
            continue
        try:
            chunks[m.group(1)] = json.loads(m.group(2))
        except json.JSONDecodeError:
            continue  # non-JSON chunks (module refs etc.) — not needed here
    if "1" not in chunks:
        raise FlightSearchError("No line '1:' in response — Next-Action hash is probably stale")
    payload = _resolve_refs(chunks["1"], chunks)
    if not payload.get("ok"):
        raise FlightSearchError(f"Search reported not-ok: {payload}")
    return payload["data"]


def _fare0(itinerary: dict) -> dict:
    return (itinerary.get("itineraryFares") or [{}])[0]


def _cabins_per_leg(itinerary: dict) -> list[list[str]]:
    """`itineraryFares[0].cabinClass` is one entry per PHYSICAL SEGMENT, not
    per requested leg — a connecting leg (MAD-CMN-JFK) is two segments, so a
    plain 2-leg round trip with one connection each way carries a 4-entry
    cabinClass, not 2. Zipping it straight against `legs` misaligns on any
    itinerary with a stop (found live: an AT MAD-JFK-via-Casablanca result
    silently reported the wrong cabin on the wrong leg). Split the flat list
    back into one chunk per requested leg using each leg's own segmentKeys
    count, which always matches (verified live)."""
    flat = _fare0(itinerary).get("cabinClass") or []
    legs = itinerary.get("legs", [])
    out, i = [], 0
    for leg in legs:
        n = max(1, len(leg.get("segmentKeys") or ()))
        out.append(flat[i:i + n])
        i += n
    return out


def summarize(itinerary: dict) -> dict:
    """Flatten one itinerary into the fields a human actually wants."""
    legs = itinerary.get("legs", [])
    cabins_per_leg = _cabins_per_leg(itinerary)
    commission = _fare0(itinerary).get("commission") or {}
    return {
        "price_usd": itinerary.get("minFareAmount"),
        # 0 commonly means genuinely non-commissionable (a public/consumer
        # fare), not missing data — NDC/contract fares carry a real number
        # (seen live: AZ/AT via sourcePcc F6V0, contractQualifier TMC26/AT2026).
        "commission_usd": commission.get("amount"),
        "airlines": itinerary.get("marketingAirlineCodes", itinerary.get("airlineCodes")),
        "alliance": itinerary.get("alliance"),
        "stops": itinerary.get("totalStops"),
        "elapsed_minutes": itinerary.get("elapsedTime"),
        "legs": [
            {
                "origin": leg.get("origin"),
                "destination": leg.get("destination"),
                "departs_at": leg.get("departsAt"),
                "arrives_at": leg.get("arrivesAt"),
                "airline": leg.get("marketingAirline"),
                # A single code normally ("C"); "/".join(...) on the rare
                # itinerary where a leg's own connection changes cabin
                # mid-leg, so that's visible rather than silently picking one.
                "cabin": "/".join(dict.fromkeys(cabins_per_leg[i])) if i < len(cabins_per_leg) and cabins_per_leg[i] else None,
                "equipment": leg.get("equipmentCodes"),
                "redeye": leg.get("redeye"),
                "stops": len(leg.get("stopLocation", [])),
            }
            for i, leg in enumerate(legs)
        ],
        "key": itinerary.get("key"),
    }


def search_legs(
    legs: list[Leg],
    adults: int = 1,
    children: int = 0,
    max_stops: int = 3,
    max_responses: int = 200,
    omit_basic_economy: bool = True,
    exclude_airlines: list[str] | None = None,
    strict_cabin: bool = True,
) -> list[dict]:
    """The core call. One-way is a single leg; a round trip is two; a
    multi-city / open-jaw / mixed-cabin trip (e.g. business out, economy
    back) is however many legs you give it, each with its own cabin.

    air1t does NOT strictly filter by the per-leg cabin you ask for — it
    returns a broad pool across cabins (asking for business out / economy
    back on MAD-JFK returned 290 itineraries: 123 were business BOTH ways,
    9 were economy/business, and only 157 actually matched what was asked).
    Sorting that pool by price alone surfaces the cheapest option
    REGARDLESS of cabin, which silently answers a different question than
    the one asked — a real bug found live building this. `strict_cabin`
    (default True) filters to itineraries whose actual per-leg cabin
    (via `_cabins_per_leg`) matches the request exactly; pass False to see
    the full pool, e.g. for "what would it cost to just go all-business
    instead."

    Returns raw itinerary dicts — see `summarize()` to flatten one for
    display. `legs` order is the ORDER FLOWN; there's no reordering here.
    """
    if not legs:
        raise FlightSearchError("search_legs() needs at least one leg")
    if len(legs) > MAX_LEGS:
        raise FlightSearchError(f"{len(legs)} legs — the UI itself caps multi-leg at {MAX_LEGS}")

    path = "/".join(leg._path() for leg in legs)
    url = (f"{BASE}/flights/{path}"
           f"?p=1,0&s=3&r={max_responses}&omitbasic={'true' if omit_basic_economy else 'false'}")

    body = json.dumps([
        {
            "legs": [leg._body() for leg in legs],
            "maxResponses": max_responses,
            "maxStops": max_stops,
            "omitBasicEconomy": omit_basic_economy,
            "passengers": {"adults": adults, "children": children, "seats": adults + children},
            "speedPriority": 10,
            "tripType": "ML",
            "includeAlliances": [],
            "includeAirlines": [],
            "excludeAirlines": exclude_airlines if exclude_airlines is not None else CARRIER_BLACKLIST_DEFAULT,
        },
        {"posthogSessionId": "00000000-0000-0000-0000-000000000000"},
    ])

    cookies = _load_cookies()
    headers = {
        "next-action": NEXT_ACTION,
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "referer": url,
        "user-agent": UA,
        # Deliberately no Accept-Encoding override — requests defaults to
        # gzip/deflate/zstd (no brotli), so Vercel won't send `br` and we
        # don't need the `brotli` package installed to read the body.
    }

    resp = requests.post(url, headers=headers, cookies=cookies, data=body, timeout=HTTP_TIMEOUT)
    if resp.status_code in (401, 403):
        raise SessionExpiredError(f"HTTP {resp.status_code} — session likely expired, "
                                   f"run scripts/fora_flights_save_session.py")
    if resp.status_code == 404:
        raise NextActionStaleError("HTTP 404 on the Server Action call — Next-Action hash is probably stale")
    resp.raise_for_status()

    data = _parse_rsc(resp.text)
    itineraries = data.get("itineraries", [])
    if strict_cabin:
        wanted = [leg.code for leg in legs]
        itineraries = [
            it for it in itineraries
            if [chunk[0] for chunk in _cabins_per_leg(it) if chunk] == wanted
        ]
    return itineraries


def search(
    origin: str,
    destination: str,
    depart_date: _date,
    return_date: _date | None = None,
    cabin: str = "economy",
    return_cabin: str | None = None,
    **kwargs,
) -> list[dict]:
    """Convenience wrapper over `search_legs()` for the common one-way /
    round-trip case. `return_cabin` defaults to `cabin` — pass it explicitly
    for a mixed-cabin round trip (e.g. business out, economy back)."""
    legs = [Leg(origin, destination, depart_date, cabin)]
    if return_date is not None:
        legs.append(Leg(destination, origin, return_date, return_cabin or cabin))
    return search_legs(legs, **kwargs)


if __name__ == "__main__":
    import sys
    from datetime import timedelta

    dep = _date.today() + timedelta(days=30)
    ret = dep + timedelta(days=7)
    orig, dest = (sys.argv[1:3] if len(sys.argv) > 2 else ("MAD", "JFK"))
    print(f"Searching {orig}->{dest} {dep} / back {ret} ...")
    results = search(orig, dest, dep, ret)
    print(f"{len(results)} itineraries")
    for it in sorted(results, key=lambda x: x.get("minFareAmount", 9e9))[:5]:
        s = summarize(it)
        print(f"  ${s['price_usd']:.2f}  {'/'.join(s['airlines'])}  {s['stops']} stops  "
              f"{s['legs'][0]['departs_at']} -> {s['legs'][-1]['arrives_at']}")
