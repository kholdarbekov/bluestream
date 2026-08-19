"""Reassigning a delivery must move it on the ROUTES too, not just in the column.

The reported production bug, end to end and through the exact endpoint the admin
panel calls (`AssignDeliveryModal` -> `staffService.reassignDelivery` -> `PUT
/admin/staff/delivery/reassign/<id>`), with the payload it actually sends.

`DeliveryRoute.optimized_order` is a plain JSON id list with no foreign key.
Only the dispatch map's own move button maintained it, so a reassignment made
from the Delivery page left the stop sitting on the losing driver's route: the
same order number appeared under two drivers' panels, both polylines were drawn
through it, and — because the SAVE guard validates against real ownership while
the read model did not — every subsequent Save on that route returned 409 with a
"Reload route" button that refetched the same phantom.

The read model is now ownership-derived, which makes the board self-correcting.
These tests pin the other half: the ids themselves move, so the stored sequence
stays a truthful plan rather than something the reader has to compensate for
every time. That matters beyond this page — the driver's own staff-bot route
card reads `optimized_order` directly.
"""

from datetime import datetime, timezone

import pytest

from business_app.models.delivery import Delivery, DeliveryRoute
from business_app.models.user import UserAddress
from business_app.services.dispatch_service import DispatchService
from shared.enums import DeliveryStatus, OrderStatus


def _delivery(db, user, sample_order, *, order_number, driver_id, lat=41.31, lng=69.25):
    address = UserAddress(
        user_id=user.id, full_address=f"Chilonzor {order_number}", city="Tashkent",
        latitude=lat, longitude=lng,
    )
    db.session.add(address)
    db.session.flush()
    order = sample_order.__class__(
        user_id=user.id,
        order_number=order_number,
        total_amount=sample_order.total_amount,
        status=OrderStatus.OUT_FOR_DELIVERY,
        payment_method=sample_order.payment_method,
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


def _route(db, driver_id, delivery_ids, **kwargs):
    route = DeliveryRoute(
        name="r", delivery_person_id=driver_id,
        start_location_lat=41.30, start_location_lng=69.24,
        route_date=datetime.now(timezone.utc), optimized_order=list(delivery_ids),
        **kwargs,
    )
    db.session.add(route)
    db.session.flush()
    return route


@pytest.fixture
def manager_auth_headers(app, admin_user):
    """`manager_or_higher_required` reads the ROLE CLAIM off the token, not the
    user row, so the shared `admin_auth_headers` fixture is a 403 on this
    endpoint. See CLAUDE.md.
    """
    from flask_jwt_extended import create_access_token

    with app.app_context():
        token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _reassign(client, headers, delivery_id, new_driver_id):
    return client.put(
        f"/api/v1/admin/staff/delivery/reassign/{delivery_id}",
        json={"new_delivery_person_id": new_driver_id},
        headers=headers,
    )


@pytest.mark.integration
class TestAdminReassignMovesTheStopBetweenRoutes:
    def test_the_stop_leaves_the_previous_drivers_stored_sequence(
        self, client, db, manager_auth_headers, delivery_driver, second_delivery_driver,
        sample_user, sample_order,
    ):
        delivery = _delivery(db, sample_user, sample_order, order_number="ORD-RA-1",
                             driver_id=delivery_driver.id)
        old_route = _route(db, delivery_driver.id, [delivery.id])
        db.session.commit()

        assert _reassign(client, manager_auth_headers, delivery.id, second_delivery_driver.id).status_code == 200

        db.session.refresh(old_route)
        assert old_route.optimized_order == []

    def test_the_stop_joins_the_new_drivers_stored_sequence(
        self, client, db, manager_auth_headers, delivery_driver, second_delivery_driver,
        sample_user, sample_order,
    ):
        existing = _delivery(db, sample_user, sample_order, order_number="ORD-RA-2A",
                             driver_id=second_delivery_driver.id, lat=41.32)
        moved = _delivery(db, sample_user, sample_order, order_number="ORD-RA-2B",
                          driver_id=delivery_driver.id)
        _route(db, delivery_driver.id, [moved.id])
        new_route = _route(db, second_delivery_driver.id, [existing.id])
        db.session.commit()

        assert _reassign(client, manager_auth_headers, moved.id, second_delivery_driver.id).status_code == 200

        db.session.refresh(new_route)
        assert new_route.optimized_order == [existing.id, moved.id]

    def test_the_losing_drivers_pin_on_that_stop_is_dropped(
        self, client, db, manager_auth_headers, delivery_driver, second_delivery_driver,
        sample_user, sample_order,
    ):
        """A pin is an instruction about a stop this driver no longer has. Left
        behind it re-anchors on whatever later occupies that slot.
        """
        kept = _delivery(db, sample_user, sample_order, order_number="ORD-RA-3A",
                         driver_id=delivery_driver.id, lat=41.32)
        moved = _delivery(db, sample_user, sample_order, order_number="ORD-RA-3B",
                          driver_id=delivery_driver.id)
        old_route = _route(
            db, delivery_driver.id, [moved.id, kept.id],
            manual_override=True, pinned_stops={str(moved.id): 0, str(kept.id): 1},
        )
        db.session.commit()

        assert _reassign(client, manager_auth_headers, moved.id, second_delivery_driver.id).status_code == 200

        db.session.refresh(old_route)
        assert old_route.optimized_order == [kept.id]
        assert old_route.pinned_stops == {str(kept.id): 0}

    def test_the_board_shows_the_stop_under_exactly_one_driver(
        self, client, db, manager_auth_headers, delivery_driver, second_delivery_driver,
        sample_user, sample_order,
    ):
        """The screenshot, asserted: one order, one panel."""
        delivery = _delivery(db, sample_user, sample_order, order_number="ORD-RA-4",
                             driver_id=delivery_driver.id)
        _route(db, delivery_driver.id, [delivery.id])
        _route(db, second_delivery_driver.id, [])
        db.session.commit()

        assert _reassign(client, manager_auth_headers, delivery.id, second_delivery_driver.id).status_code == 200

        routes = DispatchService.get_snapshot(DispatchService.today())["routes"]
        owners = [r["driver_id"] for r in routes for s in r["stops"] if s["delivery_id"] == delivery.id]
        assert owners == [second_delivery_driver.id]


@pytest.mark.integration
class TestReturningToThePoolClearsTheRoute:
    def test_redispatching_a_stop_takes_it_off_the_drivers_stored_sequence(
        self, client, db, operator_auth_headers, delivery_driver, sample_user, sample_order,
    ):
        """`StaffService.return_delivery_to_pool` is the pool-return SSOT, and
        every caller except the dispatch map's own button left the stop on the
        route — so it was simultaneously offered in the unassigned pool and
        numbered on a driver's route, and assigning it from the pool read as a
        duplicate rather than a move.
        """
        delivery = _delivery(db, sample_user, sample_order, order_number="ORD-RP-1",
                             driver_id=delivery_driver.id)
        delivery.status = DeliveryStatus.FAILED
        route = _route(db, delivery_driver.id, [delivery.id])
        db.session.commit()

        response = client.post(
            f"/api/v1/staff/delivery/redispatch/{delivery.id}",
            json={"reason": "customer_absent"},
            headers=operator_auth_headers,
        )
        assert response.status_code == 200

        db.session.refresh(route)
        assert route.optimized_order == []
