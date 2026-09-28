"""Drivers cannot cancel a delivery (F4).

F4 of docs/superpowers/specs/2026-09-25-failed-delivery-awaits-new-date-design.md. The
staff bot drew one button for every successor in the shared transition table, and that
table lists CANCELLED after every active status. A driver's Cancel left a cancelled
delivery under a live order, and nothing could bring it back: reschedule, re-dispatch and
return to pool all refuse a cancelled row, and the admin Delivery form has no move out of
it (dev order 153). A driver who cannot complete a stop marks it failed, and the order
waits for a new date.

The driver view of the table leaves CANCELLED out. The staff-bot keyboard and the backend
gate both read it. The shared table keeps its CANCELLED edges: an admin's order cancel
still has to cancel a delivery a driver holds, and `DeliveryService` validates its writes
against that table.

The endpoint tests drive the real `PUT /api/v1/staff/delivery/<id>/status` with the body
the staff bot sends. Only Celery publishing is faked. The bot half is in
tests/staff_bot/test_staff_delivery_journey_dispatcher.py: a Cancel button left on a card
drawn before this change.
"""

import pytest

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.order import OrderStatusHistory
from business_app.services.delivery_service import DeliveryService
from business_app.tasks import delivery_tasks, notification_tasks
from shared import staff_constants, status_transitions
from shared.enums import DeliveryStatus, OrderStatus
from tests.integration.test_staff_delivery_ownership_guard import _bearer, _delivery, _driver
from tests.unit.test_delivery_service_business_rules import _task_spy

pytestmark = pytest.mark.integration

STATUS = "/api/v1/staff/delivery/{}/status"

# The rungs a driver walks, each with its one way out. Written out, not derived from the
# shared table, so the test fails if the driver view loses a move a driver needs.
DRIVER_LADDER = {
    DeliveryStatus.ASSIGNED: [DeliveryStatus.PICKED_UP, DeliveryStatus.FAILED],
    DeliveryStatus.PICKED_UP: [DeliveryStatus.IN_TRANSIT, DeliveryStatus.FAILED],
    DeliveryStatus.IN_TRANSIT: [DeliveryStatus.ARRIVED, DeliveryStatus.FAILED],
    DeliveryStatus.ARRIVED: [DeliveryStatus.DELIVERED, DeliveryStatus.FAILED],
}

# Every live status an order cancel can find its delivery in.
LIVE_STATUSES = (
    DeliveryStatus.SCHEDULED,
    DeliveryStatus.PENDING,
    DeliveryStatus.ASSIGNED,
    DeliveryStatus.PICKED_UP,
    DeliveryStatus.IN_TRANSIT,
    DeliveryStatus.ARRIVED,
    DeliveryStatus.RESCHEDULED,
)


# --------------------------------------------------------------------------- #
# 1. The tables
# --------------------------------------------------------------------------- #


class TestDriverView:
    def test_no_status_offers_a_driver_cancel(self):
        driver_view = status_transitions.DRIVER_DELIVERY_STATUS_TRANSITIONS

        assert set(driver_view) == set(DeliveryStatus)
        offering_cancel = sorted(
            current.value
            for current, successors in driver_view.items()
            if DeliveryStatus.CANCELLED in successors
        )
        assert offering_cancel == []

    @pytest.mark.parametrize("current", list(DRIVER_LADDER), ids=lambda s: s.value)
    def test_a_driver_keeps_every_other_move(self, current):
        assert status_transitions.DRIVER_DELIVERY_STATUS_TRANSITIONS[current] == DRIVER_LADDER[current]

    def test_the_keyboard_and_the_gate_read_the_driver_view(self):
        """`shared/staff_constants` feeds both the staff-bot keyboard and the gate in
        `StaffService.update_delivery_status`. A second serialiser of the full table is
        how Cancel would come back."""
        assert staff_constants.DELIVERY_STATUS_TRANSITIONS == status_transitions.driver_delivery_transitions_as_strings()
        assert staff_constants.DELIVERY_STATUS_TRANSITIONS["arrived"] == ["delivered", "failed"]
        assert not hasattr(status_transitions, "delivery_transitions_as_strings")

    @pytest.mark.parametrize("current", LIVE_STATUSES, ids=lambda s: s.value)
    def test_the_shared_table_keeps_the_order_cancel_edge(self, current):
        assert status_transitions.is_valid_delivery_transition(current, DeliveryStatus.CANCELLED)


# --------------------------------------------------------------------------- #
# 2. The driver endpoint
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "delivery_status, order_status",
    [
        (DeliveryStatus.ASSIGNED, OrderStatus.CONFIRMED),
        (DeliveryStatus.ARRIVED, OrderStatus.OUT_FOR_DELIVERY),
    ],
    ids=["assigned", "arrived"],
)
def test_a_driver_cancel_is_refused_and_nothing_is_written(
    app, client, db, sample_user, monkeypatch, delivery_status, order_status
):
    """Spec §8 backend test 1: a stale Cancel button, just after accept and at the door."""
    driver = _driver(db, phone="+998901250001", first_name="Cancel")
    delivery = _delivery(db, sample_user, driver, status=delivery_status, number=f"ORD-F4-{delivery_status.value}")
    delivery_id, order_id = delivery.id, delivery.order_id
    assert delivery.order.status == order_status  # fixture: `_delivery` derives it
    delivery_history = DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count()
    order_history = OrderStatusHistory.query.filter_by(order_id=order_id).count()
    customer_updates = _task_spy(monkeypatch, notification_tasks.send_delivery_update_task, "delay")
    route_runs = _task_spy(monkeypatch, delivery_tasks.optimize_driver_route_task, "delay")

    response = client.put(STATUS.format(delivery_id), json={"status": "cancelled"}, headers=_bearer(app, driver))

    assert response.status_code == 400, response.get_data(as_text=True)
    body = response.get_json()
    assert body["error_code"] == "STAFF_INVALID_STATUS_TRANSITION"
    # Field by field: `@handle_api_exception` also folds `validation_errors: []` into
    # every ValidationError's details (tests/unit/test_staff_error_endpoints.py).
    assert body["details"]["current_status"] == delivery_status.value
    assert body["details"]["requested_status"] == "cancelled"
    db.session.expire_all()
    row = db.session.get(Delivery, delivery_id)
    assert (row.status, row.delivery_person_id) == (delivery_status, driver.id)
    assert row.order.status == order_status
    assert DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id).count() == delivery_history
    assert OrderStatusHistory.query.filter_by(order_id=order_id).count() == order_history
    assert (customer_updates, route_runs) == ([], [])


# --------------------------------------------------------------------------- #
# 3. The cancels that are not the driver's
# --------------------------------------------------------------------------- #


def test_an_admin_order_cancel_still_cancels_the_delivery_a_driver_holds(client, db, admin_claim_headers, sample_user):
    """The admin's Cancel runs the order-cancel cascade, which must still take a live stop
    off its driver. The body is `{status, reason}`: the one the Orders row-menu Cancel
    sends once F6 lands."""
    driver = _driver(db, phone="+998901250002", first_name="Held")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.ASSIGNED, number="ORD-F4-ADMIN")
    delivery_id, order_id = delivery.id, delivery.order_id

    response = client.put(
        f"/api/v1/admin/orders/{order_id}/status",
        json={"status": "cancelled", "reason": "Customer phoned to cancel"},
        headers=admin_claim_headers,
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.get_json()["data"]["order"]["status"] == "cancelled"
    db.session.expire_all()
    assert db.session.get(Delivery, delivery_id).status == DeliveryStatus.CANCELLED
    last = (
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id)
        .order_by(DeliveryStatusHistory.id.desc())
        .first()
    )
    assert (last.old_status, last.new_status) == (DeliveryStatus.ASSIGNED, DeliveryStatus.CANCELLED)


def test_delivery_service_still_cancels_a_delivery_a_driver_holds(db, sample_user):
    """`DeliveryService` validates against the shared table, not the driver view, so its
    cancel still finds the ASSIGNED -> CANCELLED edge. No route calls `cancel_delivery`,
    so the service is its real entry point."""
    driver = _driver(db, phone="+998901250003", first_name="Service")
    delivery = _delivery(db, sample_user, driver, status=DeliveryStatus.ASSIGNED, number="ORD-F4-SVC")
    delivery_id = delivery.id

    DeliveryService().cancel_delivery(delivery_id, reason="Order cancelled")

    db.session.expire_all()
    assert db.session.get(Delivery, delivery_id).status == DeliveryStatus.CANCELLED
    last = (
        DeliveryStatusHistory.query.filter_by(delivery_id=delivery_id)
        .order_by(DeliveryStatusHistory.id.desc())
        .first()
    )
    assert (last.old_status, last.new_status) == (DeliveryStatus.ASSIGNED, DeliveryStatus.CANCELLED)
