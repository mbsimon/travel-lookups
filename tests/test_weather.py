"""weather: the horizon rules, the climate math and place safety. No network."""
from datetime import date, timedelta

import pytest

from travel_lookups import weather as w

TODAY = date(2026, 9, 30)
MADRID = {"name": "Madrid", "country": "Spain", "country_code": "ES",
          "latitude": 40.42, "longitude": -3.70, "timezone": "Europe/Madrid",
          "population": 3255944}


def _archive_daily(hi=20.0, lo=10.0, wet_every=5):
    first, last = w._climate_span()
    days, d = [], date(first, 1, 1)
    while d <= date(last, 12, 31):
        days.append(d)
        d += timedelta(days=1)
    return {
        "time": [x.isoformat() for x in days],
        # the last 10 years run 1 C warmer
        "temperature_2m_max": [hi + (1.0 if x.year > last - 10 else 0.0) for x in days],
        "temperature_2m_min": [lo for _ in days],
        "precipitation_sum": [5.0 if i % wet_every == 0 else 0.0 for i in range(len(days))],
        "daylight_duration": [12 * 3600 for _ in days],
    }


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(w, "_today", lambda tz=None: TODAY)
    monkeypatch.setattr(w, "_google_geocode", lambda q: None)  # no key in tests
    calls = []
    geo = {"results": [MADRID]}
    ens_rain = {"value": 0.0}

    def fake_get(url, params=None, ttl=0, _retry=True):
        calls.append(url)
        if url == w.GEOCODE_URL:
            return geo
        if url == w.ARCHIVE_URL:
            return {"daily": _archive_daily(), "elevation": 650}
        if url == w.FORECAST_URL:
            days = [(TODAY + timedelta(days=i)).isoformat() for i in range(16)]
            return {"timezone": "Europe/Madrid", "daily": {
                "time": days, "weather_code": [3] * 16,
                "temperature_2m_max": [25.0] * 16, "temperature_2m_min": [12.0] * 16,
                "precipitation_sum": [0.0] * 16, "precipitation_probability_max": [10] * 16,
                "wind_speed_10m_max": [10.0] * 16, "wind_gusts_10m_max": [20.0] * 16,
                "uv_index_max": [5.0] * 16, "sunrise": [None] * 16, "sunset": [None] * 16}}
        if url == w.ENSEMBLE_URL:
            days = [(TODAY + timedelta(days=i)).isoformat() for i in range(15)]
            daily = {"time": days, "temperature_2m_max": [25.0] * 15}
            for m in range(1, 51):
                daily[f"temperature_2m_max_member{m:02d}"] = [24.0 + (m % 3)] * 15
                daily[f"temperature_2m_min_member{m:02d}"] = [12.0] * 15
                daily[f"precipitation_sum_member{m:02d}"] = [ens_rain["value"]] * 15
            return {"daily": daily}
        if url == w.SEASONAL_URL:
            wk = [(TODAY + timedelta(days=7 * i)).isoformat() for i in range(7)]
            return {"weekly": {"time": wk, "temperature_2m_anomaly_gt0": [90] * 7,
                               "precipitation_anomaly_gt0": [50] * 7}}
        if url == w.NHC_STORMS_URL:
            return {"activeStorms": []}
        if url in (w.MARINE_URL, w.AIR_URL, w.NWS_ALERTS_URL):
            return {}
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(w, "_get", fake_get)
    return {"calls": calls, "geo": geo, "ens_rain": ens_rain}


def test_ambiguous_name_across_countries_is_refused(fake):
    fake["geo"]["results"] = [
        {"name": "Valencia", "country": "Venezuela", "country_code": "VE",
         "latitude": 10.2, "longitude": -68.0, "population": 1619470},
        {"name": "Valencia", "country": "Spain", "country_code": "ES",
         "latitude": 39.5, "longitude": -0.4, "population": 824340}]
    with pytest.raises(w.AmbiguousPlace) as e:
        w.resolve("Valencia")
    assert "Spain" in str(e.value) and "Venezuela" in str(e.value)
    assert w.resolve("Valencia, Spain")["country_code"] == "ES"
    assert w.resolve("Valencia", country="ES")["country_code"] == "ES"


def test_coordinates_pass_straight_through(fake):
    p = w.resolve("20.77, -105.52")
    assert (p["latitude"], p["longitude"]) == (20.77, -105.52)
    assert fake["calls"] == []


def test_near_dates_get_day_by_day_with_confidence(fake):
    r = w.trip("Madrid", (TODAY + timedelta(days=1)).isoformat(),
               (TODAY + timedelta(days=3)).isoformat())
    assert r["horizon"] == "day-by-day forecast"
    assert [d["basis"] for d in r["days"]] == ["forecast"] * 3
    assert all("Confidence" in d["line"] for d in r["days"])
    assert r["days"][0]["high"] == 77  # 25 C


def test_days_8_to_15_are_ranges_never_single_day(fake):
    r = w.trip("Madrid", (TODAY + timedelta(days=9)).isoformat(),
               (TODAY + timedelta(days=12)).isoformat())
    assert {d["basis"] for d in r["days"]} == {"forecast range"}
    assert all("high" not in d for d in r["days"])
    assert all(d["confidence"] != "High" for d in r["days"])
    assert any("Typical for these dates" in s for s in r["summary"])


def test_confidence_never_high_past_day_7():
    ens = {"highs": [25.0] * 51, "lows": [12.0] * 51, "rain": [0.0] * 51}
    assert w._confidence(ens, 3) == "High"
    assert w._confidence(ens, 8) == "Moderate"
    assert w._confidence(ens, 11) == "Low"


def test_split_ensemble_is_low_confidence():
    ens = {"highs": [20.0] * 25 + [30.0] * 26, "lows": [10.0] * 51,
           "rain": [0.0] * 25 + [5.0] * 26}
    assert w._confidence(ens, 2) == "Low"


def test_far_future_is_typical_only_and_says_so(fake):
    r = w.trip("Madrid", "2028-04-08", "2028-04-12")
    assert r["horizon"] == "typical weather only"
    assert r["days"] == []
    assert w.FORECAST_URL not in fake["calls"] and w.ENSEMBLE_URL not in fake["calls"]
    assert "not a forecast" in r["typical"]["basis"]
    assert "30 years" in r["client_note"]


def test_mid_range_adds_the_46_day_lean(fake):
    r = w.trip("Madrid", (TODAY + timedelta(days=20)).isoformat(),
               (TODAY + timedelta(days=23)).isoformat())
    assert any("leans warmer than normal (90% of runs)" in s for s in r["summary"])
    assert not any("wetter" in s or "drier" in s for s in r["summary"])  # 50% is no lean


def test_typical_math(fake):
    t = w.typical(MADRID, "2027-07-01", "2027-07-05", units="C")
    assert t["median_low"] == 10
    assert t["median_high"] in (20, 21)
    assert t["wet_day_pct"] == 20       # every 5th day wet
    assert t["expected_wet_days"] == 1  # 20% of 5 days
    assert t["recent_shift"] == 1.0
    assert t["daylight_hours"] == 12.0


def test_climate_history_is_fetched_once_per_place(fake):
    w.typical(MADRID, "2027-07-01", "2027-07-05")
    w.typical(MADRID, "2029-12-24", "2030-01-02")
    assert fake["calls"].count(w.ARCHIVE_URL) == 1


def test_past_dates_are_refused(fake):
    with pytest.raises(w.WeatherError, match="past"):
        w.trip("Madrid", "2026-09-01", "2026-09-05")


@pytest.mark.parametrize("lat,lon,basin", [
    (20.8, -105.5, "East Pacific hurricane"),     # Punta Mita
    (21.1, -86.8, "Atlantic hurricane"),          # Cancun
    (25.1, -77.3, "Atlantic hurricane"),          # Nassau
    (20.8, -156.3, "Central Pacific hurricane"),  # Maui
    (40.4, -3.7, None),                           # Madrid
    (12.43, -86.88, "Atlantic and East Pacific hurricane"),  # León, Nicaragua
    (9.98, -83.03, "Atlantic hurricane"),         # Limón, Costa Rica
])
def test_storm_basins(lat, lon, basin):
    b = w._basin(lat, lon)
    assert (b[0] if b else None) == basin


def test_storm_within_radius_is_reported(monkeypatch):
    monkeypatch.setattr(w, "_get", lambda *a, **k: {"activeStorms": [
        {"name": "Near", "classification": "HU", "intensity": "85",
         "latitudeNumeric": 22.0, "longitudeNumeric": -84.0, "movementDir": 315,
         "movementSpeed": 10, "publicAdvisory": {"url": "https://nhc/x"}},
        {"name": "Far", "classification": "TS", "intensity": "40",
         "latitudeNumeric": 33.4, "longitudeNumeric": -43.6}]})
    storms = w.active_storms(21.1, -86.8)
    assert [s["name"] for s in storms] == ["Near"]
    assert "Hurricane Near" in w.storm_line(storms[0])


def test_rain_words_follow_nws():
    assert w.rain_words(10) == "dry"
    assert w.rain_words(20) == "slight chance of rain"
    assert w.rain_words(40) == "chance of rain"
    assert w.rain_words(70) == "rain likely"
    assert w.rain_words(90) == "rain expected"


def test_coastal_point_carries_the_warm_lows_caveat(fake, monkeypatch):
    real = w._get

    def low_lying(url, params=None, ttl=0, _retry=True):
        d = real(url, params, ttl, _retry)
        if url == w.FORECAST_URL:
            d = dict(d, elevation=3.0)
        return d
    monkeypatch.setattr(w, "_get", low_lying)
    r = w.trip("Madrid", (TODAY + timedelta(days=1)).isoformat())
    assert any("small island" in s for s in r["summary"])


def test_inland_point_has_no_coastal_caveat(fake):
    r = w.trip("Madrid", (TODAY + timedelta(days=1)).isoformat())
    assert not any("small island" in s for s in r["summary"])



def _google(lat, lon, cc, country, name):
    return {"name": name, "country_code": cc, "country": country, "latitude": lat,
            "longitude": lon, "source": "google geocoding"}


def test_city_with_a_country_uses_google_when_it_agrees(fake, monkeypatch):
    """'San Sebastián, Spain' must be Donostia, not La Gomera or Puerto Rico."""
    fake["geo"]["results"] = [
        {"name": "San Sebastian", "country": "Puerto Rico", "country_code": "PR",
         "latitude": 18.3, "longitude": -66.99, "population": 11590},
        {"name": "San Sebastián de La Gomera", "country": "Spain", "country_code": "ES",
         "latitude": 28.09, "longitude": -17.11, "population": 8964}]
    asked = []
    monkeypatch.setattr(w, "_google_geocode", lambda q: asked.append(q) or _google(
        43.32, -1.98, "ES", "Spain", "Donostia / San Sebastián, Gipuzkoa, Spain"))
    p = w.resolve("San Sebastián, Spain")
    assert (round(p["latitude"]), round(p["longitude"])) == (43, -2)
    assert asked == ["San Sebastián, Spain"]
    assert w.resolve("San Sebastián", country="ES")["country_code"] == "ES"
    assert asked[-1] == "San Sebastián, ES"


def test_google_in_another_country_is_ignored(fake, monkeypatch):
    monkeypatch.setattr(w, "_google_geocode", lambda q: _google(18.3, -66.99, "PR", "Puerto Rico", "x"))
    assert w.resolve("Madrid, Spain")["country_code"] == "ES"


def test_no_google_key_falls_back_to_open_meteo(fake, monkeypatch):
    monkeypatch.setattr(w, "_google_geocode", lambda q: None)
    assert w.resolve("Madrid, Spain")["name"] == "Madrid"
