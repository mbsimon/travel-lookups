"""fora_flights — the React Flight ref resolver and the leg/cabin plumbing.

The one thing that would show up as real damage: a `$N:path` back-reference
left unresolved crashes every caller downstream with
`AttributeError: 'str' object has no attribute 'get'` on a live search — this
already happened once building the module (see its docstring). No network;
these use captured/synthetic RSC-shaped fixtures.
"""
from datetime import date

from travel_lookups import fora_flights as ff


def test_resolve_refs_inlines_self_reference():
    # Shape seen live: itineraryFares[0].cabinClass is a back-ref into the
    # itinerary's own cabinCodes list.
    chunks = {
        "1": {
            "ok": True,
            "data": {
                "itineraries": [
                    {"cabinCodes": ["C", "Y"],
                     "itineraryFares": [{"cabinClass": "$1:data:itineraries:0:cabinCodes"}]}
                ]
            },
        }
    }
    resolved = ff._resolve_refs(chunks["1"], chunks)
    fares = resolved["data"]["itineraries"][0]["itineraryFares"]
    assert fares[0]["cabinClass"] == ["C", "Y"]


def test_resolve_refs_leaves_non_ref_strings_alone():
    chunks = {"1": {"ok": True, "data": {"airlineCodes": ["AA"], "alliance": "oneworld"}}}
    resolved = ff._resolve_refs(chunks["1"], chunks)
    assert resolved["data"]["alliance"] == "oneworld"


def test_parse_rsc_extracts_chunk_one():
    text = '0:{"a":"$@1"}\n1:{"ok":true,"data":{"itineraries":[{"minFareAmount":586.53}]}}\n'
    data = ff._parse_rsc(text)
    assert data["itineraries"][0]["minFareAmount"] == 586.53


def test_parse_rsc_raises_on_missing_chunk_one():
    # The shape a stale Next-Action hash produces: a 404 body with no numbered lines.
    import pytest
    with pytest.raises(ff.FlightSearchError):
        ff._parse_rsc("<html>not found</html>")


def test_leg_cabin_code_mapping():
    assert ff.Leg("MAD", "JFK", date(2026, 10, 15), "business").code == "C"
    assert ff.Leg("MAD", "JFK", date(2026, 10, 15), "economy").code == "Y"
    assert ff.Leg("MAD", "JFK", date(2026, 10, 15), "C").code == "C"


def test_leg_path_and_body():
    leg = ff.Leg("MAD", "JFK", date(2026, 10, 15), "business")
    assert leg._path() == "MAD-JFK/2026-10-15+C"
    assert leg._body() == {
        "origin": "MAD", "destination": "JFK",
        "departs": "2026-10-15", "timeMode": "departs", "cabin": "C",
    }


def test_search_legs_rejects_empty():
    import pytest
    with pytest.raises(ff.FlightSearchError):
        ff.search_legs([])


def test_summarize_reads_per_leg_cabin_and_commission():
    itinerary = {
        "minFareAmount": 1726.63,
        "marketingAirlineCodes": ["UX"],
        "alliance": None,
        "totalStops": 0,
        "elapsedTime": 900,
        "key": "abc",
        "legs": [
            {"origin": "MAD", "destination": "JFK", "departsAt": "2026-10-15T12:00:00+02:00",
             "arrivesAt": "2026-10-15T14:00:00-04:00", "marketingAirline": "UX",
             "equipmentCodes": ["772"], "redeye": False, "stopLocation": []},
            {"origin": "JFK", "destination": "MAD", "departsAt": "2026-10-22T16:00:00-04:00",
             "arrivesAt": "2026-10-23T05:00:00+02:00", "marketingAirline": "UX",
             "equipmentCodes": ["772"], "redeye": True, "stopLocation": []},
        ],
        "itineraryFares": [{"cabinClass": ["C", "Y"], "commission": {"amount": 98.65}}],
    }
    s = ff.summarize(itinerary)
    assert [leg["cabin"] for leg in s["legs"]] == ["C", "Y"]
    assert s["commission_usd"] == 98.65


def test_cabins_per_leg_handles_a_connection():
    # Real shape seen live: MAD-CMN-JFK outbound (business both segments),
    # JFK-CMN-MAD return (economy both segments) — a flat 4-entry cabinClass
    # for a 2-leg trip. Zipping cabinClass straight against legs by index
    # would put segment 1 (still business) on the return leg.
    itinerary = {
        "itineraryFares": [{"cabinClass": ["C", "C", "Y", "Y"]}],
        "legs": [
            {"origin": "MAD", "destination": "JFK",
             "segmentKeys": ["20261021MADCMNAT971", "20261022CMNJFKAT202"]},
            {"origin": "JFK", "destination": "MAD",
             "segmentKeys": ["20261202JFKCMNAT201", "20261203CMNMADAT970"]},
        ],
    }
    chunks = ff._cabins_per_leg(itinerary)
    assert chunks == [["C", "C"], ["Y", "Y"]]


def test_summarize_reports_one_cabin_per_leg_through_a_connection():
    itinerary = {
        "minFareAmount": 1992.33,
        "marketingAirlineCodes": ["AT"], "alliance": "oneworld",
        "totalStops": 2, "elapsedTime": 2155, "key": "k",
        "itineraryFares": [{"cabinClass": ["C", "C", "Y", "Y"], "commission": {"amount": 75.28}}],
        "legs": [
            {"origin": "MAD", "destination": "JFK", "departsAt": "x", "arrivesAt": "y",
             "marketingAirline": "AT", "equipmentCodes": [], "redeye": True,
             "stopLocation": ["CMN"], "segmentKeys": ["a", "b"]},
            {"origin": "JFK", "destination": "MAD", "departsAt": "x", "arrivesAt": "y",
             "marketingAirline": "AT", "equipmentCodes": [], "redeye": True,
             "stopLocation": ["CMN"], "segmentKeys": ["c", "d"]},
        ],
    }
    s = ff.summarize(itinerary)
    assert [leg["cabin"] for leg in s["legs"]] == ["C", "Y"]


def test_search_legs_strict_cabin_filters_out_mismatched_fares(monkeypatch):
    # Requested business out / economy back. Server returns a mixed pool —
    # a cheaper all-business result and a pricier genuinely-matching one.
    # strict_cabin=True (the default) must keep only the match.
    itineraries = [
        {"minFareAmount": 1992.33, "itineraryFares": [{"cabinClass": ["C", "C"]}],
         "legs": [{"segmentKeys": ["a"]}, {"segmentKeys": ["b"]}]},
        {"minFareAmount": 3058.13, "itineraryFares": [{"cabinClass": ["C", "Y"]}],
         "legs": [{"segmentKeys": ["c"]}, {"segmentKeys": ["d"]}]},
    ]
    monkeypatch.setattr(ff, "_load_cookies", lambda: {})

    class Resp:
        status_code = 200
        text = '0:{}\n1:' + __import__("json").dumps({"ok": True, "data": {"itineraries": itineraries}})

        @staticmethod
        def raise_for_status():
            pass

    monkeypatch.setattr(ff.requests, "post", lambda *a, **k: Resp)

    legs = [ff.Leg("MAD", "JFK", date(2026, 10, 15), "business"),
            ff.Leg("JFK", "MAD", date(2026, 10, 22), "economy")]

    strict = ff.search_legs(legs)
    assert len(strict) == 1
    assert strict[0]["minFareAmount"] == 3058.13

    loose = ff.search_legs(legs, strict_cabin=False)
    assert len(loose) == 2


def test_discover_next_action_survives_the_wrapped_call_idiom(monkeypatch):
    # Real shape seen live 2026-09-18: the minifier wraps the reference as
    # `(0,s.createServerReference)("<hash>",...)` — a `)(` sits between the
    # name and the call, which broke a first version of the parser that
    # assumed a plain `createServerReference("<hash>"` call and silently
    # never found a match (returning a confusing "action renamed" error for
    # what was actually a parsing bug, not a real Fora change).
    shell_html = '<script src="/_next/static/chunks/app/(private)/flights/%5B%5B...legs%5D%5D/page-abc123.js?dpl=x"></script>'
    bundle_js = ('...let l=(0,s.createServerReference)("7f9f74006c35a7e250251792c58f058315597dcbdb",'
                 's.callServer,void 0,s.findSourceMapURL,"searchFlightsAction");var a=r(61197)...')

    monkeypatch.setattr(ff, "_load_cookies", lambda: {"__Secure-authjs.session-token": "x"})

    calls = []

    class ShellResp:
        status_code = 200
        text = shell_html

        @staticmethod
        def raise_for_status():
            pass

    class BundleResp:
        status_code = 200
        text = bundle_js

        @staticmethod
        def raise_for_status():
            pass

    def fake_get(url, **kwargs):
        calls.append(url)
        return BundleResp if "page-abc123.js" in url else ShellResp

    monkeypatch.setattr(ff.requests, "get", fake_get)

    action_hash = ff._discover_next_action({})
    assert action_hash == "7f9f74006c35a7e250251792c58f058315597dcbdb"
    assert len(calls) == 2  # shell, then the one matching bundle


def test_discover_next_action_raises_when_action_name_is_gone(monkeypatch):
    shell_html = '<script src="/_next/static/chunks/app/(private)/flights/%5B%5B...legs%5D%5D/page-abc123.js?dpl=x"></script>'
    bundle_js = "...some unrelated minified code with no server action in it..."

    class ShellResp:
        status_code = 200
        text = shell_html

        @staticmethod
        def raise_for_status():
            pass

    class BundleResp:
        status_code = 200
        text = bundle_js

        @staticmethod
        def raise_for_status():
            pass

    monkeypatch.setattr(ff.requests, "get",
                        lambda url, **k: BundleResp if "page-abc123.js" in url else ShellResp)

    import pytest
    with pytest.raises(ff.NextActionStaleError, match="searchFlightsAction"):
        ff._discover_next_action({})


def test_post_search_rediscovers_and_retries_once_on_404(monkeypatch):
    # A rotated hash after a Fora redeploy must be invisible to the caller —
    # this is the exact bug found live: fora_flight_search errored on every
    # call after Fora shipped a routine deploy, when a transparent retry
    # should have absorbed it.
    import time as _time
    # fetched_at = now, so the TTL alone wouldn't trigger a rediscovery on
    # attempt 1 — only the 404 (via force=True on attempt 2) should.
    monkeypatch.setattr(ff, "_action_cache", {"hash": "stale-hash", "fetched_at": _time.monotonic()})
    discover_calls = []
    monkeypatch.setattr(ff, "_discover_next_action", lambda cookies: discover_calls.append(1) or "fresh-hash")

    post_calls = []

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.text = '0:{}\n1:' + __import__("json").dumps({"ok": True, "data": {"itineraries": []}})

        def raise_for_status(self):
            pass

    def fake_post(url, headers, cookies, data, timeout):
        post_calls.append(headers["next-action"])
        return Resp(404) if headers["next-action"] == "stale-hash" else Resp(200)

    monkeypatch.setattr(ff.requests, "post", fake_post)

    result = ff._post_search("http://x", "{}", {})
    assert result == {"itineraries": []}
    assert post_calls == ["stale-hash", "fresh-hash"]
    assert len(discover_calls) == 1  # rediscovered exactly once, not on every call


def test_post_search_raises_next_action_stale_if_retry_also_404s(monkeypatch):
    monkeypatch.setattr(ff, "_action_cache", {"hash": "h1", "fetched_at": 0.0})
    monkeypatch.setattr(ff, "_discover_next_action", lambda cookies: "h2")

    class Resp:
        status_code = 404
        text = ""

        @staticmethod
        def raise_for_status():
            pass

    monkeypatch.setattr(ff.requests, "post", lambda *a, **k: Resp)

    import pytest
    with pytest.raises(ff.NextActionStaleError):
        ff._post_search("http://x", "{}", {})


def test_post_search_distinguishes_session_expiry_from_stale_hash(monkeypatch):
    class Resp:
        status_code = 401
        text = ""

        @staticmethod
        def raise_for_status():
            pass

    monkeypatch.setattr(ff, "_get_next_action", lambda cookies, force=False: "h")
    monkeypatch.setattr(ff.requests, "post", lambda *a, **k: Resp)

    import pytest
    with pytest.raises(ff.SessionExpiredError):
        ff._post_search("http://x", "{}", {})


def test_get_next_action_caches_until_forced(monkeypatch):
    calls = []
    monkeypatch.setattr(ff, "_action_cache", {"hash": None, "fetched_at": 0.0})
    monkeypatch.setattr(ff, "_discover_next_action", lambda cookies: calls.append(1) or "h")

    assert ff._get_next_action({}) == "h"
    assert ff._get_next_action({}) == "h"  # cached, no second discovery
    assert len(calls) == 1

    ff._get_next_action({}, force=True)  # force always rediscovers
    assert len(calls) == 2
