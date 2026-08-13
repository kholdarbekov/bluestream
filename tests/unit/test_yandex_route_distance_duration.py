"""Regression for Task 12: Yandex Router API distance/duration parsing.

Two sites read `route["distance"]["value"]` / `route["duration"]["value"]`
from a Yandex Router API `/v2/route` response. **Those keys do not exist.**

Per Yandex's public Router API docs (yandex.com/maps-api/docs/router-api/
response.html, fetched during this fix): the `route` object carries only
`legs` (no route-level or leg-level distance/duration aggregate at all).
Each `route.legs[].steps[]` entry carries the real per-step metrics as
plain numbers:
    - `length`   — step length in METRES
    - `duration` — step duration in SECONDS

`MapsService._yandex_route_geometry` (same file, site 1's neighbour) already
walks this exact `legs[].steps[]` shape correctly for polyline points — these
tests hold the distance/duration fix to the same real shape, not the
fabricated flat `route.distance.value` shape that let this bug survive
eleven prior reviews.

Site 1: `business_app/services/maps_service.py::MapsService._yandex_get_route`
Site 2: `business_app/utils/distance_matrix.py::_yandex_pairwise`
"""

import logging
from unittest.mock import MagicMock

import pytest

from business_app.services.maps_service import MapsService
from business_app.utils import distance_matrix as dm
from business_app.utils import http_client as http_client_module


def _fake_response(json_payload, status_code=200, text=""):
    resp = MagicMock(name="fake_response")
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_payload)
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# Realistic Yandex Router API payload builders — NEVER a top-level
# `route.distance`/`route.duration`, always nested `legs[].steps[]`.
# ---------------------------------------------------------------------------


def _multi_leg_multi_step_route():
    """Two legs, three steps total. length is metres, duration is seconds,
    both plain numbers per the documented shape.

    Totals: length = 300 + 200 + 500 = 1000 m -> 1.0 km
            duration = 40 + 20 + 60 = 120 s -> 2.0 min
    """
    return {
        "route": {
            "legs": [
                {
                    "steps": [
                        {"length": 300, "duration": 40, "polyline": {"points": [[41.30, 69.25]]}},
                        {"length": 200, "duration": 20, "polyline": {"points": [[41.301, 69.251]]}},
                    ]
                },
                {
                    "steps": [
                        {"length": 500, "duration": 60, "polyline": {"points": [[41.32, 69.27]]}},
                    ]
                },
            ]
        }
    }


def _no_legs_route():
    return {"route": {}}


def _steps_without_metrics_route():
    """Steps exist (and even carry polyline geometry) but no length/duration
    at all — a response variant this fix must not invent numbers for."""
    return {
        "route": {
            "legs": [
                {"steps": [{"polyline": {"points": [[41.30, 69.25]]}}]},
            ]
        }
    }


def _value_wrapped_route():
    """Defensive-shape case: some Yandex response variant / mock wraps the
    step metric as {"value": ...} instead of a plain number (as Google's
    Directions API does). The fix must handle both without guessing which
    one it's talking to.

    Totals: length = 400 -> 0.4 km, duration = 50 s -> 50/60 min
    """
    return {
        "route": {
            "legs": [
                {"steps": [{"length": {"value": 400}, "duration": {"value": 50}}]},
            ]
        }
    }


# ===========================================================================
# Site 1 — MapsService._yandex_get_route (via the public get_route() API)
# ===========================================================================


@pytest.mark.unit
@pytest.mark.delivery
class TestYandexGetRouteDistanceDuration:
    def test_sums_distance_and_duration_across_multiple_legs_and_steps(self, app, monkeypatch):
        mock_request = MagicMock(return_value=_fake_response(_multi_leg_multi_step_route()))
        monkeypatch.setattr(http_client_module.requests, "request", mock_request)

        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"
            result = svc.get_route(41.30, 69.25, 41.32, 69.27)

        assert result["distance_km"] == pytest.approx(1.0)
        assert result["duration_minutes"] == pytest.approx(2.0)

    def test_returns_zero_when_route_has_no_legs(self, app, monkeypatch):
        """A genuinely empty route must yield 0/0 (the existing honest
        default), never a fabricated non-zero number."""
        mock_request = MagicMock(return_value=_fake_response(_no_legs_route()))
        monkeypatch.setattr(http_client_module.requests, "request", mock_request)

        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"
            result = svc.get_route(41.30, 69.25, 41.32, 69.27)

        assert result["distance_km"] == 0.0
        assert result["duration_minutes"] == 0.0

    def test_returns_zero_when_steps_carry_no_distance_duration_fields(self, app, monkeypatch):
        mock_request = MagicMock(return_value=_fake_response(_steps_without_metrics_route()))
        monkeypatch.setattr(http_client_module.requests, "request", mock_request)

        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"
            result = svc.get_route(41.30, 69.25, 41.32, 69.27)

        assert result["distance_km"] == 0.0
        assert result["duration_minutes"] == 0.0

    def test_handles_value_wrapped_step_fields_defensively(self, app, monkeypatch):
        mock_request = MagicMock(return_value=_fake_response(_value_wrapped_route()))
        monkeypatch.setattr(http_client_module.requests, "request", mock_request)

        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"
            result = svc.get_route(41.30, 69.25, 41.32, 69.27)

        assert result["distance_km"] == pytest.approx(0.4)
        assert result["duration_minutes"] == pytest.approx(50 / 60)

    def test_calculate_travel_time_reflects_the_same_fix(self, app, monkeypatch):
        """`calculate_travel_time` (used by `calculate_delivery_eta_task`) is
        built directly on `get_route()` — lock in that the ETA task actually
        gets real minutes now, not the silent 0 that used to flow through
        `travel_time.get('duration_minutes', 30)` (0 is truthy-absent so the
        `30` default never even kicked in)."""
        mock_request = MagicMock(return_value=_fake_response(_multi_leg_multi_step_route()))
        monkeypatch.setattr(http_client_module.requests, "request", mock_request)

        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"
            result = svc.calculate_travel_time(41.30, 69.25, 41.32, 69.27)

        assert result["distance_km"] == pytest.approx(1.0)
        assert result["duration_minutes"] == pytest.approx(2.0)


# ===========================================================================
# Site 2 — distance_matrix._yandex_pairwise
# ===========================================================================


@pytest.mark.unit
class TestYandexPairwiseDistanceDuration:
    def test_sums_across_multiple_legs_and_steps_and_counts_as_success(self, app, monkeypatch):
        route_payload = _multi_leg_multi_step_route()

        def fake_request(**kw):
            return _fake_response(route_payload)

        with app.app_context():
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)

            matrix, success, total = dm._yandex_pairwise(
                [(41.30, 69.25), (41.32, 69.27)], "fake-key", True
            )

        assert total == 2  # (0,1) and (1,0)
        assert success == 2
        assert matrix[(0, 1)]["distance_km"] == pytest.approx(1.0)
        assert matrix[(0, 1)]["duration_minutes"] == pytest.approx(2.0)
        assert matrix[(1, 0)]["distance_km"] == pytest.approx(1.0)
        assert matrix[(1, 0)]["duration_minutes"] == pytest.approx(2.0)

    def test_guard_still_raises_and_falls_back_to_haversine_when_no_legs(self, app, monkeypatch):
        """The guard in `_yandex_pairwise` ("if not distance_m or not
        duration_s: raise") must survive the fix — a route with no usable
        data must still be treated as a failure so the per-cell Haversine
        backfill runs, and the outer `get_distance_matrix` sees a low success
        ratio and moves on rather than trusting a broken Yandex response."""
        route_payload = _no_legs_route()

        def fake_request(**kw):
            return _fake_response(route_payload)

        with app.app_context():
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)

            matrix, success, total = dm._yandex_pairwise(
                [(41.30, 69.25), (41.32, 69.27)], "fake-key", True
            )

        assert success == 0
        assert total == 2
        # Backfilled with a real (positive) Haversine estimate, not 0/0 and
        # not a crash.
        assert matrix[(0, 1)]["distance_km"] > 0
        assert matrix[(0, 1)]["duration_minutes"] > 0

    def test_guard_still_raises_when_steps_carry_no_distance_duration_fields(self, app, monkeypatch):
        route_payload = _steps_without_metrics_route()

        def fake_request(**kw):
            return _fake_response(route_payload)

        with app.app_context():
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)

            matrix, success, total = dm._yandex_pairwise(
                [(41.30, 69.25), (41.32, 69.27)], "fake-key", True
            )

        assert success == 0
        assert total == 2
        assert matrix[(0, 1)]["distance_km"] > 0

    def test_handles_value_wrapped_step_fields_defensively(self, app, monkeypatch):
        route_payload = _value_wrapped_route()

        def fake_request(**kw):
            return _fake_response(route_payload)

        with app.app_context():
            monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
            monkeypatch.setattr(dm, "request_with_retry", fake_request)

            matrix, success, total = dm._yandex_pairwise(
                [(41.30, 69.25), (41.32, 69.27)], "fake-key", True
            )

        assert success == 2
        assert matrix[(0, 1)]["distance_km"] == pytest.approx(0.4)
        assert matrix[(0, 1)]["duration_minutes"] == pytest.approx(50 / 60)

    def test_end_to_end_yandex_becomes_the_matrix_source_and_is_logged(self, app, monkeypatch, caplog):
        """Behavioural consequence flagged in the task brief: fixing this
        guard means Yandex genuinely becomes the distance-matrix provider
        (source == 'yandex_pairwise') instead of silently falling through to
        OSRM/Haversine. Assert the source label AND that this is observable
        in the logs after deploy."""
        route_payload = _multi_leg_multi_step_route()

        def fake_request(**kw):
            url = kw.get("url", "")
            if "distancematrix" in url:
                # Force the single-call matrix endpoint to fail so the
                # wrapper falls through to the pairwise path under test.
                return _fake_response({}, status_code=500, text="boom")
            return _fake_response(route_payload)

        # `distance_matrix`'s module logger propagates up through
        # "business_app.utils" to "business_app", and `app.logger` (name
        # "business_app", since `Flask(__name__)` is created inside the
        # `business_app` package) is configured with `propagate=False` — so
        # pytest's root-attached caplog handler never sees it by default.
        # Attach caplog's handler directly, same pattern used elsewhere in
        # this suite (see test_notification_service_fixes.py).
        dm.logger.addHandler(caplog.handler)
        try:
            with app.app_context():
                monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
                monkeypatch.setitem(app.config, "LEGACY_MATRIX_PROVIDERS_ENABLED", True)
                monkeypatch.setitem(app.config, "YANDEX_MAPS_API_KEY", "fake-key")
                monkeypatch.setitem(app.config, "HERE_MAPS_API_KEY", None)
                monkeypatch.setattr(dm, "request_with_retry", fake_request)
                monkeypatch.setattr(dm, "_cache_get_json", lambda key: None)
                monkeypatch.setattr(dm, "_cache_set_json", lambda key, payload, ttl: None)

                with caplog.at_level(logging.INFO, logger="business_app.utils.distance_matrix"):
                    matrix, source = dm.get_distance_matrix(
                        [(41.30, 69.25), (41.32, 69.27)],
                        traffic=True,
                        provider="yandex",
                        use_cache=False,
                    )
        finally:
            dm.logger.removeHandler(caplog.handler)

        assert source == "yandex_pairwise"
        assert matrix[(0, 1)]["distance_km"] == pytest.approx(1.0)
        assert matrix[(0, 1)]["duration_minutes"] == pytest.approx(2.0)
        # Observability: it must be possible to tell from the logs that
        # Yandex pairwise routing (not OSRM/Haversine) actually served this
        # matrix.
        messages = [r.getMessage() for r in caplog.records]
        assert any("yandex" in m.lower() and "pairwise" in m.lower() for m in messages), (
            f"expected a log line identifying yandex_pairwise as the matrix source; got: {messages}"
        )
