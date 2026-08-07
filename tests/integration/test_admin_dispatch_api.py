"""Dispatch API.

The session-scoped `client` fixture leaks JWT cookies between tests, so the
403 cases send an explicit non-admin header rather than relying on "no auth".

NOTE on TestGeometry (deviation from the task-7 brief, documented per its own
"if the brief's code and tests disagree, STOP and tell me" instruction):
the brief's two geometry tests built their `DeliveryRoute` fixture with
`optimized_order=[]`. The brief's OWN implementation (Step 4) short-circuits
before ever calling `MapsService.get_route` or touching the cache whenever
`not route.optimized_order` — by design ("Do not build the snapshot to get
geometry coordinates" / degrade only when there is something to degrade).
With an empty `optimized_order`, both tests would hit that early return on
every call, so `cached` would be `False` on the second call (not `True` as
asserted) and `approximate` would be `False` on provider failure (not `True`
as asserted) — the assertions could never pass against the given
implementation. The implementation's short-circuit is correct and matches
the brief's own stated design; the fixtures were missing the one real,
geocoded `Delivery` needed to reach the cache/degrade code paths at all. Both
tests below add that delivery via a real `Order` + `UserAddress` and point
`optimized_order` at it — the assertions are otherwise unchanged from the
brief.
"""

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest


class TestSnapshotEndpoint:
    def test_admin_gets_every_snapshot_section(self, client, db, admin_auth_headers):
        resp = client.get("/api/v1/admin/dispatch/snapshot", headers=admin_auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()["data"]
        assert set(data) >= {"date", "orders", "drivers", "routes", "pool", "unmapped"}

    def test_explicit_date_is_echoed_back(self, client, db, admin_auth_headers):
        resp = client.get("/api/v1/admin/dispatch/snapshot?date=2026-08-01", headers=admin_auth_headers)
        assert resp.get_json()["data"]["date"] == "2026-08-01"

    def test_malformed_date_is_a_400(self, client, db, admin_auth_headers):
        resp = client.get("/api/v1/admin/dispatch/snapshot?date=not-a-date", headers=admin_auth_headers)
        assert resp.status_code == 400

    def test_operator_is_forbidden(self, client, db, operator_auth_headers):
        resp = client.get("/api/v1/admin/dispatch/snapshot", headers=operator_auth_headers)
        assert resp.status_code == 403


class TestStopsEndpoint:
    def test_delegates_with_exact_arguments(self, client, db, admin_auth_headers, admin_user, delivery_driver):
        from business_app.models.delivery import DeliveryRoute

        route = DeliveryRoute(
            name="r", delivery_person_id=delivery_driver.id,
            start_location_lat=41.3, start_location_lng=69.2,
            route_date=datetime.now(timezone.utc), optimized_order=[7, 9],
        )
        db.session.add(route)
        db.session.commit()

        with patch(
            "business_app.api.admin_dispatch.RouteEditService.set_stop_order", return_value=route
        ) as edit:
            resp = client.put(
                f"/api/v1/admin/dispatch/routes/{delivery_driver.id}/stops",
                json={"ordered_delivery_ids": [9, 7], "pinned": {"9": 0}, "expected_delivery_ids": [7, 9]},
                headers=admin_auth_headers,
            )

        assert resp.status_code == 200
        assert edit.call_args.kwargs["driver_id"] == delivery_driver.id
        assert edit.call_args.kwargs["ordered_delivery_ids"] == [9, 7]
        assert edit.call_args.kwargs["pinned"] == {"9": 0}
        assert edit.call_args.kwargs["expected_delivery_ids"] == [7, 9]
        assert edit.call_args.kwargs["actor_id"] == admin_user.id

    def test_stale_save_is_a_409_carrying_the_live_ids(self, client, db, admin_auth_headers, delivery_driver):
        from business_app.services.route_edit_service import RouteStaleError

        with patch(
            "business_app.api.admin_dispatch.RouteEditService.set_stop_order",
            side_effect=RouteStaleError("moved", current_delivery_ids=[9]),
        ):
            resp = client.put(
                f"/api/v1/admin/dispatch/routes/{delivery_driver.id}/stops",
                json={"ordered_delivery_ids": [9, 7], "pinned": {}, "expected_delivery_ids": [7, 9]},
                headers=admin_auth_headers,
            )

        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error_code"] == "DISPATCH_ROUTE_STALE"
        assert body["data"]["current_delivery_ids"] == [9]

    def test_missing_ordered_ids_is_a_400(self, client, db, admin_auth_headers, delivery_driver):
        resp = client.put(
            f"/api/v1/admin/dispatch/routes/{delivery_driver.id}/stops",
            json={"pinned": {}},
            headers=admin_auth_headers,
        )
        assert resp.status_code == 400


class TestReoptimizeEndpoint:
    def test_delegates_with_exact_arguments_and_returns_the_new_order(
        self, client, db, admin_auth_headers, admin_user, delivery_driver
    ):
        """"Reset to optimal" computes a BRAND NEW sequence the client has no
        other source for (unlike PUT .../stops, where the client just submitted
        the order it's echoing back) — the response must carry it, not just a
        bare 200.
        """
        from business_app.models.delivery import DeliveryRoute

        route = DeliveryRoute(
            name="r", delivery_person_id=delivery_driver.id,
            start_location_lat=41.3, start_location_lng=69.2,
            route_date=datetime.now(timezone.utc), optimized_order=[11, 5, 8],
        )
        db.session.add(route)
        db.session.commit()

        with patch(
            "business_app.api.admin_dispatch.RouteEditService.reoptimize", return_value=route
        ) as reopt:
            resp = client.post(
                f"/api/v1/admin/dispatch/routes/{delivery_driver.id}/reoptimize",
                headers=admin_auth_headers,
            )

        assert resp.status_code == 200
        assert reopt.call_args.kwargs == {"driver_id": delivery_driver.id, "actor_id": admin_user.id}
        assert resp.get_json()["data"]["route"]["optimized_order"] == [11, 5, 8]

    def test_operator_is_forbidden(self, client, db, operator_auth_headers, delivery_driver):
        resp = client.post(
            f"/api/v1/admin/dispatch/routes/{delivery_driver.id}/reoptimize",
            headers=operator_auth_headers,
        )
        assert resp.status_code == 403


class TestAssignAndUnassign:
    def test_assign_delegates_with_exact_arguments(self, client, db, admin_auth_headers, admin_user):
        with patch("business_app.api.admin_dispatch.RouteEditService.move_stop") as move:
            move.return_value.id = 812
            resp = client.post(
                "/api/v1/admin/dispatch/stops/812/assign",
                json={"driver_id": 5, "position": 2},
                headers=admin_auth_headers,
            )

        assert resp.status_code == 200
        assert move.call_args.kwargs == {
            "delivery_id": 812,
            "to_driver_id": 5,
            "actor_id": admin_user.id,
            "position": 2,
        }

    def test_unassign_delegates_with_exact_arguments(self, client, db, admin_auth_headers, admin_user):
        with patch("business_app.api.admin_dispatch.RouteEditService.return_stop_to_pool") as pool:
            pool.return_value.id = 812
            resp = client.post(
                "/api/v1/admin/dispatch/stops/812/unassign",
                json={"reason": "shop closed"},
                headers=admin_auth_headers,
            )

        assert resp.status_code == 200
        assert pool.call_args.kwargs == {
            "delivery_id": 812,
            "actor_id": admin_user.id,
            "reason": "shop closed",
        }

    def test_assign_requires_driver_id(self, client, db, admin_auth_headers):
        resp = client.post("/api/v1/admin/dispatch/stops/812/assign", json={}, headers=admin_auth_headers)
        assert resp.status_code == 400


def _make_geocoded_delivery(db, user, *, order_number, lat, lng):
    """A real, geocoded `Delivery` — Order + UserAddress + Delivery — so a
    `DeliveryRoute.optimized_order` referencing it actually resolves stop
    coordinates. See the module docstring: the geometry endpoint deliberately
    short-circuits (no Maps call, no cache) when the route has no resolvable
    stops, so exercising the cache/degrade paths needs a real one.
    """
    from business_app.models.delivery import Delivery
    from business_app.models.order import Order
    from business_app.models.user import UserAddress
    from shared.enums import DeliveryStatus, OrderStatus

    address = UserAddress(
        user_id=user.id, full_address="Chilonzor 12", city="Tashkent",
        latitude=lat, longitude=lng,
    )
    db.session.add(address)
    db.session.flush()

    order = Order(
        user_id=user.id,
        order_number=order_number,
        status=OrderStatus.OUT_FOR_DELIVERY,
        delivery_address_id=address.id,
        delivery_date=datetime.now(timezone.utc),
    )
    db.session.add(order)
    db.session.flush()

    delivery = Delivery(
        order_id=order.id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    return delivery


class TestGeometry:
    def test_second_call_is_served_from_cache(self, client, db, admin_auth_headers, delivery_driver, sample_user):
        from business_app.models.delivery import DeliveryRoute

        delivery = _make_geocoded_delivery(
            db, sample_user, order_number="ORD-GEOM-CACHE-1", lat=41.31, lng=69.25
        )
        db.session.add(
            DeliveryRoute(
                name="r", delivery_person_id=delivery_driver.id,
                start_location_lat=41.3, start_location_lng=69.2,
                route_date=datetime.now(timezone.utc), optimized_order=[delivery.id],
            )
        )
        db.session.commit()

        # `MapsService.get_route()`'s real, normalised contract (see
        # `business_app/services/maps_service.py`) is `"geometry":
        # [[lat, lng], ...] | None` — never a `"polyline"` key, never an
        # encoded string. Mocking a plain coordinate array under "geometry"
        # is realistic here because MapsService itself is responsible for
        # decoding provider-specific shapes (Google/OSRM encoded strings,
        # Yandex's nested legs/steps/polyline.points) into exactly this
        # shape before `get_route()` ever returns — that decoding is covered
        # separately, with real encoded-polyline fixtures, in
        # tests/unit/test_maps_service_geometry.py and
        # tests/unit/test_polyline_decoder.py. This test's job is only to
        # prove the HANDLER relays what the service gives it unchanged.
        with patch(
            "business_app.api.admin_dispatch.MapsService.get_route",
            return_value={"geometry": [[41.3, 69.2], [41.31, 69.25]], "distance_km": 4.0, "duration_minutes": 9.0},
        ) as get_route:
            first = client.get(
                f"/api/v1/admin/dispatch/routes/{delivery_driver.id}/geometry", headers=admin_auth_headers
            )
            second = client.get(
                f"/api/v1/admin/dispatch/routes/{delivery_driver.id}/geometry", headers=admin_auth_headers
            )

        assert first.status_code == 200 and second.status_code == 200
        first_data = first.get_json()["data"]
        assert first_data["cached"] is False
        assert first_data["geometry"] == [[41.3, 69.2], [41.31, 69.25]]
        assert first_data["approximate"] is False
        assert get_route.call_count <= 1
        assert second.get_json()["data"]["cached"] is True

    def test_successful_call_with_no_usable_geometry_is_still_marked_approximate(
        self, client, db, admin_auth_headers, delivery_driver, sample_user
    ):
        """A provider call can succeed (no exception) yet legitimately carry no
        real road path — this is exactly what happens on Yandex whenever a
        response yields no usable `legs[].steps[].polyline.points`. Against
        the old handler code, `"approximate"` was hard-coded `False` on the
        entire try/success branch regardless of whether `geometry` actually
        came back, which is precisely the "silently degrades to dashed legs
        forever, but LIES about it being real" defect: the dashed fallback
        line was drawn correctly (OperationsMap.jsx checks `geometry` length,
        not `approximate`), but the API told the truth about nothing.
        """
        from business_app.models.delivery import DeliveryRoute

        delivery = _make_geocoded_delivery(
            db, sample_user, order_number="ORD-GEOM-NOPATH-1", lat=41.32, lng=69.26
        )
        db.session.add(
            DeliveryRoute(
                name="r3", delivery_person_id=delivery_driver.id,
                start_location_lat=41.30, start_location_lng=69.24,
                route_date=datetime.now(timezone.utc), optimized_order=[delivery.id],
            )
        )
        db.session.commit()

        with patch(
            "business_app.api.admin_dispatch.MapsService.get_route",
            return_value={"geometry": None, "distance_km": 3.0, "duration_minutes": 8.0},
        ):
            resp = client.get(
                f"/api/v1/admin/dispatch/routes/{delivery_driver.id}/geometry", headers=admin_auth_headers
            )

        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["geometry"] is None
        assert body["approximate"] is True

    def test_provider_failure_degrades_to_approximate(self, client, db, admin_auth_headers, delivery_driver, sample_user):
        from business_app.models.delivery import DeliveryRoute
        from business_app.utils.exceptions import ExternalServiceError

        delivery = _make_geocoded_delivery(
            db, sample_user, order_number="ORD-GEOM-FAIL-1", lat=41.41, lng=69.31
        )
        db.session.add(
            DeliveryRoute(
                name="r2", delivery_person_id=delivery_driver.id,
                start_location_lat=41.4, start_location_lng=69.3,
                route_date=datetime.now(timezone.utc), optimized_order=[delivery.id],
            )
        )
        db.session.commit()

        with patch(
            "business_app.api.admin_dispatch.MapsService.get_route",
            side_effect=ExternalServiceError("provider down"),
        ):
            resp = client.get(
                f"/api/v1/admin/dispatch/routes/{delivery_driver.id}/geometry", headers=admin_auth_headers
            )

        assert resp.status_code == 200
        body = resp.get_json()["data"]
        assert body["geometry"] is None
        assert body["approximate"] is True
