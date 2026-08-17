"""Read-only flight and rail lookups.

Two modules, one rule each:

    flights — AeroAPI published schedules; SerpApi fares behind a budget
    trains  — Transitous open GTFS schedules; no fares at all

Neither books, holds, or reserves anything. Both convert times into the local
zone of each airport or station, because the raw values are UTC and putting one
on a client's page unconverted is a two-hour error nobody catches.
"""
from . import flights, trains  # noqa: F401

__all__ = ["flights", "trains"]
__version__ = "0.1.0"
