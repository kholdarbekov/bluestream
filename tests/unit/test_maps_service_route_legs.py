"""Per-leg distance/duration must survive `MapsService.get_route()`.

The dispatch board wants to show how far and how long it is from each stop to
the next. That data is already inside the response the app fetches for the
route polyline — OSRM returns `routes[0].legs[]`, each with its own `distance`
and `duration` — and it was being iterated purely to flatten `steps` and then
thrown away.

Normalisation belongs here rather than in the API handler: `admin_dispatch.py`
has a boundary-coupling budget of zero (test_structure_boundary_regressions),
so it must never learn what an OSRM leg or a Yandex leg looks like. Every
provider therefore reports legs in one shape, or reports `None`.

`None` is a real answer, not a failure to compute one: the routing spec's
honest-ETA rule forbids presenting a straight-line guess as a measured
figure, so a provider that cannot give per-leg numbers must yield nothing
rather than something plausible.
"""

from unittest.mock import MagicMock

import pytest

from business_app.services.maps_service import MapsService

_ENCODED_POLYLINE = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"


def _fake_response(json_payload):
    resp = MagicMock(name="fake_response")
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_payload)
    resp.status_code = 200
    resp.text = ""
    return resp


def _osrm_leg(distance_m, duration_s):
    return {"distance": distance_m, "duration": duration_s, "steps": []}


@pytest.mark.unit
@pytest.mark.delivery
class TestOsrmRouteLegs:
    def _route_with(self, monkeypatch, app, legs):
        payload = {
            "code": "Ok",
            "routes": [
                {
                    "distance": sum(leg["distance"] or 0 for leg in legs),
                    "duration": sum(leg["duration"] or 0 for leg in legs),
                    "geometry": _ENCODED_POLYLINE,
                    "legs": legs,
                }
            ],
        }
        monkeypatch.setattr(
            "business_app.services.maps_service.requests.get",
            lambda *a, **kw: _fake_response(payload),
        )
        with app.app_context():
            app.config["MAPS_PROVIDER"] = "osm"
            app.config["OSRM_BASE_URL"] = "http://osrm.test"
            service = MapsService()
            service.provider = "osm"
            service.osm_routing_url = "http://osrm.test/route/v1/driving"
            return service.get_route(41.30, 69.24, 41.33, 69.29, waypoints=[(41.31, 69.25)])

    def test_reports_one_leg_per_hop_in_kilometres_and_minutes(self, app, monkeypatch):
        result = self._route_with(
            monkeypatch, app, [_osrm_leg(4200.0, 660.0), _osrm_leg(1800.0, 300.0)]
        )

        assert result["legs"] == [
            {"distance_km": 4.2, "duration_minutes": 11.0},
            {"distance_km": 1.8, "duration_minutes": 5.0},
        ]

    def test_leg_count_matches_the_hops_between_the_supplied_points(self, app, monkeypatch):
        # start -> waypoint -> end is two hops. If this ever returns three,
        # the UI would silently pair leg[i] with the wrong stop.
        result = self._route_with(
            monkeypatch, app, [_osrm_leg(4200.0, 660.0), _osrm_leg(1800.0, 300.0)]
        )

        assert len(result["legs"]) == 2

    def test_still_returns_the_totals_and_geometry(self, app, monkeypatch):
        result = self._route_with(
            monkeypatch, app, [_osrm_leg(4200.0, 660.0), _osrm_leg(1800.0, 300.0)]
        )

        assert result["distance_km"] == 6.0
        assert result["duration_minutes"] == 16.0
        assert result["geometry"][0] == [38.5, -120.2]

    def test_a_response_with_no_legs_reports_none_rather_than_an_empty_list(self, app, monkeypatch):
        # An empty list reads as "measured: zero legs". `None` reads as "not
        # measured", which is the truth and is what the UI suppresses on.
        result = self._route_with(monkeypatch, app, [])

        assert result["legs"] is None

    def test_a_leg_missing_its_numbers_disqualifies_the_whole_set(self, app, monkeypatch):
        # Partial legs cannot be rendered against a stop list without guessing
        # which hop the gap belongs to. Refuse the set instead.
        result = self._route_with(
            monkeypatch, app, [_osrm_leg(4200.0, 660.0), {"distance": None, "duration": 300.0, "steps": []}]
        )

        assert result["legs"] is None


@pytest.mark.unit
@pytest.mark.delivery
class TestYandexRouteLegs:
    """Yandex publishes no leg aggregate at all — only `legs[].steps[]`
    carry `length`/`duration` — so a leg's numbers have to be summed from its
    own steps, the same way the route totals already are."""

    def _route_with(self, monkeypatch, app, legs):
        payload = {"route": {"legs": legs}}
        monkeypatch.setattr(
            "business_app.services.maps_service.request_with_retry",
            lambda *a, **kw: _fake_response(payload),
        )
        with app.app_context():
            service = MapsService()
            service.provider = "yandex"
            service.yandex_api_key = "test-key"
            return service.get_route(41.30, 69.24, 41.33, 69.29, waypoints=[(41.31, 69.25)])

    def test_sums_each_leg_from_its_own_steps(self, app, monkeypatch):
        result = self._route_with(
            monkeypatch,
            app,
            [
                {"steps": [{"length": 3000, "duration": 400}, {"length": 1200, "duration": 260}]},
                {"steps": [{"length": 1800, "duration": 300}]},
            ],
        )

        assert result["legs"] == [
            {"distance_km": 4.2, "duration_minutes": 11.0},
            {"distance_km": 1.8, "duration_minutes": 5.0},
        ]

    def test_a_leg_with_no_usable_steps_disqualifies_the_set(self, app, monkeypatch):
        result = self._route_with(
            monkeypatch, app, [{"steps": [{"length": 3000, "duration": 400}]}, {"steps": []}]
        )

        assert result["legs"] is None


@pytest.mark.unit
@pytest.mark.delivery
class TestGoogleRouteLegs:
    def test_reports_every_leg_not_just_the_first(self, app, monkeypatch):
        # The Google branch read `legs[0]` only, so a multi-waypoint route
        # reported the first hop's numbers as the whole route's.
        payload = {
            "status": "OK",
            "routes": [
                {
                    "overview_polyline": {"points": _ENCODED_POLYLINE},
                    "legs": [
                        {
                            "distance": {"value": 4200, "text": "4.2 km"},
                            "duration": {"value": 660, "text": "11 mins"},
                            "steps": [],
                        },
                        {
                            "distance": {"value": 1800, "text": "1.8 km"},
                            "duration": {"value": 300, "text": "5 mins"},
                            "steps": [],
                        },
                    ],
                }
            ],
        }
        monkeypatch.setattr(
            "business_app.services.maps_service.requests.get",
            lambda *a, **kw: _fake_response(payload),
        )
        with app.app_context():
            service = MapsService()
            service.provider = "google"
            service.google_api_key = "test-key"
            result = service.get_route(41.30, 69.24, 41.33, 69.29, waypoints=[(41.31, 69.25)])

        assert result["legs"] == [
            {"distance_km": 4.2, "duration_minutes": 11.0},
            {"distance_km": 1.8, "duration_minutes": 5.0},
        ]
