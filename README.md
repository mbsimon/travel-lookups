# travel-lookups

Read-only flight and rail schedule lookups, used for sketching client itineraries.

```python
from travel_lookups import flights, trains, fora_flights

flights.nonstop_service("MAD", "JFK", "2026-09-15")   # AeroAPI, codeshares collapsed
trains.journeys("Madrid Atocha", "Sevilla", "2026-09-15")  # Transitous, free
fora_flights.search("MAD", "JFK", date(2026, 10, 15), date(2026, 10, 22))  # live Fora fares
```

| Module | Source | Cost |
|---|---|---|
| `flights` | AeroAPI schedules; SerpApi fares | ~$0.002/query; fares 250/month with a 50 reserve held in code |
| `trains` | Transitous (open GTFS) | free, no key |
| `fora_flights` | flights.fora.travel (air1t), Michael's advisor login | free, but a real live GDS/NDC query each call — don't poll |

Nothing here books, holds or reserves. Keys/session files come from the
environment and `~/.config/michaelsimon/` — none are in this repo, which is
why it can be public.

`fora_flights` rides Michael's personal Fora advisor session (Google SSO,
captured to a local cookie file — see `scripts/fora_flights_save_session.py`),
not a plain API key, and supports arbitrary multi-city itineraries with a
different cabin class per leg (e.g. business outbound, economy return). See
the module docstring for the full auth chain and response format — it's a
Next.js Server Action, not a REST endpoint. The action's hash rotates on
every Fora frontend deploy; the module rediscovers it itself (reads it out
of the page's own JS bundle) and retries once on a 404, so an ordinary Fora
deploy is invisible to callers — no manual re-capture step needed anymore.

**Basic economy is excluded by default.** Standing rule: never quote it
unless specifically asked. Enforced both at the request (`omitBasicEconomy`)
and again client-side (`baggage == 0` on the fare), since the upstream flag
isn't trusted alone. Every result also carries `basic_economy`,
`checked_bags` and `fare_brands` so the fare class is visible, not just
filtered — pass `omit_basic_economy=False` to see it when asked for.

## Why it exists separately

It was implemented twice — once in agency-hq for the Slack verbs and MCP tools,
once in fora-apps for itinerary pages — and the copies drifted. AeroAPI's
airport lookup does not prefer IATA, so a bare `HND` resolves to Henderson
Executive in Las Vegas rather than Tokyo Haneda; the guard for that lived in one
copy and not the other, and the copy without it was rendering client pages.

Consumers pin a tag:

```
travel-lookups @ git+https://github.com/mbsimon/travel-lookups@v0.1.0
```

so an upgrade is an explicit, visible step rather than a surprise at the next
container restart.

## The two traps, both verified the hard way

**Times are UTC.** Every airport and station carries its own timezone. AeroAPI
reports a Madrid departure as `08:25Z`, which is 10:25 on the board; Transitous
honours offsets (asking for `05:00+02:00` returns an earlier train than
`05:00Z`), so its `Z` is real too. Both modules convert; neither exposes the raw
value as a time.

**Resolution fails closed.** An airport whose IATA does not match what was asked
comes back `iata_verified: False`. A station search that matches no rail stop
raises rather than returning the nearest thing with a name — relaxing that once
resolved "Seville" to a bus stop in the United Kingdom.
