"""`MapsService`'s OSRM branch must use OUR self-hosted engine, not the
public demo server.

Background: `business_app/utils/distance_matrix.py` already owns the OSRM
tiering policy — self-hosted at `OSRM_BASE_URL` is the primary tier, and the
public demo (`router.project-osrm.org`) is an EMERGENCY-only fallback gated
behind `OSRM_PUBLIC_FALLBACK_ENABLED`, because the demo server's usage policy
forbids production use.

`maps_service.py` hardcoded the demo URL and honoured neither setting. That is
the CLAUDE.md "never leave two places deciding the same thing" failure: one
module asks permission to touch the demo server, the other just does it. It
also meant the admin dispatch map (`admin_dispatch.py::dispatch_route_geometry`
-> `MapsService.get_route`) drew every driver's road path via a third-party
demo box, when we run a perfectly good OSRM ourselves that already has the
geometry dataset loaded.

These tests drive the real `get_route()` entry point and assert on the URL
actually requested, because that is the only thing that proves which host
serves our production traffic.
"""

from unittest.mock import MagicMock

import pytest

from business_app.services import maps_service as maps_module
from business_app.services.maps_service import MapsService
from business_app.utils.exceptions import ExternalServiceError

_ENCODED_POLYLINE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"

_OSRM_PAYLOAD = {
    "code": "Ok",
    "routes": [
        {
            "distance": 5000.0,
            "duration": 600.0,
            "geometry": _ENCODED_POLYLINE,
            "legs": [{"steps": []}],
        }
    ],
}


def _fake_response(json_payload, status_code=200):
    resp = MagicMock(name="fake_response")
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_payload)
    resp.status_code = status_code
    resp.text = ""
    return resp


def _capture_get(monkeypatch, payload=_OSRM_PAYLOAD):
    """Patch requests.get and hand back the mock so the URL can be asserted."""
    mock_get = MagicMock(return_value=_fake_response(payload))
    monkeypatch.setattr(maps_module.requests, "get", mock_get)
    return mock_get


@pytest.mark.unit
@pytest.mark.delivery
class TestOsrmRouteUsesSelfHostedEngine:
    def test_route_goes_to_the_self_hosted_engine_not_the_public_demo(self, app, monkeypatch):
        """The whole point: with a self-hosted engine configured, no request
        may leave our network for a route."""
        mock_get = _capture_get(monkeypatch)

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setitem(app.config, "OSRM_PUBLIC_FALLBACK_ENABLED", False)
            svc = MapsService()
            svc.provider = "osm"
            result = svc.get_route(41.30, 69.25, 41.35, 69.30)

        url = mock_get.call_args[0][0]
        assert url.startswith("http://osrm:5000/route/v1/driving/"), url
        assert "router.project-osrm.org" not in url
        # and the response still normalises the way every caller expects
        assert result["geometry"] and not isinstance(result["geometry"], str)

    def test_trailing_slash_on_the_base_url_does_not_produce_a_double_slash(self, app, monkeypatch):
        """`OSRM_BASE_URL` is operator-supplied; a trailing slash is the
        obvious way to write it and must not yield `//route/v1`."""
        mock_get = _capture_get(monkeypatch)

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000/")
            monkeypatch.setitem(app.config, "OSRM_PUBLIC_FALLBACK_ENABLED", False)
            svc = MapsService()
            svc.provider = "osm"
            svc.get_route(41.30, 69.25, 41.35, 69.30)

        url = mock_get.call_args[0][0]
        assert "//route/v1" not in url.replace("http://", "")
        assert url.startswith("http://osrm:5000/route/v1/driving/"), url

    def test_waypoints_are_still_threaded_through_in_lon_lat_order(self, app, monkeypatch):
        """Regression guard: the coordinate string is lon,lat — swapping it
        silently routes across the wrong hemisphere."""
        mock_get = _capture_get(monkeypatch)

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setitem(app.config, "OSRM_PUBLIC_FALLBACK_ENABLED", False)
            svc = MapsService()
            svc.provider = "osm"
            svc.get_route(41.30, 69.25, 41.35, 69.30, waypoints=[(41.32, 69.27)])

        url = mock_get.call_args[0][0]
        assert url.endswith("69.25,41.3;69.27,41.32;69.3,41.35"), url


@pytest.mark.unit
@pytest.mark.delivery
class TestOsrmStepInstructions:
    """OSRM does NOT ship a human-readable `instruction` field.

    Verified against the live engine: `maneuver` carries exactly
    `{bearing_after, bearing_before, location, modifier, type}`. OSRM leaves
    prose to the client (that is what osrm-text-instructions exists for), so
    `step["maneuver"]["instruction"]` was an unconditional KeyError — this
    whole provider branch raised on every call and nobody noticed, because
    production runs MAPS_PROVIDER=google|yandex and nothing consumes
    `get_route()["steps"]`.
    """

    def _payload(self, maneuver, name=""):
        return {
            "code": "Ok",
            "routes": [
                {
                    "distance": 1200.0,
                    "duration": 180.0,
                    "geometry": _ENCODED_POLYLINE,
                    "legs": [
                        {
                            "steps": [
                                {
                                    "distance": 1200.0,
                                    "duration": 180.0,
                                    "name": name,
                                    "maneuver": maneuver,
                                }
                            ]
                        }
                    ],
                }
            ],
        }

    def _route(self, app, monkeypatch, payload):
        _capture_get(monkeypatch, payload)
        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "http://osrm:5000")
            monkeypatch.setitem(app.config, "OSRM_PUBLIC_FALLBACK_ENABLED", False)
            svc = MapsService()
            svc.provider = "osm"
            return svc.get_route(41.30, 69.25, 41.35, 69.30)

    def test_real_osrm_maneuver_shape_does_not_raise(self, app, monkeypatch):
        """The exact maneuver dict the live engine returned."""
        payload = self._payload(
            {"bearing_after": 38, "bearing_before": 0, "location": [69.21225, 41.338856],
             "modifier": "left", "type": "depart"},
            name="Ohakchi 3-chi berk ko'cha",
        )
        result = self._route(app, monkeypatch, payload)
        assert result["steps"][0]["instruction"] == "Depart left onto Ohakchi 3-chi berk ko'cha"

    def test_turn_without_street_name_omits_the_onto_clause(self, app, monkeypatch):
        """Unnamed service roads are common in Tashkent OSM data; the text
        must not read 'Turn right onto '."""
        payload = self._payload({"type": "turn", "modifier": "right", "location": [0, 0]}, name="")
        result = self._route(app, monkeypatch, payload)
        assert result["steps"][0]["instruction"] == "Turn right"

    def test_maneuver_without_modifier_still_produces_text(self, app, monkeypatch):
        """`arrive` has no modifier."""
        payload = self._payload({"type": "arrive", "location": [0, 0]}, name="")
        result = self._route(app, monkeypatch, payload)
        assert result["steps"][0]["instruction"] == "Arrive"

    def test_a_totally_empty_maneuver_never_raises(self, app, monkeypatch):
        """Defensive: an unknown future maneuver shape must degrade, not 500."""
        payload = self._payload({}, name="Amir Temur")
        result = self._route(app, monkeypatch, payload)
        assert isinstance(result["steps"][0]["instruction"], str)
        assert result["steps"][0]["instruction"]  # non-empty


@pytest.mark.unit
@pytest.mark.delivery
class TestPublicDemoStaysGated:
    def test_public_demo_is_used_only_when_explicitly_enabled(self, app, monkeypatch):
        """With no self-hosted engine, the demo is reachable — but only
        because the operator opted in."""
        mock_get = _capture_get(monkeypatch)

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
            monkeypatch.setitem(app.config, "OSRM_PUBLIC_FALLBACK_ENABLED", True)
            svc = MapsService()
            svc.provider = "osm"
            svc.get_route(41.30, 69.25, 41.35, 69.30)

        assert "router.project-osrm.org" in mock_get.call_args[0][0]

    def test_no_engine_and_no_opt_in_refuses_rather_than_calling_the_demo(self, app, monkeypatch):
        """The defect this file exists to prevent: silently falling back to a
        third-party demo server whose usage policy forbids production use.
        Failing loudly is correct — the dispatch map already degrades to
        straight dashed legs when geometry is unavailable."""
        mock_get = _capture_get(monkeypatch)

        with app.app_context():
            monkeypatch.setitem(app.config, "OSRM_BASE_URL", "")
            monkeypatch.setitem(app.config, "OSRM_PUBLIC_FALLBACK_ENABLED", False)
            svc = MapsService()
            svc.provider = "osm"
            with pytest.raises(ExternalServiceError):
                svc.get_route(41.30, 69.25, 41.35, 69.30)

        mock_get.assert_not_called()
