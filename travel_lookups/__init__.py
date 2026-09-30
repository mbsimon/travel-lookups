"""Read-only flight, rail and weather lookups.

Four modules, one rule each:

    flights — AeroAPI published schedules; Google Flights fares, read from
              Google's own pages first (google_flights) and SerpApi, behind a
              budget, only when a page cannot be read
    trains  — Transitous open GTFS schedules; no fares
    rail_fares — live per-train fares read from each operator directly
              (Renfe, Iryo, Ouigo, Trenitalia, Italo, Eurostar, DB, SBB, ÖBB)
    weather — forecast, ranges or typical weather, chosen by how far away the
              dates are; storms and alerts from NHC and NWS

None of them books, holds, or reserves anything. flights and trains convert
times into the local zone of each airport or station, because the raw values
are UTC and putting one on a client's page unconverted is a two-hour error
nobody catches. rail_fares reports the local times each operator quotes.
"""
# Michael, 2026-09-28: every flight price quoted to a client says this. The
# fare is checked once against Google Flights at quote time and never
# re-checked, so the note is what covers a price that moves afterward. Equal
# to jev_client.FLIGHT_DISCLAIMER (a test holds them together); defined here
# so a consumer that renders a quote does not need the Jev client.
FLIGHT_DISCLAIMER = ("Flight availability and prices are dynamic and can change "
                     "until tickets are issued.")

from . import flights, trains  # noqa: E402,F401

__all__ = ["FLIGHT_DISCLAIMER", "flights", "trains"]
__version__ = "0.1.0"
