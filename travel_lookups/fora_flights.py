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

**Basic economy is excluded by default, everywhere.** Michael's standing
rule: never quote it unless specifically asked. `search_legs()`'s
`omit_basic_economy=True` default both asks air1t to exclude it AND
verifies client-side (`_is_basic_economy`, mainly `baggage == 0` on the
fare) in case the upstream flag ever misses one — this is a third-party API
with no contract, so the "never" isn't trusted to a single upstream switch.
Every surfaced itinerary also carries `basic_economy`, `checked_bags` and
`fare_brands` (the airline's own name — MAIN CABIN, LITE, OPTIMA, DELTA
MAIN BASIC, ...) via `summarize()`, so the caller can SEE the fare class
rather than just trust it was filtered.

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
4. The Server Action hash is tied to the current Vercel deployment and WILL
   go stale whenever Fora redeploys flights.fora.travel — there is no
   documented discovery endpoint, but there doesn't need to be: Next.js embeds
   the hash in the page's own JS bundle as
   `createServerReference("<hash>", ..., "searchFlightsAction")`, and the
   ACTION NAME is a stable, human-chosen string that survives redeploys even
   though the hash rotates every time. `_discover_next_action()` fetches the
   page shell, finds the one script tag for the flights page component, greps
   that bundle for the reference, and caches the result for an hour.
   `search_legs()` rediscovers and retries ONCE on a 404 before raising
   `NextActionStaleError` — which after a real rediscovery failure means Fora
   changed something structural (renamed the action, restructured the page),
   not just redeployed with the same shape. First found stale live
   2026-09-18: a routine Fora deploy between one session and the next turned
   every search into a 404 until this was built.

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
import time
from dataclasses import dataclass
from datetime import date as _date, timedelta as _timedelta
from pathlib import Path

import requests

log = logging.getLogger(__name__)

SESSION_FILE = Path(os.getenv(
    "FORA_FLIGHTS_SESSION_FILE",
    os.path.expanduser("~/.config/michaelsimon/fora_flights_session.json"),
))
BASE = "https://flights.fora.travel"

# Optional manual pin/override — mainly for tests. Production relies on
# _discover_next_action() below; leave this unset in normal operation.
_ENV_NEXT_ACTION = os.getenv("FORA_FLIGHTS_NEXT_ACTION")
_SEARCH_ACTION_NAME = "searchFlightsAction"
_ACTION_CACHE_TTL = 3600  # seconds
_action_cache: dict = {"hash": _ENV_NEXT_ACTION, "fetched_at": 0.0}

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
    """The Server Action hash is wrong even after rediscovering it — Fora
    changed something structural (renamed the action, restructured the
    flights page), not just redeployed with the same shape. A plain
    redeploy is handled transparently by _get_next_action()'s retry; seeing
    this error means that retry ALSO failed."""


class FlightsUnavailableError(FlightSearchError):
    """Fora's search page didn't respond as expected and it isn't an auth or
    action-hash problem — Fora itself is likely down or degraded."""


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


_BUNDLE_SRC_RE = re.compile(
    r'src="(/_next/static/chunks/app/\(private\)/flights/[^"]+page-[^"]+\.js[^"]*)"')
# Anchored on the hash, not the action name: this minifier wraps the call as
# `(0,s.createServerReference)("<hash>",...)` — the `)(` between the name and
# its invocation broke a first version of this regex that assumed a plain
# `createServerReference("<hash>"` call. Anchoring on the 40-char hex hash
# itself and only checking that the action name appears somewhere after it
# (within one statement) survives that kind of minifier-idiom variation.
_HASH_RE = re.compile(r'"([0-9a-f]{30,64})"')  # seen live at 42 hex chars; a range survives it changing


def _discover_next_action(cookies: dict) -> str:
    """Find the current Server Action hash by reading it out of the flights
    page's own JS bundle, where Next.js embeds it as
    `createServerReference("<hash>", ..., "searchFlightsAction")` — see the
    module docstring for why the action NAME survives redeploys even though
    the hash doesn't. Two plain GETs, no browser: the page shell (any
    validly-SHAPED route works — the values don't need to correspond to a
    real search, only the URL pattern needs to route to the flights page
    component), then the one script tag matching that component.
    """
    dummy_date = (_date.today() + _timedelta(days=30)).isoformat()
    shell_url = f"{BASE}/flights/MAD-JFK/{dummy_date}+Y"
    headers = {"user-agent": UA}

    resp = requests.get(shell_url, cookies=cookies, headers=headers, timeout=HTTP_TIMEOUT)
    if resp.status_code in (401, 403):
        raise SessionExpiredError(f"HTTP {resp.status_code} fetching the search page — session likely "
                                   f"expired, run scripts/fora_flights_save_session.py")
    if resp.status_code >= 500:
        raise FlightsUnavailableError(f"HTTP {resp.status_code} fetching the search page — Fora itself "
                                       f"looks to be down or degraded, not an auth or hash problem")
    resp.raise_for_status()

    m = _BUNDLE_SRC_RE.search(resp.text)
    if not m:
        raise NextActionStaleError(
            "No flights-page script tag in the shell HTML — Fora likely restructured the app "
            "(not just redeployed the same shape), so pattern-matching the bundle needs a look")
    bundle_url = BASE + m.group(1)

    bundle_resp = requests.get(bundle_url, cookies=cookies, headers=headers, timeout=HTTP_TIMEOUT)
    bundle_resp.raise_for_status()

    text = bundle_resp.text
    name_idx = text.find(f'"{_SEARCH_ACTION_NAME}"')
    if name_idx == -1:
        raise NextActionStaleError(
            f"Found the flights bundle but no {_SEARCH_ACTION_NAME!r} reference in it — Fora likely "
            f"renamed or restructured the search action itself, needs a fresh look at the bundle")

    # The hash sits earlier in the same statement, e.g.
    # `(0,s.createServerReference)("<hash>",s.callServer,void 0,...,"searchFlightsAction")` —
    # take the LAST hash-shaped string before the action name rather than
    # assuming any particular call-wrapping idiom around createServerReference.
    preceding = text[max(0, name_idx - 400):name_idx]
    matches = list(_HASH_RE.finditer(preceding))
    if not matches:
        raise NextActionStaleError(
            f"Found {_SEARCH_ACTION_NAME!r} in the bundle but no hash-shaped string ahead of it — "
            f"the minifier's call-wrapping idiom around createServerReference changed, needs a fresh look")

    action_hash = matches[-1].group(1)
    log.info("fora_flights: discovered Next-Action hash %s", action_hash)
    return action_hash


def _get_next_action(cookies: dict, force: bool = False) -> str:
    """Cached for an hour; `force=True` (used on a 404 retry) always
    rediscovers regardless of the cache or any FORA_FLIGHTS_NEXT_ACTION pin —
    a pin that's gone stale must not loop forever."""
    now = time.monotonic()
    stale = (now - _action_cache["fetched_at"]) > _ACTION_CACHE_TTL
    if force or _action_cache["hash"] is None or stale:
        _action_cache["hash"] = _discover_next_action(cookies)
        _action_cache["fetched_at"] = now
    return _action_cache["hash"]


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


# Airlines file basic economy under their own brand name (Air Europa "LITE",
# Air France/KLM "LIGHT", Delta "DELTA MAIN BASIC") — no single string is
# universal. `baggage == 0` is: every basic-economy-branded fare seen live
# across AF/KL/DL/UX carried it, every MAIN CABIN/STANDARD/FLEX/OPTIMA/
# COMFORT-branded fare carried `baggage: 1`. Falls back to a brand-name
# substring match only when `baggage` itself is missing — a false negative
# here means a basic economy fare gets quoted as if it weren't, which is
# the one failure mode worth over-guarding against.
_BASIC_ECONOMY_BRAND_MARKERS = ("BASIC", "LITE", "LIGHT")


def _is_basic_economy(fare: dict) -> bool:
    baggage = fare.get("baggage")
    if baggage is not None:
        return baggage == 0
    return any(
        marker in (b.get("brandName") or "").upper()
        for b in (fare.get("branding") or [])
        for marker in _BASIC_ECONOMY_BRAND_MARKERS
    )


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
    fare0 = _fare0(itinerary)
    commission = fare0.get("commission") or {}
    # branding[] is per PHYSICAL SEGMENT like cabinClass, EXCEPT it sometimes
    # collapses consecutive segments that share a brand into fewer entries —
    # seen live: a 2-segment leg reported as ONE branding entry when both
    # segments were the same brand, vs. cabinClass, which never collapses.
    # That makes branding unsafe to zip against legs the way cabin is, so
    # this surfaces the distinct brand names present without claiming which
    # leg each belongs to — still enough to answer "is any part of this
    # basic economy" and "what's it actually called".
    fare_brands = list(dict.fromkeys(
        b.get("brandName") for b in (fare0.get("branding") or []) if b.get("brandName")))
    # Both prices are named for what they are. This field used to be
    # `price_usd`, carrying minFareAmount, which is PER PERSON (the same
    # JFK-LHR fare read $2,954.33 at 1 adult and at 2). A 2-adult search read
    # as a party total came out at half the real cost, and a business quote
    # was nearly sent at half price. The party total is Fora's own
    # totalFare.totalPrice on the same fare (checked live: exactly N x the
    # per-person fare on every one of 375 itineraries).
    total = (fare0.get("totalFare") or {}).get("totalPrice")
    return {
        "price_per_person_usd": fare0.get("rawFare", itinerary.get("minFareAmount")),
        "price_total_usd": total,
        # Per ticket, like the price: $154 at 1 adult and at 2. 0 commonly
        # means genuinely non-commissionable (a public/consumer fare), not
        # missing data — NDC/contract fares carry a real number (seen live:
        # AZ/AT via sourcePcc F6V0, contractQualifier TMC26/AT2026).
        "commission_per_ticket_usd": commission.get("amount"),
        # Checked-bag count and the airline's own fare-brand name (MAIN
        # CABIN, LITE, OPTIMA, ...) — surfaced explicitly, not just filtered,
        # per Michael: he needs to SEE the fare class, not just trust it was
        # excluded. basic_economy is the one to gate any quote on.
        "basic_economy": _is_basic_economy(fare0),
        "checked_bags": fare0.get("baggage"),
        "fare_brands": fare_brands,
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


def _post_search(url: str, body: str, cookies: dict) -> dict:
    """POST the Server Action call and return the parsed `{itineraries, ...}`
    data. Discovers the action hash on first use, and on a 404 — the
    signature of a rotated hash after a Fora redeploy — rediscovers and
    retries exactly once before giving up. A caller never sees
    NextActionStaleError for an ordinary redeploy; only a genuine structural
    change (the rediscovery itself failing, or the retry ALSO 404ing) raises."""
    headers = {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "referer": url,
        "user-agent": UA,
        # Deliberately no Accept-Encoding override — requests defaults to
        # gzip/deflate/zstd (no brotli), so Vercel won't send `br` and we
        # don't need the `brotli` package installed to read the body.
    }

    for attempt in (1, 2):
        action_hash = _get_next_action(cookies, force=(attempt == 2))
        resp = requests.post(url, headers={**headers, "next-action": action_hash},
                             cookies=cookies, data=body, timeout=HTTP_TIMEOUT)
        if resp.status_code in (401, 403):
            raise SessionExpiredError(f"HTTP {resp.status_code} — session likely expired, "
                                       f"run scripts/fora_flights_save_session.py")
        if resp.status_code >= 500:
            raise FlightsUnavailableError(f"HTTP {resp.status_code} — Fora itself looks to be down "
                                           f"or degraded, not an auth or hash problem")
        if resp.status_code == 404:
            if attempt == 1:
                log.warning("fora_flights: 404 with action hash %s — rediscovering and retrying once",
                           action_hash)
                continue
            raise NextActionStaleError(
                f"Still HTTP 404 after rediscovering the action hash (now {action_hash}) — Fora "
                f"changed something structural, not just redeployed the same shape")
        resp.raise_for_status()
        return _parse_rsc(resp.text)

    raise AssertionError("unreachable")  # the loop always returns or raises


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

    `omit_basic_economy` (default True): Michael's standing rule is he NEVER
    wants basic economy quoted unless he specifically asked for it. Fora's
    own `omitBasicEconomy` request flag does the real filtering (verified
    live: 0 of 76 basic-economy fares survived it on a route/date where they
    were definitely on sale) but this is a third-party API with no contract,
    so that's not trusted alone — every returned itinerary is also checked
    client-side (`_is_basic_economy`, mainly `baggage == 0`) and dropped if
    it slipped through anyway. Pass False only when Michael has actually
    asked to see basic economy.

    Returns raw itinerary dicts — see `summarize()` to flatten one for
    display (`basic_economy`, `checked_bags`, `fare_brands`). `legs` order
    is the ORDER FLOWN; there's no reordering here.
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
    data = _post_search(url, body, cookies)
    itineraries = data.get("itineraries", [])
    if strict_cabin:
        wanted = [leg.code for leg in legs]
        itineraries = [
            it for it in itineraries
            if [chunk[0] for chunk in _cabins_per_leg(it) if chunk] == wanted
        ]
    if omit_basic_economy:
        itineraries = [it for it in itineraries if not _is_basic_economy(_fare0(it))]
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


def health_check() -> dict:
    """Exercise the whole chain — session, action discovery, and a real live
    search — so a broken deploy fails loudly here instead of mid a client
    search. Deliberately a real one-way query (nothing about this module is
    metered), not a mocked ping: the failure modes worth catching (expired
    session, rotated-and-undiscoverable action hash, Fora itself down) only
    show up under a real request. Not wired into travel-mcp's routine
    `/health` — that fires every 60s and shouldn't hit Fora's live search on
    that cadence. Run by hand or from a deploy-time smoke step instead."""
    dep = _date.today() + _timedelta(days=30)
    try:
        results = search_legs([Leg("MAD", "JFK", dep, "economy")], max_responses=10)
    except FlightSearchError as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    return {"ok": True, "itineraries_found": len(results)}


if __name__ == "__main__":
    import sys
    from datetime import timedelta

    if "--health" in sys.argv:
        print(health_check())
        sys.exit(0)

    dep = _date.today() + timedelta(days=30)
    ret = dep + timedelta(days=7)
    orig, dest = (sys.argv[1:3] if len(sys.argv) > 2 else ("MAD", "JFK"))
    print(f"Searching {orig}->{dest} {dep} / back {ret} ...")
    results = search(orig, dest, dep, ret)
    print(f"{len(results)} itineraries")
    for it in sorted(results, key=lambda x: x.get("minFareAmount", 9e9))[:5]:
        s = summarize(it)
        print(f"  ${s['price_per_person_usd']:.2f}/person  {'/'.join(s['airlines'])}  {s['stops']} stops  "
              f"{s['legs'][0]['departs_at']} -> {s['legs'][-1]['arrives_at']}")
