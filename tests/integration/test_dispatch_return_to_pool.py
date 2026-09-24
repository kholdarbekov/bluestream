"""The dispatch map's "Return to pool", driven through the route the board calls.

Spec: docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md, section 3.3.
`StaffService.return_delivery_to_pool` is now a thin wrapper over
`_pull_back_delivery`, the same core the admin reschedule runs. For this button
that changes two things:

* It refuses (409 STAFF_DELIVERY_NOT_POOLABLE) a delivery that no driver holds or
  failed. A board drawn before an admin moved the order to a later day still shows
  the stop, and returning it would drag a RESCHEDULED row into today's pool.
* Moving the order back to CONFIRMED leaves NO free-text note on the order's
  history. The customer bot prints every order-history note on its Track screen
  (telegram_bot/handlers/orders.py), so the old default "Returned to pool for
  re-dispatch" reached customers in English. The staff note stays on the
  delivery's own history, which only staff read.

Only the staff-bot pushes are faked, and both fakes check the real signature:
`notify_route_updated` is autospecced, and the unassigned task's `.delay` binds each
call against the task (`_task_spy`). `RouteEditService` swallows a push that raises,
so a drifted call records nothing and turns these tests red instead of vanishing
into a log line.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from business_app.models.delivery import Delivery, DeliveryPerson, DeliveryRoute, DeliveryStatusHistory
from business_app.models.order import Order, OrderStatusHistory
from business_app.models.user import User, UserAddress
from business_app.tasks.staff_tasks import notify_staff_order_unassigned
from business_app.utils.password_security import hash_password
from shared.enums import DeliveryStatus, OrderStatus, PaymentMethod, UserRole, UserStatus, UserType
from tests.unit.test_delivery_service_business_rules import _task_spy

pytestmark = pytest.mark.integration

DRIVER_TELEGRAM_ID = "700800900"
UNASSIGN = "/api/v1/admin/dispatch/stops/{id}/unassign"


@pytest.fixture
def route_driver(db):
    user = User(
        email="route-driver@example.com",
        phone="+998900002301",
        first_name="Route",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        status=UserStatus.ACTIVE,
        telegram_id=DRIVER_TELEGRAM_ID,
        is_verified=True,
        password_hash=hash_password("DriverPassword123!"),
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(
        DeliveryPerson(
            user_id=user.id,
            full_name="Route Driver",
            phone=user.phone,
            is_active=True,
            is_available=True,
            working_hours_start="00:00",
            working_hours_end="23:59",
        )
    )
    db.session.commit()
    return user


def _stop(db, customer, order_number, *, order_status, status, driver_id):
    address = UserAddress(
        user_id=customer.id,
        title="Home",
        full_address=f"Amir Temur {order_number}",
        street_address="Amir Temur 1",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
    )
    db.session.add(address)
    db.session.flush()
    order = Order(
        user_id=customer.id,
        order_number=order_number,
        status=order_status,
        subtotal=Decimal("50000"),
        total_amount=Decimal("50000"),
        payment_method=PaymentMethod.CASH,
        order_source="admin",
        delivery_address_id=address.id,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(order)
    db.session.flush()
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver_id,
        status=status,
        scheduled_date=datetime.now(timezone.utc),
        scheduled_time_slot="anytime",
    )
    db.session.add(delivery)
    db.session.commit()
    return delivery


def _route(db, driver_id, delivery_ids):
    route = DeliveryRoute(
        name="today",
        delivery_person_id=driver_id,
        start_location_lat=41.30,
        start_location_lng=69.24,
        route_date=datetime.now(timezone.utc),
        optimized_order=list(delivery_ids),
    )
    db.session.add(route)
    db.session.commit()
    return route


@pytest.mark.parametrize(
    "status",
    [DeliveryStatus.RESCHEDULED, DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING, DeliveryStatus.CANCELLED],
    ids=lambda s: s.value,
)
def test_return_to_pool_refuses_a_delivery_no_driver_holds_and_changes_nothing(
    client, db, sample_user, admin_auth_headers, monkeypatch, status
):
    """RESCHEDULED is the stale-board case. The stop was on a driver's route when the
    board was drawn, and an admin has since moved the order to a later day;
    returning it now would put it in today's pool and undo the reschedule.
    SCHEDULED and PENDING are driverless already. Pulling a PENDING (admin-hold) row
    to SCHEDULED would also release a hold nobody asked to release."""
    delivery = _stop(
        db, sample_user, "RTP-REFUSE", order_status=OrderStatus.CONFIRMED, status=status, driver_id=None
    )
    delivery_id, order_id = delivery.id, delivery.order_id
    unassigned = _task_spy(monkeypatch, notify_staff_order_unassigned, "delay")

    with patch("business_app.services.route_edit_service.notify_route_updated", autospec=True) as pushed:
        resp = client.post(
            UNASSIGN.format(id=delivery_id), json={"reason": "stale board"}, headers=admin_auth_headers
        )

    assert resp.status_code == 409, resp.get_data(as_text=True)
    assert resp.get_json()["error_code"] == "STAFF_DELIVERY_NOT_POOLABLE"
    db.session.expire_all()
    row = db.session.get(Delivery, delivery_id)
    assert (row.status, row.delivery_person_id) == (status, None)
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count() == 0
    assert db.session.get(Order, order_id).status == OrderStatus.CONFIRMED
    assert OrderStatusHistory.query.filter_by(order_id=order_id).count() == 0
    pushed.assert_not_called()
    assert unassigned == []


@pytest.mark.parametrize(
    "order_status,delivery_status",
    [
        # OUT_FOR_DELIVERY -> CONFIRMED is not in the forward-only transition
        # table: `_restore_order_to_pool_eligible` writes it directly.
        (OrderStatus.OUT_FOR_DELIVERY, DeliveryStatus.IN_TRANSIT),
        # PENDING -> CONFIRMED is legal: it goes through
        # `OrderService.update_order_status`, the other branch that took a note.
        # The dispatch map keeps this move (`restore_order` defaults to True);
        # only the admin reschedule turns it off (R23).
        (OrderStatus.PENDING, DeliveryStatus.ASSIGNED),
    ],
    ids=["out-for-delivery", "pending"],
)
def test_returning_a_stop_leaves_the_staff_note_on_the_delivery_not_on_the_customers_timeline(
    client, db, sample_user, auth_headers, admin_auth_headers, admin_user, route_driver, monkeypatch,
    order_status, delivery_status,
):
    delivery = _stop(
        db, sample_user, "RTP-NOTE", order_status=order_status, status=delivery_status,
        driver_id=route_driver.id,
    )
    route = _route(db, route_driver.id, [delivery.id])
    delivery_id, order_id, route_id = delivery.id, delivery.order_id, route.id
    driver_id, admin_id = route_driver.id, admin_user.id
    unassigned = _task_spy(monkeypatch, notify_staff_order_unassigned, "delay")

    with patch("business_app.services.route_edit_service.notify_route_updated", autospec=True) as pushed:
        resp = client.post(
            UNASSIGN.format(id=delivery_id),
            json={"reason": "customer asked for later"},
            headers=admin_auth_headers,
        )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["data"] == {"delivery_id": delivery_id}

    db.session.expire_all()
    row = db.session.get(Delivery, delivery_id)
    assert (row.status, row.delivery_person_id, row.failed_delivery_reason) == (
        DeliveryStatus.SCHEDULED, None, None,
    )
    (moved,) = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).all()
    assert (moved.old_status, moved.new_status, moved.changed_by, moved.notes, moved.reason) == (
        delivery_status,
        DeliveryStatus.SCHEDULED,
        admin_id,
        "Returned to pool from the dispatch map",
        "customer asked for later",
    )
    (restored,) = OrderStatusHistory.query.filter_by(order_id=order_id).all()
    assert (restored.old_status, restored.new_status, restored.changed_by, restored.notes) == (
        order_status, OrderStatus.CONFIRMED, admin_id, None,
    )
    assert db.session.get(DeliveryRoute, route_id).optimized_order == []

    pushed.assert_called_once_with(driver_id)
    assert unassigned == [
        (
            (
                DRIVER_TELEGRAM_ID,
                {
                    "delivery_id": delivery_id,
                    "order_id": order_id,
                    "order_number": "RTP-NOTE",
                    "status": order_status.value,
                    "total_amount": 50000.0,
                    "payment_method": "cash",
                    "delivery_address": "Amir Temur RTP-NOTE",
                },
            ),
            {},
        )
    ]

    # What the customer's Track screen reads (GET /orders/<id>/track): every
    # timeline note is printed there, so none may be a staff sentence.
    track = client.get(f"/api/v1/orders/{order_id}/track", headers=auth_headers)
    assert track.status_code == 200, track.get_data(as_text=True)
    timeline = track.get_json()["data"]["timeline"]
    assert timeline[-1]["status"] == "confirmed"
    assert [entry["notes"] for entry in timeline] == [None] * len(timeline)
