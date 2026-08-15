"""Per-leg distance/time reaches the dispatch board, tied to the right stops.

The geometry endpoint already fetches everything needed for this in the call
it makes for the polyline, so these tests are about the two ways the feature
can lie rather than about whether the numbers arrive:

1. Legs must be paired with stops EXPLICITLY. `route_stop_coordinates` drops
   any stop whose address has no coordinates, so on a route with one
   ungeocoded stop, `legs[k]` and `optimized_order[k]` refer to different
   deliveries. A UI that zipped them positionally would print one stop's
   drive time next to another stop's name — confidently, and with no way to
   notice.

2. When the provider gives no measured legs, the endpoint must say so rather
   than substitute anything. Straight-line fill is forbidden by the routing
   spec's honest-ETA rule.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryRoute
from business_app.models.order import Order
from business_app.models.user import UserAddress
from shared.enums import DeliveryStatus, OrderStatus


def _delivery(db, user, *, order_number, lat, lng, driver_id):
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
        delivery_person_id=driver_id,
        status=DeliveryStatus.ASSIGNED,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    return delivery


def _route(db, driver_id, delivery_ids):
    db.session.add(
        DeliveryRoute(
            name="r",
            delivery_person_id=driver_id,
            start_location_lat=41.30,
            start_location_lng=69.24,
            route_date=datetime.now(timezone.utc),
            optimized_order=delivery_ids,
        )
    )
    db.session.commit()


def _get(client, headers, driver_id):
    response = client.get(f"/api/v1/admin/dispatch/routes/{driver_id}/geometry", headers=headers)
    assert response.status_code == 200
    return response.get_json()["data"]


@pytest.mark.integration
class TestLegsRelay:
    def test_relays_the_measured_legs(self, client, db, admin_auth_headers, delivery_driver, sample_user):
        first = _delivery(db, sample_user, order_number="ORD-LEG-1", lat=41.31, lng=69.25,
                          driver_id=delivery_driver.id)
        second = _delivery(db, sample_user, order_number="ORD-LEG-2", lat=41.33, lng=69.29,
                           driver_id=delivery_driver.id)
        _route(db, delivery_driver.id, [first.id, second.id])

        with patch(
            "business_app.api.admin_dispatch.MapsService.get_route",
            return_value={
                "geometry": [[41.30, 69.24], [41.31, 69.25], [41.33, 69.29]],
                "distance_km": 6.0,
                "duration_minutes": 16.0,
                "legs": [
                    {"distance_km": 4.2, "duration_minutes": 11.0},
                    {"distance_km": 1.8, "duration_minutes": 5.0},
                ],
            },
        ):
            data = _get(client, admin_auth_headers, delivery_driver.id)

        assert data["legs"] == [
            {"distance_km": 4.2, "duration_minutes": 11.0},
            {"distance_km": 1.8, "duration_minutes": 5.0},
        ]

    def test_publishes_which_stop_each_leg_arrives_at(
        self, client, db, admin_auth_headers, delivery_driver, sample_user
    ):
        first = _delivery(db, sample_user, order_number="ORD-LEG-M1", lat=41.31, lng=69.25,
                          driver_id=delivery_driver.id)
        second = _delivery(db, sample_user, order_number="ORD-LEG-M2", lat=41.33, lng=69.29,
                           driver_id=delivery_driver.id)
        _route(db, delivery_driver.id, [first.id, second.id])

        with patch(
            "business_app.api.admin_dispatch.MapsService.get_route",
            return_value={
                "geometry": [[41.30, 69.24], [41.31, 69.25]],
                "legs": [
                    {"distance_km": 4.2, "duration_minutes": 11.0},
                    {"distance_km": 1.8, "duration_minutes": 5.0},
                ],
            },
        ):
            data = _get(client, admin_auth_headers, delivery_driver.id)

        # leg[k] is the hop that ARRIVES at leg_delivery_ids[k]; leg 0 is the
        # depot -> first stop hop, which is why this list is the stop sequence
        # itself rather than being one shorter.
        assert data["leg_delivery_ids"] == [first.id, second.id]

    def test_mapping_skips_stops_that_were_never_sent_to_the_provider(
        self, client, db, admin_auth_headers, delivery_driver, sample_user
    ):
        """The bug this exists to prevent.

        The middle stop has no coordinates, so it is not part of the route the
        provider measured. The published mapping must therefore name only the
        two stops that WERE measured — never the raw `optimized_order`, which
        would slide every leg onto the wrong stop from that point on.
        """
        first = _delivery(db, sample_user, order_number="ORD-LEG-G1", lat=41.31, lng=69.25,
                          driver_id=delivery_driver.id)
        ungeocoded = _delivery(db, sample_user, order_number="ORD-LEG-G2", lat=None, lng=None,
                               driver_id=delivery_driver.id)
        third = _delivery(db, sample_user, order_number="ORD-LEG-G3", lat=41.33, lng=69.29,
                          driver_id=delivery_driver.id)
        _route(db, delivery_driver.id, [first.id, ungeocoded.id, third.id])

        with patch(
            "business_app.api.admin_dispatch.MapsService.get_route",
            return_value={
                "geometry": [[41.30, 69.24], [41.31, 69.25]],
                "legs": [
                    {"distance_km": 4.2, "duration_minutes": 11.0},
                    {"distance_km": 1.8, "duration_minutes": 5.0},
                ],
            },
        ):
            data = _get(client, admin_auth_headers, delivery_driver.id)

        assert data["leg_delivery_ids"] == [first.id, third.id]
        assert ungeocoded.id not in data["leg_delivery_ids"]


@pytest.mark.integration
class TestLegsAreNeverInvented:
    def test_a_provider_without_legs_reports_none(
        self, client, db, admin_auth_headers, delivery_driver, sample_user
    ):
        delivery = _delivery(db, sample_user, order_number="ORD-LEG-NONE", lat=41.31, lng=69.25,
                             driver_id=delivery_driver.id)
        _route(db, delivery_driver.id, [delivery.id])

        with patch(
            "business_app.api.admin_dispatch.MapsService.get_route",
            return_value={"geometry": [[41.30, 69.24], [41.31, 69.25]], "distance_km": 4.0},
        ):
            data = _get(client, admin_auth_headers, delivery_driver.id)

        assert data["legs"] is None

    def test_a_failed_provider_call_reports_no_legs(
        self, client, db, admin_auth_headers, delivery_driver, sample_user
    ):
        # The endpoint degrades to a dashed straight-line map on failure. It
        # must not also degrade to straight-line NUMBERS: a haversine figure
        # rendered in the same place as a measured one is indistinguishable
        # from it.
        delivery = _delivery(db, sample_user, order_number="ORD-LEG-FAIL", lat=41.31, lng=69.25,
                             driver_id=delivery_driver.id)
        _route(db, delivery_driver.id, [delivery.id])

        with patch(
            "business_app.api.admin_dispatch.MapsService.get_route",
            side_effect=Exception("provider down"),
        ):
            data = _get(client, admin_auth_headers, delivery_driver.id)

        assert data["legs"] is None
        assert data["approximate"] is True

    def test_a_route_with_no_resolvable_stops_reports_no_legs(
        self, client, db, admin_auth_headers, delivery_driver, sample_user
    ):
        ungeocoded = _delivery(db, sample_user, order_number="ORD-LEG-NOSTOP", lat=None, lng=None,
                               driver_id=delivery_driver.id)
        _route(db, delivery_driver.id, [ungeocoded.id])

        data = _get(client, admin_auth_headers, delivery_driver.id)

        assert data["legs"] is None
        assert data["leg_delivery_ids"] == []
