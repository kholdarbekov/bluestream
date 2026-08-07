"""Geometry-shape regression for `MapsService.get_route()` across providers.

Companion to `test_maps_service_timeouts_comprehensive.py` (which uses
invented payloads like `{"geometry": "yandex-poly"}` / `{"geometry":
"osrm-poly"}` purely to exercise the timeout plumbing) — this file is the
one that must be realistic about geometry, because that is exactly the bug
class the admin-dispatch map review caught: `admin_dispatch.py` forwarded
whatever `get_route()["polyline"]`/`["geometry"]` held straight to Leaflet's
`<Polyline positions=...>`, and every provider handed back a shape other
than `[[lat, lng], ...]`.

The fix moves normalisation into `MapsService` itself: `get_route()` must
always return a `"geometry"` key that is either `[[lat, lng], ...]` or
`None` — decoded from Google/OSRM's encoded-polyline strings, and pulled
from Yandex's real (nested, nowhere near the top level) response shape.

Real Google encoded-polyline test vector, reused from
`test_polyline_decoder.py`: Google's own documented example at
https://developers.google.com/maps/documentation/utilities/polylinealgorithm.
OSRM uses the identical encoding (precision 5, same [lat,lng] output order —
see `business_app/utils/polyline.py`'s module docstring), so the same string
is valid for both.
"""

from unittest.mock import MagicMock

import pytest

from business_app.services import maps_service as maps_module
from business_app.services.maps_service import MapsService
from business_app.utils import http_client as http_client_module

_ENCODED_POLYLINE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
_DECODED_POINTS = [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]]


def _fake_response(json_payload, status_code=200):
    resp = MagicMock(name="fake_response")
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_payload)
    resp.status_code = status_code
    resp.text = ""
    return resp


@pytest.mark.unit
@pytest.mark.delivery
class TestGoogleRouteGeometry:
    def test_get_route_decodes_the_overview_polyline_into_coordinate_pairs(self, app, monkeypatch):
        """`overview_polyline.points` is a raw encoded STRING (verified against
        Google's real Directions API response shape) — not an array of
        `[lat, lng]` pairs. Against the current (pre-fix) code, `get_route()`
        returns that raw string under `"polyline"`, and `result["geometry"]`
        is a KeyError/None — this must instead be the DECODED array.
        """
        payload = {
            "status": "OK",
            "routes": [
                {
                    "overview_polyline": {"points": _ENCODED_POLYLINE},
                    "legs": [
                        {
                            "distance": {"value": 5000, "text": "5 km"},
                            "duration": {"value": 600, "text": "10 mins"},
                            "steps": [],
                        }
                    ],
                }
            ],
        }
        mock_get = MagicMock(return_value=_fake_response(payload))
        monkeypatch.setattr(maps_module.requests, "get", mock_get)

        with app.app_context():
            svc = MapsService()
            svc.provider = "google"
            svc.google_api_key = "g-key"

            result = svc.get_route(41.30, 69.25, 41.35, 69.30)

        assert result["geometry"] == _DECODED_POINTS
        # Never a bare string — that is the exact defect Leaflet's
        # <Polyline positions={...}> silently swallowed as "characters".
        assert not isinstance(result["geometry"], str)


@pytest.mark.unit
@pytest.mark.delivery
class TestOSRMRouteGeometry:
    def test_get_route_decodes_the_geometry_field_into_coordinate_pairs(self, app, monkeypatch):
        """OSRM's `routes[].geometry` (with `geometries=polyline`, the mode
        this codebase requests) is the SAME encoded-string format as Google's
        overview_polyline — see the OSRM HTTP API docs: 'polyline with
        precision 5 in [latitude,longitude] encoding'."""
        payload = {
            "code": "Ok",
            "routes": [
                {
                    "distance": 5000,
                    "duration": 600,
                    "geometry": _ENCODED_POLYLINE,
                    "legs": [{"steps": []}],
                }
            ],
        }
        mock_get = MagicMock(return_value=_fake_response(payload))
        monkeypatch.setattr(maps_module.requests, "get", mock_get)

        with app.app_context():
            svc = MapsService()
            svc.provider = "osm"

            result = svc.get_route(41.30, 69.25, 41.35, 69.30)

        assert result["geometry"] == _DECODED_POINTS
        assert not isinstance(result["geometry"], str)


@pytest.mark.unit
@pytest.mark.delivery
class TestYandexRouteGeometry:
    """Per Yandex's public Router API docs (yandex.com/maps-api/docs/router-api/
    response.html and examples.html, fetched 2026-08): the response has NO
    top-level `route.geometry` field at all. The real path is nested under
    `route.legs[].steps[].polyline.points`, already `[lat, lng]` pairs (no
    decoding needed) — this is a genuinely different bug shape from Google/OSRM
    (wrong JSON path entirely, not merely an undecoded string).
    """

    def test_get_route_concatenates_geometry_from_every_leg_and_step(self, app, monkeypatch):
        payload = {
            "route": {
                "legs": [
                    {
                        "steps": [
                            {"polyline": {"points": [[41.30, 69.20], [41.301, 69.201]]}},
                            {"polyline": {"points": [[41.301, 69.201], [41.305, 69.21]]}},
                        ]
                    },
                    {
                        "steps": [
                            {"polyline": {"points": [[41.305, 69.21], [41.31, 69.25]]}},
                        ]
                    },
                ]
            }
        }
        mock_request = MagicMock(return_value=_fake_response(payload))
        monkeypatch.setattr(http_client_module.requests, "request", mock_request)

        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"

            result = svc.get_route(41.30, 69.20, 41.31, 69.25)

        assert result["geometry"] == [
            [41.30, 69.20],
            [41.301, 69.201],
            [41.301, 69.201],
            [41.305, 69.21],
            [41.305, 69.21],
            [41.31, 69.25],
        ]

    def test_get_route_returns_none_geometry_when_steps_carry_no_polyline(self, app, monkeypatch):
        """A 200 response that doesn't actually carry any polyline data (e.g. a
        tariff/response variant without it) must degrade to `None`, not raise
        and not silently invent an empty-but-truthy `[]`."""
        payload = {"route": {"legs": [{"steps": [{"duration": 5, "length": 10}]}]}}
        mock_request = MagicMock(return_value=_fake_response(payload))
        monkeypatch.setattr(http_client_module.requests, "request", mock_request)

        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"

            result = svc.get_route(41.30, 69.20, 41.31, 69.25)

        assert result["geometry"] is None

    def test_get_route_returns_none_geometry_when_route_key_is_entirely_absent(self, app, monkeypatch):
        payload = {}
        mock_request = MagicMock(return_value=_fake_response(payload))
        monkeypatch.setattr(http_client_module.requests, "request", mock_request)

        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"

            result = svc.get_route(41.30, 69.20, 41.31, 69.25)

        assert result["geometry"] is None

    def test_provider_failure_returns_none_geometry_not_the_string_key_typo(self, app, monkeypatch):
        """Regression for the key rename: the Haversine-fallback branch must
        report its absent geometry under `"geometry"`, the same key the
        success path uses — not the old `"polyline"` key admin_dispatch.py
        used to fall back to."""
        from business_app.utils.exceptions import ProviderUnavailableError

        mock_request = MagicMock(side_effect=ProviderUnavailableError("down", provider="yandex_route"))
        monkeypatch.setattr(http_client_module.requests, "request", mock_request)

        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"

            result = svc.get_route(41.30, 69.20, 41.31, 69.25)

        assert result["geometry"] is None
        assert "polyline" not in result
