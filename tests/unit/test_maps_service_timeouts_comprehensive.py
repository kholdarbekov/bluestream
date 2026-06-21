"""Comprehensive regression: EVERY outbound MapsService HTTP call must carry a
positive request timeout.

Prod incident (02:02 TimeLimitExceeded(120)): a bare ``requests.get(...)`` with
no ``timeout=`` blocks indefinitely on a stalled connection, which hung
``optimize_driver_route_task`` past its hard limit and got the worker SIGKILLed.

The fix wires ``timeout=self.request_timeout`` (default 10, overridable via the
``MAPS_REQUEST_TIMEOUT`` config) into every outbound map request across the
google / yandex / osm geocode + reverse-geocode + route + places methods.

These tests drive each provider through its PUBLIC method
(geocode_address / reverse_geocode / get_route / find_nearby_places) with the
matching provider set, and assert the outbound HTTP layer received a positive
``timeout=`` kwarg. The companion ``test_maps_service_request_timeout.py``
covers the yandex-geocode case only; this file is exhaustive.

NOTE on the yandex *route* path: it does NOT use ``requests.get`` — it goes
through ``business_app.utils.http_client.request_with_retry`` which calls
``requests.request(..., timeout=timeout_seconds)`` internally. We assert the
timeout on that real underlying call so the end-to-end path is exercised.
"""

from unittest.mock import MagicMock

import pytest

from business_app.services import maps_service as maps_module
from business_app.services.maps_service import MapsService
from business_app.utils import http_client as http_client_module


# ---------------------------------------------------------------------------
# Provider-shaped fake JSON payloads (just enough for each parser to succeed).
# ---------------------------------------------------------------------------

_GOOGLE_GEOCODE_OK = {
    "status": "OK",
    "results": [
        {
            "geometry": {"location": {"lat": 41.3111, "lng": 69.2401}},
            "formatted_address": "Tashkent, Uzbekistan",
            "address_components": [],
            "place_id": "g-place-1",
        }
    ],
}

_GOOGLE_DIRECTIONS_OK = {
    "status": "OK",
    "routes": [
        {
            "overview_polyline": {"points": "abc123"},
            "legs": [
                {
                    "distance": {"value": 5000, "text": "5 km"},
                    "duration": {"value": 600, "text": "10 mins"},
                    "duration_in_traffic": {"value": 720},
                    "steps": [
                        {
                            "html_instructions": "Head north",
                            "distance": {"text": "1 km"},
                            "duration": {"text": "2 mins"},
                        }
                    ],
                }
            ],
        }
    ],
}

_GOOGLE_PLACES_OK = {
    "results": [
        {
            "name": "Aqua Shop",
            "place_id": "g-place-2",
            "geometry": {"location": {"lat": 41.30, "lng": 69.25}},
            "rating": 4.5,
            "types": ["store"],
            "vicinity": "Some street",
        }
    ]
}

_YANDEX_GEOCODE_OK = {
    "response": {
        "GeoObjectCollection": {
            "featureMember": [
                {
                    "GeoObject": {
                        "Point": {"pos": "69.2401 41.3111"},
                        "metaDataProperty": {
                            "GeocoderMetaData": {"text": "Tashkent", "precision": "exact"}
                        },
                    }
                }
            ]
        }
    }
}

_YANDEX_ROUTE_OK = {
    "route": {
        "distance": {"value": 5000},
        "duration": {"value": 600},
        "duration_in_traffic": {"value": 720},
        "geometry": "yandex-poly",
    }
}

_OSM_SEARCH_OK = [
    {
        "lat": "41.3111",
        "lon": "69.2401",
        "display_name": "Tashkent, Uzbekistan",
        "osm_id": 12345,
        "place_id": 67890,
    }
]

_OSM_REVERSE_OK = {
    "display_name": "Tashkent, Uzbekistan",
    "address": {"city": "Tashkent"},
    "osm_id": 12345,
    "place_id": 67890,
}

_OSRM_ROUTE_OK = {
    "code": "Ok",
    "routes": [
        {
            "distance": 5000,
            "duration": 600,
            "geometry": "osrm-poly",
            "legs": [
                {
                    "steps": [
                        {
                            "maneuver": {"instruction": "Head north"},
                            "distance": 100,
                            "duration": 30,
                        }
                    ]
                }
            ],
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


def _install_get(monkeypatch, json_payload):
    """Patch maps_service.requests.get to a recording mock returning a fake resp."""
    mock_get = MagicMock(return_value=_fake_response(json_payload))
    monkeypatch.setattr(maps_module.requests, "get", mock_get)
    return mock_get


def _assert_positive_timeout(mock_get):
    assert mock_get.called, "expected an outbound HTTP call"
    for call in mock_get.call_args_list:
        timeout = call.kwargs.get("timeout")
        assert timeout is not None, "outbound map request must pass a timeout= kwarg"
        assert timeout > 0, f"timeout must be positive, got {timeout!r}"


# ===========================================================================
# Google provider
# ===========================================================================


@pytest.mark.unit
@pytest.mark.delivery
class TestGoogleMapsTimeouts:
    def test_google_geocode_passes_positive_timeout(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "google"
            svc.google_api_key = "g-key"
            mock_get = _install_get(monkeypatch, _GOOGLE_GEOCODE_OK)

            result = svc.geocode_address("Some Street 1", "Tashkent")

        assert result["latitude"] == 41.3111
        _assert_positive_timeout(mock_get)
        # Geocode is a single GET.
        assert mock_get.call_args.kwargs["timeout"] == 10

    def test_google_reverse_geocode_passes_positive_timeout(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "google"
            svc.google_api_key = "g-key"
            mock_get = _install_get(monkeypatch, _GOOGLE_GEOCODE_OK)

            result = svc.reverse_geocode(41.3111, 69.2401)

        assert result["formatted_address"] == "Tashkent, Uzbekistan"
        _assert_positive_timeout(mock_get)

    def test_google_get_route_directions_passes_positive_timeout(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "google"
            svc.google_api_key = "g-key"
            mock_get = _install_get(monkeypatch, _GOOGLE_DIRECTIONS_OK)

            result = svc.get_route(41.30, 69.25, 41.35, 69.30)

        assert result["distance_km"] == 5.0
        _assert_positive_timeout(mock_get)

    def test_google_get_route_with_waypoints_passes_positive_timeout(self, app, monkeypatch):
        """The waypoints branch is a different param-building path; still must
        carry a timeout."""
        with app.app_context():
            svc = MapsService()
            svc.provider = "google"
            svc.google_api_key = "g-key"
            mock_get = _install_get(monkeypatch, _GOOGLE_DIRECTIONS_OK)

            svc.get_route(41.30, 69.25, 41.35, 69.30, waypoints=[(41.32, 69.27)])

        _assert_positive_timeout(mock_get)

    def test_google_find_nearby_places_passes_positive_timeout(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "google"
            svc.google_api_key = "g-key"
            mock_get = _install_get(monkeypatch, _GOOGLE_PLACES_OK)

            result = svc.find_nearby_places(41.30, 69.25, "store", 1000)

        assert result[0]["name"] == "Aqua Shop"
        _assert_positive_timeout(mock_get)


# ===========================================================================
# Yandex provider
# ===========================================================================


@pytest.mark.unit
@pytest.mark.delivery
class TestYandexMapsTimeouts:
    def test_yandex_geocode_passes_positive_timeout(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"
            mock_get = _install_get(monkeypatch, _YANDEX_GEOCODE_OK)

            result = svc.geocode_address("Some Street 1", "Tashkent")

        assert result["latitude"] == 41.3111
        _assert_positive_timeout(mock_get)

    def test_yandex_reverse_geocode_via_geocode_passes_positive_timeout(self, app, monkeypatch):
        """Yandex reverse-geocode delegates to _yandex_geocode, which is the
        requests.get path — it must still carry a timeout."""
        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"
            mock_get = _install_get(monkeypatch, _YANDEX_GEOCODE_OK)

            result = svc.reverse_geocode(41.3111, 69.2401)

        assert result["formatted_address"] == "Tashkent"
        _assert_positive_timeout(mock_get)

    def test_yandex_get_route_passes_positive_timeout(self, app, monkeypatch):
        """The yandex route path goes through request_with_retry, which calls
        requests.request(..., timeout=timeout_seconds). Assert that underlying
        call carries a positive timeout end-to-end."""
        with app.app_context():
            svc = MapsService()
            svc.provider = "yandex"
            svc.yandex_api_key = "y-key"

            mock_request = MagicMock(return_value=_fake_response(_YANDEX_ROUTE_OK))
            monkeypatch.setattr(http_client_module.requests, "request", mock_request)

            result = svc.get_route(41.30, 69.25, 41.35, 69.30)

        assert result["distance_km"] == 5.0
        assert mock_request.called, "yandex route must hit the HTTP layer"
        timeout = mock_request.call_args.kwargs.get("timeout")
        assert timeout is not None, "yandex route request must pass a timeout"
        assert timeout > 0


# ===========================================================================
# OpenStreetMap / OSRM provider
# ===========================================================================


@pytest.mark.unit
@pytest.mark.delivery
class TestOSMMapsTimeouts:
    def test_osm_nominatim_geocode_search_passes_positive_timeout(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "osm"
            mock_get = _install_get(monkeypatch, _OSM_SEARCH_OK)

            result = svc.geocode_address("Some Street 1", "Tashkent")

        assert result["latitude"] == 41.3111
        _assert_positive_timeout(mock_get)

    def test_osm_nominatim_reverse_passes_positive_timeout(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "osm"
            mock_get = _install_get(monkeypatch, _OSM_REVERSE_OK)

            result = svc.reverse_geocode(41.3111, 69.2401)

        assert result["formatted_address"] == "Tashkent, Uzbekistan"
        _assert_positive_timeout(mock_get)

    def test_osrm_get_route_passes_positive_timeout(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "osm"
            mock_get = _install_get(monkeypatch, _OSRM_ROUTE_OK)

            result = svc.get_route(41.30, 69.25, 41.35, 69.30)

        assert result["distance_km"] == 5.0
        _assert_positive_timeout(mock_get)

    def test_osrm_get_route_with_waypoints_passes_positive_timeout(self, app, monkeypatch):
        with app.app_context():
            svc = MapsService()
            svc.provider = "osm"
            mock_get = _install_get(monkeypatch, _OSRM_ROUTE_OK)

            svc.get_route(41.30, 69.25, 41.35, 69.30, waypoints=[(41.32, 69.27)])

        _assert_positive_timeout(mock_get)


# ===========================================================================
# Config override: MAPS_REQUEST_TIMEOUT
# ===========================================================================


@pytest.mark.unit
@pytest.mark.delivery
class TestMapsRequestTimeoutConfig:
    def test_default_request_timeout_is_ten(self, app):
        with app.app_context():
            svc = MapsService()
        assert svc.request_timeout == 10

    def test_config_override_is_honoured_on_google_geocode(self, app, monkeypatch):
        monkeypatch.setitem(app.config, "MAPS_REQUEST_TIMEOUT", 3)
        with app.app_context():
            svc = MapsService()
            assert svc.request_timeout == 3
            svc.provider = "google"
            svc.google_api_key = "g-key"
            mock_get = _install_get(monkeypatch, _GOOGLE_GEOCODE_OK)

            svc.geocode_address("Some Street 1", "Tashkent")

        assert mock_get.call_args.kwargs["timeout"] == 3

    def test_config_override_is_honoured_on_osm_reverse(self, app, monkeypatch):
        monkeypatch.setitem(app.config, "MAPS_REQUEST_TIMEOUT", 7)
        with app.app_context():
            svc = MapsService()
            svc.provider = "osm"
            mock_get = _install_get(monkeypatch, _OSM_REVERSE_OK)

            svc.reverse_geocode(41.3111, 69.2401)

        assert mock_get.call_args.kwargs["timeout"] == 7

    def test_zero_or_negative_override_would_be_caught(self, app, monkeypatch):
        """Documenting invariant: a non-positive timeout is a misconfiguration.
        The service stores whatever config says; our timeout assertions require
        a POSITIVE value, so a regression to 0/None would fail the suite."""
        monkeypatch.setitem(app.config, "MAPS_REQUEST_TIMEOUT", 0)
        with app.app_context():
            svc = MapsService()
            svc.provider = "google"
            svc.google_api_key = "g-key"
            mock_get = _install_get(monkeypatch, _GOOGLE_GEOCODE_OK)
            svc.geocode_address("Some Street 1", "Tashkent")

        # A zero timeout (effectively no timeout for requests) must be treated
        # as a bug by our assertion helper.
        passed_timeout = mock_get.call_args.kwargs.get("timeout")
        assert passed_timeout == 0
        with pytest.raises(AssertionError):
            _assert_positive_timeout(mock_get)


# ===========================================================================
# Structural sweep: NO outbound requests.get may omit a timeout, across every
# provider method, driven via the public API.
# ===========================================================================


@pytest.mark.unit
@pytest.mark.delivery
class TestNoOutboundCallWithoutTimeout:
    """Patch requests.get to record EVERY call and assert none omit a timeout,
    no matter which provider/method initiates it. This is the broad backstop
    that would catch a NEW bare requests.get added to any provider method."""

    _SCENARIOS = [
        # (provider, method-name, callable building the public-method call, json)
        ("google", "geocode_address", _GOOGLE_GEOCODE_OK,
         lambda s: s.geocode_address("Street 1", "Tashkent")),
        ("google", "reverse_geocode", _GOOGLE_GEOCODE_OK,
         lambda s: s.reverse_geocode(41.31, 69.24)),
        ("google", "get_route", _GOOGLE_DIRECTIONS_OK,
         lambda s: s.get_route(41.30, 69.25, 41.35, 69.30)),
        ("google", "find_nearby_places", _GOOGLE_PLACES_OK,
         lambda s: s.find_nearby_places(41.30, 69.25, "store", 1000)),
        ("yandex", "geocode_address", _YANDEX_GEOCODE_OK,
         lambda s: s.geocode_address("Street 1", "Tashkent")),
        ("yandex", "reverse_geocode", _YANDEX_GEOCODE_OK,
         lambda s: s.reverse_geocode(41.31, 69.24)),
        ("osm", "geocode_address", _OSM_SEARCH_OK,
         lambda s: s.geocode_address("Street 1", "Tashkent")),
        ("osm", "reverse_geocode", _OSM_REVERSE_OK,
         lambda s: s.reverse_geocode(41.31, 69.24)),
        ("osm", "get_route", _OSRM_ROUTE_OK,
         lambda s: s.get_route(41.30, 69.25, 41.35, 69.30)),
    ]

    @pytest.mark.parametrize(
        "provider,label,json_payload,invoke",
        _SCENARIOS,
        ids=[f"{p}-{m}" for p, m, _j, _c in _SCENARIOS],
    )
    def test_no_get_without_timeout(self, app, monkeypatch, provider, label, json_payload, invoke):
        recorded_timeouts = []

        def recording_get(*args, **kwargs):
            recorded_timeouts.append(kwargs.get("timeout", "MISSING"))
            return _fake_response(json_payload)

        with app.app_context():
            svc = MapsService()
            svc.provider = provider
            svc.google_api_key = "g-key"
            svc.yandex_api_key = "y-key"
            monkeypatch.setattr(maps_module.requests, "get", recording_get)

            invoke(svc)

        assert recorded_timeouts, f"{provider}/{label}: expected an outbound GET"
        for t in recorded_timeouts:
            assert t != "MISSING", f"{provider}/{label}: a requests.get omitted timeout="
            assert isinstance(t, (int, float)) and t > 0, (
                f"{provider}/{label}: timeout must be a positive number, got {t!r}"
            )
