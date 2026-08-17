"""Unit tests for the distance matrix wrapper.

Covers cache key construction, the Haversine fallback path, single-point
short-circuit, and Yandex matrix parsing. Real Yandex calls are blocked by
conftest's `block_external_side_effects` fixture; we substitute the HTTP
layer via monkeypatch.
"""

import re
from unittest.mock import MagicMock

import pytest

from business_app.utils import distance_matrix as dm
from business_app.utils.http_client import CircuitBreaker


@pytest.mark.unit
class TestSplitCacheKeys:
    """Spec 8.3: the static tier must be immune to origin movement and stop
    ordering; the live tier must move with the origin but absorb GPS drift."""

    def test_static_key_ignores_stop_order(self):
        a = dm._static_key([(41.30, 69.25), (41.32, 69.27)], traffic=True)
        b = dm._static_key([(41.32, 69.27), (41.30, 69.25)], traffic=True)
        assert a == b

    def test_static_key_changes_when_a_stop_changes(self):
        a = dm._static_key([(41.30, 69.25), (41.32, 69.27)], traffic=True)
        b = dm._static_key([(41.30, 69.25), (41.33, 69.28)], traffic=True)
        assert a != b

    def test_live_key_changes_when_origin_moves(self):
        stops = [(41.32, 69.27)]
        a = dm._live_key((41.300, 69.250), stops, traffic=True)
        b = dm._live_key((41.310, 69.260), stops, traffic=True)
        assert a != b

    def test_tiny_gps_drift_collapses_to_same_live_key(self):
        # 1e-6 degrees is under the 5-decimal rounding precision (~1 m).
        stops = [(41.32, 69.27)]
        a = dm._live_key((41.300001, 69.250001), stops, traffic=True)
        b = dm._live_key((41.300002, 69.250002), stops, traffic=True)
        assert a == b

    def test_traffic_flag_changes_both_keys(self):
        stops = [(41.32, 69.27)]
        assert dm._static_key(stops, True) != dm._static_key(stops, False)
        assert dm._live_key((41.3, 69.25), stops, True) != dm._live_key((41.3, 69.25), stops, False)


@pytest.mark.unit
class TestCacheSplitBehaviour:
    """The whole point of the split (spec 8.3 + design §4.2): today's key
    rounds the MOVING driver into the hash so it never hits. After the
    split, driver movement must cost only the origin row/column."""

    def _fake_table_request(self, call_log):
        def fake_request(**kw):
            url = kw.get("url", "")
            params = dict(kw.get("params") or {})
            call_log.append((url, params))
            n = url.rsplit("/", 1)[-1].count(";") + 1
            cell_d, cell_t = 1500.0, 240.0
            if params.get("sources") == "0":
                distances = [[0.0 if j == 0 else cell_d for j in range(n)]]
                durations = [[0.0 if j == 0 else cell_t for j in range(n)]]
            elif params.get("destinations") == "0":
                distances = [[0.0 if i == 0 else cell_d] for i in range(n)]
                durations = [[0.0 if i == 0 else cell_t] for i in range(n)]
            else:
                distances = [[0.0 if i == j else cell_d for j in range(n)] for i in range(n)]
                durations = [[0.0 if i == j else cell_t for j in range(n)] for i in range(n)]
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"code": "Ok", "distances": distances, "durations": durations}
            resp.text = "ok"
            return resp

        return fake_request

    def _full_calls(self, call_log):
        return [c for c in call_log if "sources" not in c[1] and "destinations" not in c[1]]

    def _partial_calls(self, call_log):
        return [c for c in call_log if "sources" in c[1] or "destinations" in c[1]]

    def test_static_tier_survives_driver_movement(self, app, monkeypatch):
        store = {}
        monkeypatch.setattr(dm, "_cache_get_json", lambda key: store.get(key))
        monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: store.__setitem__(key, payload))
        call_log = []
        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", self._fake_table_request(call_log))

            stops = [(41.310, 69.270), (41.320, 69.280)]
            m1, s1 = dm.get_distance_matrix([(41.300, 69.250)] + stops, traffic=True)
            assert s1 == "osrm_selfhosted"
            assert len(self._full_calls(call_log)) == 1

            # Driver moved ~600 m; stops identical.
            m2, s2 = dm.get_distance_matrix([(41.305, 69.255)] + stops, traffic=True)
            assert s2 == "osrm_selfhosted"
            assert len(self._full_calls(call_log)) == 1, (
                "stop sub-matrix must come from the static cache after the driver moves"
            )
            # Just the sources=0 row — no destinations=0 call. No consumer
            # reads the stop->origin column's VALUE (only its key, for
            # presence), so it's mirrored from the row instead of fetched
            # (task-4 review fix 1).
            assert len(self._partial_calls(call_log)) == 1
            assert m2[(1, 2)] == m1[(1, 2)]  # stop->stop cell served from cache

    def test_identical_repeat_call_is_a_full_cache_hit(self, app, monkeypatch):
        store = {}
        monkeypatch.setattr(dm, "_cache_get_json", lambda key: store.get(key))
        monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: store.__setitem__(key, payload))
        call_log = []
        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", self._fake_table_request(call_log))

            pts = [(41.300, 69.250), (41.310, 69.270), (41.320, 69.280)]
            dm.get_distance_matrix(pts, traffic=True)
            n_calls = len(call_log)
            matrix, source = dm.get_distance_matrix(pts, traffic=True)

            assert source == "cache"
            assert len(call_log) == n_calls, "second identical call must make zero HTTP requests"
            assert matrix[(0, 1)]["distance_km"] == 1.5
            assert matrix[(1, 2)]["duration_minutes"] == 4.0

    def test_haversine_is_never_written_to_either_tier(self, app, monkeypatch):
        writes = []
        monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
        monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: writes.append(key))
        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
            _, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)], traffic=True, use_cache=False
            )
        assert source == "haversine"
        assert writes == []

    def test_two_point_call_never_takes_the_degenerate_partial_path(self, app, monkeypatch):
        """N=2 (a single stop) has no cross-stop pair, so the static tier's
        `cells` is always {} — reusing it buys nothing. Task-4 review fix 2:
        a moved-origin repeat call (the driver->committed-stop leg and the
        bot's next-leg ETA are both exactly this shape, on nearly every
        solve/render) must cost exactly one request per solve, never two."""
        store = {}
        monkeypatch.setattr(dm, "_cache_get_json", lambda key: store.get(key))
        monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: store.__setitem__(key, payload))
        call_log = []
        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", self._fake_table_request(call_log))

            stop = [(41.320, 69.280)]
            m1, s1 = dm.get_distance_matrix([(41.300, 69.250)] + stop, traffic=True)
            assert s1 == "osrm_selfhosted"
            n_after_first = len(call_log)
            assert n_after_first == 1

            m2, s2 = dm.get_distance_matrix([(41.305, 69.255)] + stop, traffic=True)  # moved
            assert s2 == "osrm_selfhosted"
            assert len(call_log) - n_after_first == 1, (
                "the degenerate (single-stop) static tier must not trigger a wasted partial fetch"
            )

    def test_reordered_stops_reuse_the_static_cache_with_correctly_assembled_cells(
        self, app, monkeypatch
    ):
        """`test_static_key_ignores_stop_order` proves key equality only.
        This pins the actual assembly: reordering stops on a repeat call
        must be a full cache hit (0 requests) AND must place each cached
        cell at the right (i, j) in the reassembled matrix — using distinct,
        direction-asymmetric fake values so a future positional-index
        assumption (vs. the correct stop-key lookup) would be caught."""
        store = {}
        monkeypatch.setattr(dm, "_cache_get_json", lambda key: store.get(key))
        monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: store.__setitem__(key, payload))
        call_log = []

        def fake_request(**kw):
            url = kw.get("url", "")
            call_log.append((url, dict(kw.get("params") or {})))
            n = url.rsplit("/", 1)[-1].count(";") + 1
            # Distinct, asymmetric per (i, j) so a transposition bug shows up.
            distances = [[0.0 if i == j else (i + 1) * 10000 + (j + 1) * 1000 for j in range(n)] for i in range(n)]
            durations = [[0.0 if i == j else (i + 1) * 100 + (j + 1) * 10 for j in range(n)] for i in range(n)]
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"code": "Ok", "distances": distances, "durations": durations}
            resp.text = "ok"
            return resp

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)

            origin = (41.300, 69.250)
            a, b = (41.310, 69.270), (41.320, 69.280)
            dm.get_distance_matrix([origin, a, b], traffic=True)
            n_calls = len(call_log)

            matrix, source = dm.get_distance_matrix([origin, b, a], traffic=True)  # stops reordered

            assert source == "cache"
            assert len(call_log) == n_calls, "reordering stops must not issue any new requests"
            assert matrix[(0, 1)]["distance_km"] == 13.0  # origin -> b (was index 2 originally)
            assert matrix[(0, 2)]["distance_km"] == 12.0  # origin -> a (was index 1 originally)
            assert matrix[(1, 2)]["distance_km"] == 32.0  # b -> a
            assert matrix[(2, 1)]["distance_km"] == 23.0  # a -> b


@pytest.mark.unit
class TestStaticTierTrafficAwareTTL:
    """Task-4 review fix 4: MATRIX_CACHE_TTL_TRAFFIC_SECONDS was being read
    by nobody — the static tier always got the 24h TTL regardless of the
    `traffic` flag or the actual provider. Self-hosted/public OSRM are
    free-flow always (correct to keep 24h); a genuinely traffic-aware
    source (Yandex, only reachable behind LEGACY_MATRIX_PROVIDERS_ENABLED)
    must get the shorter traffic TTL so a route solved at rush hour doesn't
    silently price an off-peak solve a day later."""

    def test_selfhosted_source_keeps_the_24h_static_ttl_even_when_traffic_true(self, app, monkeypatch):
        ttls = {}
        monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
        monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: ttls.__setitem__(key, ttl))

        def fake_request(**kw):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "code": "Ok",
                "distances": [[0, 1500], [1500, 0]],
                "durations": [[0, 240], [240, 0]],
            }
            resp.text = "ok"
            return resp

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setitem(app.config, "MATRIX_CACHE_TTL_STATIC_SECONDS", 86400)
            monkeypatch.setitem(app.config, "MATRIX_CACHE_TTL_TRAFFIC_SECONDS", 1800)
            monkeypatch.setattr(dm, "request_with_retry", fake_request)

            _, source = dm.get_distance_matrix([(41.30, 69.25), (41.32, 69.27)], traffic=True)
            assert source == "osrm_selfhosted"

        static_key = dm._static_key([(41.32, 69.27)], traffic=True)
        assert ttls[static_key] == 86400

    def test_traffic_aware_source_gets_the_shorter_static_ttl(self, app, monkeypatch):
        ttls = {}
        monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
        monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: ttls.__setitem__(key, ttl))

        def fake_request(**kw):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
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
            }
            resp.text = "ok"
            return resp

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
            monkeypatch.setitem(app.config, "LEGACY_MATRIX_PROVIDERS_ENABLED", True)
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setitem(app.config, "MATRIX_CACHE_TTL_STATIC_SECONDS", 86400)
            monkeypatch.setitem(app.config, "MATRIX_CACHE_TTL_TRAFFIC_SECONDS", 1800)
            monkeypatch.setattr(dm, "request_with_retry", fake_request)

            _, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)], traffic=True, provider="yandex"
            )
            assert source == "yandex_matrix"

        static_key = dm._static_key([(41.32, 69.27)], traffic=True)
        assert ttls[static_key] == 1800


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
    def test_falls_back_when_provider_is_not_yandex(self, app, monkeypatch):
        # Default `MAPS_PROVIDER` in test config is not 'yandex' → straight to Haversine.
        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
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

    def test_haversine_results_are_symmetric(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
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
        provider next time. Verify by asserting that `_store_split_cache` is
        never called when the source is haversine."""
        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
            calls = []
            monkeypatch.setattr(
                dm, "_store_split_cache", lambda points, traffic, mx, source: calls.append(points)
            )
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
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
            monkeypatch.setitem(app.config, "LEGACY_MATRIX_PROVIDERS_ENABLED", True)
            # Force Yandex creds + override the HTTP layer.
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key-for-tests")
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
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

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
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
            monkeypatch.setitem(app.config, "LEGACY_MATRIX_PROVIDERS_ENABLED", True)
            monkeypatch.setitem(app.config, "OSRM_PUBLIC_FALLBACK_ENABLED", True)
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

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
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
            monkeypatch.setitem(app.config, "LEGACY_MATRIX_PROVIDERS_ENABLED", True)
            monkeypatch.setitem(app.config, "OSRM_PUBLIC_FALLBACK_ENABLED", True)
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

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
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
            monkeypatch.setitem(app.config, "LEGACY_MATRIX_PROVIDERS_ENABLED", True)
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

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
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
            monkeypatch.setitem(app.config, "LEGACY_MATRIX_PROVIDERS_ENABLED", True)
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
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
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

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


@pytest.mark.unit
class TestSelfHostedOsrmPrimary:
    """Spec 8.1/8.2: self-hosted OSRM is tier 1; the legacy Yandex tier is OFF
    the hot path (401 on every production call today, ~1.5 s of guaranteed
    failure); the public demo server only runs behind an explicit flag.

    The full outbound-host allowlist is pinned separately by
    `TestNoUnapprovedMatrixProviders`."""

    def _fake_response(self, status, body):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.text = "err"
        return resp

    def _ok_table(self):
        return self._fake_response(
            200,
            {
                "code": "Ok",
                "distances": [[0, 5460], [5460, 0]],
                "durations": [[0, 486], [486, 0]],
            },
        )

    def test_selfhosted_osrm_is_the_first_and_only_provider_called(self, app, monkeypatch):
        call_log = []

        def fake_request(**kw):
            call_log.append(kw.get("url", ""))
            if kw.get("url", "").startswith("http://osrm:5000/table/v1/driving/"):
                return self._ok_table()
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            # Key PRESENT but the flag is off — it must not be touched.
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "yandex-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)], traffic=True, provider="yandex", use_cache=False
            )

        assert source == "osrm_selfhosted"
        assert matrix[(0, 1)]["distance_km"] == 5.46
        assert matrix[(0, 1)]["duration_minutes"] == pytest.approx(8.1, abs=0.1)
        assert call_log == ["http://osrm:5000/table/v1/driving/69.25,41.3;69.27,41.32"]

    def test_dead_legacy_providers_never_reached_by_default(self, app, monkeypatch):
        """Self-hosted down, legacy flag off, demo flag off -> haversine.
        No outbound call to any external provider."""
        call_log = []

        def fake_request(**kw):
            call_log.append(kw.get("url", ""))
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "yandex-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

            _, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)], traffic=True, provider="yandex", use_cache=False
            )

        assert source == "haversine"
        assert all("yandex.net" not in u for u in call_log)
        assert all("project-osrm.org" not in u for u in call_log)

    def test_public_demo_runs_only_behind_its_flag(self, app, monkeypatch):
        call_log = []

        def fake_request(**kw):
            url = kw.get("url", "")
            call_log.append(url)
            if url.startswith("https://router.project-osrm.org/table/v1/driving/"):
                return self._ok_table()
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setitem(app.config, "OSRM_PUBLIC_FALLBACK_ENABLED", True)
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

            _, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)], traffic=True, use_cache=False
            )

        assert source == "osrm_table"
        assert any(u.startswith("http://osrm:5000/") for u in call_log), "self-hosted tried first"

@pytest.mark.unit
class TestOsrmMajorityRealCellGuard:
    """Final review round, I2: `_osrm_matrix` backfills any null cell with
    Haversine, per-cell, with no ceiling — unlike `_yandex_pairwise`, which
    the wrapper only trusts as `yandex_pairwise` when >=50% of pairs are
    real. A mostly-null OSRM response must not be cached/labelled as real
    `osrm_selfhosted`/`osrm_table` data; it must fall through exactly like a
    provider failure would."""

    def _fake_response(self, status, body):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.text = "err"
        return resp

    def _mostly_null_table(self):
        # 3 points -> 6 off-diagonal cells. Only (0,1)/(1,0) are real;
        # (0,2)/(2,0)/(1,2)/(2,1) are null -> 2/6 = 33% real, under the 50%
        # guard.
        return self._fake_response(
            200,
            {
                "code": "Ok",
                "distances": [[0, 1500, None], [1500, 0, None], [None, None, 0]],
                "durations": [[0, 240, None], [240, 0, None], [None, None, 0]],
            },
        )

    def test_selfhosted_mostly_null_response_falls_through_instead_of_caching_as_real(
        self, app, monkeypatch
    ):
        cache_calls = []

        def fake_request(**kw):
            if kw.get("url", "").startswith("http://osrm:5000/table/v1/driving/"):
                return self._mostly_null_table()
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(
                dm, "_store_split_cache",
                lambda points, traffic, mx, source: cache_calls.append(source),
            )

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27), (41.34, 69.29)],
                traffic=True,
                use_cache=False,
            )

        # No legacy providers, no demo fallback configured -> the mostly-null
        # OSRM result must fall all the way through to the honest Haversine
        # label, exactly like a hard provider failure would.
        assert source == "haversine"
        assert cache_calls == [], "a mostly-Haversine matrix must never be cached under a real-provider label"

    def test_selfhosted_majority_real_response_is_still_accepted(self, app, monkeypatch):
        """Control: the same shape, but only 1/6 cells null (>=50% real)
        must still be accepted and cached as osrm_selfhosted — the guard
        must not reject a normal partial-coverage response."""

        def fake_request(**kw):
            if kw.get("url", "").startswith("http://osrm:5000/table/v1/driving/"):
                return self._fake_response(
                    200,
                    {
                        "code": "Ok",
                        "distances": [[0, 1500, 2000], [1500, 0, 2500], [2000, None, 0]],
                        "durations": [[0, 240, 300], [240, 0, 320], [300, None, 0]],
                    },
                )
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27), (41.34, 69.29)],
                traffic=True,
                use_cache=False,
            )

        assert source == "osrm_selfhosted"
        assert matrix[(0, 1)]["distance_km"] == 1.5


@pytest.mark.unit
class TestSelfHostedOsrmDevBoxDegradation:
    """CARRIED FORWARD from Task 1's review: on a normal dev box
    OSRM_BASE_URL (default http://osrm:5000, uncommented in .env.example)
    points at a hostname that doesn't resolve because the `osrm` compose
    service sits behind `--profile routing`. That must degrade FAST (no
    multi-second connect-timeout/backoff) and QUIET (at most one WARNING per
    process/interval, not one per optimization)."""

    def _fake_response(self, status, body):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.text = "connection refused"
        return resp

    def test_selfhosted_tier_uses_a_short_timeout_and_no_backoff_retries(self, app, monkeypatch):
        """Pin the retry/timeout policy passed for the self-hosted circuit
        specifically — it must NOT be the 15s/2-retry policy used for the
        (internet-hosted) public demo fallback. That 15s/2-retry policy is
        appropriate for an external API; it is not appropriate for a
        same-network service that is either up (answers in ms) or simply not
        there (dev box, `osrm` compose profile not started)."""
        captured = {}

        def fake_request(**kw):
            captured["timeout_seconds"] = kw.get("timeout_seconds")
            captured["retry_config"] = kw.get("retry_config")
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)
            # Reset the shared per-process breaker so an earlier test in this
            # module can't leave it OPEN and short-circuit this one.
            monkeypatch.setattr(dm, "get_circuit_breaker", lambda *a, **kw: CircuitBreaker())

            dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)], traffic=True, use_cache=False
            )

        assert captured["timeout_seconds"] <= 5, "primary tier must not wait as long as an internet API"
        retry_config = captured["retry_config"]
        assert retry_config is not None
        assert retry_config.max_retries <= 1, "no multi-attempt backoff against a dead LAN host"

    def test_open_circuit_skips_the_network_call_entirely(self, app, monkeypatch):
        """Once the breaker for osrm_selfhosted is OPEN, get_distance_matrix
        must not even attempt the HTTP call (no request_with_retry call at
        all) — that's what makes every call after the first one instant."""
        call_log = []

        def fake_request(**kw):
            call_log.append(kw.get("url", ""))
            return self._fake_response(500, {})

        class _AlwaysOpenBreaker:
            def allow_request(self):
                return False

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)
            monkeypatch.setattr(dm, "get_circuit_breaker", lambda *a, **kw: _AlwaysOpenBreaker())

            _, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.32, 69.27)], traffic=True, use_cache=False
            )

        assert source == "haversine"
        assert call_log == [], "circuit open must skip the network call, not just fail fast inside it"

    def test_unavailable_warning_is_throttled_to_once_per_interval(self, app, monkeypatch, caplog):
        """Two optimizations in a row against a dead self-hosted tier must
        produce exactly one WARNING (plus, at most, DEBUG noise for the
        rest) — not a WARNING per call."""
        import logging as _logging

        def fake_request(**kw):
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)
            monkeypatch.setattr(dm, "get_circuit_breaker", lambda *a, **kw: CircuitBreaker())
            # Force "never logged yet" regardless of prior test runs. NOT
            # 0.0 — time.monotonic() is host/process uptime and can itself
            # be under the throttle window on a freshly started container,
            # which would make this assertion flaky (see the dedicated
            # test_first_warning_fires_even_when_monotonic_clock_is_near_zero).
            monkeypatch.setattr(dm, "_osrm_selfhosted_last_logged_at", None)

            # `business_app.utils.distance_matrix`'s logger is configured
            # with propagate=False (see test_yandex_route_distance_duration.py)
            # so pytest's root-attached caplog handler never sees it — attach
            # caplog's handler directly, same established pattern.
            dm.logger.addHandler(caplog.handler)
            try:
                with caplog.at_level(_logging.DEBUG, logger=dm.logger.name):
                    dm.get_distance_matrix(
                        [(41.30, 69.25), (41.32, 69.27)], traffic=True, use_cache=False
                    )
                    dm.get_distance_matrix(
                        [(41.30, 69.25), (41.32, 69.27)], traffic=True, use_cache=False
                    )
            finally:
                dm.logger.removeHandler(caplog.handler)

        warnings = [
            r for r in caplog.records if r.levelno == _logging.WARNING and "OSRM" in r.getMessage()
        ]
        assert len(warnings) == 1, f"expected exactly one WARNING, got: {[r.getMessage() for r in warnings]}"

    def test_first_warning_fires_even_when_monotonic_clock_is_near_zero(self, app, monkeypatch, caplog):
        """`time.monotonic()` is host/process uptime, not wall clock — right
        after a fresh boot (or a fresh CI VM) it can be smaller than the
        throttle window itself. The "never logged yet" state must be
        distinguishable from "logged at monotonic time ~0", or the very
        first WARNING after a reboot — the single most important moment for
        it to fire, since it means OSRM didn't come back up — gets silently
        demoted to DEBUG."""
        import logging as _logging

        def fake_request(**kw):
            return self._fake_response(500, {})

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)
            monkeypatch.setattr(dm, "get_circuit_breaker", lambda *a, **kw: CircuitBreaker())
            # Never logged in this process yet (the real "never logged"
            # sentinel, not a specific past monotonic timestamp).
            monkeypatch.setattr(dm, "_osrm_selfhosted_last_logged_at", None)
            # Simulate a freshly booted host: monotonic() is well under the
            # 300s throttle window.
            monkeypatch.setattr(dm.time, "monotonic", lambda: 0.001)

            dm.logger.addHandler(caplog.handler)
            try:
                with caplog.at_level(_logging.DEBUG, logger=dm.logger.name):
                    dm.get_distance_matrix(
                        [(41.30, 69.25), (41.32, 69.27)], traffic=True, use_cache=False
                    )
            finally:
                dm.logger.removeHandler(caplog.handler)

        warnings = [
            r for r in caplog.records if r.levelno == _logging.WARNING and "OSRM" in r.getMessage()
        ]
        assert len(warnings) == 1, (
            f"the first-ever occurrence must always WARN, even at near-zero monotonic time; "
            f"got: {[r.getMessage() for r in caplog.records]}"
        )


import logging as _logging


@pytest.mark.unit
class TestSourceLabelObservability:
    """The deploy verification (docs/routing_engine_deploy_rollback.md) greps
    Loki for `distance_matrix_built source=`. Pin the label format here so
    the production proof cannot silently rot."""

    def _ok_table_response(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "code": "Ok",
            "distances": [[0, 5460], [5460, 0]],
            "durations": [[0, 486], [486, 0]],
        }
        resp.text = "ok"
        return resp

    def test_selfhosted_fetch_logs_the_source_label(self, app, monkeypatch, caplog):
        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", lambda **kw: self._ok_table_response())
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)
            # `business_app.utils.distance_matrix`'s logger is configured
            # with propagate=False (see test_yandex_route_distance_duration.py
            # and the throttle tests above) so pytest's root-attached caplog
            # handler never sees it — attach caplog's handler directly, same
            # established pattern.
            dm.logger.addHandler(caplog.handler)
            try:
                with caplog.at_level(_logging.INFO, logger=dm.logger.name):
                    dm.get_distance_matrix([(41.30, 69.25), (41.32, 69.27)], traffic=True)
            finally:
                dm.logger.removeHandler(caplog.handler)
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "distance_matrix_built source=osrm_selfhosted" in joined
        assert "static_tier=miss live_tier=miss" in joined

    def test_full_cache_hit_logs_the_cache_label(self, app, monkeypatch, caplog):
        store = {}
        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", lambda **kw: self._ok_table_response())
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: store.get(key))
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: store.__setitem__(key, payload))
            pts = [(41.30, 69.25), (41.32, 69.27)]
            dm.get_distance_matrix(pts, traffic=True)
            # Same propagate=False caveat as above — attach the handler
            # directly for the second call, whose log line is under test.
            dm.logger.addHandler(caplog.handler)
            try:
                with caplog.at_level(_logging.INFO, logger=dm.logger.name):
                    _, source = dm.get_distance_matrix(pts, traffic=True)
            finally:
                dm.logger.removeHandler(caplog.handler)
        assert source == "cache"
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "distance_matrix_built source=cache" in joined
        assert "static_tier=hit live_tier=hit" in joined

    def test_moved_origin_reuses_static_tier_logs_fetched_live_tier(self, app, monkeypatch, caplog):
        """The most operationally diagnostic label of the three: it proves
        the two-tier cache SPLIT is working — a moved origin re-fetches only
        the origin row (one `sources=0` request) while the stop<->stop
        sub-matrix is served from the static cache — as opposed to
        `static_tier=miss live_tier=miss`, which only proves OSRM answered
        at all. Mirrors `TestCacheSplitBehaviour._fake_table_request`'s
        sources=0 handling so the partial fetch resolves correctly."""
        store = {}
        call_log = []

        def fake_request(**kw):
            url = kw.get("url", "")
            params = dict(kw.get("params") or {})
            call_log.append((url, params))
            n = url.rsplit("/", 1)[-1].count(";") + 1
            cell_d, cell_t = 1500.0, 240.0
            if params.get("sources") == "0":
                distances = [[0.0 if j == 0 else cell_d for j in range(n)]]
                durations = [[0.0 if j == 0 else cell_t for j in range(n)]]
            else:
                distances = [[0.0 if i == j else cell_d for j in range(n)] for i in range(n)]
                durations = [[0.0 if i == j else cell_t for j in range(n)] for i in range(n)]
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"code": "Ok", "distances": distances, "durations": durations}
            resp.text = "ok"
            return resp

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: store.get(key))
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: store.__setitem__(key, payload))

            stops = [(41.3111, 69.2799), (41.3260, 69.2280)]
            # Prime both cache tiers with a first, unmoved call.
            dm.get_distance_matrix([(41.3000, 69.2500)] + stops, traffic=True)
            n_calls_before_move = len(call_log)

            # Same propagate=False caveat as the tests above — attach the
            # handler directly, only around the call under test.
            dm.logger.addHandler(caplog.handler)
            try:
                with caplog.at_level(_logging.INFO, logger=dm.logger.name):
                    matrix, source = dm.get_distance_matrix(
                        [(41.3050, 69.2550)] + stops, traffic=True  # origin moved; stops unchanged
                    )
            finally:
                dm.logger.removeHandler(caplog.handler)

        assert source == "osrm_selfhosted"
        # The substantive claim behind `live_tier=fetched`: exactly one
        # upstream request for the moved-origin call (sources=0), not two
        # (no separate destinations=0 request) and not a full N×N re-fetch.
        assert len(call_log) - n_calls_before_move == 1
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "distance_matrix_built source=osrm_selfhosted" in joined
        assert "static_tier=hit live_tier=fetched" in joined

@pytest.mark.unit
class TestNoUnapprovedMatrixProviders:
    """The matrix wrapper may contact ONLY the approved hosts below.

    An allowlist, deliberately, not a blocklist. A paid third-party vendor once
    sat in tier 2 ungated for ~100 days and billed us before anyone noticed;
    naming that one vendor in a blocklist would guard against exactly the
    mistake we already made and nothing else. These tests instead assert the
    complete set of reachable hosts and map credentials, so ANY new outbound
    provider — paid or free — fails the suite until it is added here on purpose.
    """

    #: Every host the module is permitted to build a URL for. Hosts are compared
    #: without their port, so the self-hosted engine is `osrm`, not `osrm:5000`.
    APPROVED_HOSTS = frozenset({
        "osrm",                       # our own self-hosted engine (OSRM_BASE_URL default)
        "api.routing.yandex.net",     # legacy tier, LEGACY_MATRIX_PROVIDERS_ENABLED only
        "router.project-osrm.org",    # emergency demo tier, OSRM_PUBLIC_FALLBACK_ENABLED only
    })

    #: The complete set of map credentials the config may define.
    APPROVED_MAP_CREDENTIALS = frozenset({"GOOGLE_MAPS_API_KEY", "YANDEX_MAPS_API_KEY"})

    def _fake_response(self, status, body):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.text = "err"
        return resp

    @staticmethod
    def _hosts_in(text):
        return {m.group(1) for m in re.finditer(r"https?://([A-Za-z0-9.\-]+)", text)}

    def test_module_source_contains_no_unapproved_host(self):
        """A deleted provider must leave no endpoint literal behind, and a new
        one cannot be introduced without updating APPROVED_HOSTS."""
        import inspect

        found = self._hosts_in(inspect.getsource(dm))
        unapproved = found - self.APPROVED_HOSTS
        assert not unapproved, f"unapproved provider host(s) in distance_matrix: {sorted(unapproved)}"

    def test_no_unapproved_host_is_contacted_with_every_tier_enabled(self, app, monkeypatch):
        """The most permissive configuration possible: legacy tier on, demo tier
        on, every credential present, self-hosted OSRM failing. Even here the
        wrapper may only reach approved hosts."""
        call_log = []

        def fake_request(**kw):
            url = kw.get("url", "")
            call_log.append(url)
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
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setitem(app.config, "LEGACY_MATRIX_PROVIDERS_ENABLED", True)
            monkeypatch.setitem(app.config, "OSRM_PUBLIC_FALLBACK_ENABLED", True)
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "yandex-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)
            monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
            monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

            matrix, source = dm.get_distance_matrix(
                [(41.30, 69.25), (41.275, 69.220)],
                traffic=True,
                provider="yandex",
                use_cache=False,
            )

        assert source == "yandex_matrix"
        assert matrix[(0, 1)]["distance_km"] == 8.0
        contacted = set()
        for url in call_log:
            contacted |= self._hosts_in(url)
        unapproved = contacted - self.APPROVED_HOSTS
        assert not unapproved, f"contacted unapproved host(s): {sorted(unapproved)}; full log: {call_log}"

    def test_config_defines_only_approved_map_credentials(self, app):
        """A removed provider must leave no credential behind, and a new one
        cannot be added without updating APPROVED_MAP_CREDENTIALS."""
        defined = {k for k in app.config if k.endswith("_MAPS_API_KEY") or k.endswith("_MAPS_APP_ID")}
        assert defined == self.APPROVED_MAP_CREDENTIALS, (
            f"map credentials drifted from the approved set: "
            f"unexpected={sorted(defined - self.APPROVED_MAP_CREDENTIALS)} "
            f"missing={sorted(self.APPROVED_MAP_CREDENTIALS - defined)}"
        )

    def test_traffic_aware_sources_are_all_producible(self):
        """`_TRAFFIC_AWARE_SOURCES` drives the short cache TTL. Every label in it
        must still be reachable in `get_distance_matrix`, or the set is carrying
        a ghost from a deleted provider."""
        import inspect

        body = inspect.getsource(dm.get_distance_matrix)
        for label in dm._TRAFFIC_AWARE_SOURCES:
            assert f'"{label}"' in body, f"{label} can never be produced — stale entry"
