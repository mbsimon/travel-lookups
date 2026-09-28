"""No test opens a browser or reaches Google Flights.

itineraries() and flight_qa read Google's own pages first. Tests exercise
that path through captured fixtures and fakes; the switch below makes any
real fetch raise DirectError("disabled") instead, which is the fallback path.
"""
import os

os.environ["GOOGLE_FLIGHTS_DIRECT"] = "0"
