"""DeliveryRoute override columns + relationship disambiguation.

The `overridden_by` FK is the SECOND foreign key from delivery_routes to
users. Without explicit `foreign_keys=` on both relationships SQLAlchemy
cannot configure the mapper at all, so these tests guard app boot, not just
column presence.
"""

from datetime import datetime, timezone

from business_app.models.delivery import DeliveryRoute
from shared.enums import AssignmentSource


class TestDeliveryRouteOverrideColumns:
    def test_defaults_to_not_overridden(self, db, delivery_driver, admin_user):
        route = DeliveryRoute(
            name="t",
            delivery_person_id=delivery_driver.id,
            start_location_lat=41.3,
            start_location_lng=69.2,
            route_date=datetime.now(timezone.utc),
        )
        db.session.add(route)
        db.session.commit()

        assert route.manual_override is False
        assert route.pinned_stops == {}
        assert route.overridden_by is None
        assert route.overridden_at is None

    def test_override_fields_round_trip(self, db, delivery_driver, admin_user):
        now = datetime.now(timezone.utc)
        route = DeliveryRoute(
            name="t",
            delivery_person_id=delivery_driver.id,
            start_location_lat=41.3,
            start_location_lng=69.2,
            route_date=now,
            optimized_order=[812, 809, 815],
            manual_override=True,
            pinned_stops={"809": 0},
            overridden_by=admin_user.id,
            overridden_at=now,
        )
        db.session.add(route)
        db.session.commit()
        db.session.expire_all()

        loaded = DeliveryRoute.query.get(route.id)
        assert loaded.manual_override is True
        assert loaded.pinned_stops == {"809": 0}
        assert loaded.overridden_by == admin_user.id

    def test_both_user_relationships_resolve(self, db, delivery_driver, admin_user):
        """Guards the ambiguous-FK boot failure."""
        route = DeliveryRoute(
            name="t",
            delivery_person_id=delivery_driver.id,
            start_location_lat=41.3,
            start_location_lng=69.2,
            route_date=datetime.now(timezone.utc),
            overridden_by=admin_user.id,
        )
        db.session.add(route)
        db.session.commit()

        assert route.delivery_person.id == delivery_driver.id
        assert route.overridden_by_user.id == admin_user.id

    def test_to_dict_exposes_override_state(self, db, delivery_driver, admin_user):
        route = DeliveryRoute(
            name="t",
            delivery_person_id=delivery_driver.id,
            start_location_lat=41.3,
            start_location_lng=69.2,
            route_date=datetime.now(timezone.utc),
            manual_override=True,
            pinned_stops={"809": 0},
            overridden_by=admin_user.id,
        )
        db.session.add(route)
        db.session.commit()

        payload = route.to_dict()
        assert payload["manual_override"] is True
        assert payload["pinned_stops"] == {"809": 0}
        assert payload["overridden_by_name"] == admin_user.full_name


class TestAssignmentSource:
    def test_admin_dispatch_member_exists(self):
        assert AssignmentSource.ADMIN_DISPATCH.value == "admin_dispatch"


class TestAdminRouteDetailReadsDeliveryIds:
    """admin.py resolved route stops with Delivery.order_id.in_(optimized_order)
    while the optimiser writes DELIVERY ids — so the endpoint returned the wrong
    deliveries (or none). It must match on Delivery.id and return them in
    optimized_order sequence."""

    def test_returns_deliveries_in_route_order(
        self, client, db, admin_auth_headers, delivery_driver, sample_order, sample_user
    ):
        from datetime import datetime, timezone

        from business_app.models.delivery import Delivery, DeliveryRoute
        from shared.enums import DeliveryStatus

        made = []
        for _ in range(3):
            order = sample_order.__class__(
                user_id=sample_user.id,
                order_number=f"ORD-{_}",
                total_amount=sample_order.total_amount,
                status=sample_order.status,
                payment_method=sample_order.payment_method,
                delivery_address_id=sample_order.delivery_address_id,
                delivery_date=datetime.now(timezone.utc),
            )
            db.session.add(order)
            db.session.flush()
            delivery = Delivery(
                order_id=order.id,
                delivery_person_id=delivery_driver.id,
                status=DeliveryStatus.ASSIGNED,
                scheduled_date=datetime.now(timezone.utc),
                scheduled_time_slot="09:00-12:00",
            )
            db.session.add(delivery)
            db.session.flush()
            made.append(delivery.id)

        route = DeliveryRoute(
            name="r",
            delivery_person_id=delivery_driver.id,
            start_location_lat=41.3,
            start_location_lng=69.2,
            route_date=datetime.now(timezone.utc),
            optimized_order=[made[2], made[0], made[1]],
        )
        db.session.add(route)
        db.session.commit()

        resp = client.get(f"/api/v1/admin/delivery-routes/{route.id}", headers=admin_auth_headers)
        assert resp.status_code == 200
        returned = [d["id"] for d in resp.get_json()["data"]["route"]["deliveries"]]
        assert returned == [made[2], made[0], made[1]]
