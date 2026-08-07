"""The driver's 'Optimise routes' button must not look broken when dispatch
has locked the route: the call succeeds, the list is unchanged, and the
payload says why."""

from datetime import datetime, timezone
from unittest.mock import patch

from business_app.models.delivery import DeliveryRoute


class TestOptimizeRouteLocked:
    def test_locked_route_reports_route_locked_true(
        self, client, db, driver_auth_headers, delivery_driver, driver_with_location
    ):
        route = DeliveryRoute(
            name="r",
            delivery_person_id=delivery_driver.id,
            start_location_lat=41.30,
            start_location_lng=69.24,
            route_date=datetime.now(timezone.utc),
            optimized_order=[],
            manual_override=True,
        )
        db.session.add(route)
        db.session.commit()

        with patch(
            "business_app.services.route_optimization_service.RouteOptimizationService.optimize_for_driver",
            return_value=route,
        ):
            resp = client.post("/api/v1/staff/delivery/optimize-route", headers=driver_auth_headers)

        assert resp.status_code == 200
        assert resp.get_json()["data"]["route_locked"] is True

    def test_unlocked_route_reports_route_locked_false(
        self, client, db, driver_auth_headers, delivery_driver, driver_with_location
    ):
        with patch(
            "business_app.services.route_optimization_service.RouteOptimizationService.optimize_for_driver",
            return_value=None,
        ):
            resp = client.post("/api/v1/staff/delivery/optimize-route", headers=driver_auth_headers)

        assert resp.status_code == 200
        assert resp.get_json()["data"]["route_locked"] is False
