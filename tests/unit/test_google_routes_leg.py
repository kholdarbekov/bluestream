"""Unit contract of the Google Routes computeRoutes next-leg client.

Spec 8.2: TRAFFIC_AWARE, ONE number (the next-leg ETA), bills per REQUEST.
computeRouteMatrix bills per ELEMENT and must never be called. The module
degrades silently — any failure returns None and the caller falls back to
the OSRM duration.
"""

from unittest.mock import MagicMock

import pytest

from business_app.utils import google_routes as gr


def _fake_response(status, body):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    resp.text = "err"
    return resp


@pytest.mark.unit
class TestGoogleRoutesLeg:
    def test_returns_none_when_unconfigured(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", None)
            assert gr.get_traffic_aware_leg((41.3, 69.25), (41.32, 69.27)) is None

    def test_uses_compute_routes_never_the_matrix_endpoint(self, app, monkeypatch):
        captured = {}

        def fake_request(**kw):
            captured.update(kw)
            return _fake_response(200, {"routes": [{"duration": "540s", "distanceMeters": 4200}]})

        with app.app_context():
            monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", "g-key")
            monkeypatch.setattr(gr, "request_with_retry", fake_request)
            leg = gr.get_traffic_aware_leg((41.3, 69.25), (41.32, 69.27))

        assert captured["url"] == "https://routes.googleapis.com/directions/v2:computeRoutes"
        assert "computeRouteMatrix" not in captured["url"], "matrix endpoint bills per ELEMENT — forbidden"
        assert captured["headers"]["X-Goog-Api-Key"] == "g-key"
        assert captured["headers"]["X-Goog-FieldMask"] == "routes.duration,routes.distanceMeters"
        body = captured["json"]
        assert body["travelMode"] == "DRIVE"
        assert body["routingPreference"] == "TRAFFIC_AWARE"
        assert body["origin"]["location"]["latLng"] == {"latitude": 41.3, "longitude": 69.25}
        assert body["destination"]["location"]["latLng"] == {"latitude": 41.32, "longitude": 69.27}
        assert leg == {"duration_minutes": 9.0, "distance_km": 4.2}

    def test_http_error_degrades_to_none(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", "g-key")
            monkeypatch.setattr(gr, "request_with_retry", lambda **kw: _fake_response(403, {}))
            assert gr.get_traffic_aware_leg((41.3, 69.25), (41.32, 69.27)) is None

    def test_transport_exception_degrades_to_none(self, app, monkeypatch):
        def boom(**kw):
            raise RuntimeError("socket timeout")

        with app.app_context():
            monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", "g-key")
            monkeypatch.setattr(gr, "request_with_retry", boom)
            assert gr.get_traffic_aware_leg((41.3, 69.25), (41.32, 69.27)) is None

    def test_malformed_duration_degrades_to_none(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", "g-key")
            monkeypatch.setattr(
                gr,
                "request_with_retry",
                lambda **kw: _fake_response(200, {"routes": [{"duration": 540, "distanceMeters": 4200}]}),
            )
            assert gr.get_traffic_aware_leg((41.3, 69.25), (41.32, 69.27)) is None

    def test_empty_routes_degrades_to_none(self, app, monkeypatch):
        with app.app_context():
            monkeypatch.setitem(app.config, "GOOGLE_ROUTES_API_KEY", "g-key")
            monkeypatch.setattr(gr, "request_with_retry", lambda **kw: _fake_response(200, {"routes": []}))
            assert gr.get_traffic_aware_leg((41.3, 69.25), (41.32, 69.27)) is None
