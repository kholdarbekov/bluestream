"""Delivered from the Orders page checks the trip first (spec 2026-11-18 §6.5, call 20).

The office marks an order Delivered without the driver's own tap, so the progress guard has not run for that stop. The
branch runs it before it tallies: an order never bound to a trip late-binds onto its driver's open trip and is counted
there (10 L line and 18.9 L tallies alike), and a ten-litre-only order with no driver (so nothing to bind to) is refused
in strict mode and delivered, uncounted, in legacy mode.

Driven through `PUT /api/v1/admin/orders/<id>/status` with the body `adminService.updateOrderStatus` sends and an admin
role-claim token. Only Celery publishing is faked.
"""

import pytest

from business_app.models.bottle import BottleBalance, BottleLedger, DriverBottleSession, DriverBottleSessionOrder
from business_app.models.order import Order
from shared.enums import DeliveryStatus, DriverBottleSessionStatus, OrderStatus
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401  (fixture)
from tests.integration.trip_unit_builders import (  # noqa: F401  (fixtures)
    big,
    delivery_for,
    driver,
    order_with_lines,
    small,
    strict,
    trip,
)

pytestmark = pytest.mark.integration

ORDER_STATUS = "/api/v1/admin/orders/{}/status"


def _admin_marks_delivered(client, headers, order_id):
    return client.put(ORDER_STATUS.format(order_id), json={"status": "delivered", "notes": ""}, headers=headers)


def _fresh(db, session_id):
    db.session.rollback()
    return db.session.get(DriverBottleSession, session_id)


@pytest.mark.parametrize("lines", ["ten_litre", "nineteen"])
def test_an_unbound_order_late_binds_to_the_drivers_open_trip_and_is_counted_there(
    app, client, db, monkeypatch, enqueued, admin_claim_headers, driver, big, small, sample_user, lines  # noqa: F811
):
    strict(app, monkeypatch, True)
    s = trip(db, driver, bottles=10, units={small.id: 12})
    wanted = [(small, 4)] if lines == "ten_litre" else [(big, 3)]
    order = order_with_lines(db, sample_user, wanted, status=OrderStatus.OUT_FOR_DELIVERY)
    delivery_for(db, order, driver, DeliveryStatus.ARRIVED)
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).count() == 0

    r = _admin_marks_delivered(client, admin_claim_headers, order.id)

    assert r.status_code == 200, r.get_data(as_text=True)
    s = _fresh(db, s.id)
    assert DriverBottleSessionOrder.query.filter_by(order_id=order.id).one().session_id == s.id
    if lines == "ten_litre":
        assert (s.unit_lines[0].delivered, s.bottles_delivered) == (4, 0)
    else:
        assert (s.unit_lines[0].delivered, s.bottles_delivered) == (0, 3)
    assert db.session.get(Order, order.id).status is OrderStatus.DELIVERED


@pytest.mark.parametrize("on,code", [(True, 400), (False, 200)])
def test_a_ten_litre_only_order_with_no_driver(
    app, client, db, monkeypatch, enqueued, admin_claim_headers, small, sample_user, on, code  # noqa: F811
):
    """No Delivery row at all: a delivery without a driver cannot reach Arrived, so `complete_delivery` would refuse it
    in both modes. Strict refuses (nothing to count on); legacy delivers and tallies nowhere."""
    strict(app, monkeypatch, on)
    order = order_with_lines(db, sample_user, [(small, 4)], status=OrderStatus.OUT_FOR_DELIVERY)

    r = _admin_marks_delivered(client, admin_claim_headers, order.id)

    assert r.status_code == code, r.get_data(as_text=True)
    if code == 400:
        assert r.get_json()["data"]["error_code"] == "BOTTLE_SESSION_REQUIRED"
    db.session.rollback()
    assert db.session.get(Order, order.id).status is (OrderStatus.OUT_FOR_DELIVERY if on else OrderStatus.DELIVERED)
    assert DriverBottleSessionOrder.query.count() == 0
    assert BottleLedger.query.filter_by(order_id=order.id).count() == 0 and BottleBalance.query.count() == 0
    assert DriverBottleSession.query.filter_by(status=DriverBottleSessionStatus.OPEN).count() == 0
