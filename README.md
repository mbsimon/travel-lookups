# travel-lookups

Read-only flight and rail schedule lookups, used for sketching client itineraries.

```python
from travel_lookups import flights, trains

flights.nonstop_service("MAD", "JFK", "2026-09-15")   # AeroAPI, codeshares collapsed
trains.journeys("Madrid Atocha", "Sevilla", "2026-09-15")  # Transitous, free
```

| Module | Source | Cost |
|---|---|---|
| `flights` | AeroAPI schedules; SerpApi fares | ~$0.002/query; fares 250/month with a 50 reserve held in code |
| `trains` | Transitous (open GTFS) | free, no key |

Nothing here books, holds or reserves. Keys come from the environment
(`AEROAPI_KEY`, `SERPAPI_KEY`) — none are in this repo, which is why it can be
public.

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
