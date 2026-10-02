"""Google Flights read straight from its own pages, with no SerpApi search spent.

Google Flights has no public API. Its page takes the whole search in one URL
parameter, `tfs`: a protobuf, base64url-encoded without padding. Building that
URL and reading the page with a headless browser gives the same figures as
SerpApi, to the dollar (DL2294+DL278 BNA-NAP 2027-05-27: Main Classic $1,166,
Main Extra $1,276, Comfort Classic $1,916, 2026-09-27), for no money.

Two pages matter:

  /travel/flights/search    the result list. Each row's aria-label reads
                            "From 677 US dollars. 1 stop flight with Delta.
                            Leaves ... Total duration 13 hr 25 min. Layover
                            (1 of 1) is a 2 hr 9 min layover at ... Select
                            flight", and the row carries a travelimpactmodel.org
                            link naming every segment (BNA-JFK-DL-4664-20270527).
  /travel/flights/booking   booking options for flights named by number. Each
                            fare is a button labeled "Continue to book with
                            Delta, Delta Main Classic for 1166 US dollars", and
                            its card lists the fare's terms (bags, changes,
                            refunds) in Google's words.

UNITS. Every figure on both pages is the PARTY total: 677 at 1 adult is 1354
at 2 (checked live 2026-09-27, and the page says "Prices include required
taxes + fees for 2 adults"). Everything returned here carries
`party_total_usd` and `per_person_usd` with `tickets`, the labels
`flights.itineraries()` uses, because reading a party total as per person is
the error that made a Fora quote look half price once already.

Verified encoding (2026-09-27): field 1 = 28; field 2 = 2 (1 returns an empty
page); field 3 = one message per leg {2: date, 4: segment, repeatable
{1: origin, 2: date, 3: destination, 5: carrier, 6: number}, 13: {1: 1,
2: IATA}, 14: {1: 1, 2: IATA}}; field 8 = one entry per passenger (1 adult,
2 child, 3 infant in seat, 4 infant on lap); field 9 = cabin (1 economy);
field 14 = 1; field 19 = trip type (1 round trip, 2 one-way, 3 multi-city,
read off a multi-city click-through). A wrong flight number gives "Sorry, the
itinerary you selected is no longer available", which is also what flights
that cannot be sold together on one ticket give.

EU consent: from Spain Google redirects to consent.google.com; a preset SOCS
cookie on .google.com skips it. Rows need element.click() through JS, since
an overlay intercepts ordinary clicks.

Playwright is an optional dependency (`pip install 'travel-lookups[browser]'`)
imported only when a page is fetched, so agency-hq and fora-apps can import
this package without a browser. Without it every fetch raises DirectError
("unavailable") and callers fall back to SerpApi.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("travel_lookups.google_flights")

BASE_URL = "https://www.google.com/travel/flights"
QUERY = "hl=en&gl=us&curr=USD"
CONSENT_COOKIE = {"name": "SOCS", "value": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg",
                  "domain": ".google.com", "path": "/", "secure": True}
SOURCE = "google_direct"

TRIP_ROUND, TRIP_ONE_WAY, TRIP_MULTI = 1, 2, 3
CABINS = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
CABIN_OF_FORA_CODE = {"Y": 1, "S": 2, "W": 2, "C": 3, "J": 3, "F": 4, "P": 4}
ADULT, CHILD, INFANT_IN_SEAT, INFANT_ON_LAP = 1, 2, 3, 4

PAGE_TIMEOUT_SECONDS = float(os.getenv("GOOGLE_FLIGHTS_TIMEOUT", "20"))
LAUNCH_TIMEOUT_SECONDS = 90
# A new browser has an empty cache, and its first Google Flights pages arrive
# too slowly for PAGE_TIMEOUT_SECONDS: on 2026-10-02, 2 of 3 cold reads timed
# out while 3 of 3 warm ones read fine, so every restart sent the next real
# search to SerpApi. One throwaway page right after launch fixed 3 of 3.
WARMUP_URL = "https://www.google.com/travel/flights?hl=en&curr=USD"
WARMUP_SECONDS = 30
SETTLE_SECONDS = 0.8          # rows keep arriving for a moment after the first one
EXPAND_SECONDS = 1.2          # after "View more flights" / "Flight details"
POLL_SECONDS = 0.25
MAX_PAGES = int(os.getenv("GOOGLE_FLIGHTS_MAX_PAGES", "3"))
# "chrome" on the Mac uses the installed Chrome; empty uses Playwright's own
# Chromium, which is what the container ships.
CHANNEL = os.getenv("GOOGLE_FLIGHTS_CHANNEL") or None
BLOCKED_RESOURCES = ("image", "media", "font")
VIEWPORT = {"width": 1280, "height": 900}
MAX_UNPARSED_SHARE = 0.5      # more unreadable rows than this is a page change, not noise


class DirectError(RuntimeError):
    """The page could not be read. `kind` says why; callers fall back on it.

    Kinds: unavailable (no Playwright or no browser), timeout, blocked (a
    captcha or "unusual traffic" page), consent, no_rows, parse (the page
    changed shape), party_mismatch (Google priced a different party),
    wrong_itinerary (the page shows other flights than asked), browser.
    """

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind, self.detail = kind, detail


class ItineraryUnavailable(DirectError):
    """Google answered: these flights are not sold together as asked."""

    def __init__(self, detail: str = "Google says the itinerary is no longer available"):
        super().__init__("itinerary_unavailable", detail)


# ─── Building the URL ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Segment:
    origin: str
    date: str          # the segment's own local departure date, YYYY-MM-DD
    destination: str
    carrier: str       # marketing carrier, IATA ("DL")
    number: str        # "2294"


@dataclass(frozen=True)
class Leg:
    date: str
    origin: str
    destination: str
    segments: tuple = field(default_factory=tuple)


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _field(num: int, value) -> bytes:
    if isinstance(value, int):
        return _varint(num << 3) + _varint(value)
    raw = value.encode() if isinstance(value, str) else value
    return _varint(num << 3 | 2) + _varint(len(raw)) + raw


def _airport(code: str) -> bytes:
    return _field(1, 1) + _field(2, code.strip().upper())     # 1 = airport code


def _segment(s: Segment) -> bytes:
    return (_field(1, s.origin.upper()) + _field(2, s.date) + _field(3, s.destination.upper())
            + _field(5, s.carrier.upper()) + _field(6, str(s.number)))


def trip_type(legs: list[Leg]) -> int:
    if len(legs) == 1:
        return TRIP_ONE_WAY
    if (len(legs) == 2 and legs[0].origin == legs[1].destination
            and legs[0].destination == legs[1].origin):
        return TRIP_ROUND
    return TRIP_MULTI


def cabin_code(cabin) -> int:
    """Google's cabin number from 1-4, "business", or a Fora code ("C")."""
    if isinstance(cabin, int):
        return cabin
    c = str(cabin or "economy").strip()
    if c in CABIN_OF_FORA_CODE:
        return CABIN_OF_FORA_CODE[c]
    if c.lower() in CABINS:
        return CABINS[c.lower()]
    raise ValueError(f"unknown cabin {cabin!r}")


def encode(legs: list[Leg], *, adults: int = 1, children: int = 0, cabin=1,
           trip: int | None = None) -> str:
    """The `tfs` parameter for these legs and passengers."""
    if not legs:
        raise ValueError("at least one leg is required")
    if adults < 1:
        raise ValueError("at least one adult is required")
    msg = _field(1, 28) + _field(2, 2)            # field 2 is always 2
    for leg in legs:
        body = _field(2, leg.date) + b"".join(_field(4, _segment(s)) for s in leg.segments)
        msg += _field(3, body + _field(13, _airport(leg.origin)) + _field(14, _airport(leg.destination)))
    msg += b"".join(_field(8, ADULT) for _ in range(adults))
    msg += b"".join(_field(8, CHILD) for _ in range(children))
    msg += _field(9, cabin_code(cabin)) + _field(14, 1) + _field(19, trip or trip_type(legs))
    return base64.urlsafe_b64encode(msg).decode().rstrip("=")


def search_url(legs: list[Leg], **kw) -> str:
    return f"{BASE_URL}/search?tfs={encode(legs, **kw)}&{QUERY}"


def booking_url(legs: list[Leg], **kw) -> str:
    """Straight to the booking options for flights named by number."""
    if any(not leg.segments for leg in legs):
        raise ValueError("a booking URL needs the flights of every leg")
    return f"{BASE_URL}/booking?tfs={encode(legs, **kw)}&{QUERY}"


def decode(tfs: str) -> dict:
    """Read a `tfs` back into {field: [values]} (nested messages as dicts)."""
    raw = base64.urlsafe_b64decode(tfs + "=" * (-len(tfs) % 4))
    return _decode(raw)


def _decode(buf: bytes) -> dict:
    out, i = {}, 0

    def varint():
        nonlocal i
        n = s = 0
        while True:
            b = buf[i]
            i += 1
            n |= (b & 0x7F) << s
            s += 7
            if not b & 0x80:
                return n
    while i < len(buf):
        key = varint()
        num, wire = key >> 3, key & 7
        if wire == 0:
            val = varint()
        elif wire == 2:
            n = varint()
            sub, i = buf[i:i + n], i + n
            val = sub.decode() if sub and all(32 <= c < 127 for c in sub) else _decode(sub)
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.setdefault(num, []).append(val)
    return out


def segments_from_flights(flights: list[str], legs_airports: list[list[tuple]]) -> list[Leg]:
    """Legs for a booking URL from flight numbers and each segment's
    (origin, destination, local departure "YYYY-MM-DD HH:MM") per leg."""
    it = iter(flights)
    out = []
    for segs in legs_airports:
        built = []
        for o, d, when in segs:
            fl = next(it)
            m = re.match(r"^([A-Z0-9]{2})\s?(\d{1,4})$", fl.strip().upper())
            if not m:
                raise ValueError(f"cannot read flight number {fl!r}")
            built.append(Segment(o, str(when)[:10], d, m.group(1), m.group(2).lstrip("0") or "0"))
        out.append(Leg(built[0].date, built[0].origin, built[-1].destination, tuple(built)))
    return out


# ─── Reading the result list (pure) ──────────────────────────────────────────

def _clean(s: str) -> str:
    return re.sub(r"[    ]+", " ", s or "").strip()


def _minutes(text: str | None) -> int | None:
    if not text:
        return None
    h = re.search(r"(\d+)\s*hr", text)
    m = re.search(r"(\d+)\s*min", text)
    if not h and not m:
        return None
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def _clock(t: str) -> str:
    """"10:42 PM" -> "22:42"."""
    return datetime.strptime(_clean(t).upper(), "%I:%M %p").strftime("%H:%M")


def resolve_date(text: str, on_or_after: str) -> str | None:
    """"Friday, May 28" -> "2027-05-28", the first such date on or after
    `on_or_after` (labels carry no year)."""
    m = re.search(r"([A-Z][a-z]+) (\d{1,2})$", _clean(text))
    if not m:
        return None
    ref = date.fromisoformat(on_or_after[:10])
    for year in (ref.year, ref.year + 1):
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {year}", "%B %d %Y").date()
        except ValueError:
            continue
        if d >= ref - timedelta(days=1):
            return d.isoformat()
    return None


_ROW = re.compile(
    r"^(?:From (?P<price>[\d,]+) US dollars(?P<kind> round trip total| total)?"
    r"|(?P<noprice>Total price is unavailable))\.(?P<note>.*?) ?"
    r"(?:(?P<nonstop>Nonstop)|(?P<stops>\d+) stops?) flight with (?P<airlines>.+?)\. "
    r"(?:Operated by (?P<operated>.+?)\. )?"
    r"Leaves (?P<dep_airport>.+?) at (?P<dep_time>\d{1,2}:\d{2} [AP]M) on (?P<dep_day>[^.]+?) "
    r"and arrives at (?P<arr_airport>.+?) at (?P<arr_time>\d{1,2}:\d{2} [AP]M) on "
    r"(?P<arr_day>[^.]+?)\. Total duration (?P<duration>[^.]+?)\.")
_LAYOVER = re.compile(
    r"Layover \((?P<n>\d+) of (?P<of>\d+)\) is an? (?P<dur>(?:\d+ hr)? ?(?:\d+ min)?) "
    r"(?P<overnight>overnight )?layover at (?P<airport>.+?)(?: in (?P<city>[^.]+?))?\.")


def _names(s: str) -> list[str]:
    return [x.strip() for x in re.split(r", | and ", s) if x.strip()]


def parse_row_label(label: str) -> dict | None:
    """One result row's aria-label, or None when it does not read as a row."""
    s = _clean(label)
    m = _ROW.search(s)
    if not m:
        return None
    lays = []
    for lm in _LAYOVER.finditer(s):
        text = s[lm.start():lm.end()]
        lays.append({"minutes": _minutes(lm.group("dur")), "airport_name": lm.group("airport"),
                     "city": lm.group("city"),
                     "overnight": bool(lm.group("overnight")) or "overnight" in text.lower(),
                     "airport_change": "change" in text.lower() and "airport" in text.lower()})
    kind = (m.group("kind") or "").strip()
    price = m.group("price")
    return {"party_total_usd": int(price.replace(",", "")) if price else None,
            "price_note": _clean(m.group("note")).rstrip(".") or None,
            "price_kind": {"round trip total": "round_trip_total", "total": "trip_total"}.get(
                kind, "one_way"),
            "stops": 0 if m.group("nonstop") else int(m.group("stops")),
            "airlines": _names(m.group("airlines")),
            "operated_by": _names(m.group("operated") or ""),
            "departs": {"airport_name": m.group("dep_airport"), "time": _clock(m.group("dep_time")),
                        "day": m.group("dep_day")},
            "arrives": {"airport_name": m.group("arr_airport"), "time": _clock(m.group("arr_time")),
                        "day": m.group("arr_day")},
            "total_duration_minutes": _minutes(m.group("duration")),
            "layovers": lays}


def parse_tim(url: str | None) -> list[dict]:
    """The row's travelimpactmodel.org link: every segment by number and date."""
    if not url or "itinerary=" not in url:
        return []
    out = []
    for part in url.split("itinerary=", 1)[1].split("&")[0].split(","):
        bits = part.split("-")
        if len(bits) != 5 or not re.fullmatch(r"\d{8}", bits[4]):
            return []
        o, d, carrier, number, ymd = bits
        out.append({"from": o, "to": d, "carrier": carrier, "number": number,
                    "flight": f"{carrier}{number}", "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"})
    return out


_DETAIL_TIME = re.compile(r"^(\d{1,2}:\d{2} [AP]M)(?:\+(\d))?\s*(.+?) \(([A-Z]{3})\)$")
_DETAIL_FLIGHT = re.compile(
    r"^(?P<airline>.*?)(?P<cabin>Economy|Premium economy|Business|First)(?P<aircraft>.*?)"
    r"(?P<code>[A-Z0-9]{2}) (?P<num>\d{1,4})$")


def parse_details(text: str | None, dep_date: str) -> list[dict]:
    """Per-segment local times, cabin and aircraft from an expanded row.

    Best effort: the lines come from innerText, where spans run together
    ("DeltaEconomyBoeing 737DL 2294"). An unreadable block returns [] and the
    row keeps its segment list from the link.
    """
    if not text:
        return []
    lines = [_clean(x) for x in text.splitlines() if _clean(x)]
    times = [m for m in (_DETAIL_TIME.match(x) for x in lines) if m]
    flights = [m for m in (_DETAIL_FLIGHT.match(x) for x in lines) if m]
    if not times or len(times) % 2 or len(times) // 2 != len(flights):
        return []
    base = date.fromisoformat(dep_date[:10])
    out = []
    for k, f in enumerate(flights):
        a, b = times[2 * k], times[2 * k + 1]
        dday = int(a.group(2) or 0)
        aday = int(b.group(2) or 0)
        out.append({"from": a.group(4), "to": b.group(4),
                    "depart": f"{(base + timedelta(days=dday)).isoformat()} {_clock(a.group(1))}",
                    "arrive": f"{(base + timedelta(days=aday)).isoformat()} {_clock(b.group(1))}",
                    "airline": f.group("airline").strip() or None,
                    "cabin": f.group("cabin"), "aircraft": f.group("aircraft").strip() or None,
                    "flight": f"{f.group('code')}{f.group('num')}"})
    return out


def parse_passengers(text: str | None) -> int | None:
    """"Prices include required taxes + fees for 2 adults." -> 2."""
    m = re.search(r"Prices include required taxes \+ fees for (\d+) (?:adults?|passengers?)",
                  _clean(text or ""))
    return int(m.group(1)) if m else None


def build_row(raw: dict, dep_date: str, tickets: int) -> dict | None:
    """One result row as the rest of the package reads itineraries.

    Same keys as `flights._itinerary()`: legs (one per flight), stops,
    party_total_usd, per_person_usd, tickets, layovers, total_duration_minutes,
    carriers; plus booking_segments for a direct booking-options fetch.
    """
    lab = parse_row_label(raw.get("label") or "")
    if lab is None:
        return None
    segs = parse_tim(raw.get("tim"))
    det = parse_details(raw.get("details"), dep_date)
    if det and [d["flight"] for d in det] != [s["flight"] for s in segs]:
        det = []                 # details for another row; keep the link's segments
    legs = []
    for k, s in enumerate(segs):
        d = det[k] if det else {}
        legs.append({"flight": f"{s['carrier']} {s['number']}", "airline": d.get("airline"),
                     "also_sold_by": [], "from": s["from"], "depart": d.get("depart"),
                     "to": s["to"], "arrive": d.get("arrive"), "aircraft": d.get("aircraft"),
                     "cabin": d.get("cabin"), "duration_minutes": None, "date": s["date"]})
    if legs and not det:
        legs[0]["depart"] = f"{dep_date[:10]} {lab['departs']['time']}"
        arr_day = resolve_date(lab["arrives"]["day"], dep_date)
        legs[-1]["arrive"] = f"{arr_day} {lab['arrives']['time']}" if arr_day else None
    lays = []
    for k, lay in enumerate(lab["layovers"]):
        code = segs[k]["to"] if k < len(segs) else None
        nxt = segs[k + 1]["from"] if k + 1 < len(segs) else None
        lays.append({"airport": code if code == nxt or nxt is None else f"{code}/{nxt}",
                     "minutes": lay["minutes"], "overnight": lay["overnight"],
                     "airport_change": bool(code and nxt and code != nxt) or lay["airport_change"]})
    total = lab["party_total_usd"]
    tickets = max(int(tickets or 1), 1)
    return {"legs": legs, "stops": lab["stops"], "party_total_usd": total, "tickets": tickets,
            "per_person_usd": round(total / tickets, 2) if total is not None else None,
            "price_kind": lab["price_kind"], "price_note": lab["price_note"],
            "booking_token": None, "departure_token": None, "extensions": [],
            "layovers": lays, "total_duration_minutes": lab["total_duration_minutes"],
            "price": total, "carriers": lab["airlines"], "operated_by": lab["operated_by"],
            "departs": lab["departs"], "arrives": lab["arrives"],
            "flights": [s["flight"] for s in segs], "segments": segs}


def as_serpapi_option(row: dict) -> dict:
    """A row in SerpApi's Google Flights option shape, for flight_qa's matcher."""
    return {"flights": [{"flight_number": leg["flight"], "airline": leg.get("airline"),
                         "departure_airport": {"id": leg["from"], "time": leg.get("depart")},
                         "arrival_airport": {"id": leg["to"], "time": leg.get("arrive")},
                         "travel_class": leg.get("cabin")} for leg in row["legs"]],
            "layovers": [{"id": lay["airport"], "duration": lay["minutes"],
                          "overnight": lay["overnight"]} for lay in row["layovers"]],
            "total_duration": row["total_duration_minutes"], "price": row["party_total_usd"]}


# ─── Reading booking options (pure) ──────────────────────────────────────────

_FARE = re.compile(
    r"^Continue to book with (?P<who>.+?)(?: for (?P<price>[\d,]+) US dollars)?"
    r"(?: \(Equivalent to (?P<other>[^)]*)\))?$")


def parse_fare_label(label: str) -> dict | None:
    """"Continue to book with Delta, Delta Main Classic for 1166 US dollars"."""
    m = _FARE.match(_clean(label))
    if not m:
        return None
    who = m.group("who")
    seller, brand, airline = who, None, False
    if who.endswith(" airline"):
        seller, airline = who[: -len(" airline")], True
    elif ", " in who:
        seller, brand = who.split(", ", 1)
        airline = True           # Google names fare brands only for the airline itself
    price = m.group("price")
    return {"seller": seller.strip(), "brand": brand.strip() if brand else None,
            "airline_direct": airline,
            "party_total_usd": int(price.replace(",", "")) if price else None,
            "local_price": m.group("other")}


_MONEY_LINE = re.compile(r"^[$€£]\s?[\d,.]+$")


def parse_fare_terms(card: str | None, brand: str | None) -> list[str]:
    """The terms lines of one fare card (bags, changes, refunds, seats)."""
    if not card:
        return []
    lines = [_clean(x) for x in card.splitlines() if _clean(x)]
    out, started = [], False
    for x in lines:
        if x in ("Continue", "View options", "Hide options"):
            break
        if _MONEY_LINE.match(x):
            started = True
            continue
        if started and x != brand:
            out.append(x)
    return out


def classify_terms(terms: list[str]) -> dict:
    """Google's fare terms as refund / changes / free checked bags."""
    t = [x.lower() for x in terms]
    refund = ("full" if any("full refund" in x for x in t) else
              "partial" if any("partial refund" in x for x in t) else
              "none" if any(x.startswith("no refund") for x in t) else None)
    changes = ("free" if any(x.startswith("free change") for x in t) else
               "none" if any("no ticket changes" in x or x == "no changes" for x in t) else
               "fee" if any("changes for a fee" in x for x in t) else None)
    bags = None
    for x in t:
        m = re.search(r"(\d+) free checked bags?", x)
        if m:
            bags = int(m.group(1))
        elif re.search(r"1st checked bag( per passenger)? free", x):
            bags = max(bags or 0, 1)
        elif bags is None and ("checked bag costs" in x or "checked bag available for a fee" in x
                               or re.search(r"1st checked bag( per passenger)?: ", x)):
            bags = 0
    return {"refund": refund, "changes": changes, "free_checked_bags": bags,
            "extra_legroom": any(x == "extra legroom" for x in t)}


# ─── Fare tiers ───────────────────────────────────────────────────────────────
# flight_qa compares brand to brand and never against basic economy. Its
# canonical tiers are these three; a fare that cannot be placed is "unknown"
# and never stands in for another.

TIERS = ("basic", "standard", "flexible")

# Brand names seen live on Google Flights (2026-09-27), seller prefix removed.
BRAND_TIERS = {
    "main basic": "basic", "basic economy": "basic", "economy light": "basic",
    "light": "basic", "main base": "basic", "even more base": "basic",
    "economy basic": "basic", "basic fare": "basic", "basic": "basic",
    "economy saver": "basic", "saver": "basic", "blue basic": "basic",
    "main classic": "standard", "main cabin": "standard", "economy": "standard",
    "economy classic": "standard", "classic": "standard", "main": "standard",
    "economy optimal": "standard", "economy bundle": "standard", "main plus": "standard",
    "economy plus": "standard", "ecofly": "standard", "extrafly": "standard",
    "flexfly": "standard", "comfort classic": "standard", "economy delight": "standard",
    "premium economy": "standard", "base premium economy": "standard",
    "even more": "standard", "standard": "standard", "economy standard": "standard",
    "comfort": "standard", "puente aereo comfort": "standard",
    "main extra": "flexible", "comfort extra": "flexible", "economy classic flex": "flexible",
    "economy delight flex": "flexible", "main flex": "flexible", "even more flex": "flexible",
    "economy fully refundable": "flexible", "economy flex": "flexible", "flex": "flexible",
    "primefly": "flexible",
}
_BASIC_WORDS = re.compile(r"\b(basic|light|lite|saver|base)\b")
_FLEX_WORDS = re.compile(r"\b(flex|fully refundable|refundable)\b")

JEV_MIN_CONFIDENCE = 0.8
JEV_QUESTION = {"tier": {
    "type": "choice",
    "instructions": ("An airline sells this fare brand. Which tier is it? basic is the "
                     "cheapest restricted fare (no or paid changes, paid checked bag, seat "
                     "for a fee); standard is the normal fare (a free checked bag or free "
                     "changes, not refundable); flexible is refundable."),
    "criteria": {"basic": "Cheapest restricted fare: no or paid changes, paid bags",
                 "standard": "Normal fare: free checked bag or free changes, no refund",
                 "flexible": "Refundable or fully flexible fare"}}}
_jev_cache: dict[tuple, tuple] = {}
_jev_lock = threading.Lock()


def _brand_key(brand: str, seller: str | None) -> str:
    b = re.sub(r"[^a-z0-9 ]", "", (brand or "").lower()).strip()
    s = re.sub(r"[^a-z0-9 ]", "", (seller or "").lower()).strip()
    if s and b.startswith(s + " "):
        b = b[len(s) + 1:]
    return re.sub(r"\s+", " ", b)


def tier_from_terms(terms: dict) -> str | None:
    """From Google's own terms for the fare, when they settle it."""
    if terms.get("refund") == "full":
        return "flexible"
    if terms.get("changes") == "none":
        return "basic"
    bags, changes = terms.get("free_checked_bags"), terms.get("changes")
    if bags == 0 and changes in ("fee", None):
        return "basic"
    if changes == "free" or (bags or 0) >= 1:
        return "standard"
    return None


def tier_from_name(brand: str, seller: str | None = None) -> str | None:
    key = _brand_key(brand, seller)
    if key in BRAND_TIERS:
        return BRAND_TIERS[key]
    if _BASIC_WORDS.search(key):
        return "basic"
    if _FLEX_WORDS.search(key):
        return "flexible"
    return None


def _jev_log(entry: dict) -> None:
    """Every Jev mapping, kept: the log line and a JSONL file beside the caches."""
    log.info("jev fare tier: %s / %s -> %s (confidence %s)", entry.get("seller"),
             entry.get("brand"), entry.get("tier"), entry.get("confidence"))
    try:
        base = Path(os.getenv("FLIGHTS_CACHE_DIR", str(Path.home() / ".cache/flight-research")))
        base.mkdir(parents=True, exist_ok=True)
        with open(base / "jev_fare_tiers.jsonl", "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def tier_from_jev(brand: str, seller: str | None) -> tuple[str | None, float | None]:
    """Ask Jev which tier an unknown brand is. Under 0.8 confidence is unknown."""
    key = (_brand_key(brand, seller), (seller or "").lower())
    with _jev_lock:
        if key in _jev_cache:
            return _jev_cache[key]
    try:
        from . import jev_client
        ans = jev_client.ask(f"Airline: {seller or 'unknown'}. Fare brand: {brand}", JEV_QUESTION)
        a = ans["tier"]
        choice, conf = a.get("choice"), float(a.get("confidence") or 0)
    except Exception as exc:                      # no key, timeout: unknown, never a guess
        log.warning("jev fare tier failed for %s / %s: %s", seller, brand, exc)
        return None, None
    tier = choice if choice in TIERS and conf >= JEV_MIN_CONFIDENCE else None
    _jev_log({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seller": seller,
              "brand": brand, "choice": choice, "confidence": conf, "tier": tier,
              "probabilities": a.get("probabilities")})
    with _jev_lock:
        _jev_cache[key] = (tier, conf)
    return tier, conf


def fare_tier(brand: str | None, seller: str | None, terms: dict,
              use_jev: bool = True) -> tuple[str, str]:
    """(tier, how): Google's terms first, then the brand table, then Jev.

    "unknown" when none of them settles it; flight_qa never compares an
    unknown fare as if it were standard.
    """
    t = tier_from_terms(terms)
    if t:
        return t, "terms"
    if not brand:
        return "unknown", "no brand or terms"
    t = tier_from_name(brand, seller)
    if t:
        return t, "brand table"
    if use_jev:
        t, conf = tier_from_jev(brand, seller)
        if t:
            return t, f"jev {conf:.2f}"
        if conf is not None:
            return "unknown", f"jev below {JEV_MIN_CONFIDENCE} ({conf:.2f})"
    return "unknown", "brand not in the table"


def build_fares(raw_cards: list[list], tickets: int, use_jev: bool = True) -> list[dict]:
    """Booking-option cards as the fare ladder flight_qa compares with."""
    out = []
    for label, card in raw_cards:
        f = parse_fare_label(label)
        if f is None or f["party_total_usd"] is None:
            continue
        terms = parse_fare_terms(card, f["brand"])
        cls = classify_terms(terms)
        tier, how = fare_tier(f["brand"], f["seller"], cls, use_jev=use_jev)
        out.append({"seller": f["seller"], "airline_direct": f["airline_direct"],
                    "option_title": f["brand"], "family": tier, "tier_source": how,
                    "party_total_usd": f["party_total_usd"],
                    "per_person_usd": round(f["party_total_usd"] / max(tickets, 1), 2),
                    "terms": terms, "classified_terms": cls,
                    "baggage": [x for x in terms if "bag" in x.lower()]})
    return out


# ─── The browser ──────────────────────────────────────────────────────────────

def _no_sandbox() -> bool:
    if os.getenv("GOOGLE_FLIGHTS_NO_SANDBOX") in ("1", "true", "yes"):
        return True
    return hasattr(os, "geteuid") and os.geteuid() == 0     # root in a container


class _Browser:
    """One headless Chromium per process, owned by its own thread.

    Playwright objects belong to the event loop that made them, and callers
    here are ordinary threads (the MCP server's workers, a thread pool in the
    retail check). So the browser lives on one background loop and callers
    hand it work; a semaphore bounds how many pages are open at once.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._loop = self._pw = self._browser = self._ctx = self._sem = None

    def _alive(self) -> bool:
        return bool(self._loop and self._browser and self._browser.is_connected())

    def _start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise DirectError("unavailable", "Playwright is not installed "
                              "(pip install 'travel-lookups[browser]')") from exc
        if self._loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, daemon=True,
                             name="google-flights-browser").start()
            self._loop = loop

        async def boot():
            if self._pw is None:
                self._pw = await async_playwright().start()
            args = ["--no-sandbox", "--disable-dev-shm-usage"] if _no_sandbox() else []
            browser = await self._pw.chromium.launch(headless=True, channel=CHANNEL, args=args)
            ctx = await browser.new_context(locale="en-US", viewport=VIEWPORT)
            await ctx.add_cookies([CONSENT_COOKIE])

            async def trim(route):
                if route.request.resource_type in BLOCKED_RESOURCES:
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", trim)
            await _prime(ctx)
            return browser, ctx, asyncio.Semaphore(MAX_PAGES)
        try:
            self._browser, self._ctx, self._sem = asyncio.run_coroutine_threadsafe(
                boot(), self._loop).result(timeout=LAUNCH_TIMEOUT_SECONDS)
        except DirectError:
            raise
        except Exception as exc:
            raise DirectError("unavailable", f"could not start the browser: {exc}") from exc

    def run(self, job, timeout: float):
        with self._lock:
            if not self._alive():
                self._start()
            loop, ctx, sem = self._loop, self._ctx, self._sem

        async def guarded():
            async with sem:
                page = await ctx.new_page()
                try:
                    return await job(page)
                finally:
                    await page.close()
        fut = asyncio.run_coroutine_threadsafe(guarded(), loop)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            fut.cancel()
            raise DirectError("timeout", f"no answer in {timeout:.0f}s") from exc
        except DirectError:
            raise
        except Exception as exc:
            raise DirectError("browser", f"{type(exc).__name__}: {exc}") from exc


_browser = _Browser()

_STATE_JS = """(sel) => {
  const u = location.href, t = (document.body && document.body.innerText) || '';
  if (u.includes('consent.google')) return 'consent';
  if (u.includes('/sorry/') || /unusual traffic|not a robot|captcha/i.test(t)) return 'blocked';
  if (/itinerary you selected is no longer available/i.test(t)) return 'unavailable';
  const ok = [...document.querySelectorAll(sel)].some(e => e.offsetParent !== null);
  return ok ? 'ready' : 'waiting';
}"""

_ROWS_JS = """() => {
  const out = [], seen = new Set();
  document.querySelectorAll('li').forEach(li => {
    if (li.offsetParent === null) return;
    const lab = li.querySelector('[aria-label*="Select flight"]');
    if (!lab) return;
    const tim = li.querySelector('[data-travelimpactmodelwebsiteurl]');
    const row = {label: lab.getAttribute('aria-label'),
                 tim: tim ? tim.getAttribute('data-travelimpactmodelwebsiteurl') : null,
                 details: li.innerText};
    const k = row.label + '|' + row.tim;
    if (!seen.has(k)) { seen.add(k); out.push(row); }
  });
  return out;
}"""

_CARDS_JS = """() => [...document.querySelectorAll('[aria-label^="Continue to book with"]')].map(b => {
  let n = b;
  while (n.parentElement &&
         n.parentElement.querySelectorAll('[aria-label^="Continue to book with"]').length === 1 &&
         !/Booking options|Prices include/.test(n.parentElement.innerText))
    n = n.parentElement;
  return [b.getAttribute('aria-label'), n.innerText];
})"""

_TIMS_JS = """() => [...new Set([...document.querySelectorAll('[data-travelimpactmodelwebsiteurl]')]
  .map(e => e.getAttribute('data-travelimpactmodelwebsiteurl')))]"""

async def _prime(ctx) -> bool:
    """Load one Google Flights page so the first real read finds a warm cache.

    Best effort: a warm-up that fails or times out costs the first read its
    head start, never the read itself.
    """
    page = None
    try:
        page = await ctx.new_page()
        await page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=WARMUP_SECONDS * 1000)
        try:
            await page.wait_for_load_state("networkidle", timeout=WARMUP_SECONDS * 1000)
        except Exception:
            pass
        return True
    except Exception:
        return False
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


def warm() -> bool:
    """Start the browser now (and warm it) instead of on the first real read.

    For a long-running server: call it in a background thread at boot so the
    first search after a deploy is as fast as the rest. False when the direct
    source is off or the browser can't start; the reads then fall back as usual.
    """
    if not enabled():
        return False
    try:
        with _browser._lock:
            if not _browser._alive():
                _browser._start()
        return True
    except Exception:
        return False


_PAX_JS = """() => { const t = document.body.innerText; const i = t.indexOf('Prices include');
  return i < 0 ? null : t.slice(i, i + 120); }"""

_CLICK_VISIBLE_JS = """(sel) => { const b = [...document.querySelectorAll(sel)]
  .filter(e => e.offsetParent !== null); b.forEach(e => e.click()); return b.length; }"""


async def _wait(page, selector: str, timeout: float) -> str:
    end = time.monotonic() + timeout
    state = "waiting"
    while time.monotonic() < end:
        try:
            state = await page.evaluate(_STATE_JS, selector)
        except Exception:
            state = "waiting"             # mid-navigation
        if state != "waiting":
            return state
        await asyncio.sleep(POLL_SECONDS)
    return "timeout"


def _raise_for(state: str, what: str) -> None:
    if state == "ready":
        return
    if state == "unavailable":
        raise ItineraryUnavailable()
    if state == "timeout":
        raise DirectError("no_rows", f"no {what} within {PAGE_TIMEOUT_SECONDS:.0f}s")
    raise DirectError(state, f"Google showed a {state} page instead of {what}")


async def _search_job(page, url: str, details: bool) -> dict:
    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_SECONDS * 1000)
    _raise_for(await _wait(page, '[aria-label*="Select flight"]', PAGE_TIMEOUT_SECONDS),
               "result rows")
    await asyncio.sleep(SETTLE_SECONDS)
    if await page.evaluate(_CLICK_VISIBLE_JS, 'button[aria-label="View more flights"]'):
        await asyncio.sleep(EXPAND_SECONDS)
    if details and await page.evaluate(_CLICK_VISIBLE_JS, 'button[aria-label^="Flight details"]'):
        await asyncio.sleep(EXPAND_SECONDS)
    return {"rows": await page.evaluate(_ROWS_JS), "passengers": await page.evaluate(_PAX_JS),
            "final_url": page.url}


async def _booking_job(page, url: str) -> dict:
    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_SECONDS * 1000)
    _raise_for(await _wait(page, '[aria-label^="Continue to book with"]', PAGE_TIMEOUT_SECONDS),
               "booking options")
    await asyncio.sleep(SETTLE_SECONDS)
    return {"cards": await page.evaluate(_CARDS_JS), "tims": await page.evaluate(_TIMS_JS),
            "passengers": await page.evaluate(_PAX_JS), "final_url": page.url}


def _job_timeout() -> float:
    # Queueing behind other pages counts too.
    return PAGE_TIMEOUT_SECONDS * 3 + EXPAND_SECONDS * 2 + 10


def enabled() -> bool:
    """GOOGLE_FLIGHTS_DIRECT=0 turns the direct source off (every caller then
    uses SerpApi). The test suites set it so no test can open a browser."""
    return os.getenv("GOOGLE_FLIGHTS_DIRECT", "1").lower() not in ("0", "false", "no", "off")


def _guard() -> None:
    if not enabled():
        raise DirectError("disabled", "GOOGLE_FLIGHTS_DIRECT is off")


def fetch_search_page(url: str, details: bool = True) -> dict:
    """Raw rows from a search page. I/O only; see `search()`."""
    _guard()
    return _browser.run(lambda page: _search_job(page, url, details), _job_timeout())


def fetch_booking_page(url: str) -> dict:
    """Raw fare cards from a booking page. I/O only; see `booking()`."""
    _guard()
    return _browser.run(lambda page: _booking_job(page, url), _job_timeout())


# ─── Public calls ─────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_party(passengers_text, tickets: int) -> int | None:
    n = parse_passengers(passengers_text)
    if n is not None and n != tickets:
        raise DirectError("party_mismatch", f"Google priced {n} passengers, asked for {tickets}")
    return n


def read_search(raw: dict, legs: list[Leg], tickets: int) -> dict:
    """Pure: raw search-page rows into itineraries. Raises DirectError."""
    rows = raw.get("rows") or []
    if not rows:
        raise DirectError("no_rows", "the result list was empty")
    _check_party(raw.get("passengers"), tickets)
    built = [build_row(r, legs[0].date, tickets) for r in rows]
    bad = sum(b is None for b in built)
    if bad / len(rows) > MAX_UNPARSED_SHARE:
        raise DirectError("parse", f"{bad} of {len(rows)} rows did not read as flights; "
                                   "the page has probably changed")
    # "Total price is unavailable" rows read fine but carry no fare to report.
    its = [b for b in built if b is not None and b["party_total_usd"] is not None]
    first = legs[0].origin
    its = [b for b in its if not b["segments"] or b["segments"][0]["from"] == first
           or trip_type(legs) == TRIP_MULTI] or its
    return {"itineraries": its, "unparsed_rows": bad}


def read_booking(raw: dict, legs: list[Leg], tickets: int, use_jev: bool = True) -> dict:
    """Pure: raw booking-page cards into the fare ladder. Raises DirectError."""
    _check_party(raw.get("passengers"), tickets)
    want = {(s.origin, s.destination, s.carrier, str(s.number), s.date)
            for leg in legs for s in leg.segments}
    shown = {(s["from"], s["to"], s["carrier"], s["number"], s["date"])
             for t in raw.get("tims") or [] for s in parse_tim(t)}
    if shown and want and not want <= shown:
        raise DirectError("wrong_itinerary", "the booking page shows different flights: "
                          + ", ".join(sorted(f"{s[2]}{s[3]}" for s in shown)))
    cards = raw.get("cards") or []
    if cards and all(parse_fare_label(c[0]) is None for c in cards):
        raise DirectError("parse", "no booking option read as a fare; the page has "
                                   "probably changed")
    fares = build_fares(cards, tickets, use_jev=use_jev)
    if not fares:
        raise DirectError("no_rows", "no priced booking option")
    return {"fares": fares}


def search(legs: list[Leg], *, adults: int = 1, children: int = 0, cabin=1,
           details: bool = True) -> dict:
    """The result list for these legs. Raises DirectError; see the module doc."""
    url = search_url(legs, adults=adults, children=children, cabin=cabin)
    raw = fetch_search_page(url, details=details)
    out = read_search(raw, legs, adults + children)
    return {"source": SOURCE, "url": url, "fetched_at": _now(), "tickets": adults + children,
            "currency": "USD", **out}


def booking(legs: list[Leg], *, adults: int = 1, children: int = 0, cabin=1,
            use_jev: bool = True) -> dict:
    """Every fare Google sells on these exact flights, brand by brand."""
    url = booking_url(legs, adults=adults, children=children, cabin=cabin)
    raw = fetch_booking_page(url)
    out = read_booking(raw, legs, adults + children, use_jev=use_jev)
    return {"source": SOURCE, "url": url, "fetched_at": _now(), "tickets": adults + children,
            "currency": "USD", **out}


def available() -> bool:
    """Whether Playwright can be imported here (not whether Google answers)."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False
