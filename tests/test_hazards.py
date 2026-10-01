"""hazards: the events the first storm watch missed. No network."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from travel_lookups import hazards as hz

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def test_noreaster_coastal_flood_warning_is_caught_once():
    """September 25-28, 2026, Midtown Manhattan: 20 alerts, one warning that matters."""
    feats = json.loads((FIX / "nws_noreaster_nyc_2026-09-25.json").read_text())["features"]
    got = hz.nws_from_features(feats)
    assert [h["kind"] for h in got] == ["Coastal Flood Warning"]
    assert "Wind Advisory" not in json.dumps(got)


@pytest.mark.parametrize("event,kept", [
    ("Winter Storm Warning", True), ("High Wind Warning", True),
    ("Excessive Heat Warning", True), ("Hurricane Watch", True),
    ("Wind Advisory", False), ("Coastal Flood Statement", False),
    ("Special Weather Statement", False)])
def test_nws_keeps_warnings_and_tropical_watches(event, kept):
    assert hz.nws_keep(event) is kept


# ─── MeteoAlarm ──────────────────────────────────────────────────────────────

def _ma(level, color, code="ES219", expires="2026-10-02T23:59:00+02:00", kind="Rain"):
    return {"alert": {"msgType": "Alert", "info": [
        {"language": "es-ES", "parameter": []},
        {"language": "en-GB", "senderName": "AEMET", "event": f"{color} {kind} warning",
         "headline": f"{color} {kind.lower()} warning. Madrid", "onset": "2026-10-02T00:00:00+02:00",
         "expires": expires, "web": "https://www.aemet.es",
         "area": [{"areaDesc": "Madrid", "geocode": [{"valueName": "EMMA_ID", "value": code}]}],
         "parameter": [{"valueName": "awareness_level", "value": f"{level}; {color}; x"},
                       {"valueName": "awareness_type", "value": f"10; {kind}"}]}]}}


def test_meteoalarm_orange_in_region_is_kept():
    got = hz.meteoalarm_from_warnings([_ma(3, "orange")], {"ES219"}, NOW)
    assert len(got) == 1 and got[0]["severity"] == "orange"
    assert "AEMET" in got[0]["title"]


def test_meteoalarm_drops_yellow_other_regions_and_expired():
    ws = [_ma(2, "yellow"), _ma(4, "red", code="ES079"),
          _ma(4, "red", expires="2026-09-30T00:00:00+02:00")]
    assert hz.meteoalarm_from_warnings(ws, {"ES219"}, NOW) == []


def test_meteoalarm_point_in_region(monkeypatch, tmp_path):
    monkeypatch.setattr(hz, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(hz, "_ensure_geocodes", lambda: None)
    hz._region_memo.clear()
    (tmp_path / "meteoalarm").mkdir()
    square = {"type": "Polygon", "coordinates": [[[-4, 40], [-3, 40], [-3, 41], [-4, 41], [-4, 40]]]}
    (tmp_path / "meteoalarm" / "ES.json").write_text(json.dumps(
        [{"code": "ES219", "geometry": square}]))
    assert hz.regions_at(40.42, -3.70, "ES") == ["ES219"]
    assert hz.regions_at(37.88, -4.77, "ES") == []


def test_polygon_hole_is_outside():
    geom = {"type": "Polygon", "coordinates": [
        [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
        [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]]}
    assert hz._in_geometry(1, 1, geom)
    assert not hz._in_geometry(5, 5, geom)


# ─── volcanic ash ────────────────────────────────────────────────────────────

# An ash cloud drifting southeast off Etna, the shape of the August 13, 2026
# eruption that closed Catania. The live feed keeps only current warnings, so
# August cannot be replayed; this is the shape a VA SIGMET for Etna takes.
ETNA = [{"firId": "LIRR", "firName": "LIRR ROMA", "qualifier": "ETNA",
         "validTimeTo": 4102444800,  # 2100; a live, unexpired warning
         "coords": [{"lat": 37.80, "lon": 14.95}, {"lat": 37.85, "lon": 15.20},
                    {"lat": 37.40, "lon": 15.40}, {"lat": 37.35, "lon": 15.00},
                    {"lat": 37.80, "lon": 14.95}]}]


def test_etna_ash_flags_taormina_and_catania():
    taormina = hz.ash_from_sigmets(ETNA, 37.85, 15.29)
    catania = hz.ash_from_sigmets(ETNA, 37.47, 15.07)  # CTA airport
    assert taormina and catania
    assert "Etna" in taormina[0]["title"]
    assert catania[0]["distance_km"] == 0  # under the cloud


def test_ash_far_away_is_ignored():
    assert hz.ash_from_sigmets(ETNA, 40.42, -3.70) == []  # Madrid


# ─── GDACS ───────────────────────────────────────────────────────────────────

def _g(t, level, lat, lon, name="x", eid=1):
    return {"properties": {"eventtype": t, "alertlevel": level, "name": name,
                           "eventid": eid, "fromdate": "2026-09-30", "todate": "2026-10-01"},
            "geometry": {"coordinates": [lon, lat]}}


def test_gdacs_rules_by_type():
    feats = [_g("VO", "Green", 37.75, 15.0, "Eruption Etna", 1),     # 20 km, any level
             _g("EQ", "Green", 37.9, 15.2, "small quake", 2),         # below Orange
             _g("WF", "Orange", 39.0, 16.0, "far fire", 3),           # 150 km > 100
             _g("DR", "Red", 37.8, 15.2, "drought", 4)]               # not watched
    got = hz.gdacs_from_features(feats, 37.85, 15.29, set())
    assert [h["key"] for h in got] == ["gdacs:VO:1"]


def test_gdacs_cyclone_already_reported_by_nhc_is_skipped():
    feats = [_g("TC", "Orange", 25.5, -77.0, "Tropical Cyclone IVY-26", 9)]
    assert hz.gdacs_from_features(feats, 25.06, -77.34, {"ivy"}) == []
    assert len(hz.gdacs_from_features(feats, 25.06, -77.34, set())) == 1


def test_near_reports_gaps_instead_of_failing(monkeypatch):
    monkeypatch.setattr(hz, "nhc", lambda lat, lon: [])
    monkeypatch.setattr(hz, "ash", lambda lat, lon: (_ for _ in ()).throw(hz.WeatherError("down")))
    monkeypatch.setattr(hz, "gdacs", lambda lat, lon, names: [])
    out = hz.near(37.85, 15.29, "BS")
    assert out["hazards"] == [] and out["gaps"] == ["ash SIGMETs: down"]


def test_expired_ash_warning_is_dropped():
    old = [dict(ETNA[0], validTimeTo=1786640400)]  # August 2026
    assert hz.ash_from_sigmets(old, 37.85, 15.29) == []
