"""Unit tests for the distance matrix wrapper.

Covers cache key construction, the Haversine fallback path, single-point
short-circuit, and Yandex matrix parsing. Real Yandex calls are blocked by
conftest's `block_external_side_effects` fixture; we substitute the HTTP
layer via monkeypatch.
"""

from unittest.mock import MagicMock

import pytest

from business_app.utils import distance_matrix as dm


@pytest.mark.unit
class TestCacheKey:
    def test_same_coords_produce_same_key(self):
        a = dm._cache_key([(41.30001, 69.25001), (41.32, 69.27)], traffic=True)
        b = dm._cache_key([(41.30001, 69.25001), (41.32, 69.27)], traffic=True)
        assert a == b

    def test_traffic_flag_changes_key(self):
        pts = [(41.3, 69.25), (41.32, 69.27)]
        assert dm._cache_key(pts, traffic=True) != dm._cache_key(pts, traffic=False)

    def test_tiny_gps_drift_collapses_to_same_key(self):
        # 1e-6 degrees is well under the 5-decimal rounding precision.
        a = dm._cache_key([(41.300001, 69.250001)], traffic=True)
        b = dm._cache_key([(41.300002, 69.250002)], traffic=True)
        assert a == b

    def test_different_point_order_produces_different_key(self):
        # Order matters — the matrix is direction-aware.
        a = dm._cache_key([(41.30, 69.25), (41.32, 69.27)], traffic=True)
        b = dm._cache_key([(41.32, 69.27), (41.30, 69.25)], traffic=True)
        assert a != b


@pytest.mark.unit
class TestEdgeCases:
    def test_empty_input_short_circuits(self, app):
        with app.app_context():
            matrix, source = dm.get_distance_matrix([], traffic=True)
            assert matrix == {}
            assert source == "empty"

    def test_single_point_returns_trivial_matrix(self, app):
        with app.app_context():
            matrix, source = dm.get_distance_matrix([(41.3, 69.25)], traffic=True)
            assert matrix == {(0, 0): {"distance_km": 0.0, "duration_minutes": 0.0}}
            assert source == "trivial"


@pytest.mark.unit
class TestHaversineFallback:
    def test_falls_back_when_provider_is_not_yandex(self, app):
        # Default `MAPS_PROVIDER` in test config is not 'yandex' → straight to Haversine.
        with app.app_context():
            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)],
                traffic=True,
                provider="osm",
                use_cache=False,
            )
        assert source == "haversine"
        assert matrix[(0, 0)]["distance_km"] == 0.0
        assert matrix[(0, 1)]["distance_km"] > 0
        assert matrix[(0, 1)]["duration_minutes"] > 0

    def test_haversine_results_are_symmetric(self, app):
        with app.app_context():
            matrix, _ = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27), (41.34, 69.30)],
                traffic=True,
                provider="osm",
                use_cache=False,
            )
        for i, j in [(0, 1), (1, 2), (0, 2)]:
            assert matrix[(i, j)]["distance_km"] == pytest.approx(matrix[(j, i)]["distance_km"])

    def test_haversine_does_not_get_cached(self, app, monkeypatch):
        """Fallbacks should NOT poison the cache — we want to retry the real
        provider next time. Verify by asserting that `_matrix_to_cache` is
        never called when the source is haversine."""
        with app.app_context():
            calls = []
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: calls.append(key))
            dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)],
                traffic=True,
                provider="osm",
                use_cache=False,
            )
            assert calls == [], "haversine fallback must not be cached"


@pytest.mark.unit
class TestYandexMatrixParsing:
    def _fake_response(self, status, body):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.text = "ok"
        return resp

    def test_parses_yandex_matrix_response(self, app, monkeypatch):
        """Verify the Yandex matrix path correctly converts m/s to km/min."""
        with app.app_context():
            # Force Yandex creds + override the HTTP layer.
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key-for-tests")
            monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", None)
            yandex_body = {
                "rows": [
                    {
                        "elements": [
                            {"distance": {"value": 0}, "duration": {"value": 0}},
                            {"distance": {"value": 1500}, "duration": {"value": 240}},
                        ]
                    },
                    {
                        "elements": [
                            {"distance": {"value": 1500}, "duration": {"value": 240}},
                            {"distance": {"value": 0}, "duration": {"value": 0}},
                        ]
                    },
                ]
            }
            monkeypatch.setattr(
                dm,
                "request_with_retry",
                lambda **kw: self._fake_response(200, yandex_body),
            )
            # Block cache reads/writes so we go straight to the provider.
            monkeypatch.setattr(dm, "_matrix_from_cache", lambda key: None)
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: None)

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)],
                traffic=True,
                provider="yandex",
                use_cache=False,
            )
            assert source == "yandex_matrix"
            assert matrix[(0, 1)]["distance_km"] == 1.5
            assert matrix[(0, 1)]["duration_minutes"] == 4.0  # 240s = 4 min

    def test_falls_back_to_osrm_when_yandex_completely_fails(self, app, monkeypatch):
        """Yandex matrix AND pairwise both fail → wrapper must try OSRM /table
        next (real-road, no API key). Source label must reflect *what
        actually produced the data*, not what was attempted first. The old
        bug was returning `yandex_pairwise` while every cell was actually
        Haversine — so the user saw straight-line km in the bot."""
        call_log = []

        def fake_request(**kw):
            url = kw.get("url", "")
            call_log.append(url)
            # All Yandex calls fail with the auth error we see in production
            # when the key isn't authorised for routing.
            if "yandex.net" in url:
                return self._fake_response(401, {"errors": ["Apikey rejected."]})
            # OSRM /table call succeeds with real road data.
            if "project-osrm.org" in url:
                return self._fake_response(
                    200,
                    {
                        "code": "Ok",
                        "distances": [[0, 5460], [5460, 0]],   # metres
                        "durations": [[0, 486], [486, 0]],     # seconds
                    },
                )
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", None)
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_matrix_from_cache", lambda key: None)
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: None)

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)],
                traffic=True,
                provider="yandex",
                use_cache=False,
            )
            # Real-road km from OSRM, NOT straight-line Haversine (~3.0 km).
            assert source == "osrm_table"
            assert matrix[(0, 1)]["distance_km"] == 5.46
            assert matrix[(0, 1)]["duration_minutes"] == pytest.approx(8.1, abs=0.1)
            # And we attempted Yandex first.
            assert any("yandex.net" in u for u in call_log)
            assert any("project-osrm.org" in u for u in call_log)

    def test_here_is_tried_first_when_HERE_MAPS_API_KEY_is_set(self, app, monkeypatch):
        """When the HERE key is configured, the wrapper must hit HERE first
        — not Yandex, not OSRM. HERE is our primary because it's traffic-aware
        and has the most generous free tier (250k req/month)."""
        call_log = []

        def fake_request(**kw):
            url = kw.get("url", "")
            call_log.append(url)
            if "matrix.router.hereapi.com" in url:
                return self._fake_response(
                    200,
                    {
                        "matrix": {
                            "numOrigins": 2,
                            "numDestinations": 2,
                            # Row-major: [d(0,0), d(0,1), d(1,0), d(1,1)]
                            "travelTimes": [0, 1020, 1020, 0],   # 17 min
                            "distances": [0, 8000, 8000, 0],     # 8 km
                            "errorCodes": [0, 0, 0, 0],
                        }
                    },
                )
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", "fake-here-key")
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-yandex-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_matrix_from_cache", lambda key: None)
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: None)

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.275, 69.220)],
                traffic=True,
                provider="yandex",
                use_cache=False,
            )

            assert source == "here_matrix"
            # Real-world numbers from HERE response: 8.0 km, 17.0 min.
            assert matrix[(0, 1)]["distance_km"] == 8.0
            assert matrix[(0, 1)]["duration_minutes"] == 17.0
            # Yandex was NOT touched — HERE answered first.
            assert all("yandex" not in u for u in call_log), \
                f"Yandex should not be called when HERE is configured and works; got: {call_log}"

    def test_here_request_is_post_with_correct_body_shape(self, app, monkeypatch):
        """Lock in the request contract per HERE Matrix Routing v8 docs:
        POST, JSON body with origins/destinations as {lat, lng} objects,
        matrixAttributes including both travelTimes and distances, departureTime
        as ISO timestamp for traffic, apiKey as query param."""
        captured = {}

        def fake_request(**kw):
            captured["method"] = kw.get("method")
            captured["url"] = kw.get("url")
            captured["params"] = kw.get("params")
            captured["json"] = kw.get("json")
            return self._fake_response(
                200,
                {
                    "matrix": {
                        "travelTimes": [0, 60, 60, 0],
                        "distances": [0, 1000, 1000, 0],
                        "errorCodes": [0, 0, 0, 0],
                    }
                },
            )

        with app.app_context():
            monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", "the-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_matrix_from_cache", lambda key: None)
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: None)

            dm.get_distance_matrix(
                [(41.30, 69.25), (41.275, 69.220)],
                traffic=True,
                provider="yandex",
                use_cache=False,
            )

            assert captured["method"] == "POST"
            assert captured["url"] == "https://matrix.router.hereapi.com/v8/matrix"
            assert captured["params"]["apiKey"] == "the-key"
            assert captured["params"]["async"] == "false"
            body = captured["json"]
            assert body["origins"] == [{"lat": 41.30, "lng": 69.25}, {"lat": 41.275, "lng": 69.220}]
            assert body["destinations"] == body["origins"]
            assert body["transportMode"] == "car"
            assert "travelTimes" in body["matrixAttributes"]
            assert "distances" in body["matrixAttributes"]
            assert body["regionDefinition"]["type"] == "circle"
            # Traffic enabled → departureTime should be a timestamp, not "any".
            assert body["departureTime"] != "any"
            assert "T" in body["departureTime"]  # ISO 8601 marker

    def test_here_traffic_disabled_sends_departureTime_any(self, app, monkeypatch):
        captured = {}

        def fake_request(**kw):
            captured["json"] = kw.get("json")
            return self._fake_response(
                200,
                {
                    "matrix": {
                        "travelTimes": [0, 60, 60, 0],
                        "distances": [0, 1000, 1000, 0],
                        "errorCodes": [0, 0, 0, 0],
                    }
                },
            )

        with app.app_context():
            monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", "x")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_matrix_from_cache", lambda key: None)
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: None)

            dm.get_distance_matrix(
                [(41.30, 69.25), (41.275, 69.220)],
                traffic=False,
                use_cache=False,
            )
            assert captured["json"]["departureTime"] == "any"

    def test_here_failure_falls_back_to_yandex(self, app, monkeypatch):
        """If HERE returns a 4xx/5xx, the wrapper must continue down the chain
        — Yandex first, OSRM next, Haversine last. The user must never get
        stuck if their primary provider has a hiccup."""
        call_log = []

        def fake_request(**kw):
            url = kw.get("url", "")
            call_log.append(url)
            if "hereapi.com" in url:
                return self._fake_response(503, {"error": "Service Unavailable"})
            if "yandex.net" in url:
                return self._fake_response(
                    200,
                    {
                        "rows": [
                            {"elements": [
                                {"distance": {"value": 0}, "duration": {"value": 0}},
                                {"distance": {"value": 8000}, "duration": {"value": 1020}},
                            ]},
                            {"elements": [
                                {"distance": {"value": 8000}, "duration": {"value": 1020}},
                                {"distance": {"value": 0}, "duration": {"value": 0}},
                            ]},
                        ]
                    },
                )
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", "k")
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "y")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_matrix_from_cache", lambda key: None)
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: None)

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.275, 69.220)],
                traffic=True,
                provider="yandex",
                use_cache=False,
            )
            assert source == "yandex_matrix"
            assert matrix[(0, 1)]["distance_km"] == 8.0
            # Both providers attempted, in order.
            assert any("hereapi.com" in u for u in call_log)
            assert any("yandex.net" in u for u in call_log)

    def test_here_unreachable_cells_get_haversine_backfill(self, app, monkeypatch):
        """HERE returns errorCodes[k] != 0 for unreachable pairs (e.g. island,
        island bridge out, etc). Those cells must fall back to Haversine so
        the matrix is always well-formed and the TSP solver doesn't choke
        on negative or null values."""
        def fake_request(**kw):
            return self._fake_response(
                200,
                {
                    "matrix": {
                        "travelTimes": [0, -1, 60, 0],
                        "distances": [0, -1, 1000, 0],
                        "errorCodes": [0, 3, 0, 0],   # cell (0,1) is unreachable
                    }
                },
            )

        with app.app_context():
            monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", "x")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_matrix_from_cache", lambda key: None)
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: None)

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)],
                traffic=True,
                use_cache=False,
            )
            assert source == "here_matrix"
            # Unreachable cell (0,1): backfilled with positive Haversine.
            assert matrix[(0, 1)]["distance_km"] > 0
            assert matrix[(0, 1)]["duration_minutes"] > 0
            # Reachable cell (1,0): real HERE data.
            assert matrix[(1, 0)]["distance_km"] == 1.0
            assert matrix[(1, 0)]["duration_minutes"] == 1.0

    def test_osrm_eta_is_returned_as_is_with_no_synthetic_adjustment(self, app, monkeypatch):
        """When OSRM is the source, the wrapper must return its free-flow
        durations untouched. We deliberately don't apply any heuristic
        traffic multiplier — that would be guessing dressed as data and
        could be wildly wrong on weekends/holidays/quiet days. The honest
        signal: OSRM ETAs are free-flow; for traffic-aware data the operator
        must use Yandex routing (paid tariff)."""
        def fake_request(**kw):
            url = kw.get("url", "")
            if "yandex.net" in url:
                return self._fake_response(401, {"errors": ["Apikey rejected."]})
            if "project-osrm.org" in url:
                return self._fake_response(
                    200,
                    {
                        "code": "Ok",
                        "distances": [[0, 7700], [7700, 0]],
                        "durations": [[0, 720], [720, 0]],     # 12 min free-flow
                    },
                )
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", None)
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_matrix_from_cache", lambda key: None)
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: None)

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.275, 69.220)],
                traffic=True,
                provider="yandex",
                use_cache=False,
            )

            assert source == "osrm_table"
            assert matrix[(0, 1)]["distance_km"] == 7.7
            # Exactly the OSRM free-flow value: 720s = 12 min, no scaling.
            assert matrix[(0, 1)]["duration_minutes"] == 12.0

    def test_yandex_departure_time_is_unix_timestamp_not_string(self, app, monkeypatch):
        """Yandex requires `departure_time` as a uint32 Unix timestamp. Passing
        the literal string "now" returns 400 Expected uint32, which silently
        kicked the whole request into the Haversine fallback — that's how the
        user ended up with straight-line km in the bot. Lock this in."""
        captured = {}

        def fake_request(**kw):
            captured["params"] = kw.get("params", {})
            return self._fake_response(
                200,
                {
                    "rows": [
                        {"elements": [
                            {"distance": {"value": 0}, "duration": {"value": 0}},
                            {"distance": {"value": 1500}, "duration": {"value": 240}},
                        ]},
                        {"elements": [
                            {"distance": {"value": 1500}, "duration": {"value": 240}},
                            {"distance": {"value": 0}, "duration": {"value": 0}},
                        ]},
                    ]
                },
            )

        with app.app_context():
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", None)
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_matrix_from_cache", lambda key: None)
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: None)

            dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)],
                traffic=True,
                provider="yandex",
                use_cache=False,
            )
            dt = captured["params"].get("departure_time")
            assert isinstance(dt, int), f"departure_time must be int, got {type(dt).__name__}"
            assert dt > 1_000_000_000, "departure_time must be a Unix epoch second value"

    def test_substitutes_haversine_for_unreachable_yandex_cells(self, app, monkeypatch):
        """Yandex sometimes returns elements with null distance/duration when
        a destination is unreachable — for those cells we should substitute a
        Haversine estimate so the matrix is always well-formed."""
        with app.app_context():
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", None)
            yandex_body = {
                "rows": [
                    {
                        "elements": [
                            {"distance": {"value": 0}, "duration": {"value": 0}},
                            {"distance": None, "duration": None},  # unreachable
                        ]
                    },
                    {
                        "elements": [
                            {"distance": None, "duration": None},
                            {"distance": {"value": 0}, "duration": {"value": 0}},
                        ]
                    },
                ]
            }
            monkeypatch.setattr(
                dm,
                "request_with_retry",
                lambda **kw: self._fake_response(200, yandex_body),
            )
            monkeypatch.setattr(dm, "_matrix_from_cache", lambda key: None)
            monkeypatch.setattr(dm, "_matrix_to_cache", lambda key, mx, ttl: None)

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)],
                traffic=True,
                provider="yandex",
                use_cache=False,
            )
            assert source == "yandex_matrix"
            # Unreachable cell should still have a positive Haversine estimate.
            assert matrix[(0, 1)]["distance_km"] > 0
            assert matrix[(0, 1)]["duration_minutes"] > 0
